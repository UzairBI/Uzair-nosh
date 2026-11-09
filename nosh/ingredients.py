"""Ingredient-line parsing, name normalization, matching and dietary-group classification.

Design (see README "Normalization"):
  * A raw line such as "2 (10 ounce) cans condensed cream of chicken soup ADVERTISEMENT"
    is turned into (qty, unit, alts) where `alts` is a tuple of normalized names
    (more than one when the line says "butter or margarine").
  * A name is a string of space-separated tokens.  Tokens that denote a *different
    product* are fused with underscores ("tomato paste" -> "tomato_paste",
    "peanut butter" -> "peanut_butter", "green onion" -> "green_onion"), so that
    searching for "tomato" or "butter" does NOT match them, while "cherry tomato"
    or "unsalted butter" do match.
  * Dietary groups (dairy, egg, peanut, ...) are decided from the same tokens with a
    lexicon; everything is text based, never a medical statement.
"""
from __future__ import annotations

import re
from functools import lru_cache

# ----------------------------------------------------------------------------- #
# Quantities and units
# ----------------------------------------------------------------------------- #
_FRACS = {"½": " 1/2", "¼": " 1/4", "¾": " 3/4", "⅓": " 1/3", "⅔": " 2/3", "⅛": " 1/8",
          "⅜": " 3/8", "⅝": " 5/8", "⅞": " 7/8", "⅕": " 1/5"}

UNIT_CANON = {
    "cup": "cup", "c": "cup", "tablespoon": "tbsp", "tbsp": "tbsp", "tbs": "tbsp", "tbl": "tbsp",
    "tbsps": "tbsp", "teaspoon": "tsp", "tsp": "tsp", "tsps": "tsp", "ounce": "oz", "oz": "oz",
    "pound": "lb", "lb": "lb", "gram": "g", "g": "g", "kilogram": "kg", "kg": "kg",
    "milliliter": "ml", "ml": "ml", "liter": "l", "litre": "l", "l": "l", "quart": "qt", "qt": "qt",
    "pint": "pt", "pt": "pt", "gallon": "gal", "can": "can", "package": "package", "pkg": "package",
    "packet": "package", "jar": "jar", "bottle": "bottle", "box": "box", "bag": "bag",
    "container": "container", "carton": "carton", "clove": "clove", "pinch": "pinch",
    "dash": "dash", "stick": "stick", "slice": "slice", "piece": "piece", "head": "head",
    "bunch": "bunch", "sprig": "sprig", "stalk": "stalk", "sheet": "sheet", "cube": "cube",
    "handful": "handful", "envelope": "envelope", "splash": "splash", "fillet": "fillet", "ear": "ear",
}
# conversion to a base unit: (dimension, factor)
_UNIT_BASE = {
    "cup": ("vol", 236.6), "tbsp": ("vol", 14.8), "tsp": ("vol", 4.93), "ml": ("vol", 1.0),
    "l": ("vol", 1000.0), "qt": ("vol", 946.0), "pt": ("vol", 473.0), "gal": ("vol", 3785.0),
    "g": ("mass", 1.0), "kg": ("mass", 1000.0), "oz": ("mass", 28.35), "lb": ("mass", 453.6),
}

_NUM = r"(?:\d+/\d+|\d+(?:\.\d+)?(?:\s+\d+/\d+)?)"
_QTY_RE = re.compile(rf"^\s*(?:about\s+|approximately\s+|approx\.?\s+)?({_NUM})(?:\s*(?:-|–|to)\s*({_NUM}))?")


def _to_float(s: str) -> float:
    total = 0.0
    for part in s.split():
        if "/" in part:
            a, b = part.split("/")
            total += float(a) / float(b)
        else:
            total += float(part)
    return total


def convert(qty: float | None, unit: str | None):
    """Return (dimension, amount_in_base) or None when the unit cannot be compared."""
    if qty is None:
        return None
    if unit in _UNIT_BASE:
        dim, f = _UNIT_BASE[unit]
        return dim, qty * f
    if unit is None:
        return "count", qty
    return unit, qty  # same non-metric unit (clove, can, ...) is comparable only to itself


# ----------------------------------------------------------------------------- #
# Name normalization
# ----------------------------------------------------------------------------- #
DESCRIPTORS = set("""
a an the of fresh freshly large small medium big chopped minced diced sliced grated shredded peeled
crushed cooked uncooked melted softened divided optional finely coarsely roughly thinly thickly
boneless skinless lean extra virgin unsalted salted unsweetened low-fat lowfat fat-free nonfat non-fat
reduced-fat light packed firmly heaped level rounded ripe raw dried dry frozen thawed canned drained
rinsed seedless pitted trimmed halved quartered cubed torn beaten sifted toasted roasted pure good
quality high your favorite homemade store-bought prepared organic kosher granulated plain lukewarm
heavy-duty skim 2% 1% fine coarse ground-up hot-ish miniature mini jumbo thick thin
""".split())

STOPWORDS = {"and", "or", "with", "in", "into", "for", "to", "taste", "as", "needed", "if", "desired",
             "plus", "more", "about", "per", "each", "such", "any", "other", "your", "choice", "at",
             "room", "temperature", "see", "note", "garnish", "serving", "optional", "whole", "from",
             "on", "by", "the", "a", "an", "of"}

PHRASE_SYNONYMS = [
    (r"\bscallions?\b", "green onion"), (r"\bspring onions?\b", "green onion"),
    (r"\bcapsicums?\b", "bell pepper"), (r"\b(?:green|red|yellow|orange) (?:bell )?peppers?\b(?! flake)", "bell pepper"),
    (r"\baubergines?\b", "eggplant"), (r"\bcourgettes?\b", "zucchini"),
    (r"\bgarbanzo(?: beans?)?\b", "chickpea"), (r"\bchick peas?\b", "chickpea"),
    (r"\b(?:confectioners'?|confectioner'?s|icing|powdered|10x)\s+sugar\b", "powdered sugar"),
    (r"\b(?:caster|castor|superfine|granulated|white)\s+sugar\b", "sugar"),
    (r"\b(?:light |dark )?brown sugar\b", "brown sugar"),
    (r"\b(?:heavy|double|whipping|heavy whipping|whipping) cream\b", "heavy cream"),
    (r"\bsun[- ]?dried\b", "sundried"), (r"\byoghurt\b", "yogurt"), (r"\bmayo\b", "mayonnaise"), (r"\bprawns?\b", "shrimp"),
    (r"\bbicarbonate of soda\b", "baking soda"), (r"\bbicarb\b", "baking soda"),
    (r"\bcorn ?flour\b", "cornstarch"), (r"\bcorn starch\b", "cornstarch"),
    (r"\bcoriander (?:leaves|leaf)\b", "cilantro"), (r"\bfresh coriander\b", "cilantro"),
    (r"\bchil(?:e|li|lies|ies|is)\b", "chili"), (r"\bcastor\b", ""),
    (r"\b(?:sea|table|kosher|coarse|fine) salt\b", "salt"),
    (r"\b(?:freshly )?ground (?:black )?pepper\b", "black pepper"), (r"^pepper$", "black pepper"),
    (r"\bblack pepper\b", "black pepper"), (r"\ball[- ]purpose\b", "allpurpose"),
    (r"\bbutter beans?\b", "lima bean"), (r"\bhalf[- ]and[- ]half\b", "half_and_half"),
    (r"\bwhole (?=wheat|grain)", "whole_"), (r"\bsweetened condensed milk\b", "condensed milk"),
    (r"\bcream of (\w+)", r"cream_of_\1"), (r"\bcrème fraîche\b", "creme fraiche"),
    (r"\b(\d+)%\s*", ""), (r"\bgarlic cloves?\b", "garlic"), (r"\bcloves? garlic\b", "garlic"),
    (r"\bbaking soda\b", "baking_soda"), (r"\bbaking powder\b", "baking_powder"),
    (r"\bpine nuts?\b", "pine_nut"), (r"\bbrazil nuts?\b", "brazil_nut"),
]
_PHRASE_RE = [(re.compile(p), r) for p, r in PHRASE_SYNONYMS]

# multi-word names that are a *different product* from the head noun
ATOMIC = [
    "green onion", "sweet potato", "bell pepper", "black pepper", "brown sugar", "powdered sugar",
    "peanut butter", "almond butter", "apple butter", "cocoa butter", "sunflower seed butter",
    "sour cream", "ice cream", "cream cheese", "cottage cheese", "coconut milk", "coconut cream",
    "almond milk", "soy milk", "oat milk", "rice milk", "cashew milk", "condensed milk",
    "evaporated milk", "cream of tartar", "green bean", "black bean", "kidney bean", "lima bean",
    "pinto bean", "white bean", "red onion", "hot dog", "maple syrup", "corn syrup",
    "baking soda", "baking powder", "egg noodle", "rice noodle", "rice vinegar", "bay leaf",
    "red pepper flake", "pepper flake", "oyster mushroom", "short rib", "ginger ale", "root beer", "ice water",
]
# (red onion kept atomic? no -- red onion is an onion; removed again below)
ATOMIC = [a for a in ATOMIC if a != "red onion"]
_ATOMIC_RE = [(re.compile(r"\b" + re.escape(a) + r"\b"), a.replace(" ", "_")) for a in sorted(ATOMIC, key=len, reverse=True)]

PRODUCT_SUFFIX = {"paste", "sauce", "juice", "soup", "puree", "ketchup", "powder", "extract", "vinegar",
                  "syrup", "stock", "broth", "flour", "noodle", "bread", "chip", "crumb", "seasoning",
                  "salt", "mix", "flake", "wine", "oil", "bouillon", "liqueur"}
# a query for one of these matches any product ending in it ("oil" -> olive_oil)
GENERIC = {"oil", "flour", "vinegar", "wine", "broth", "stock", "bread", "sauce", "noodle"}

_NO_SINGULAR = {"molasses", "hummus", "asparagus", "couscous", "swiss", "grits", "lemongrass", "watercress",
                "cress", "bass", "citrus", "hibiscus", "chassis", "less", "glass", "brussels", "tahini",
                "swiss", "gas", "plus", "this", "bulgur", "quinoa"}
_IES_KEEP = {"cookies", "brownies", "pies", "smoothies", "veggies", "calories", "zucchinis", "movies",
             "collies", "rookies", "selfies", "hoagies", "yuppies", "birdies"}


@lru_cache(maxsize=None)
def singular(w: str) -> str:
    if w in _NO_SINGULAR or len(w) <= 3:
        return w
    if w in _IES_KEEP:
        return w[:-1]
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w in ("leaves", "loaves", "halves", "knives", "wolves", "calves"):
        return w[:-3] + "f"
    if w.endswith("oes"):
        return w[:-2]
    if w.endswith(("ches", "shes", "xes", "sses", "zzes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


_PAREN = re.compile(r"\([^)]*\)")
_LABEL = re.compile(r"^[^\d:]{0,30}:\s+")  # "Garnish: ..." / "For the sauce: ..."
_WORD = re.compile(r"[a-z0-9_%']+(?:-[a-z0-9]+)*")


def _unit_word(w: str) -> str | None:
    return UNIT_CANON.get(singular(w) if w not in UNIT_CANON else w)


def clean_line(raw: str) -> str | None:
    """Strip scraper noise; return None for blank / header lines."""
    s = raw.replace("ADVERTISEMENT", " ").strip()
    s = re.sub(r"\s+", " ", s)
    if not s:
        return None
    if s.endswith(":") and not re.search(r"\d", s):  # section header ("For the sauce:")
        return None
    return s


_PREP_WORDS = {"center", "cut", "thick", "thin", "bone", "in", "cured", "chilled", "whole", "half", "halves", "large", "small",
               "medium", "bite", "sized", "size", "piece", "pieces", "fine", "coarse", "very", "well", "lightly", "freshly",
               "not", "removed", "discarded", "reserved", "separated", "needed", "divided", "plus", "more", "extra"}
_NOT_PREP_ED = {"red", "seed", "need", "weed", "speed", "bed", "bread", "shred", "curd", "cured_meat"}


def _prep_only(name: str) -> bool:
    """True when a normalized name consists only of preparation words ('diced chilled', 'center cut')."""
    toks = name.split()
    return bool(toks) and all((t.endswith(("ed", "ly")) and t not in _NOT_PREP_ED) or t in _PREP_WORDS for t in toks)


def tail_groups(raw: str) -> tuple[int, int]:
    """Dietary flags from EVERY comma segment / 'or' alternative of a line (conservative): 'cooked chicken meat' stays flagged
    even when the parsed ingredient name is another segment."""
    s = raw.lower().replace("advertisement", " ").replace("(", ",").replace(")", ",")   # parentheses often name the real ingredient
    sure = maybe = 0
    for seg in s.split(","):
        for part in re.split(r"\s+or\s+|/", seg):
            n = normalize_name(part.strip())
            if n:
                a, b = name_groups(n)
                sure |= a
                maybe |= b
    return sure, maybe & ~sure


@lru_cache(maxsize=500_000)
def parse_line(raw: str):
    """-> (qty, unit, alts) ; alts is a tuple of normalized names (possibly empty)."""
    s = raw.lower().replace("advertisement", " ")
    for k, v in _FRACS.items():
        s = s.replace(k, v)
    s = _PAREN.sub(" ", s)
    s = s.replace("’", "'")
    qty = None
    unit = None
    m = _QTY_RE.match(s)
    if m:
        try:
            qty = _to_float(m.group(1))
        except Exception:
            qty = None
        s = s[m.end():]
    s = s.strip(" ,;")
    # units (possibly repeated: "2 cans" after parenthesised sizes were removed)
    words = s.split(" ")
    i = 0
    while i < len(words) and words[i].strip(",."):
        w0 = words[i].strip(",.")
        if re.fullmatch(r"\d+(?:\.\d+)?", w0) and i + 1 < len(words) and _unit_word(words[i + 1].strip(",.")):
            i += 1  # "1 14.5 ounce can ..." -> skip the package size
            continue
        if not _unit_word(w0):
            break
        u = _unit_word(w0)
        if unit is None:
            unit = u
        i += 1
        if i < len(words) and words[i] == "of":
            i += 1
        if i < len(words) and words[i] in ("fl", "fluid"):  # fluid ounce
            pass
    s = " ".join(words[i:])
    mp = re.match(r"^plus\s+(\d[\d/. ]*)\s*([a-z]+)\s+", s)      # "1 tablespoon PLUS 2 teaspoons gelatin"
    if mp and _unit_word(mp.group(2)):
        s = s[mp.end():]
    s = _LABEL.sub("", s)
    segs = [x for x in s.split(",") if x.strip()]  # drop ", finely chopped" style tails
    s = segs[0] if segs else ""
    for cand in segs:  # "skinless, boneless chicken breast" / "diced and chilled, cooked chicken": skip prep-only segments
        names = [normalize_name(p) for p in re.split(r"\s+or\s+|/", cand)]
        if any(n and not _prep_only(n) for n in names):
            s = cand
            break
    s = re.split(r"\b(?:plus|to taste|for (?:the|garnish|serving|dusting|frying|greasing|brushing)|such as|cut into|as needed|if desired)\b", s)[0]
    s = s.strip(" .;:-")
    if not s:
        return qty, unit, ()
    # split "X or Y" alternatives; "salt and pepper" into two lines worth of names
    parts = [s]
    if " or " in s:
        parts = [p for p in re.split(r"\s+or\s+|/", s) if p.strip()]
    elif " and " in s:
        a, b = s.split(" and ", 1)
        if normalize_name(a) in ("salt", "black_pepper") or normalize_name(b) in ("salt", "black_pepper"):
            parts = [a, b]
    alts = []
    for p in parts[:4]:
        n = normalize_name(p)
        if n and n not in alts:
            alts.append(n)
    return qty, unit, tuple(alts)


@lru_cache(maxsize=200_000)
def normalize_name(text: str) -> str:
    """Normalize a bare ingredient phrase (no quantity) to a canonical token string."""
    s = text.lower().replace("’", "'").strip()
    s = _PAREN.sub(" ", s)
    s = re.sub(r"[^a-z0-9%'\- /]", " ", s)
    for rx, rep in _PHRASE_RE:
        s = rx.sub(rep, s)
    toks = []
    for w in _WORD.findall(s):
        w = w.strip("'")
        if not w:
            continue
        if w.endswith("'s"):
            w = w[:-2]
        if "-" in w and w not in DESCRIPTORS:
            toks.extend(w.split("-"))
        else:
            toks.append(w)
    out = []
    for w in toks:
        if w in DESCRIPTORS or w in STOPWORDS or w.isdigit():
            continue
        out.append(singular(w))
    s = " ".join(out)
    for rx, rep in _ATOMIC_RE:
        s = rx.sub(rep, s)
    toks = s.split()
    fused: list[str] = []
    for t in toks:
        if t in PRODUCT_SUFFIX and fused and not fused[-1].endswith("_"):
            fused[-1] = fused[-1] + "_" + t
        else:
            fused.append(t)
    return " ".join(fused)


def query_name(text: str) -> str:
    """Normalize a user-typed ingredient the same way recipe lines are normalized."""
    return normalize_name(text)


# ----------------------------------------------------------------------------- #
# Matching
# ----------------------------------------------------------------------------- #
def name_matches(query: str, alt: str) -> bool:
    """Does normalized name `alt` contain `query` as a contiguous token run (with generic expansion)?"""
    if not query:
        return False
    q = query.split()
    a = alt.split()
    n = len(q)
    for i in range(len(a) - n + 1):
        ok = True
        for j in range(n):
            t, u = a[i + j], q[j]
            if t == u:
                continue
            if u in GENERIC and t.endswith("_" + u) and j == n - 1:
                continue
            ok = False
            break
        if ok:
            return True
    return False


def _esc(x: str) -> str:
    return x.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sql_like_patterns(query: str) -> list[str]:
    """LIKE patterns (ESCAPE '\') over the padded `alts` column (' a b | c d ') equal to name_matches."""
    q = query.split()
    if not q:
        return []
    last = q[-1]
    head = " ".join(q[:-1])
    head = (_esc(head) + " ") if head else ""
    pats = [f"% {head}{_esc(last)} %"]
    if last in GENERIC:
        pats.append(f"% {head}%\\_{last} %")
    return pats


# ----------------------------------------------------------------------------- #
# Dietary groups
# ----------------------------------------------------------------------------- #
G_DAIRY, G_EGG, G_PEANUT, G_TREENUT, G_GLUTEN, G_SOY, G_SESAME = 1, 2, 4, 8, 16, 32, 64
G_FISH, G_SHELLFISH, G_MEAT, G_PORK, G_ALCOHOL, G_HONEY = 128, 256, 512, 1024, 2048, 4096

GROUP_NAMES = {G_DAIRY: "dairy", G_EGG: "egg", G_PEANUT: "peanut", G_TREENUT: "tree nut", G_GLUTEN: "gluten (wheat/barley/rye)",
               G_SOY: "soy", G_SESAME: "sesame", G_FISH: "fish", G_SHELLFISH: "shellfish", G_MEAT: "meat",
               G_PORK: "pork", G_ALCOHOL: "alcohol", G_HONEY: "honey"}

_HEADS = {
    G_DAIRY: set("milk butter buttermilk cream cheese cheddar parmesan mozzarella ricotta feta gouda gruyere provolone brie "
                 "mascarpone romano yogurt kefir ghee whey casein custard half_and_half lactose paneer curd gorgonzola "
                 "camembert fontina pecorino asiago halloumi quark creme".split()),
    G_EGG: set("egg mayonnaise meringue eggnog aioli".split()),
    G_PEANUT: {"peanut", "groundnut"},
    G_TREENUT: set("almond walnut pecan cashew pistachio hazelnut macadamia brazil_nut pine_nut chestnut marzipan praline "
                   "nut filbert nutella amaretto".split()),
    G_GLUTEN: set("flour wheat bread breadcrumb pasta spaghetti macaroni penne fettuccine linguine noodle couscous barley rye "
                  "semolina cracker tortilla pita bun biscuit seitan bulgur farro orzo lasagna ravioli panko crouton "
                  "tamari_wheat pretzel bagel".split()),
    G_SOY: set("soy soybean tofu tempeh edamame miso tamari".split()),
    G_SESAME: {"sesame", "tahini"},
    G_FISH: set("fish salmon tuna cod anchovy tilapia halibut trout sardine mackerel catfish snapper haddock swordfish bass "
                "herring".split()),
    G_SHELLFISH: set("shrimp crab crabmeat lobster clam oyster mussel scallop crawfish crayfish squid calamari langoustine octopus "
                     "abalone shellfish seafood".split()),
    G_MEAT: set("chicken beef pork bacon ham sausage turkey lamb veal steak salami pepperoni prosciutto duck venison gelatin "
                "lard chorizo pancetta brisket meat meatball hotdog hot_dog bologna sirloin liver goose rabbit "
                "mutton hamburger kielbasa bratwurst andouille frankfurter wiener mortadella capicola tenderloin oxtail tripe "
                "gizzard drippings tallow suet foie quail pheasant bison elk chuck gelatine guanciale meatloaf bonito katsuobushi "
                "flounder sole short_rib".split()),
    G_PORK: set("pork bacon ham prosciutto pancetta lard chorizo".split()),
    G_ALCOHOL: set("wine beer rum vodka whiskey whisky brandy bourbon liqueur sherry tequila gin champagne amaretto "
                   "kahlua cognac".split()),
    G_HONEY: {"honey"},
}
# modifier tokens that turn e.g. "X_milk" into a NON-dairy / different product
_DAIRY_SAFE_PREFIX = {"coconut", "almond", "soy", "rice", "oat", "cashew", "peanut", "cocoa", "apple", "hemp",
                      "sunflower", "nut", "shea", "cream_of_tartar"}
_SAFE_TOKENS = {
    G_DAIRY: {"cream_of_tartar", "cream_of_wheat", "coconut_cream", "coconut_milk", "almond_milk", "soy_milk", "oat_milk",
              "rice_milk", "cashew_milk", "peanut_butter", "almond_butter", "apple_butter", "cocoa_butter",
              "sunflower_seed_butter", "butter_flavor", "nut_butter"},
    G_GLUTEN: {"rice_noodle", "rice_flour", "corn_flour", "almond_flour", "coconut_flour", "chickpea_flour", "potato_flour",
               "tapioca_flour", "oat_flour", "buckwheat_flour", "corn_tortilla", "rice_bread", "rice_vinegar",
               "gluten_free_flour", "cornbread"},
    G_TREENUT: {"coconut", "nutmeg"},
    G_MEAT: set(),
    G_SHELLFISH: {"oyster_mushroom", "oyster_sauce_vegetarian"},
    G_EGG: {"egg_noodle_free"},
    G_SOY: set(),
}
_UNCERTAIN = {
    G_DAIRY: set("margarine chocolate chocolate_chip caramel butterscotch toffee pudding_mix cake_mix whipped_topping "
                 "ranch_dressing creamer bread_crumb".split()),
    G_EGG: set("pasta cake_mix pancake_mix noodle lasagna fettuccine ravioli".split()),
    G_GLUTEN: set("oat soy_sauce beer".split()),
    G_MEAT: set("broth stock bouillon chicken_broth worcestershire_sauce".split()) - {"chicken_broth"},
    G_FISH: {"worcestershire_sauce", "caesar_dressing"},
    G_PORK: {"sausage"},
}
# group classification for words that are heads of a group only when last component
_LAST_COMPONENT_ONLY = {G_DAIRY, G_GLUTEN}


def _token_groups(tok: str) -> tuple[int, int]:
    """(definite_mask, uncertain_mask) for one normalized token."""
    sure = 0
    maybe = 0
    comps = tok.split("_")
    for g, heads in _HEADS.items():
        if tok in _SAFE_TOKENS.get(g, ()):
            continue
        hit = tok in heads
        if not hit and len(comps) > 1:
            if g == G_DAIRY:
                # "cream_of_mushroom", "X_cheese", "chocolate_milk": dairy unless modifier says otherwise
                if comps[0] in _DAIRY_SAFE_PREFIX or "_".join(comps[:3]) in _SAFE_TOKENS[g]:
                    hit = False
                else:
                    hit = any(c in heads for c in comps)
            elif g in (G_GLUTEN,):
                hit = comps[-1] in heads and not any(
                    c in ("rice", "corn", "almond", "coconut", "chickpea", "potato", "tapioca", "oat", "buckwheat", "gluten")
                    for c in comps[:-1])
            else:
                hit = any(c in heads for c in comps)
        if hit:
            sure |= g
    for g, toks in _UNCERTAIN.items():
        if tok in toks:
            maybe |= g
    # pork / meat nesting: pork-only items are also "meat"
    if sure & G_PORK:
        sure |= G_MEAT
    return sure, maybe


@lru_cache(maxsize=200_000)
def name_groups(name: str) -> tuple[int, int]:
    sure = maybe = 0
    for t in name.split():
        s, m = _token_groups(t)
        sure |= s
        maybe |= m
    maybe &= ~sure
    return sure, maybe


def line_groups(alts: tuple[str, ...]) -> tuple[int, int]:
    """Conservative: a line with 'A or B' is flagged if ANY alternative is flagged."""
    sure = maybe = 0
    for a in alts:
        s, m = name_groups(a)
        sure |= s
        maybe |= m
    return sure, maybe & ~sure


STAPLES = {"salt", "black_pepper", "water", "ice_water"}


def is_staple(alts: tuple[str, ...]) -> bool:
    return bool(alts) and all(a in STAPLES or a in ("water", "salt", "black pepper") for a in alts)


def display(name: str) -> str:
    return name.replace("_", " ").replace("allpurpose", "all-purpose")


def describe_groups(mask: int) -> str:
    return ", ".join(n for g, n in GROUP_NAMES.items() if mask & g)
