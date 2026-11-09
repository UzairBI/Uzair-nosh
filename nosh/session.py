"""Conversation state + executors for the three tasks.  All facts come from the SQLite store; all numbers
are computed in SQL/Python here, never by an LLM."""
from __future__ import annotations

import re

from . import ingredients as ing
from . import matcher, nlu, substitute as sub
from .build import SRC_NAMES
from .store import Filter, Store

HELP = """I search, analyse, match and adapt recipes from the Recipe Box dataset (AllRecipes, Epicurious, Food Network).
Examples:
  Find a penne pasta recipe.            -> then: I cannot have dairy.   /  show the ingredients of #2
  How many recipes contain both garlic and onion? What percentage of the dataset is that?
  Among recipes containing tomato, which five have the fewest instruction steps?
  What is the median number of ingredients in recipes with chicken?   /   most common ingredients in recipes with tomato
  I have eggs, onions, tomatoes and rice. Suggest five recipes, no peanuts, at most six steps, fewest extra ingredients.
     -> then: Exclude eggs too.
  Take this banana bread recipe and remove eggs.   /   Replace cream in this pasta recipe without dairy.
Other: 'more' (next results), 'start over', 'help'. Recipe references: #2, 'the first one', or a recipe id like ar:abc..."""

_FOOT = "Dietary checks read the ingredient text only and are not medical advice, so always check labels."


def _plural(n, w):
    return f"{n:,} {w}{'' if n == 1 else 's'}"


# The dataset has no category or cuisine labels: these words can only be matched against recipe TITLES (and the answer says so).
DISH_TYPES = set("""dessert desserts breakfast brunch lunch dinner supper snack snacks appetizer appetizers starter starters entree main side salad salads soup soups stew stews
cake cakes cookie cookies pie pies bread breads drink drinks cocktail cocktails smoothie smoothies sandwich sandwiches casserole casseroles sauce sauces dip dips
italian mexican indian chinese thai french greek japanese korean spanish american vietnamese turkish moroccan lebanese cajun southern vegan vegetarian""".split())


def _diet_tag(mask: int) -> str:
    """Short label for a card: 'vegan', 'vegetarian' or '<group>-free'."""
    if not mask:
        return ""
    veg = ing.G_MEAT | ing.G_FISH | ing.G_SHELLFISH
    if mask & (veg | ing.G_DAIRY | ing.G_EGG | ing.G_HONEY) == (veg | ing.G_DAIRY | ing.G_EGG | ing.G_HONEY):
        return "vegan"
    if mask & veg == veg:
        return "vegetarian"
    return ing.describe_groups(mask).split(",")[0] + "-free"


def _phrase(f: Filter) -> str:
    """Natural-language description of a filter, e.g. 'penne pasta recipes free of dairy'."""
    noun = (" ".join(f.text) + " recipes") if f.text else "recipes"
    bits = []
    if f.include:
        bits.append("containing " + " and ".join(ing.display(x) for x in f.include))
    if f.any_of:
        bits.append("containing " + " or ".join(ing.display(x) for x in f.any_of))
    if f.exclude:
        bits.append("without " + " or ".join(ing.display(x) for x in f.exclude))
    if f.excl_mask:
        bits.append("free of " + ing.describe_groups(f.excl_mask))
    if f.title_has:
        bits.append("with " + " and ".join(f.title_has) + " in the title")
    if f.max_steps is not None:
        bits.append(f"with at most {f.max_steps} steps")
    if f.min_steps is not None:
        bits.append(f"with at least {f.min_steps} steps")
    if f.src:
        bits.append("from " + SRC_NAMES.get(f.src, f.src))
    return noun + (" " + ", ".join(bits) if bits else "")


class Session:
    def __init__(self, store: Store, llm=None):
        self.store = store
        self.llm = llm
        self.mode = None              # search | match | stats | subst
        self.filt = Filter()          # active search filter
        self.results: list[int] = []  # rids shown in the last list (for "#2")
        self.shown = 0
        self.focus: int | None = None
        self.spec: matcher.MatchSpec | None = None
        self.last_stats: nlu.Intent | None = None
        self.stats_filt = Filter()
        self.sub = None
        self.log = []
        self.method_note: str = ""          # how the LAST answer was computed; used only when the user asks "how did you calculate that?"
        self.semantic_hint: str = ""          # natural-language form of the request, used by the semantic recipe search
        self.turn_qnote: str = ""             # one-line statement about quantity checking (only when the user gave amounts)
        self.turn_cards: list | None = None   # recipe cards produced by the last answer (for the web UI)
        self.turn_lead: str | None = None     # short text shown above the cards

    # ------------------------------------------------------------------ public
    def handle(self, msg: str) -> str:
        self.turn_cards = self.turn_lead = None
        it = nlu.parse(msg, self)
        if it.kind == "unknown" and self.llm:
            it = self.llm.parse(msg, self) or it
        return self.execute(it, msg)

    def execute(self, it, msg: str = "") -> str:
        """Run an already-parsed Intent (the assistant may substitute an LLM-parsed one for vague natural-language requests)."""
        self.turn_cards = self.turn_lead = None          # never leak the previous answer's cards into this one
        self.turn_qnote = ""
        self.log.append((msg, it.kind))
        self.last_kind, self.unmapped = it.kind, False
        try:
            fn = getattr(self, "_do_" + it.kind, None)
            if fn is None:
                self.unmapped = True
                return ("I could not map that to a search, statistic, ingredient match or substitution. "
                        "Try rephrasing, or type 'help' for examples." + ("" if self.llm else
                        " (An optional Gemini key would let me interpret free-form wording; see README.)"))
            return fn(it)
        except Exception as e:  # pragma: no cover
            raise

    # ------------------------------------------------------------------ helpers
    def _card(self, rid: int, meta: list, extra: str = "") -> dict:
        rec = self.store.recipe(rid)
        meta = [re.sub(r"\b1 (step|ingredient)s\b", r"1 \1", m) for m in meta]      # "1 steps" -> "1 step"
        return {"rid": rid, "id": rec["id"], "title": rec["title"], "steps": rec["n_steps"], "n_ing": rec["n_ing"],
                "ingredients": [ing.display(l["name"]) for l in rec["ingredients"] if l["name"]][:6],
                "meta": meta, "extra": extra}

    def _fmt_item(self, i, b, rec=None, show_lines=False, filt: Filter | None = None, mask: int = 0, full=False):
        rec = rec or self.store.recipe(b["rid"])
        out = [f"{i}. {rec['title']}   [id: {rec['id']}]",
               f"   {rec['n_ing']} ingredients, {rec['n_steps']} steps - {SRC_NAMES.get(rec['src'], rec['src'])}"]
        names = [ing.display(l["name"]) for l in rec["ingredients"] if l["name"]]
        if filt and filt.include:
            hits = [l["raw"] for l in rec["ingredients"] if any(ing.name_matches(t, a) for t in filt.include for a in l["alts"])]
            if hits:
                out.append("   matching ingredient lines: " + " | ".join(hits[:4]))
        if full:
            out.append("   ingredients:")
            out += [f"     - {l['raw']}" for l in rec["ingredients"]]
            out.append("   instructions:")
            out += [f"     {n}. {s}" for n, s in enumerate(rec["steps"], 1)]
        else:
            out.append("   ingredients: " + ", ".join(names[:12]) + (" ..." if len(names) > 12 else ""))
        if mask:
            warn = [l["raw"] for l in rec["ingredients"] if l["uflags"] & (mask | (ing.G_DAIRY if mask & ing.G_DAIRY else 0)) or (l["uflags"] and mask & l["uflags"])]
            ok = f"   Checked: none of the {rec['n_ing']} ingredients look like {ing.describe_groups(mask)}"
            out.append(ok + (f"; BUT possible hidden source(s) to verify on the label: {'; '.join(warn[:3])}" if warn else ""))
        return "\n".join(out)

    def _list(self, rids, f: Filter | None, header: str, total: int, mode: str, full=False, mask: int = 0) -> str:
        briefs = self.store.brief(rids)
        lines = [header]
        for i, b in enumerate(briefs, self.shown + 1):
            lines.append(self._fmt_item(i, b, filt=f, mask=mask, full=full))
        return "\n".join(lines)

    # ------------------------------------------------------------------ search
    def _do_reset(self, it):
        self.__init__(self.store, self.llm)
        return "Cleared the active search and context. What would you like to look for?"

    def _do_help(self, it):
        return HELP

    def _merge(self, f: Filter, it, replace_text=False) -> Filter:
        inc = tuple(dict.fromkeys(list(f.include) + it.include))
        exc = tuple(dict.fromkeys(list(f.exclude) + it.exclude))
        # a newly excluded ingredient must not also be required
        inc = tuple(x for x in inc if x not in exc)
        exc = tuple(x for x in exc if x not in it.include)
        return f.with_(include=inc, exclude=exc, any_of=tuple(dict.fromkeys(list(f.any_of) + it.any_of)),
                       excl_mask=f.excl_mask | it.mask,
                       max_steps=it.max_steps if it.max_steps is not None else f.max_steps,
                       min_steps=it.min_steps if it.min_steps is not None else f.min_steps)

    def _do_search(self, it):
        self.mode = "search"
        self.filt = self._merge(Filter(text=tuple(it.text), semantic=self.semantic_hint or ""), it)
        return self._run_search(it.want_full)

    def _run_search(self, full=False, header_prefix=""):
        f = self.filt
        rids, total, how = self.store.search(f, limit=5)
        if how == 'any word':
            self.filt = f = f.with_(text_any=True)
        self.results, self.shown = [], 0
        if not rids:
            return (f"{header_prefix}Sorry, I couldn't find any {_phrase(f)}. "
                    "I haven't loosened any of your conditions - try removing one, or say 'start over'.")
        self.results = list(rids)
        self.shown = len(rids)
        if f.text and "all words" not in how and how != "filters only":
            head = f"{header_prefix}I couldn't find an exact match for {_phrase(f)}, but these are the closest recipes I have:"
        else:
            head = f"{header_prefix}Here's what I found for {_phrase(f)}:"
        self.method_note = ("Recipes are ranked by text relevance (BM25 over title and ingredients, title weighted 8x) combined with semantic similarity of the request; "
                            f"the conditions ({f.describe()}) are applied exactly in the database, never approximately. Exact duplicate records are hidden.")
        briefs = self.store.brief(rids)
        lines = [head]
        for i, b in enumerate(briefs, 1):
            lines.append(self._fmt_item(i, b, filt=f, mask=f.excl_mask, full=full))
        if f.excl_mask:
            lines.append(_FOOT.format(n=13))
        tag = _diet_tag(f.excl_mask)
        self.turn_lead = head + (("\n" + _FOOT.format(n=13)) if f.excl_mask else "")
        self.turn_cards = [self._card(b["rid"], [f"{b['n_steps']} steps", tag or f"{b['n_ing']} ingredients"]) for b in briefs]
        return "\n".join(lines)

    def _do_more(self, it):
        if self.mode == "match" and self.spec:
            return self._run_match(limit=5 + self.shown, skip=self.shown)
        if self.mode != "search":
            return "There's nothing to continue yet - try searching for a recipe first."
        rids, total, how = self.store.search(self.filt, limit=5, offset=self.shown)
        if not rids:
            return "That's everything I have for this search."
        start = self.shown
        self.results += list(rids)
        self.shown += len(rids)
        lines = ["Here are some more:"]
        briefs = self.store.brief(rids)
        for i, b in enumerate(briefs, start + 1):
            lines.append(self._fmt_item(i, b, filt=self.filt, mask=self.filt.excl_mask))
        self.turn_lead = "Here are some more:"
        self.turn_cards = [self._card(b["rid"], [f"{b['n_steps']} steps", f"{b['n_ing']} ingredients"]) for b in briefs]
        return "\n".join(lines)

    # ------------------------------------------------------------------ refinement of whatever is active
    def _do_refine(self, it):
        if self.mode == "search":
            self.filt = self._merge(self.filt, it)
            return self._run_search(header_prefix="Got it, I've narrowed down the same search.\n")
        if self.mode == "match" and self.spec:
            return self._refine_match(it)
        if self.mode == "stats" and self.last_stats:
            li = self.last_stats
            base = self._merge(self.stats_filt, it)
            self.stats_filt = base
            return self._exec_stats(li, base, prefix="Here it is again with your extra condition.\n")
        return "There's nothing to refine yet - ask me to find a recipe first."

    # ------------------------------------------------------------------ unsupported attributes
    def _do_unsupported(self, it):
        rec = self._resolve(it) if (it.ref_id or it.ref_index or it.ref_this) else None
        what = "cooking/prep time" if re.search(r"how long|time|minutes|hours", it.note) else it.note
        txt = (f"I can't answer that: the dataset has no '{what}' data. Each record only contains a title, an ingredient list (free text with "
               "quantities), the instructions and a picture link - no nutrition values, cooking/prep times, servings, ratings or prices. "
               "I won't estimate them, because that would be a guess presented as a fact.")
        if rec:
            txt += (f"\nWhat I can show for '{rec['title']}' [id: {rec['id']}]: {rec['n_ing']} ingredient lines and {rec['n_steps']} instruction steps "
                    "(ask 'show the ingredients of it'). Number of steps is only a rough proxy for effort, not a time.")
        else:
            txt += "\nWhat I can do instead: rank by number of instruction steps or ingredient lines (e.g. 'which 5 tomato recipes have the fewest steps')."
        return txt

    # ------------------------------------------------------------------ viewing
    def _resolve(self, it):
        """recipe dict for a reference in the intent (id / list index / 'this')."""
        if it.ref_id:
            rid = self.store.find_id(it.ref_id)
            return self.store.recipe(rid) if rid else None
        if it.ref_index:
            idx = it.ref_index
            if idx == -1:
                idx = len(self.results)
            if 1 <= idx <= len(self.results):
                return self.store.recipe(self.results[idx - 1])
            return None
        if it.ref_this or True:
            if self.focus:
                return self.store.recipe(self.focus)
            if self.results:
                return self.store.recipe(self.results[0])
        return None

    def _do_view(self, it):
        rec = self._resolve(it)
        if not rec:
            return "Which recipe do you mean? Tell me a number from the list (like '#2') and I'll pull it up."
        self.focus = rec["rid"]
        out = [f"Here's {rec['title']}   [id: {rec['id']}]   ({SRC_NAMES.get(rec['src'])}, {rec['n_ing']} ingredients, {rec['n_steps']} steps)"]
        if it.view in ("ingredients", "full"):
            out.append("Ingredients:")
            out += [f"  - {l['raw']}" for l in rec["ingredients"]]
        if it.view in ("instructions", "full"):
            out.append("Instructions:")
            out += [f"  {n}. {s}" for n, s in enumerate(rec["steps"], 1)]
        return "\n".join(out)

    # ------------------------------------------------------------------ statistics
    def _stats_filter(self, it) -> Filter:
        base = Filter()
        if it.inherit:
            base = self.filt if self.mode == "search" else (self.stats_filt if self.mode == "stats" else Filter())
        f = self._merge(base, it).with_(title_has=tuple(it.title_has) or base.title_has, src=it.src or base.src)
        # "dessert recipes", "italian recipes": a word that is not an ingredient anywhere is a dish type -> filter on the title instead
        real = [t for t in f.include if self.store.has_ingredient(t) and ing.display(t).split()[-1] not in DISH_TYPES]
        moved = [ing.display(t).split()[-1] for t in f.include if t not in real]
        if moved:
            f = f.with_(include=tuple(real), title_has=tuple(dict.fromkeys(list(f.title_has) + moved)))
        return f

    def _do_stats(self, it):
        f = self._stats_filter(it)
        self.mode = "stats"
        self.stats_filt = f
        self.last_stats = it
        return self._exec_stats(it, f)

    def _exec_stats(self, it, f: Filter, prefix: str = "") -> str:
        st = self.store
        D = st.total
        n = st.count(f)
        self._stat_cards = None
        self._tie_line = ""
        out = [prefix + ("Here's what I found for " + _phrase(f) + ":" if f.describe() != "no filters (whole dataset)"
                         else "Here's what I found across the whole dataset:")]
        notes = []
        for s in dict.fromkeys(it.stat):
            if s == "count" and "percent" not in it.stat:
                out.append(f"{n:,} recipes match.")
                notes.append("counted with a database COUNT over every recipe that satisfies the conditions")
            elif s == "percent":
                out.append(f"{n:,} recipes match - that's {100 * n / D:.2f}% of the {D:,} recipes in the dataset ({n:,} / {D:,}).")
                notes.append("percentage = matching recipes / all complete recipes")
            elif s in ("mean", "median"):
                col = it.stat_col
                sm = st.summarize(f, col)
                what = "ingredients" if col == "n_ing" else "instruction steps"
                if not sm["n"]:
                    out.append("No recipes match, so there's nothing to average.")
                    continue
                out.append(f"On average a recipe has {sm['mean']:.2f} {what} (median {sm['median']:g}, range {sm['min']}-{sm['max']}), across {sm['n']:,} recipes.")
                notes.append("mean and median of the per-recipe count over the whole filtered set")
        if "top_ingredients" in it.stat:
            k = it.n or 10
            skip = tuple(ing.normalize_name(x) for x in f.include) if f.include else ()
            top = st.top_ingredients(f, k, skip_staples=it.skip_staples, skip=skip)
            out.append(f"The {k} most common ingredients" + (" (leaving out the ones you filtered on)" if skip else "") + ":")
            for name, c in top:
                out.append(f"  {ing.display(name):<22} in {c:,} recipes ({100 * c / max(n, 1):.1f}%)")
            notes.append("ingredient names are normalized, so 'tomato paste', 'peanut butter' and 'green onion' count as their own ingredients; "
                         "for 'X or Y' lines only X is counted")
        for key, fewest in (("rank_fewest", True), ("rank_most", False)):
            if key in it.stat:
                k = it.n or 5
                rows, ties = st.rank_steps(f, k, fewest)
                if not rows:
                    out.append("No recipes match, so there's nothing to rank.")
                    continue
                out.append(f"Here are the {len(rows)} recipes with the {'fewest' if fewest else 'most'} steps:")
                for i, r in enumerate(rows, 1):
                    out.append(f"  {i}. {r['title']}   [id: {r['id']}]  - {r['n_steps']} step{'' if r['n_steps'] == 1 else 's'}, {r['n_ing']} ingredients")
                if ties > 1:
                    out.append(f"  {ties:,} recipes share the last-listed step count of {rows[-1]['n_steps']}, so these are the first {k} after tie-breaking.")
                notes.append("a step is one non-empty line of the instructions (or one sentence if the instructions are a single block); "
                             f"ties are broken by {'fewer' if fewest else 'more'} ingredients, then recipe id")
                self.results = [r["rid"] for r in rows]
                self.shown = len(rows)
                self._stat_cards = [self._card(r["rid"], [f"{r['n_steps']} steps", f"{r['n_ing']} ingredients"]) for r in rows]
                last = rows[-1]["n_steps"]
                self._tie_line = (f"{ties:,} recipes tie at {last} step{'' if last == 1 else 's'}, so these are the first {k} "
                                  f"ordered by {'fewest' if fewest else 'most'} ingredients.") if ties > 1 else ""
        if n == 0:
            out.append("No recipe satisfies these conditions, and I haven't loosened any of them.")
        dropped = sum(int(v) for v in eval(st.meta["dropped"]).values())
        out.append(f"How I worked this out: {n:,} recipes analysed out of {D:,} complete ones ({dropped:,} incomplete records were dropped, "
                   f"{int(st.meta['exact_duplicates']):,} exact duplicates are kept); " + "; ".join(notes) + ".")
        self.method_note = (f"Filters: {f.describe()}. " + out[-1].replace("How I worked this out: ", "")) if n else ""
        if self._stat_cards:
            self.turn_cards = self._stat_cards
            self.turn_lead = out[0]            # tie and method details stay in "Show full details"
        return "\n".join(out)

    # ------------------------------------------------------------------ ingredient matching
    def _do_match(self, it):
        if not it.have and not (self.spec and not it.reset_filters):
            return ("Which ingredients do you have? e.g. 'I have eggs, onions, tomatoes and rice' "
                    "(quantities optional: '2 eggs, 1 cup rice').")
        old = self.spec
        keep = old if (self.mode == "match" and old and not it.reset_filters) else None
        have = list(it.have)
        if keep:
            names = {h.name for h in keep.have}
            if it.note == "remove_have":
                gone = {h[0] for h in it.have}
                have = [h for h in keep.have if h.name not in gone]
                spec_have = tuple(have)
            else:
                spec_have = tuple(keep.have) + tuple(matcher.Have(n, q, u, lb) for n, q, u, lb in have if n not in names)
        else:
            spec_have = tuple(matcher.Have(n, q, u, lb) for n, q, u, lb in have)
        self.mode = "match"
        excl = tuple(dict.fromkeys((keep.exclude if keep else ()) + tuple(it.exclude)))
        self.spec = matcher.MatchSpec(
            have=spec_have, exclude=excl, excl_mask=(keep.excl_mask if keep else 0) | it.mask,
            max_steps=it.max_steps if it.max_steps is not None else (keep.max_steps if keep else None),
            prefer=it.prefer or (keep.prefer if keep else "balanced"))
        self._match_n = it.n or 5
        return self._run_match(limit=self._match_n)

    def _refine_match(self, it):
        s = self.spec
        exc = tuple(dict.fromkeys(s.exclude + tuple(it.exclude)))
        newmask = s.excl_mask | it.mask
        gone = lambda h: h.name in exc or (ing.name_groups(h.name)[0] & newmask)
        have = tuple(h for h in s.have if not gone(h))
        dropped = [h.label or h.name for h in s.have if gone(h)]
        add = [matcher.Have(n, q, u, lb) for n, q, u, lb in it.have if n not in {h.name for h in have}]
        self.spec = matcher.MatchSpec(have=have + tuple(add), exclude=exc, excl_mask=s.excl_mask | it.mask,
                                      max_steps=it.max_steps if it.max_steps is not None else s.max_steps,
                                      prefer=it.prefer or s.prefer)
        pre = "Okay, I've updated the list."
        if dropped:
            pre += f" '{', '.join(dropped)}' is now off the menu, so I'm no longer counting it as something you have."
        return self._run_match(limit=getattr(self, "_match_n", 5), prefix=pre + "\n")

    def _run_match(self, limit=5, skip=0, prefix=""):
        spec = self.spec
        if not spec.have:
            return "I have no ingredients left to work with - tell me what you've got."
        res = matcher.match(self.store, spec, limit=limit)
        rs = res["results"][skip:]
        names = ", ".join(h.label or ing.display(h.name) for h in spec.have)
        cons = spec.describe_constraints()
        head = [prefix + f"Here's what you can make with {names}" + (f" ({cons})" if cons != "none" else "") + ":"]
        how = {"balanced": "then by fewest extra ingredients and steps",
               "missing": "then by fewest extra ingredients",
               "steps": "then by fewest steps"}[spec.prefer]
        note = (f"How I ranked them: by how many of your ingredients they use, {how}. Salt, pepper and water are assumed on hand, "
                f"and 'tomato' won't match tomato paste or sauce. {res['eligible']:,} recipes passed your conditions.")
        qnote = (" Amounts you gave were compared with the recipe amounts only where the units are comparable (volume, weight or the same unit); otherwise a recipe is matched "
                 "by ingredient name only and quantity sufficiency is not verified." if any(h.qty for h in spec.have) else "")
        self.method_note = note + qnote + f" Hard constraints applied exactly: {spec.describe_constraints()}."
        self.turn_qnote = qnote
        if not res["results"]:
            head.append("No match - no recipe satisfies all of your conditions, and I haven't loosened any of them.")
            if res["alternatives"]:
                head.append("The closest ones are below, but each breaks one of your conditions, so they aren't real matches:")
                for a in res["alternatives"]:
                    head.append(self._fmt_match(a, None, violated=a["violations"]))
            return "\n".join(head)
        self.results = [r["rid"] for r in res["results"]]
        self.shown = len(res["results"])
        body = [self._fmt_match(r, i) for i, r in enumerate(rs, skip + 1)]
        self.turn_lead = head[0]           # the ranking explanation stays in "Show full details"
        self.turn_cards = [self._card(r["rid"], [f"{r['n_steps']} steps", f"uses {r['matched_terms']} of {len(spec.have)}"],
                                      ("Still need: " + "; ".join(r["missing"][:3]) + (" ..." if len(r["missing"]) > 3 else "")) if r["missing"] else "Nothing else needed")
                           for r in rs]
        return "\n".join(head + body + [note])

    def _fmt_match(self, r, i, violated=None):
        title = f"{i}. " if i else "- "
        n_have = len(self.spec.have)
        out = [f"{title}{r['title']}   [id: {r['id']}]   ({r['n_steps']} steps)",
               f"   Uses {r['matched_terms']} of your {n_have} ingredients and needs {r['missing_n']} more."]
        if violated:
            out.append("   Breaks your conditions: " + "; ".join(violated))
        for m in r["matched"]:
            out.append(f"   you have {m['have']}: {m['line']}  ({m['qty_check']})")
        if r["unused"]:
            out.append("   not used: " + ", ".join(r["unused"]))
        if r["missing"]:
            out.append("   still need: " + "; ".join(r["missing"][:8]) + (" ..." if len(r["missing"]) > 8 else ""))
        else:
            out.append("   still need: nothing else")
        if self.spec.excl_mask and r.get("uflags", 0) & self.spec.excl_mask:
            out.append("   heads-up: a vague ingredient line might hide something you avoid - check the label")
        return "\n".join(out)

    # ------------------------------------------------------------------ substitution
    def _title_matches(self, rid, words) -> bool:
        """A recipe from the previous results is only 'this <dish> recipe' if its title contains the dish words."""
        b = self.store.brief([rid])
        title = b[0]["title"].lower() if b else ""
        return all(re.search(r"\b" + re.escape(w) + r"(?:s|es)?\b", title) for w in words if len(w) > 2)

    def _has_target(self, rid, target):
        if not target:
            return True
        rec = self.store.recipe(rid)
        return any(ing.name_matches(target, a) for l in rec["ingredients"] for a in l["alts"])

    def _do_substitute(self, it):
        st = self.store
        prev = self.sub if self.mode == "subst" else None
        # --- which recipe? -----------------------------------------------------------------
        rec = None
        note = ""
        if it.ref_id or it.ref_index:
            rec = self._resolve(it)
        elif prev and not it.text:
            rec = st.recipe(prev["rid"])
        elif it.text and it.ref_this and self.results and any(self._has_target(r, it.target) and self._title_matches(r, it.text) for r in self.results):
            rid = next(r for r in self.results if self._has_target(r, it.target) and self._title_matches(r, it.text))
            rec = st.recipe(rid)
        elif it.text:
            f = Filter(text=tuple(it.text), include=((it.target,) if it.target else ()))
            rids, total, how = st.search(f, limit=1)
            if not rids and it.target:
                rids, total, how = st.search(Filter(text=tuple(it.text)), limit=1)
            if rids:
                rec = st.recipe(rids[0])
                note = (f"Using the best match for '{' '.join(it.text)}'"
                        + (f" that contains {ing.display(it.target)}" if it.target else "") + f": {rec['title']} [id: {rec['id']}]. "
                        "(Say a recipe id or #n to use a different one.)\n")
        else:
            rec = self._resolve(it)
        if not rec:
            return "Which recipe should I adapt? Search for one first, or give a recipe id / '#2'."
        self.focus = rec["rid"]
        target = it.target or (prev["target"] if prev else None)
        mask = it.mask | (prev["mask"] if prev and not it.text else 0)
        self.mode = "subst"
        # --- target lines ------------------------------------------------------------------
        if target is None and mask:
            bad = [l for l in rec["ingredients"] if l["flags"] & mask]
            if not bad:
                return f"{note}No ingredient line of '{rec['title']}' matched {ing.describe_groups(mask)} terms, so there is nothing to change for that restriction (checked on ingredient text only)."
            target = ing.normalize_name(bad[0]["alts"][0]) if bad[0]["alts"] else None
            for g in (ing.G_DAIRY, ing.G_EGG, ing.G_GLUTEN):  # use the group head rather than the full phrase
                pass
        if target is None:
            return f"{note}What should be replaced or removed (e.g. 'remove eggs', 'replace cream')?"
        lines = [l for l in rec["ingredients"] if any(ing.name_matches(target, a) for a in l["alts"])]
        if not lines:
            return (f"{note}'{rec['title']}' has no ingredient matching '{ing.display(target)}', so there is nothing to substitute. Its ingredients:\n"
                    + "\n".join(f"  - {l['raw']}" for l in rec["ingredients"]))
        self.sub = {"rid": rec["rid"], "target": target, "mask": mask}
        # --- candidates --------------------------------------------------------------------
        forbidden = set(it.exclude)
        key = sub.table_key(target)
        out = [note + f"Here's how I'd adapt {rec['title']}   [id: {rec['id']}]",
               f"Swapping out: {', '.join(repr(l['raw']) for l in lines)}"
               + (f"   (must be free of {ing.describe_groups(mask)})" if mask else "")]
        plan = self._plan(rec, target, mask, forbidden, it.with_)
        if plan.get("refuse"):
            out.append(plan["refuse"])
            return "\n".join(out)
        evs, ctx, dropped = plan["evs"], plan["ctx"], plan["dropped"]
        out.append(f"What {ing.display(target)} does in a dish like this (general cooking knowledge, not from the dataset): "
                   f"{sub.TABLE[key]['role'] if key else 'unknown'}.")
        if dropped:
            out.append("Ruled out straight away: " + "; ".join(dropped))
        out.append(f"I compared it with the {ctx['neighbours']} most similar recipes in the dataset: {ctx['n_with']} still use {ing.display(target)} "
                   f"and {ctx['n_without']} don't, plus {ctx['labelled']} recipes labelled eggless / vegan / dairy-free for the same kind of dish.")
        out.append("Options, best-supported first:")
        for e in evs:
            out.append(self._fmt_ev(e, target))
        chosen = evs[0]
        if it.with_:
            chosen = evs[0]
        if chosen.strength == "none":
            out.append("\nLOCAL EVIDENCE IS INADEQUATE for every candidate. The adaptation below is a prediction from general cooking knowledge only, "
                       "not supported by the dataset (no web search was used). Treat it as untested.")
        elif chosen.strength != "strong":
            out.append("\nEvidence is WEAK or INDIRECT (see notes), so treat the choice below as a reasoned suggestion, not a verified fix.")
        # --- adaptation ----------------------------------------------------------------------
        new_lines, new_steps, changes, modified, mentioned = sub.adapt(rec, target, chosen.option)
        applied = [(target, chosen, changes)]
        cur = self._rebuild(rec, new_lines, new_steps)
        # other lines that still break the restriction: swap them too when we have vetted options + evidence
        extra_notes = []
        if mask and not it.with_:
            done = {key}
            for _ in range(3):
                bad = [l for l in cur["ingredients"] if l["flags"] & mask and not l["raw"].startswith("[")]
                nxt = None
                for l in bad:
                    t2 = l["alts"][0] if l["alts"] else None
                    k2 = sub.table_key(t2) if t2 else None
                    if k2 and k2 not in done:
                        nxt = (t2, k2)
                        break
                if not nxt:
                    break
                t2, k2 = nxt
                done.add(k2)
                p2 = self._plan(cur, t2, mask, forbidden, None)
                if p2.get("refuse") or p2["evs"][0].strength == "none":
                    extra_notes.append(f"Could not find a dataset-supported replacement for '{ing.display(t2)}' (it also breaks the restriction).")
                    continue
                c2 = p2["evs"][0]
                nl, ns, ch, md, _ = sub.adapt(cur, t2, c2.option)
                applied.append((t2, c2, ch))
                modified = sorted(set(modified) | set(md))
                cur = self._rebuild(cur, nl, ns)
                new_lines, new_steps = nl, ns
        out.append(f"\nMy pick for {ing.display(target)}: {chosen.option.key}   (say 'use <ingredient> instead' to try another one)")
        for t2, c2, ch in applied[1:]:
            tot = len(c2.neighbour_support) + len(c2.labelled_support)
            ids = ", ".join(f"{x[1]} [id: {x[0]}]" for x in (c2.neighbour_support + c2.labelled_support)[:2])
            out.append(f"Also replaced {ing.display(t2)} -> {c2.option.key} (evidence {c2.strength.upper()}, {tot} supporting recipe(s): {ids})"
                       + (f" ! {'; '.join(c2.notes)}" if c2.notes else ""))
        out.append("New ingredient list:")
        new_set = {c[1] for _, _, ch in applied for c in ch}
        for l in new_lines:
            out.append(f"  - {l}" + ("   <- changed" if l in new_set else ""))
        for t2, c2, ch in applied:
            for old, new in ch:
                out.append(f"  (was: {old}  ->  {new})")
        out.append("New instructions:" + (f"  (changed steps: {', '.join(map(str, modified))}; '[Added]' marks a new preparation step)" if modified else ""))
        for n, s in enumerate(new_steps, 1):
            out.append(f"  {n}. {s}")
        if not mentioned:
            out.append(f"  (The original steps never mention {ing.display(target)} by name, so only a preparation step was added; check where it is mixed in.)")
        out.append("What to expect (PREDICTIONS from general cooking knowledge, not verified by the dataset):")
        for t2, c2, ch in applied:
            out.append(f"  - {ing.display(t2)} -> {c2.option.key}: {c2.option.effect}.")
        out.append("Automatic rewording only swaps ingredient names in the original steps - re-read them for verbs that no longer fit (e.g. 'melt', 'beat').")
        out += extra_notes
        # --- remaining violations ------------------------------------------------------------
        if mask:
            parsed = [ing.line_groups(ing.parse_line(l)[2]) for l in new_lines]
            rest = [l for l, (s_, m_) in zip(new_lines, parsed) if s_ & mask]
            maybe = [l for l, (s_, m_) in zip(new_lines, parsed) if m_ & mask]
            if rest:
                out.append(f"WARNING - still contains {ing.describe_groups(mask)}: " + "; ".join(rest)
                           + ". I could not replace these with dataset-supported options; ask me to replace them one by one.")
            elif maybe:
                out.append("Possible hidden source(s) in vague lines (verify on labels): " + "; ".join(maybe))
            else:
                out.append(f"After the changes, no remaining ingredient line matches {ing.describe_groups(mask)} terms.")
            out.append(_FOOT.format(n=13) + " Recipe text alone cannot establish that a substitution is safe for an allergy.")
        out.append("Sources: only recipes from this dataset (cited above). I didn't use any external/web source.")
        return "\n".join(out)

    # ---- helpers for substitution ----------------------------------------------------------
    def _plan(self, rec, target, mask, forbidden, with_):
        st = self.store
        key = sub.table_key(target)
        curated = [o for o in (sub.TABLE[key]["options"] if key else []) if with_ and (with_ in o.probes or with_.split()[-1] in o.key.lower().split())]
        if with_ and curated:
            options = curated[:1]
        elif with_:
            options = [sub.Option(ing.display(with_), ing.display(with_), (with_,), sub._same(ing.display(with_)), ing.display(with_),
                                  effect="no curated expectation for this user-chosen substitute; effects are unknown")]
        elif key:
            options = list(sub.TABLE[key]["options"])
        else:
            return {"refuse": f"I have no vetted substitution knowledge for '{ing.display(target)}', and the dataset alone does not tell me what plays its role, "
                              "so I cannot support a reliable adaptation. I did not search the web. You can propose a replacement ('use X instead') and I will check "
                              "whether similar recipes in the dataset support it."}
        ok_opts, dropped = [], []
        for o in options:
            probe_names = list(o.probes) + [ing.normalize_name(o.label)]
            g = 0
            for p in probe_names:
                g |= ing.name_groups(p)[0]
            if o.key == "flax egg":
                g = 0
            if g & mask or any(ing.name_matches(f, p) for f in forbidden for p in probe_names):
                dropped.append(f"{o.key} (violates {ing.describe_groups(g & mask) or 'an exclusion'})")
                continue
            if sub._occurrences(st, o.probes[0]) == 0:
                dropped.append(f"{o.key} (never appears in the dataset)")
                continue
            ok_opts.append(o)
        if not ok_opts:
            return {"refuse": "No candidate satisfies your restrictions and exists in the dataset, so I cannot propose a supported adaptation.",
                    "dropped": dropped}
        evs, ctx = sub.gather_evidence(st, rec, target, ok_opts, mask)
        return {"evs": evs, "ctx": ctx, "dropped": dropped}

    @staticmethod
    def _fmt_ev(e, target):
        out = [f"  * {e.option.key}: evidence = {e.strength.upper()}  (supported by {len(e.neighbour_support)} similar recipe(s) without {ing.display(target)} "
               f"and {len(e.labelled_support)} free-from-labelled recipe(s); appears in {e.rate_with:.0%} of similar recipes that keep {ing.display(target)}; "
               f"{e.dataset_occurrences:,} recipes in the dataset use it)"]
        for rid_s, title, raw in (e.neighbour_support + e.labelled_support)[:3]:
            out.append(f"      - {title} [id: {rid_s}]  ->  \"{raw}\"")
        for nt in e.notes:
            out.append(f"      ! {nt}")
        return "\n".join(out)

    @staticmethod
    def _rebuild(rec, new_lines, new_steps):
        r2 = dict(rec)
        ls = []
        for raw in new_lines:
            q, u, alts = ing.parse_line(raw)
            s_, m_ = ing.line_groups(alts) if alts else (0, 0)
            ls.append(dict(raw=raw, qty=q, unit=u, name=alts[0] if alts else "", alts=list(alts), flags=s_, uflags=m_, staple=0))
        r2["ingredients"], r2["steps"] = ls, new_steps
        return r2
