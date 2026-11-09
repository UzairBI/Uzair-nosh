# Evaluation report

Reproduce: `python -m eval.run_eval` (writes `RESULTS.md` = pass/fail table, `transcripts.md` = verbatim chats).
11 conversations, 42 checks, **42/42 pass** at the time of writing. Each check is verified by a code path *independent* of the one that
produced the answer: Python-side matching instead of SQL `LIKE`, a regex over raw text instead of the dietary lexicon, id and verbatim-quote
look-ups for every citation. The set is my own, so a pass shows the behaviours below work, not that the language coverage is complete.

## The conversations

| Id | Task | What it exercises |
|---|---|---|
| T1-a | 1 | "Find a penne pasta recipe." -> "I cannot have dairy." The restriction refines the *same* search (the matching set shrinks sharply); shown recipes pass an independent raw-text dairy scan |
| T1-b | 1 | Aggregate: recipes with garlic AND onion = 16,415 = 13.36% of 122,893. SQL count equals an independent Python-side count over every parsed line |
| T1-c | 1 | Ranking with 62 ties at 1 step: tie-break rule and step rule are stated; all five returned recipes have the global minimum and really list tomato |
| T1-d | 1 | "How many of those are vegetarian?" / "average and median steps for those" use the *complete* active result set (175 recipes), not the 5 shown |
| T1-e | 1 | "How many calories does the first one have?", "How long does it take to cook?" -> refused: the data has no such fields |
| T1-f | 1 | "recipe with peanuts" + "no peanuts" -> no result, says nothing was relaxed |
| T2-a | 2 | Multi-turn: 5 recipes using eggs/onions/tomatoes/rice, no peanuts, <= 6 steps, fewest extras; then "Exclude eggs too" removes eggs from the available list, makes egg a hard exclusion, and re-ranks (50,705 -> 26,133 eligible) |
| T2-b | 2 | **No-match**: "only lobster, no shellfish" -> `NO MATCH`; the three closest recipes are listed separately, each labelled `VIOLATES: contains shellfish` |
| T3-a | 3 | **Weak/conflicting evidence**: banana bread without eggs. Only 1 similar recipe lacks eggs; the "mashed banana" candidate is flagged as a normal ingredient of the dish (98% of similar egg recipes already contain it); yogurt/applesauce have a single supporting vegan recipe each; flax egg, tofu, vinegar+soda have *none* locally. All 6 citations exist and their quoted lines are verbatim |
| T3-b | 3 | Cream in pasta without dairy: candidates containing dairy are removed up front; butter is also swapped (olive oil, weak evidence); Parmesan has no dataset-supported swap so the answer ends with `WARNING - still contains dairy` instead of claiming dairy-free |
| T3-c | 3 | Follow-up "use applesauce instead" re-checks evidence for the user's choice and rewrites the recipe |

## One-shot LLM vs this system

*Question:* "How many recipes contain both garlic and onion? What percentage of the dataset is that?"

* A one-shot LLM has no access to the 122,893 records, so any number it gives is invented. (`python -m eval.oneshot_llm` with a Gemini key prints its
  answer next to the grounded one. I could not run it - no key during development - so I make no claim about what a specific model says.)
* A *retrieve-then-answer* pipeline fails differently and I can show it: it sees only the top-k hits (k = 10 -> "about 10 recipes") while the full filtered
  set has 16,415. This system computes the count over the whole filtered set in SQL and prints filter, count, denominator, percentage and the rule.
* A *naive substring* count (`'%garlic%' AND '%onion%'` over raw lines) gives 20,241, which is wrong for the intended meaning because it counts garlic
  powder, onion soup mix, green onions etc. Normalization (`green_onion`, `garlic_powder` ... are different products) gives 16,415 (T1-b prints both).
* Banana bread without eggs: a one-shot answer will confidently recommend flax eggs/applesauce with ratios. Locally, flax egg has **no** supporting
  recipe among the similar/eggless banana-bread records, and the system says so, labels the effect as a prediction, and still gives the adaptation.

## What worked

* Normalization that keeps products apart; dietary flags computed once and used consistently by search, stats, matching and substitution.
* Every list shows the raw ingredient lines, ids and the rule used. Hard constraints are never relaxed silently (T1-f, T2-b).
* Statistics always use the whole filtered set and print their denominator.
* Citations in substitutions are machine-checked (id exists, quote is verbatim).

## What failed / bugs found while building (and fixes)

1. Matching first ranked by "share of the recipe covered", so one-ingredient recipes ("Hard-Boiled Eggs") won. Fixed: lexicographic rank, your ingredients first.
2. Title-labelled evidence matched "Vegan Breaded Tofu" for banana bread (stemming "bread" ~ "breaded"). Fixed: require >= 2 distinct dish words in the title.
3. "Exclude eggs too" kept "eggs" as an available ingredient. Fixed: excluded groups are removed from the available list and the user is told.
4. "recipes with peanuts" was parsed as a restriction. Fixed: positive mentions are ingredients.
5. "how many recipes have no eggs or dairy" was parsed as "contains: no egg"; "which 3 recipes have the most steps" as "contains: most step". Fixed in the parser.

## Known limitations (be aware)

* **Lexical retrieval only** (BM25). No embeddings, so vague asks ("something light and fresh") are matched by words, not meaning. The optional LLM
  fallback only helps parsing, and is untested.
* **Rule-based NLU** is brittle outside the phrasings in the tests. Wrong parses are visible because every answer echoes the filters it applied.
* **Normalization is name-level.** "red onion" is counted separately from "onion" in top-ingredient lists (a `contains onion` filter does match red onion);
  `tomato` does not match tomato sauce/paste/juice by design; compound lines such as "diced tomatoes with garlic and onion" become one name; "X or Y" lines
  count only X in frequency tables. Cooked vs uncooked rice is not distinguished (quantity checks print a caution when the recipe says cooked).
* **Dietary lexicon is incomplete** (it will miss exotic ingredients) and text-only; vague items are flagged "possible hidden source". Not medical advice.
* **Step counts are a heuristic** (lines, or sentences for single-block instructions); Epicurious/Food Network text is often one block.
* **Substitution knowledge is a small curated table** (~16 ingredients). The *evidence* is local, but the candidate list and the predicted effects are
  general cooking knowledge (labelled). Instruction rewriting swaps names only, so verbs may no longer fit (the answer says so). Ingredient swaps that need
  quantity conversion beyond simple ratios are approximate. External web search is not implemented (allowed but optional).
* 2,090 exact-duplicate records are included in statistics (reported) and hidden in lists.
