# Evaluation results

Dataset: 122,893 complete recipes. Run: `python -m eval.run_eval`.

| Conversation | Check | Result | Detail |
|---|---|---|---|
| T1-a multi-turn refinement (penne -> no dairy) | refined the SAME search (text kept, dairy added) | PASS | text: penne pasta; free of: dairy |
| T1-a multi-turn refinement (penne -> no dairy) | every shown recipe is penne + passes independent raw-text dairy scan | PASS | 5 recipes checked |
| T1-a multi-turn refinement (penne -> no dairy) | answer shows ingredient evidence | PASS |  |
| T1-a multi-turn refinement (penne -> no dairy) | dairy-free result count < unrestricted count | PASS | 504 -> 112 |
| T1-b aggregate statistics | reported count equals SQL count | PASS | 16,415 |
| T1-b aggregate statistics | SQL count equals independent python-side count | PASS | sql=16415 python=16415 |
| T1-b aggregate statistics | percentage + denominator stated | PASS |  |
| T1-b aggregate statistics | (info) naive substring count would differ | PASS | naive '%garlic%' AND '%onion%' = 20,241 (counts garlic powder, green onion, onion soup mix...) vs normalized 16,415 |
| T1-c ranking with ties | five recipes returned with ids | PASS |  |
| T1-c ranking with ties | all five have the global minimum step count among tomato recipes | PASS | min=1 steps=[1, 1, 1, 1, 1] |
| T1-c ranking with ties | every returned recipe really lists tomato | PASS |  |
| T1-c ranking with ties | states tie situation and step rule | PASS |  |
| T1-d context carried into statistics | 'those' reuses the complete active search (not the top 5) | PASS | n=175 |
| T1-d context carried into statistics | follow-up statistic on same set (steps) | PASS |  |
| T2-a ingredient matching + follow-up | after turn 1: all results <= 6 steps | PASS |  |
| T2-a ingredient matching + follow-up | after turn 2 ('Exclude eggs too'): egg is a hard exclusion | PASS | free of egg, peanut; at most 6 steps |
| T2-a ingredient matching + follow-up | turn 2 results: none contains egg (independent raw-text scan) | PASS | 5 checked |
| T2-a ingredient matching + follow-up | turn 2 results: <= 6 steps and no peanut | PASS |  |
| T2-a ingredient matching + follow-up | eggs no longer counted as available | PASS |  |
| T2-a ingredient matching + follow-up | matched / extra-required / ranking explanation shown | PASS |  |
| T2-a ingredient matching + follow-up | quantities not given -> says not verified | PASS |  |
| T2-b no eligible recipe (hard constraint is never relaxed silently) | states NO MATCH explicitly | PASS |  |
| T2-b no eligible recipe (hard constraint is never relaxed silently) | no recipe offered as a valid result | PASS |  |
| T2-b no eligible recipe (hard constraint is never relaxed silently) | alternatives (if any) are each labelled VIOLATES | PASS | 3 labelled |
| T2-b no eligible recipe (hard constraint is never relaxed silently) | alternatives really do violate (contain shellfish) | PASS |  |
| T3-a substitution, weak/conflicting evidence (banana bread, no eggs) | evidence labelled WEAK/indirect (not oversold) | PASS |  |
| T3-a substitution, weak/conflicting evidence (banana bread, no eggs) | every cited recipe id exists | PASS | 6 citations |
| T3-a substitution, weak/conflicting evidence (banana bread, no eggs) | every quoted supporting line is verbatim in the cited recipe | PASS |  |
| T3-a substitution, weak/conflicting evidence (banana bread, no eggs) | updated list contains no egg line | PASS | 6 lines |
| T3-a substitution, weak/conflicting evidence (banana bread, no eggs) | revised instructions + prediction label + no external source | PASS |  |
| T3-a substitution, weak/conflicting evidence (banana bread, no eggs) | allergy disclaimer present | PASS |  |
| T3-b substitution under a dietary restriction (cream, no dairy) | cited ids exist | PASS | 6 |
| T3-b substitution under a dietary restriction (cream, no dairy) | cream line replaced with a non-dairy substitute | PASS |  |
| T3-b substitution under a dietary restriction (cream, no dairy) | any dairy still present is reported as a WARNING (no false 'dairy-free' claim) | PASS | remaining dairy lines: [] |
| T3-b substitution under a dietary restriction (cream, no dairy) | no candidate violating the restriction was proposed | PASS |  |
| T1-e unsupported attributes | refuses to invent calories | PASS |  |
| T1-e unsupported attributes | refuses to invent cooking time | PASS |  |
| T1-e unsupported attributes | contains no fabricated numbers like 'kcal' | PASS |  |
| T1-f contradictory constraints | no recipe contains peanuts while peanut-free is required | PASS | Got it, I've narrowed down the same search. |
| T1-f contradictory constraints | tells the user nothing was relaxed | PASS |  |
| T3-c follow-up choice | free-text 'use X instead' re-runs evidence for X | PASS |  |
| T3-c follow-up choice | curated expectation reused for the chosen swap | PASS |  |

**42/42 checks passed.**

## Baseline proxy: why retrieval-then-answer is not enough for aggregates

A naive pipeline that retrieves the top-10 semantic/BM25 hits for 'garlic and onion' and reports what it sees would answer 'about 10 recipes'. The complete filtered set has 16,415. This system computes the count over the whole filtered set in SQL and states the filter, denominator and normalization.
