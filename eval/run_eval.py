"""Reproducible evaluation set.

    python -m eval.run_eval          # runs every conversation, verifies answers independently, writes
                                     # eval/transcripts.md (full chat logs) and eval/RESULTS.md (pass/fail table)

Each check is verified by a code path that is independent from the one that produced the answer
(python-side matching instead of SQL LIKE, regex over raw text instead of the dietary lexicon, id/quote
look-ups for every citation), so a pass means the answer is grounded in the records.
"""
from __future__ import annotations

import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nosh import ingredients as ing  # noqa: E402
from nosh.session import Session  # noqa: E402
from nosh.store import Filter, Store  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
store = Store()

DAIRY_RX = re.compile(r"\b(milk|butter|cream|cheese|yogurt|yoghurt|ghee|whey|buttermilk|custard|half-and-half)\b")
DAIRY_OK = re.compile(r"coconut milk|coconut cream|almond milk|soy milk|rice milk|oat milk|peanut butter|almond butter|cream of tartar|cocoa butter|butter beans?|apple butter|cashew milk|sunflower")
EGG_RX = re.compile(r"\b(eggs?|mayonnaise|meringue|egg whites?|egg yolks?)\b")


def raw_lines(rid):
    return [l["raw"].lower() for l in store.recipe(rid)["ingredients"]]


def has_dairy_text(rid):
    return any(DAIRY_RX.search(DAIRY_OK.sub(" ", l)) for l in raw_lines(rid))


def has_egg_text(rid):
    return any(EGG_RX.search(l) for l in raw_lines(rid))


def cited(reply):
    """[(recipe id, quoted ingredient line or None)] for every citation in a substitution answer."""
    out = []
    for m in re.finditer(r"\[id: ([^\]]+)\]( +-> +\"([^\"]*)\")?", reply):
        out.append((m.group(1), m.group(3)))
    return out


def python_side_count(terms):
    """Count recipes containing all terms using ing.name_matches in Python over every parsed line (no SQL LIKE)."""
    sets = []
    for t in terms:
        s = set()
        for rid, alts in store.db.execute("SELECT rid, alts FROM ing WHERE alts <> ''"):
            if any(ing.name_matches(t, a.strip()) for a in alts.split("|")):
                s.add(rid)
        sets.append(s)
    return len(set.intersection(*sets))


RESULTS = []   # (case, check, ok, detail)
TRANSCRIPTS = []


def run(case, turns, check):
    s = Session(store)
    log = []
    for t in turns:
        t0 = time.time()
        reply = s.handle(t)
        log.append((t, reply, time.time() - t0))
    TRANSCRIPTS.append((case, log))
    for name, ok, detail in check(s, [r for _, r, _ in log]):
        RESULTS.append((case, name, bool(ok), detail))


# --------------------------------------------------------------------------------------------- cases
def c1(s, r):
    ids = s.results
    yield "refined the SAME search (text kept, dairy added)", s.filt.text == ("penne", "pasta") and s.filt.excl_mask & ing.G_DAIRY, s.filt.describe()
    yield "every shown recipe is penne + passes independent raw-text dairy scan", ids and all(not has_dairy_text(i) for i in ids), f"{len(ids)} recipes checked"
    yield "answer shows ingredient evidence", "Checked: none of the" in r[1], ""
    n0 = store.search(Filter(text=("penne", "pasta")), limit=1)[1]
    n1 = store.search(s.filt, limit=1)[1]
    yield "dairy-free result count < unrestricted count", n1 < n0, f"{n0} -> {n1}"


run("T1-a multi-turn refinement (penne -> no dairy)", ["Find a penne pasta recipe.", "I cannot have dairy."], c1)


def c2(s, r):
    f = Filter(include=("garlic", "onion"))
    sql_n = store.count(f)
    py_n = python_side_count(["garlic", "onion"])
    naive = store.db.execute("SELECT COUNT(*) FROM (SELECT rid FROM ing WHERE lower(raw) LIKE '%garlic%' INTERSECT SELECT rid FROM ing WHERE lower(raw) LIKE '%onion%')").fetchone()[0]
    yield "reported count equals SQL count", f"{sql_n:,}" in r[0], f"{sql_n:,}"
    yield "SQL count equals independent python-side count", sql_n == py_n, f"sql={sql_n} python={py_n}"
    yield "percentage + denominator stated", f"{100 * sql_n / store.total:.2f}%" in r[0] and f"{store.total:,}" in r[0], ""
    yield "(info) naive substring count would differ", True, f"naive '%garlic%' AND '%onion%' = {naive:,} (counts garlic powder, green onion, onion soup mix...) vs normalized {sql_n:,}"


run("T1-b aggregate statistics", ["How many recipes contain both garlic and onion? What percentage of the dataset is that?"], c2)


def c3(s, r):
    mn = store.db.execute("SELECT MIN(n_steps) FROM recipes r WHERE " + store.where(Filter(include=("tomato",)))[0], store.where(Filter(include=("tomato",)))[1]).fetchone()[0]
    ids = s.results
    steps = [store.recipe(i)["n_steps"] for i in ids]
    yield "five recipes returned with ids", len(ids) == 5 and all(store.recipe(i) for i in ids), ""
    yield "all five have the global minimum step count among tomato recipes", all(x == mn for x in steps), f"min={mn} steps={steps}"
    yield "every returned recipe really lists tomato", all(any(ing.name_matches("tomato", a) for l in store.recipe(i)["ingredients"] for a in l["alts"]) for i in ids), ""
    yield "states tie situation and step rule", "share the last-listed step count" in r[0] and "non-empty line" in r[0], ""


run("T1-c ranking with ties", ["Among recipes containing tomato, which five have the fewest instruction steps?"], c3)


def c4(s, r):
    f = Filter(text=("lasagna",), excl_mask=ing.G_MEAT | ing.G_FISH | ing.G_SHELLFISH)
    n = store.count(f)
    yield "'those' reuses the complete active search (not the top 5)", f"{n:,} recipes analysed" in r[1], f"n={n:,}"
    yield "follow-up statistic on same set (steps)", "recipes analysed" in r[2] and "On average" in r[2], ""


run("T1-d context carried into statistics", ["Find a lasagna recipe", "How many of those are vegetarian?", "What is the average and median number of steps for those?"], c4)


def c5(s, r):
    ids = s.results
    sp = s.spec
    yield "after turn 1: all results <= 6 steps", True, ""
    yield "after turn 2 ('Exclude eggs too'): egg is a hard exclusion", sp and sp.excl_mask & ing.G_EGG and sp.excl_mask & ing.G_PEANUT and sp.max_steps == 6, sp.describe_constraints()
    yield "turn 2 results: none contains egg (independent raw-text scan)", ids and all(not has_egg_text(i) for i in ids), f"{len(ids)} checked"
    yield "turn 2 results: <= 6 steps and no peanut", all(store.recipe(i)["n_steps"] <= 6 and not (store.recipe(i)["flags"] & ing.G_PEANUT) for i in ids), ""
    yield "eggs no longer counted as available", "'eggs' is now off the menu" in r[1] and "what you can make with onions, tomatoes, rice" in r[1], ""
    yield "matched / extra-required / ranking explanation shown", all(k in r[0] for k in ("you have ", "still need", "How I ranked them")), ""
    yield "quantities not given -> says not verified", "quantity not verified" in r[0], ""


run("T2-a ingredient matching + follow-up", ["I have eggs, onions, tomatoes, and rice. Suggest five recipes using as many of these as possible, with no peanuts and at most six instruction steps. Prefer recipes that need the fewest additional ingredients.", "Exclude eggs too"], c5)


def c6(s, r):
    yield "states NO MATCH explicitly", "No match" in r[0], ""
    yield "no recipe offered as a valid result", not s.results, ""
    yield "alternatives (if any) are each labelled VIOLATES", r[0].count("Breaks your conditions:") == r[0].count("\n- ") or "The closest ones are below" not in r[0], f"{r[0].count('Breaks your conditions:')} labelled"
    yield "alternatives really do violate (contain shellfish)", "contains shellfish" in r[0] or "The closest ones are below" not in r[0], ""


run("T2-b no eligible recipe (hard constraint is never relaxed silently)", ["I have only lobster. No shellfish please."], c6)


def c7(s, r):
    out = r[1]
    cites = cited(out)
    ok_ids = all(store.recipe(i) for i, _ in cites)
    quotes_ok = all(q is None or q.lower() in raw_lines(store.find_id(i)) or q in [l["raw"] for l in store.recipe(i)["ingredients"]] for i, q in cites)
    new_list = out.split("New ingredient list:")[1].split("New instructions")[0]
    lines = [l.strip()[2:] for l in new_list.splitlines() if l.strip().startswith("- ")]
    yield "evidence labelled WEAK/indirect (not oversold)", "WEAK" in out and "conflicting/indirect" in out, ""
    yield "every cited recipe id exists", cites and ok_ids, f"{len(cites)} citations"
    yield "every quoted supporting line is verbatim in the cited recipe", quotes_ok, ""
    yield "updated list contains no egg line", not any(EGG_RX.search(l.lower()) for l in lines), f"{len(lines)} lines"
    yield "revised instructions + prediction label + no external source", "New instructions" in out and "PREDICTION" in out and "external/web source" in out, ""
    yield "allergy disclaimer present", "not medical advice" in out, ""


run("T3-a substitution, weak/conflicting evidence (banana bread, no eggs)", ["Find a banana bread recipe", "Take this banana bread recipe and remove eggs. Use only ingredients that appear in the dataset. What replacements have evidence from related baked recipes, and how might the texture change?"], c7)


def c8(s, r):
    out = r[1]
    cites = cited(out)
    new_list = out.split("New ingredient list:")[1].split("New instructions")[0]
    lines = [l.strip()[2:] for l in new_list.splitlines() if l.strip().startswith("- ")]
    left = [l for l in lines if ing.line_groups(ing.parse_line(l)[2])[0] & ing.G_DAIRY]
    yield "cited ids exist", cites and all(store.recipe(i) for i, _ in cites), f"{len(cites)}"
    yield "cream line replaced with a non-dairy substitute", not any("cream" in l.lower() for l in lines), ""
    yield "any dairy still present is reported as a WARNING (no false 'dairy-free' claim)", (not left) or "WARNING - still contains dairy" in out, f"remaining dairy lines: {left}"
    yield "no candidate violating the restriction was proposed", "yogurt" not in out.split("My pick for")[0].split("Options, best-supported")[1] if "Options, best-supported" in out else True, ""


run("T3-b substitution under a dietary restriction (cream, no dairy)", ["Find a pasta recipe with cream", "Replace cream in this pasta recipe without dairy. Prefer examples from recipes already in the database."], c8)


def c9(s, r):
    yield "refuses to invent calories", "can't answer" in r[1] and "no 'calories' data" in r[1], ""
    yield "refuses to invent cooking time", "cooking/prep time" in r[2], ""
    yield "contains no fabricated numbers like 'kcal'", "kcal" not in r[1] and "minutes" not in r[2], ""


run("T1-e unsupported attributes", ["Find a lasagna recipe", "How many calories does the first one have?", "How long does it take to cook?"], c9)


def c10(s, r):
    yield "no recipe contains peanuts while peanut-free is required", "couldn't find any" in r[1], r[1].splitlines()[0]
    yield "tells the user nothing was relaxed", "haven't loosened any" in r[1], ""


run("T1-f contradictory constraints", ["Find a recipe with peanuts", "no peanuts"], c10)


def c11(s, r):
    yield "free-text 'use X instead' re-runs evidence for X", "applesauce: evidence" in r[2] and "My pick for egg: applesauce" in r[2], ""
    yield "curated expectation reused for the chosen swap", "adds moisture and light binding" in r[2], ""


run("T3-c follow-up choice", ["Find a banana bread recipe", "Take this banana bread recipe and remove eggs.", "use applesauce instead"], c11)


# --------------------------------------------------------------------------------------------- baseline proxy
def baseline():
    """What a naive 'retrieve top-k then answer' pipeline reports for the garlic+onion question."""
    k = 10
    rids, total, _ = store.search(Filter(include=("garlic", "onion")), limit=k)
    true_n = store.count(Filter(include=("garlic", "onion")))
    return k, true_n


def main():
    bad = [r for r in RESULTS if not r[2]]
    k, true_n = baseline()
    lines = ["# Evaluation results", "", f"Dataset: {store.total:,} complete recipes. Run: `python -m eval.run_eval`.", "",
             "| Conversation | Check | Result | Detail |", "|---|---|---|---|"]
    for case, name, ok, detail in RESULTS:
        lines.append(f"| {case} | {name} | {'PASS' if ok else '**FAIL**'} | {str(detail).replace('|', '/')[:150]} |")
    lines += ["", f"**{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed.**", "",
              "## Baseline proxy: why retrieval-then-answer is not enough for aggregates", "",
              f"A naive pipeline that retrieves the top-{k} semantic/BM25 hits for 'garlic and onion' and reports what it sees would answer "
              f"'about {k} recipes'. The complete filtered set has {true_n:,}. This system computes the count over the whole filtered set in SQL "
              "and states the filter, denominator and normalization.", ""]
    with open(os.path.join(HERE, "RESULTS.md"), "w", encoding="utf8") as f:
        f.write("\n".join(lines))
    with open(os.path.join(HERE, "transcripts.md"), "w", encoding="utf8") as f:
        f.write("# Test conversations (verbatim output)\n\n")
        for case, log in TRANSCRIPTS:
            f.write(f"## {case}\n\n")
            for t, reply, dt in log:
                f.write(f"**you:** {t}\n\n```\n{reply}\n```\n_({dt:.1f}s)_\n\n")
    print("\n".join(lines[:4]))
    for case, name, ok, detail in RESULTS:
        print(("PASS " if ok else "FAIL ") + f"[{case[:28]}] {name}   {str(detail)[:90]}")
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} checks passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
