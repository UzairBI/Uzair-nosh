"""Rule-based natural-language understanding for the chatbot.

The rules are deliberately transparent: every message is turned into an `Intent`; the executor in
session.py then does all retrieval and arithmetic.  An optional LLM (Gemini) can re-parse messages the rules
cannot classify (llm.py), but it only ever produces the same structured Intent -- it never answers from memory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import ingredients as ing

NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split())}
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "fifth": 5, "5th": 5,
            "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}

# restriction words -> dietary group mask
RESTRICT = {
    "dairy": ing.G_DAIRY, "lactose": ing.G_DAIRY, "milk products": ing.G_DAIRY, "non-dairy": ing.G_DAIRY,
    "egg": ing.G_EGG, "eggs": ing.G_EGG, "peanut": ing.G_PEANUT, "peanuts": ing.G_PEANUT,
    "nut": ing.G_TREENUT | ing.G_PEANUT, "nuts": ing.G_TREENUT | ing.G_PEANUT, "tree nut": ing.G_TREENUT,
    "tree nuts": ing.G_TREENUT, "gluten": ing.G_GLUTEN, "wheat": ing.G_GLUTEN, "soy": ing.G_SOY, "soya": ing.G_SOY,
    "sesame": ing.G_SESAME, "fish": ing.G_FISH, "shellfish": ing.G_SHELLFISH,
    "seafood": ing.G_FISH | ing.G_SHELLFISH, "pork": ing.G_PORK, "meat": ing.G_MEAT, "alcohol": ing.G_ALCOHOL,
    "vegetarian": ing.G_MEAT | ing.G_FISH | ing.G_SHELLFISH,
    "vegan": ing.G_MEAT | ing.G_FISH | ing.G_SHELLFISH | ing.G_DAIRY | ing.G_EGG | ing.G_HONEY,
    "honey": ing.G_HONEY,
}
FREE_FORMS = {"dairy-free": "dairy", "dairy free": "dairy", "lactose-free": "lactose", "gluten-free": "gluten", "gluten free": "gluten",
              "egg-free": "egg", "egg free": "egg", "eggless": "egg", "nut-free": "nuts", "nut free": "nuts",
              "peanut-free": "peanut", "peanut free": "peanut", "soy-free": "soy", "shellfish-free": "shellfish",
              "meatless": "vegetarian", "non-vegetarian": "", "fish-free": "fish", "pork-free": "pork", "sesame-free": "sesame"}

FILLER = set("""a an the some any please me my i you can could would show find give get want need looking look for search
recipe recipes dish dishes something anything that which are is it with of to and or also too either again just only
suggest tell about make cook one ones us we let's lets using use something""".split())

_NUMBER = r"(\d+|" + "|".join(NUM_WORDS) + r")"


def to_int(tok: str) -> int | None:
    tok = tok.lower()
    if tok.isdigit():
        return int(tok)
    return NUM_WORDS.get(tok)


@dataclass
class Intent:
    kind: str                       # search | match | stats | substitute | view | refine | more | reset | help | unknown
    raw: str = ""
    text: list = field(default_factory=list)      # dish words
    include: list = field(default_factory=list)   # normalized ingredient names (all required)
    any_of: list = field(default_factory=list)
    exclude: list = field(default_factory=list)
    mask: int = 0
    max_steps: int | None = None
    min_steps: int | None = None
    have: list = field(default_factory=list)      # [(name, qty, unit, label)]
    stat: list = field(default_factory=list)      # count, percent, mean, median, top_ingredients, rank_fewest, rank_most
    stat_col: str = "n_ing"
    n: int | None = None
    src: str | None = None
    title_has: list = field(default_factory=list)
    inherit: bool = False           # reuse the filter of the previous query ("among those")
    ref_index: int | None = None
    ref_id: str | None = None
    ref_this: bool = False
    view: str = "full"              # ingredients | instructions | full
    target: str | None = None       # ingredient to replace / remove
    with_: str | None = None        # user-proposed substitute
    prefer: str | None = None
    want_full: bool = False
    skip_staples: bool = False
    note: str = ""
    reset_filters: bool = False


# -------------------------------------------------------------------------------- helpers
def split_list(s: str) -> list[str]:
    s = re.sub(r"\b(?:both|all of|either|neither|any of)\b", " ", s)
    parts = re.split(r",|;|\band\b|\bor\b|\bnor\b|/|\bplus\b|&", s)
    out = []
    for p in parts:
        p = p.strip(" .?!'\"")
        p = re.sub(r"\b(?:too|either|as well|please|in it|in them|in the recipe|also|them|it)\b", "", p).strip()
        p = re.sub(r"^(?:the|some|any|a|an|more|extra|fresh)\s+", "", p).strip()
        if p and p not in FILLER:
            out.append(p)
    return out


def classify_item(item: str):
    """-> ('mask', int) | ('term', normalized name) | None"""
    it = item.lower().strip()
    if it in FREE_FORMS:
        it = FREE_FORMS[it]
    if it in RESTRICT:
        return "mask", RESTRICT[it]
    sing = ing.singular(it)
    if sing in RESTRICT:
        return "mask", RESTRICT[sing]
    n = ing.normalize_name(it)
    return ("term", n) if n else None


_NEG_LEAD = (r"(?:can(?:no|')?t|cannot|can not|unable to|am unable to|shouldn'?t|should not|mustn'?t|must not)\s+(?:have|eat|consume|tolerate|use|take)"
             r"|(?:don'?t|do not|doesn'?t|does not|won'?t|will not)\s+(?:eat|want|like|have|use|consume)"
             r"|(?:am|is|are|i'?m|im|i am)\s+allergic\s+to|allerg(?:y|ic)\s+(?:to)?|allergies?\s+(?:to|:)?"
             r"|(?:no|without|exclude|excluding|avoid|avoiding|skip|except|minus|omit|omitting|not containing|(?:that|which) (?:has|have|contains?) no|free of|free from|hold the|leave out)")
_NEG_RE = re.compile(rf"(?<![a-z-])({_NEG_LEAD})\s+((?:[a-z' -]+?)(?:\s*(?:,|and|or|/)\s*[a-z' -]+?)*?)(?=[.?!;]|$|\s+(?:and\s+(?:i|it|make|use|then|show|please|give|with|at|in|only)|but|because|since|so|for|in|from|when|while|at|under|within|that|which|using|use)\b)")


def extract_negations(msg: str):
    """Return (mask, exclude_terms, message_with_negations_removed)."""
    m = msg.lower()
    mask, terms = 0, []
    for ff, word in FREE_FORMS.items():
        if re.search(rf"(?<![a-z-]){re.escape(ff)}(?![a-z])", m):
            m = re.sub(rf"(?<![a-z-]){re.escape(ff)}(?![a-z])", " ", m)
            if word:
                k = classify_item(word)
                if k and k[0] == "mask":
                    mask |= k[1]
    # "I'm vegetarian / vegan"
    for w in ("vegetarian", "vegan"):
        if re.search(rf"(?<![a-z-]){w}\b", m) and not re.search(rf"\b(?:non|not)[- ]{w}\b", m):
            mask |= RESTRICT[w]
            m = re.sub(rf"\b{w}\b", " ", m)
    def repl(mt):
        nonlocal mask
        items = split_list(mt.group(2))
        keep = []
        for it in items:
            # stop at trailing junk words
            it = re.sub(r"\b(?:steps?|instruction.*|ingredients?)\b.*$", "", it).strip()
            it = re.sub(r"^(?:no|without|exclude|excluding|avoid|skip|not|any|more)\s+", "", it).strip()
            if not it:
                continue
            k = classify_item(it)
            if not k:
                continue
            if k[0] == "mask":
                mask |= k[1]
            else:
                terms.append(k[1])
        return " "
    m2 = _NEG_RE.sub(repl, m)
    return mask, list(dict.fromkeys(terms)), m2


def extract_steps(msg: str):
    m = msg.lower()
    mx = mn = None
    pats = [
        (rf"(?:at most|no more than|up to|max(?:imum)?(?: of)?|not more than|within|<=)\s*{_NUMBER}\s*(?:instruction |cooking |recipe )?steps?", "max", 0),
        (rf"(?:fewer than|less than|under|below|<)\s*{_NUMBER}\s*(?:instruction |cooking |recipe )?steps?", "max", -1),
        (rf"{_NUMBER}\s*(?:instruction |cooking )?steps?\s*(?:or (?:fewer|less)|max(?:imum)?|or under)", "max", 0),
        (rf"(?:at least|no fewer than|minimum(?: of)?|>=)\s*{_NUMBER}\s*(?:instruction |cooking |recipe )?steps?", "min", 0),
        (rf"(?:more than|over|above)\s*{_NUMBER}\s*(?:instruction |cooking |recipe )?steps?", "min", 1),
    ]
    for p, kind, adj in pats:
        mt = re.search(p, m)
        if mt:
            v = to_int(mt.group(1))
            if v is not None:
                v += adj
                if kind == "max":
                    mx = v
                else:
                    mn = v
                m = m.replace(mt.group(0), " ")
    return mx, mn, m


def parse_have(s: str):
    """'2 eggs, 1 cup rice and onions' -> [(name, qty, unit, label)]"""
    out = []
    for item in split_list(s):
        item = re.sub(r"^(?:i have|i've got|i got|i've|got|have|using|with|some|only|just)\s+", "", item)
        qty = unit = None
        mt = ing._QTY_RE.match(item)
        label = item
        if mt:
            try:
                qty = ing._to_float(mt.group(1))
            except Exception:
                qty = None
            rest = item[mt.end():].strip()
            w = rest.split(" ", 1)
            if w and ing.UNIT_CANON.get(w[0]) or (w and ing.UNIT_CANON.get(ing.singular(w[0]))):
                unit = ing.UNIT_CANON.get(w[0]) or ing.UNIT_CANON.get(ing.singular(w[0]))
                rest = w[1] if len(w) > 1 else ""
                rest = re.sub(r"^of\s+", "", rest)
            item = rest
        else:
            mt2 = re.match(rf"^{_NUMBER}\s+(.*)$", item)
            if mt2 and to_int(mt2.group(1)) is not None:
                qty = float(to_int(mt2.group(1)))
                item = mt2.group(2)
        name = ing.normalize_name(item)
        if name and name not in FILLER:
            out.append((name, qty, unit, label.strip()))
    return out


_LIST_END = r"(?=[.?!;]|$|\s*,?\s+(?:what|which|how|and what|and how|and which|that|then|but|so|prefer|preferably|with no|with at|at most|no more|and no|and at|and only|and prefer|without|excluding|exclude|except|where|ranked|sorted|in the|from the|from all)\b)"


def extract_contains(msg: str):
    """-> (include list, any_of list, remaining msg)"""
    m = msg
    include, any_of = [], []
    pat = re.compile(r"(?:contain(?:s|ing)?|with|has|have|having|use|uses|using|include|includes|including|made with|that (?:use|have|include|contain))\s+(?!no\b|without\b|the (?:most|fewest|least|highest|lowest)\b)(?:both\s+|all of\s+|only\s+)?"
                     r"((?:[a-z' -]+?)(?:\s*(?:,|and|or|/)\s*[a-z' -]+?)*?)" + _LIST_END)
    mt = pat.search(m)
    if not mt:
        return include, any_of, m
    seg = mt.group(1)
    is_or = bool(re.search(r"\bor\b", seg)) and not re.search(r"\band\b", seg)
    for item in split_list(seg):
        k = classify_item(item)
        if k and k[0] == "term":
            (any_of if is_or else include).append(k[1])
        elif k and k[0] == "mask":  # "recipes with peanuts": a positive mention is an ingredient, not a restriction
            n = ing.normalize_name(item)
            if n:
                (any_of if is_or else include).append(n)
    return include, any_of, m.replace(mt.group(0), " ")


def find_ref(msg: str, it: Intent):
    m = msg.lower()
    mt = re.search(r"\b((?:ar|epi|fn):[A-Za-z0-9._/\-]+)", msg)
    if mt:
        it.ref_id = mt.group(1).rstrip(".,?!")
        return
    mt = re.search(r"(?:#|number\s+|no\.?\s*|result\s+|recipe\s+)(\d+)\b", m)
    if mt:
        it.ref_index = int(mt.group(1))
        return
    for w, i in ORDINALS.items():
        if re.search(rf"\b(?:the\s+)?{w}\s*(?:one|recipe|result|option)?\b", m) and re.search(r"\b(one|recipe|result|option|dish|of them)\b|\b(show|tell|view|open|use|take|adapt|substitute|replace)\b", m):
            it.ref_index = i
            return
    if re.search(r"\b(?:the\s+)?last\s+(?:one|recipe|result)\b", m):
        it.ref_index = -1
        return
    if re.search(r"\b(this|that|it|the same|the recipe|the dish|these|above)\b", m):
        it.ref_this = True


# -------------------------------------------------------------------------------- main entry
SUBST_RE = re.compile(r"\b(substitut\w*|replac\w*|swap\w*|instead of|alternative(?:s)? (?:to|for)|remove\b|get rid of|take out|leave out|omit|"
                      r"without\b.*\b(?:in|from) (?:this|that|the|it)|adapt\w*|egg[- ]?free version|convert (?:it|this)|make (?:it|this|that) (?:\w+[- ]?free|vegan|vegetarian|eggless))")
MATCH_RE = re.compile(r"\b(i have|i've got|i got|i've|we have|we've got|what can i (?:make|cook)|use up|using up|in my (?:fridge|pantry|kitchen)|"
                      r"available ingredients|ingredients i have|leftover|with what i have|i only have|got some)\b")
STAT_RE = re.compile(r"\b(how many|count|number of|percentage|percent|what fraction|what share|what proportion|average|mean|median|most common|"
                     r"commonly|common ingredients?|top\s*\d*\s*ingredients?|most frequent|fewest|most steps|least steps|fewest steps|"
                     r"longest|shortest|rank|ranking|which (?:\w+\s+)?(?:have|has) the (?:most|fewest|least)|how often|statistic|total number)\b")
VIEW_RE = re.compile(r"\b(ingredients?|instructions?|steps|directions|method|full recipe|details|recipe card|read it|tell me more|more about|what'?s in|how (?:do i|to) make|how is it made)\b")
SEARCH_RE = re.compile(r"\b(find|search|look(?:ing)? for|recipes? for|recipes? (?:with|containing|using|that)|show me|give me|i want|i'?d like|suggest|recommend|do you have|any recipes?|got any)\b")


UNSUPPORTED_RE = re.compile(r"\b(calories?|kcal|nutrition\w*|protein|carbs?|carbohydrates?|macros?|sodium|cholesterol|fiber|prep(?:aration)? time|"
                            r"cook(?:ing)? time|total time|how long (?:does|will|to|do|is|should)|how many (?:minutes|hours)|servings?|serves how|"
                            r"how many people|price|how much does it cost|cost per|rating|reviews?)\b")


def parse(msg: str, state=None) -> Intent:
    raw = msg.strip()
    m = raw.lower().strip()
    it = Intent(kind="unknown", raw=raw)
    if not m:
        return it
    if re.search(r"^(?:help|\?|what can you do|how do(?:es)? this work|commands)\b", m):
        it.kind = "help"
        return it
    if re.search(r"\b(start over|start again|new search|reset|clear (?:everything|all|filters|the search)|forget (?:that|everything|it|all)|never ?mind)\b", m):
        it.kind = "reset"
        return it
    if re.fullmatch(r"(?:please )?(?:(?:show|give|see|get|find|bring)(?: me)?(?: some)? |any |are there |what about |i want |let me see )?(?:some )?(?:more|next|others|further)"
                    r"(?: (?:results?|recipes?|options|ones|ideas|suggestions|please|like (?:these|that|those)))*(?: please)?[.!?]*|next(?: page| \d+)?[.!?]*", m):
        it.kind = "more"
        return it

    if UNSUPPORTED_RE.search(m):
        it.kind = "unsupported"
        find_ref(raw, it)
        it.note = UNSUPPORTED_RE.search(m).group(1)
        return it

    mx, mn, m_steps = extract_steps(m)  # take step limits out first so "no peanuts, at most 6 steps" parses cleanly
    mask, excl_terms, m_clean = extract_negations(m_steps)
    mode = getattr(state, "mode", None)

    # --- substitution ------------------------------------------------------------------------
    subst = SUBST_RE.search(m) and not MATCH_RE.search(m) and not (STAT_RE.search(m) and not re.search(r"replac|substitut|swap", m))
    if subst:
        return _parse_subst(raw, m, it, state)
    if mode == "subst" and re.search(r"\b(?:use|try|what about|how about|go with|prefer|choose|pick|option)\b", m) and not SEARCH_RE.search(m):
        return _parse_subst(raw, m, it, state, follow=True)

    # --- ingredient matching -----------------------------------------------------------------
    if MATCH_RE.search(m) or (mode == "match" and re.search(r"\b(also have|i also|add|plus|i don'?t have|no longer have|ran out|prefer|fewest|fewer|fewest additional|fewer additional|least steps|shortest)\b", m)):
        return _parse_match(raw, m, m_clean, it, mask, excl_terms, mx, state)

    # --- statistics --------------------------------------------------------------------------
    if STAT_RE.search(m):
        return _parse_stats(raw, m, it, mask, excl_terms, mx, mn, state)

    # --- viewing a result --------------------------------------------------------------------
    if VIEW_RE.search(m) and not SEARCH_RE.search(m):
        find_ref(raw, it)
        if it.ref_index or it.ref_id or it.ref_this or re.search(r"\b(ingredients?|instructions?|directions|method|steps)\b", m):
            it.kind = "view"
            if re.search(r"\b(ingredients?|what'?s in)\b", m) and not re.search(r"instruction|direction|method", m):
                it.view = "ingredients"
            elif re.search(r"\b(instructions?|directions|method|how (?:do i|to) make|steps)\b", m) and not re.search(r"ingredient", m):
                it.view = "instructions"
            if not (it.ref_index or it.ref_id):
                it.ref_this = True
            return it
    if re.search(r"^(?:#|number\s+)?\d\.?$", m) or re.fullmatch(r"(?:the )?(?:first|second|third|fourth|fifth|last)(?: one)?\.?", m):
        find_ref(raw, it)
        it.kind = "view"
        return it

    # --- search / refinement -----------------------------------------------------------------
    inc, any_of, rest = extract_contains(m_clean)
    it.include, it.any_of, it.exclude, it.mask = inc, any_of, excl_terms, mask
    it.max_steps, it.min_steps = mx, mn
    words = re.findall(r"[a-z][a-z'-]*", rest)
    text = [w for w in words if w not in FILLER and w not in ("recipes", "recipe", "dish", "cooking", "steps", "step", "than",
                                                                   "fewer", "least", "most", "make", "cook", "least", "within", "can't", "cant", "cannot",
                                                                   "have", "has", "had", "like", "love", "something", "good", "nice", "ok", "okay", "thanks", "thank",
                                                                   "yes", "no", "not", "dont", "don't", "now", "then", "so", "but", "tonight", "today")]
    it.text = [ing.singular(w) if len(w) > 3 else w for w in text]
    explicit_search = bool(SEARCH_RE.search(m))
    only_constraints = not it.text and (mask or excl_terms or mx is not None or mn is not None or inc or any_of)
    if only_constraints and not explicit_search and state is not None and getattr(state, "mode", None) in ("search", "match", "stats"):
        it.kind = "refine"
        return it
    if it.text or inc or any_of or mask or excl_terms or mx is not None:
        it.kind = "search"
        it.want_full = bool(re.search(r"\b(ingredients?|instructions?|directions)\b", m))
        if state is not None and getattr(state, "mode", None) == "search" and not explicit_search and not re.search(r"\b(new|another|different|instead)\b", m):
            # short follow-up like "with garlic" / "vegetarian" keeps the active search
            if not it.text or re.match(r"^(?:and |also |with |only |but )", m):
                it.kind = "refine"
        return it
    return it


NP_STOP = set("""the all any many these those my some which what of in for are is how do does have has more most fewer fewest least average median mean number
total percentage percent among across per each every other different various unique whole entire and or than with without contain containing having a an there
that this your our their its recipe recipes dish dishes many much ingredient ingredients step steps instruction instructions line lines top list show give find tell
get see me us first last next""".split())


def _parse_stats(raw, m, it, mask, excl_terms, mx, mn, state):
    it.kind = "stats"
    # dataset-wide contains clause
    inc, any_of, _ = extract_contains(re.sub(r"\bwhat percent(?:age)?.*$", "", extract_negations(raw)[2]))
    # "among recipes containing tomato" / "recipes with X in the title"
    mt = re.search(r"(?:with|containing|has|have)\s+([a-z' -]+?)\s+in\s+(?:the\s+)?title", m)
    if mt:
        it.title_has = [w for w in split_list(mt.group(1))]
        inc = [i for i in inc if i not in [ing.normalize_name(t) for t in it.title_has]]
    mt = re.search(r"title(?:s)?\s+(?:contain(?:s|ing)?|with|has)\s+([a-z' -]+?)(?=[.?!,;]|$)", m)
    if mt:
        it.title_has = split_list(mt.group(1))
    if not any_of and not it.title_has:
        # "average ingredients in CHICKEN recipes", "how many VEGETARIAN dishes", "PASTA recipes that have garlic":
        # the word right before recipes/dishes is a filter too
        isstop = lambda w: w in NP_STOP or w.isdigit() or w in NUM_WORDS or w in ORDINALS
        for mt in re.finditer(r"((?:[a-z][a-z'-]*\s+){0,2})([a-z][a-z'-]*)\s+(?:recipes?|dishes)\b", m):
            last = mt.group(2)
            if isstop(last):
                continue
            prev = [w for w in mt.group(1).split() if not isstop(w)]
            cand = f"{prev[-1]} {last}" if prev else last
            k = classify_item(cand) if prev else classify_item(last)
            if k and k[0] == "term" and prev and ing.normalize_name(cand) in ("", cand):
                pass
            if k and k[0] == "mask":
                mask |= k[1]
            elif k and k[0] == "term" and k[1] and k[1] not in inc:
                inc = list(inc) + [k[1]]
            break
    it.include, it.any_of, it.exclude, it.mask = inc, any_of, excl_terms, mask
    it.max_steps, it.min_steps = mx, mn
    for k, v in (("allrecipes", "ar"), ("all recipes", "ar"), ("epicurious", "epi"), ("food network", "fn"), ("foodnetwork", "fn")):
        if k in m:
            it.src = v
    if re.search(r"\b(those|them|these|among the results|same (?:filter|search)|of the results|the results|so far)\b", m) or (
            not (inc or any_of or it.title_has or excl_terms or mask or mx or mn or it.src) and state is not None and getattr(state, "mode", None) in ("search", "stats")
            and re.search(r"\b(that|this|it)\b", m)):
        it.inherit = True
    if re.search(r"how many|\bcount\b|number of (?:recipes|dishes)|total number|how often", m):
        it.stat.append("count")
    if re.search(r"percent|fraction|share|proportion|what %|ratio", m):
        it.stat += ["count", "percent"]
    if re.search(r"\b(average|mean)\b", m):
        it.stat.append("mean")
    if re.search(r"\bmedian\b", m):
        it.stat.append("median")
    if re.search(r"\b(most common|commonly|common ingredients?|most frequent|top\s*\d*\s*ingredients?|most used|popular ingredients?)\b", m):
        it.stat.append("top_ingredients")
    if re.search(r"\b(fewest|least|shortest|lowest|simplest|quickest)\b.*\b(steps?|instructions?)\b|\bfewest\b", m):
        it.stat.append("rank_fewest")
    if re.search(r"\b(most|longest|highest|greatest|largest)\b.*\b(steps?|instructions?)\b", m) and "rank_fewest" not in it.stat:
        it.stat.append("rank_most")
    if re.search(r"\bsteps?\b|\binstruction", m) and ("mean" in it.stat or "median" in it.stat) and not re.search(r"\bingredient", m):
        it.stat_col = "n_steps"
    if "mean" in it.stat or "median" in it.stat:
        if re.search(r"\bsteps?\b", m) and not re.search(r"ingredient", m):
            it.stat_col = "n_steps"
        else:
            it.stat_col = "n_ing"
    mt = re.search(rf"(?:top|which|first|show|list)\s+{_NUMBER}\b", m) or re.search(rf"\b{_NUMBER}\s+(?:recipes?|ingredients?|most|fewest|least|longest|shortest)\b", m)
    if mt and to_int(mt.group(1)) is not None:
        it.n = to_int(mt.group(1))
    if re.search(r"excluding (?:salt|staples|pantry)|without (?:salt|counting salt)|ignore (?:salt|staples)|except (?:salt|staples)", m):
        it.skip_staples = True
        it.exclude = [e for e in it.exclude if e not in ("salt",)]
    if not it.stat:
        it.stat = ["count"]
    # "contain X and Y" where the parse swallowed 'dataset' etc.
    it.include = [i for i in it.include if i not in ("dataset", "recipe", "ingredient", "step")]
    return it


def _parse_match(raw, m, m_clean, it, mask, excl_terms, mx, state):
    it.kind = "match"
    seg = m_clean
    mt = re.search(r"(?:i have|i've got|i got|i've|we have|we've got|i only have|got some|have|using|use up|with|leftover|in my (?:fridge|pantry|kitchen)(?: i have)?|also have|also|add|plus)\s+(.*)", seg)
    body = mt.group(1) if mt else seg
    # cut the ingredient list at the first constraint / preference clause
    body = re.split(r"[.?!;]|\b(?:suggest|recommend|give me|show me|find|what|please|prefer|preferably|at most|no more than|with no|without|excluding|exclude|that need|needing|that use|that require|using as many|as many)\b", body)[0]
    if re.match(r"\s*(?:suggest|recommend)", seg):
        body = ""
    it.have = parse_have(body) if body.strip() else []
    # also catch "suggest five recipes using X" where the list follows "using"
    if not it.have:
        mt = re.search(r"(?:using|with|from)\s+(.*?)(?:[.?!;]|$)", seg)
        if mt:
            lst = re.split(r"\b(?:as many|prefer|at most|no more|with no|without|excluding|exclude)\b", mt.group(1))[0]
            it.have = parse_have(lst)
    it.exclude, it.mask, it.max_steps = excl_terms, mask, mx
    have_names = {h[0] for h in it.have}
    it.exclude = [e for e in it.exclude if e not in have_names] if False else it.exclude
    if re.search(r"\b(fewest|fewer|least|minimal|minimum)\s+(?:additional|extra|missing|other)\b|\bfew(?:est)? (?:more )?ingredients\b|as few (?:additional|extra)", m):
        it.prefer = "missing"
    if re.search(r"\b(fewer|fewest|least|shortest|quickest|simplest|easiest)\s+(?:instruction |cooking )?steps\b|\bshort(?:er|est)\b|\bquick(?:er|est)?\b", m):
        it.prefer = "steps"
    mt = re.search(rf"\b(?:suggest|recommend|give me|show me|top|find)\s+(?:me\s+)?{_NUMBER}\b", m)
    if mt and to_int(mt.group(1)):
        it.n = to_int(mt.group(1))
    if re.search(r"\b(?:i don'?t have|i do not have|no longer have|ran out of|out of|used up)\b", m):
        it.note = "remove_have"
        mt = re.search(r"(?:i don'?t have|i do not have|no longer have|ran out of|out of|used up)\s+(.*?)(?:[.?!;]|$)", m)
        if mt:
            it.exclude = []
            it.have = parse_have(mt.group(1))
    it.reset_filters = not (state is not None and getattr(state, "mode", None) == "match" and not re.search(r"\b(i have|i've got|i got|we have|what can i)\b", m))
    return it


def _parse_subst(raw, m, it, state, follow=False):
    it.kind = "substitute"
    mask, excl_terms, m_neg = extract_negations(raw)
    it.mask = mask
    find_ref(raw, it)
    mt = re.search(r"(?:replace|substitute|swap(?: out)?|remove|get rid of|take out|omit|leave out|drop|instead of|alternative(?:s)? (?:to|for)|without)\s+(?:the\s+|all\s+|any\s+)?([a-z' ]+?)(?=\s+(?:in|from|with|using|for|by|and|but|without|so|because|that)\b|[.,?!;]|$)", m)
    if mt:
        tgt = mt.group(1).strip()
        if tgt not in ("it", "this", "that", "them", "the recipe"):
            k = classify_item(tgt)
            if k and k[0] == "mask":
                it.mask |= k[1]
            elif k:
                it.target = k[1]
    # restrictions given only as 'without dairy'
    if it.target is None and excl_terms:
        it.target = excl_terms[0]
        excl_terms = excl_terms[1:]
    if it.target and not it.mask:
        # "remove eggs": also forbid the whole egg group when the target is a known allergen-like word
        for g, heads in ing._HEADS.items():
            if any(ing.name_matches(h, it.target) for h in heads if g in (ing.G_EGG, ing.G_DAIRY, ing.G_PEANUT, ing.G_GLUTEN, ing.G_SOY)):
                if it.target in heads or it.target.split()[-1] in heads:
                    it.mask |= g
                break
    mt = re.search(r"(?:with|using|use|try|by|to)\s+(?:some\s+|a\s+|an\s+)?([a-z' ]+?)(?:\s+instead)?(?=[.,?!;]|$|\s+(?:in|from|and|but|so)\b)", m)
    if mt:
        w = mt.group(1).strip()
        if w and w not in ("this", "that", "it", "the recipe", "dataset") and not re.match(r"(?:only|ingredients)", w):
            k = classify_item(w)
            if k and k[0] == "term" and k[1] != it.target:
                it.with_ = k[1]
    mt = re.search(r"\b(?:use|try|what about|how about|go with)\s+(?:the\s+)?([a-z' ]+?)(?:\s+instead)?[.?!]*$", m)
    if mt and follow:
        k = classify_item(mt.group(1).strip())
        if k and k[0] == "term":
            it.with_ = k[1]
    # dish words ("this pasta recipe", "banana bread recipe") for locating a recipe
    mt = re.search(r"(?:this|the|that|my|a|an)\s+([a-z' -]+?)\s+(?:recipe|dish)\b", m)
    if mt:
        words = [w for w in re.findall(r"[a-z]+", mt.group(1)) if w not in FILLER and w not in ("this", "that", "same")]
        it.text = words
    mt = re.search(r"take\s+(?:the\s+)?([a-z' -]+?)\s+(?:recipe|and)\b", m)
    if mt and not it.text:
        it.text = [w for w in re.findall(r"[a-z]+", mt.group(1)) if w not in FILLER]
    return it
