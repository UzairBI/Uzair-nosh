"""Task 3 - evidence-based ingredient substitution.

Pipeline
  1. resolve the target ingredient line(s) inside the chosen recipe (+ every other line that breaks the
     requested dietary restriction);
  2. take candidate substitutes from a small curated table of culinary roles (this is *general knowledge*, it
     is labelled as such), drop candidates that violate the restriction or never occur in the dataset;
  3. gather LOCAL EVIDENCE for each candidate from the dataset:
       a. "neighbours": the recipes most similar to the target (title words + Jaccard on ingredient names);
          neighbours without the target ingredient that contain the candidate are direct support;
       b. "labelled free-from" recipes: titles containing eggless / egg-free / dairy-free / vegan ... plus the dish words;
  4. rate evidence strong / weak / none and flag conflicts (candidate equally common in neighbours that still use
     the original ingredient, i.e. it is probably not acting as a replacement);
  5. produce the adapted ingredient list and rewritten steps, marking everything that is a prediction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import ingredients as ing
from .store import Filter, Store


def fmt(x: float) -> str:
    if x is None:
        return ""
    whole = int(x)
    frac = x - whole
    table = [(0, ""), (1 / 8, "1/8"), (1 / 4, "1/4"), (1 / 3, "1/3"), (1 / 2, "1/2"), (2 / 3, "2/3"), (3 / 4, "3/4"), (1, "")]
    best = min(table, key=lambda t: abs(t[0] - frac))
    if best[0] == 1:
        whole, best = whole + 1, (0, "")
    s = (str(whole) if whole else "") + ((" " if whole and best[1] else "") + best[1])
    return s or "0"


def amt(q, u):
    """'1 1/2 cup' style text of the original amount."""
    if q is None:
        return ""
    return (fmt(q) + (" " + u if u else "")).strip()


@dataclass
class Option:
    key: str                # display key
    label: str              # what it is called in the ingredient list
    probes: tuple           # normalized dataset names used to look for evidence (any of them)
    line: callable          # (qty, unit) -> new ingredient line text
    short: str              # how the step text refers to it
    prep: str = ""          # extra preparation step to insert
    effect: str = ""        # PREDICTED effect (not verified)
    require_all_probes: bool = False


def _per(factor, unit_out, label):
    return lambda q, u: f"{fmt((q if q is not None else 1) * factor)} {unit_out} {label}".replace("  ", " ")


def _same(label):
    return lambda q, u: f"{amt(q, u)} {label}".strip()


TABLE: dict[str, dict] = {
    "egg": {"role": "binder, leavening and moisture (richness in baking)", "options": [
        Option("applesauce", "unsweetened applesauce", ("applesauce",), _per(0.25, "cup", "unsweetened applesauce"), "applesauce",
               effect="adds moisture and light binding; crumb will be denser/moister and a little sweeter, with less lift and browning"),
        Option("flax egg", "ground flaxseed", ("flaxseed", "flax"), lambda q, u: f"{fmt(q if q is not None else 1)} tbsp ground flaxseed + {fmt(3 * (q if q is not None else 1))} tbsp water",
               "flax egg", prep="Whisk the ground flaxseed with the water and let it stand 5 minutes until gel-like (this is the 'flax egg').",
               effect="binds reasonably well in dense bakes; slightly nutty, denser crumb, little leavening"),
        Option("mashed banana", "ripe banana", ("banana",), _per(0.25, "cup", "mashed ripe banana"), "mashed banana",
               effect="binds and moistens; adds banana flavour and sweetness, denser texture"),
        Option("silken tofu", "silken tofu", ("tofu",), _per(0.25, "cup", "silken tofu, blended smooth"), "blended silken tofu",
               effect="good for binding in custards/cakes; neutral taste, soft dense crumb"),
        Option("yogurt", "plain yogurt", ("yogurt",), _per(0.25, "cup", "plain yogurt"), "yogurt",
               effect="adds moisture and some lift when combined with baking soda; tangy note"),
        Option("vinegar + baking soda", "vinegar & baking soda", ("vinegar",), lambda q, u: f"{fmt(q if q is not None else 1)} tbsp vinegar + {fmt(q if q is not None else 1)} tsp baking soda",
               "vinegar and baking soda mixture", prep="Stir the vinegar and baking soda together just before adding; it will fizz.",
               effect="provides lift only (no binding); works in cakes/muffins, not in custards or meringues"),
    ]},
    "milk": {"role": "liquid, richness, browning", "options": [
        Option("soy milk", "soy milk", ("soy_milk",), _same("soy milk"), "soy milk", effect="1:1 swap; slightly beany, browns less"),
        Option("almond milk", "almond milk", ("almond_milk",), _same("unsweetened almond milk"), "almond milk", effect="1:1 swap; thinner and less rich"),
        Option("oat milk", "oat milk", ("oat_milk",), _same("oat milk"), "oat milk", effect="1:1 swap; creamy, mildly sweet"),
        Option("coconut milk", "coconut milk", ("coconut_milk",), _same("coconut milk"), "coconut milk", effect="richer; adds coconut flavour"),
        Option("water", "water", ("water",), _same("water"), "water", effect="thins flavour and richness; fine where milk is only a liquid"),
    ]},
    "butter": {"role": "fat, flavour, tenderness", "options": [
        Option("margarine", "margarine (check it is dairy-free)", ("margarine",), _same("dairy-free margarine"), "margarine",
               effect="1:1 swap; similar texture, less butter flavour"),
        Option("olive oil", "olive oil", ("olive_oil",), lambda q, u: f"{fmt((q or 1) * 0.75)} {u or ''} olive oil".replace("  ", " "), "olive oil",
               effect="use about 3/4 of the amount; softer, more tender crumb; no browning from milk solids"),
        Option("vegetable oil", "vegetable oil", ("vegetable_oil",), lambda q, u: f"{fmt((q or 1) * 0.75)} {u or ''} vegetable oil".replace("  ", " "), "vegetable oil",
               effect="use about 3/4 of the amount; neutral, more tender but less flavour"),
        Option("coconut oil", "coconut oil", ("coconut_oil",), _same("coconut oil"), "coconut oil", effect="1:1 swap; firm when cool, slight coconut flavour"),
        Option("shortening", "vegetable shortening", ("shortening",), _same("vegetable shortening"), "shortening", effect="1:1 swap; tender but no butter flavour"),
    ]},
    "cream": {"role": "richness, body, thickening in sauces", "options": [
        Option("coconut cream", "coconut cream", ("coconut_cream", "coconut_milk"), _same("full-fat coconut milk or coconut cream"), "coconut milk",
               effect="similar richness and body; adds coconut flavour"),
        Option("soy milk + oil", "soy milk and olive oil", ("soy_milk",), _same("unsweetened soy milk, plus 1 tbsp olive oil per cup"), "soy milk and olive oil",
               effect="thinner than cream; simmer a little longer to reduce, or thicken with 1 tsp cornstarch per cup"),
        Option("silken tofu", "silken tofu", ("tofu",), _same("silken tofu, blended smooth with a splash of water"), "blended silken tofu",
               effect="creamy body, neutral taste; do not boil hard"),
        Option("olive oil + water", "pasta water & olive oil", ("olive_oil",), _same("starchy pasta water + 1 tbsp olive oil"), "pasta water and olive oil",
               effect="a lighter, glossy sauce rather than a creamy one"),
    ]},
    "sour cream": {"role": "tang, moisture", "options": [
        Option("coconut cream + lemon", "coconut cream and lemon juice", ("coconut_cream", "coconut_milk"), _same("coconut cream + 1 tsp lemon juice"), "coconut cream with lemon",
               effect="similar thickness and tang; coconut flavour"),
        Option("silken tofu + lemon", "silken tofu and lemon juice", ("tofu",), _same("blended silken tofu + 1 tsp lemon juice"), "blended tofu with lemon", effect="similar body; milder tang"),
    ]},
    "cheese": {"role": "salty/umami flavour, melt", "options": [
        Option("nutritional yeast", "nutritional yeast", ("nutritional_yeast",), lambda q, u: f"{fmt((q or 1) * 0.5)} {u or ''} nutritional yeast".replace("  ", " "), "nutritional yeast",
               effect="gives cheesy/umami flavour but does not melt or stretch"),
        Option("more salt + lemon", "salt & lemon", ("lemon",), _same("pinch of salt + squeeze of lemon juice"), "a pinch of salt and lemon juice", effect="replaces the savoury tang only; no melt"),
    ]},
    "parmesan": {"role": "salty/umami topping", "options": [
        Option("nutritional yeast", "nutritional yeast", ("nutritional_yeast",), _same("nutritional yeast"), "nutritional yeast", effect="umami, no melt"),
        Option("breadcrumbs", "toasted breadcrumbs", ("bread_crumb",), _same("toasted breadcrumbs with a pinch of salt"), "toasted breadcrumbs", effect="adds crunch and savouriness (contains gluten)"),
    ]},
    "flour": {"role": "structure, thickening", "options": [
        Option("rice flour", "rice flour", ("rice_flour",), _same("rice flour"), "rice flour", effect="gritty/crumbly in baking; fine for thickening"),
        Option("cornstarch", "cornstarch", ("cornstarch",), lambda q, u: f"{fmt((q or 1) * 0.5)} {u or ''} cornstarch".replace("  ", " "), "cornstarch", effect="thickening only: use half the amount; not a structural flour"),
        Option("almond flour", "almond flour", ("almond_flour",), _same("almond flour"), "almond flour", effect="denser and moister; contains tree nuts"),
    ]},
    "sugar": {"role": "sweetness, moisture, browning", "options": [
        Option("honey", "honey", ("honey",), lambda q, u: f"{fmt((q or 1) * 0.75)} {u or ''} honey".replace("  ", " "), "honey",
               prep="Because honey adds liquid, reduce other liquid by about 1/4 cup per cup of honey and lower the oven by 25 °F (about 15 °C).",
               effect="sweeter and moister, browns faster"),
        Option("maple syrup", "maple syrup", ("maple_syrup",), lambda q, u: f"{fmt((q or 1) * 0.75)} {u or ''} maple syrup".replace("  ", " "), "maple syrup",
               prep="Because maple syrup adds liquid, reduce other liquid slightly.", effect="moister, mild maple flavour"),
    ]},
    "honey": {"role": "sweetness, moisture", "options": [
        Option("maple syrup", "maple syrup", ("maple_syrup",), _same("maple syrup"), "maple syrup", effect="1:1 swap; thinner, mild maple flavour"),
        Option("sugar", "sugar", ("sugar",), lambda q, u: f"{fmt((q or 1) * 0.75)} {u or ''} sugar".replace("  ", " "), "sugar", effect="drier; add a splash of water"),
    ]},
    "mayonnaise": {"role": "emulsified fat, binder", "options": [
        Option("avocado", "mashed avocado", ("avocado",), _same("mashed avocado"), "mashed avocado", effect="creamy and rich; green colour, browns on standing"),
        Option("yogurt", "plain yogurt", ("yogurt",), _same("plain yogurt"), "yogurt", effect="tangier and thinner"),
    ]},
    "peanut butter": {"role": "fat, binder, flavour", "options": [
        Option("sunflower seed butter", "sunflower seed butter", ("sunflower_seed_butter",), _same("sunflower seed butter"), "sunflower seed butter", effect="1:1 swap; similar texture, earthier"),
        Option("tahini", "tahini", ("tahini",), _same("tahini"), "tahini", effect="1:1 swap; more bitter and savoury (sesame allergen)"),
    ]},
    "soy sauce": {"role": "salt and umami", "options": [
        Option("worcestershire", "worcestershire sauce", ("worcestershire_sauce",), _same("worcestershire sauce"), "worcestershire sauce", effect="salty-umami but not identical; check labels for gluten/fish"),
        Option("salt", "salt", ("salt",), lambda q, u: "1/2 tsp salt + 1/2 tsp water per tablespoon of soy sauce", "salt water", effect="salt only; loses the savoury depth"),
    ]},
    "chicken broth": {"role": "savoury liquid", "options": [
        Option("vegetable broth", "vegetable broth", ("vegetable_broth",), _same("vegetable broth"), "vegetable broth", effect="1:1 swap; lighter flavour"),
        Option("water", "water + salt", ("water",), _same("water + extra salt/herbs"), "water", effect="much less flavour"),
    ]},
    "ground beef": {"role": "bulk, savouriness", "options": [
        Option("lentils", "cooked lentils", ("lentil",), _same("cooked lentils"), "lentils", effect="earthy, softer bite"),
        Option("mushroom", "finely chopped mushrooms", ("mushroom",), _same("finely chopped mushrooms"), "mushrooms", effect="meaty umami, moister"),
        Option("tofu", "crumbled firm tofu", ("tofu",), _same("crumbled firm tofu"), "tofu", effect="mild; soaks up seasoning"),
    ]},
    "buttermilk": {"role": "tang, tenderness", "options": [
        Option("soy milk + vinegar", "soy milk and vinegar", ("soy_milk",), _same("soy milk + 1 tbsp vinegar per cup (let stand 5 min)"), "soured soy milk", effect="close to buttermilk in baking"),
    ]},
}


def table_key(name: str) -> str | None:
    """Map a normalized ingredient name to a TABLE key ('heavy cream' -> 'cream')."""
    for key in sorted(TABLE, key=len, reverse=True):
        if ing.name_matches(key.replace(" ", " "), name) or ing.name_matches(ing.normalize_name(key), name):
            return key
    return None


# ----------------------------------------------------------------------------------------------- evidence
_STOP_TITLE = {"the", "and", "with", "a", "of", "easy", "best", "perfect", "quick", "classic", "homemade", "my", "recipe",
               "simple", "favorite", "world", "famous", "ultimate", "great", "grandma", "mom", "s", "style", "in", "for", "to"}
_LABELS = {ing.G_EGG: ["eggless", '"egg free"', "vegan", '"no egg"', '"without eggs"'],
           ing.G_DAIRY: ["dairy", '"dairy free"', "vegan", "nondairy", '"non dairy"'],
           ing.G_GLUTEN: ['"gluten free"', "gluten-free"], ing.G_PEANUT: ['"peanut free"', "nut-free"]}


def _title_words(title: str) -> list[str]:
    return list(dict.fromkeys(w for w in re.findall(r"[a-z]+", title.lower()) if w not in _STOP_TITLE and len(w) > 2))[:6]


def _core_names(store: Store, rids: list[int]) -> dict[int, list[list[str]]]:
    out: dict[int, list[list[str]]] = {r: [] for r in rids}
    for i in range(0, len(rids), 400):
        part = rids[i:i + 400]
        q = ",".join("?" * len(part))
        for row in store.db.execute(f"SELECT rid, alts, raw FROM ing WHERE rid IN ({q}) AND alts<>'' AND staple=0", part):
            out[row["rid"]].append(([a.strip() for a in row["alts"].split("|") if a.strip()], row["raw"]))
    return out


def _line_with(lines, probe: str):
    for alts, raw in lines:
        if any(ing.name_matches(probe, a) for a in alts):
            return raw
    return None


def _has(lines, name):
    return _line_with(lines, name) is not None


@dataclass
class Evidence:
    option: Option
    neighbour_support: list = field(default_factory=list)   # [(id, title, line)]
    labelled_support: list = field(default_factory=list)
    n_without: int = 0
    n_with: int = 0
    rate_with: float = 0.0
    rate_without: float = 0.0
    dataset_occurrences: int = 0
    strength: str = "none"
    conflict: bool = False
    notes: list = field(default_factory=list)


def gather_evidence(store: Store, recipe: dict, target: str, options: list[Option], mask: int, k: int = 60) -> tuple[list[Evidence], dict]:
    words = _title_words(recipe["title"])
    ctx = {"neighbours": 0, "n_with": 0, "n_without": 0, "labelled": 0, "title_words": words}
    own = {a for l in recipe["ingredients"] if l["name"] for a in l["alts"]}
    core_t = set(own)
    # --- neighbours -------------------------------------------------------------------------
    neigh = []
    if words:
        rids, _, _ = store.search(Filter(text=tuple(words)), limit=400)
        rids = [r for r in rids if r != recipe["rid"]]
        names = _core_names(store, rids)
        scored = []
        for r in rids:
            ns = {a for alts, _ in names[r] for a in alts[:1]}
            j = len(ns & core_t) / max(1, len(ns | core_t))
            scored.append((j, r))
        scored.sort(reverse=True)
        neigh = [(r, names[r]) for j, r in scored[:k] if j >= 0.08]
    ctx["neighbours"] = len(neigh)
    with_x = [(r, l) for r, l in neigh if _has(l, target)]
    without_x = [(r, l) for r, l in neigh if not _has(l, target)]
    if mask:
        flags = {b["rid"]: b["flags"] for b in store.brief([r for r, _ in without_x])}
        without_x = [(r, l) for r, l in without_x if not (flags.get(r, 0) & mask)]
    ctx["n_with"], ctx["n_without"] = len(with_x), len(without_x)
    # --- labelled free-from recipes -----------------------------------------------------------
    labelled = []
    labs = []
    for g, ls in _LABELS.items():
        if mask & g:
            labs += ls
    if labs and words:
        expr = "title : ((" + " OR ".join(labs) + ") AND (" + " OR ".join(f'"{w}"' for w in words[:4]) + "))"
        try:
            rows = store.db.execute("SELECT r.rid FROM fts JOIN recipes r ON r.rid=fts.rowid WHERE fts MATCH ? AND r.dup=0 "
                                    "ORDER BY bm25(fts, 8.0, 1.0) LIMIT 60", (expr,)).fetchall()
        except Exception:
            rows = []
        dw = list(dict.fromkeys(words))
        need = min(2, len(dw))
        lrids = []
        for b in store.brief([r[0] for r in rows if r[0] != recipe["rid"]]):
            if sum(1 for w in dw if re.search(r"\b" + re.escape(w) + r"(?:s|es)?\b", b["title"].lower())) >= need:
                lrids.append(b["rid"])
        names = _core_names(store, lrids)
        labelled = [(r, names[r]) for r in lrids if not _has(names[r], target)]
    ctx["labelled"] = len(labelled)
    titles = {b["rid"]: b for b in store.brief([r for r, _ in without_x] + [r for r, _ in labelled])}
    out = []
    for opt in options:
        ev = Evidence(option=opt, n_without=len(without_x), n_with=len(with_x))
        for r, l in without_x:
            for p in opt.probes:
                raw = _line_with(l, p)
                if raw:
                    ev.neighbour_support.append((titles[r]["id"], titles[r]["title"], raw))
                    break
        for r, l in labelled:
            for p in opt.probes:
                raw = _line_with(l, p)
                if raw:
                    ev.labelled_support.append((titles[r]["id"], titles[r]["title"], raw))
                    break
        ev.rate_without = len(ev.neighbour_support) / len(without_x) if without_x else 0.0
        ev.rate_with = sum(1 for _, l in with_x if any(_has(l, p) for p in opt.probes)) / len(with_x) if with_x else 0.0
        ev.dataset_occurrences = _occurrences(store, opt.probes[0])
        total = len(ev.neighbour_support) + len(ev.labelled_support)
        if total >= 3 and (len(ev.neighbour_support) >= 2 or len(ev.labelled_support) >= 2):
            ev.strength = "strong"
        elif total >= 1:
            ev.strength = "weak"
        if total and with_x and (ev.rate_with >= 0.5 or (len(without_x) >= 5 and ev.rate_with >= max(0.15, ev.rate_without))):
            ev.notes.append(f"conflicting/indirect: {opt.probes[0].replace('_', ' ')} also appears in {ev.rate_with:.0%} of similar recipes that still use "
                            f"{ing.display(target)}, so it is a normal ingredient of this dish and may not be acting as a replacement")
            ev.conflict = True
            if ev.strength == "strong":
                ev.strength = "weak"
        if not total:
            ev.notes.append("no similar recipe in the dataset shows this swap")
        elif len(without_x) < 5 and not ev.labelled_support:
            ev.notes.append(f"indirect: only {len(without_x)} similar recipes lack {ing.display(target)}")
        out.append(ev)
    rank = {"strong": 0, "weak": 1, "none": 2}
    out.sort(key=lambda e: (rank[e.strength], e.conflict, -(len(e.neighbour_support) + len(e.labelled_support)), -e.dataset_occurrences))
    return out, ctx


_OCC: dict[str, int] = {}


def _occurrences(store: Store, name: str) -> int:
    if name not in _OCC:
        _OCC[name] = store.count(Filter(include=(name,)))
    return _OCC[name]


# ----------------------------------------------------------------------------------------------- adaptation
def _word_re(name: str):
    last = ing.display(name).split()[-1]
    stem = re.escape(last)
    return re.compile(rf"\b{stem}(?:s|es)?\b", re.I)


def adapt(recipe: dict, target: str, opt: Option, extra_swaps: list | None = None):
    """Return (new_ingredient_lines, new_steps, changes) with the target replaced by `opt`."""
    new_lines, changes = [], []
    swapped = False
    for l in recipe["ingredients"]:
        if l["alts"] and any(ing.name_matches(target, a) for a in l["alts"]) and not swapped:
            new = opt.line(l["qty"], l["unit"])
            new_lines.append(new)
            changes.append((l["raw"], new))
            swapped = True
        elif l["alts"] and any(ing.name_matches(target, a) for a in l["alts"]):
            # a second line for the same ingredient (e.g. egg yolk + egg): drop, mention in changes
            changes.append((l["raw"], "(removed - covered by the substitute above)"))
        else:
            new_lines.append(l["raw"])
    steps = list(recipe["steps"])
    rx = _word_re(target)
    first = None
    modified = []
    for i, s in enumerate(steps):
        if rx.search(s):
            if first is None:
                first = i
            steps[i] = rx.sub(opt.short, s)
            modified.append(i + 1)
    prep = opt.prep
    if prep:
        pos = first if first is not None else 0
        steps.insert(pos, f"[Added] {prep}")
        modified = [m + 1 if m - 1 >= pos else m for m in modified]
    return new_lines, steps, changes, modified, (first is not None)
