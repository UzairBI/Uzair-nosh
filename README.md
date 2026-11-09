# Recipe Box chatbot (GenAI intern assignment)

A conversational assistant over the full [Recipe Box dataset](https://eightportions.com/datasets/Recipes/) (all three JSON files,
122,893 complete recipes). It searches recipes, answers statistics, matches recipes to the ingredients you have, and adapts a recipe
when an ingredient is unavailable or restricted. **Answers are grounded in the records; numbers are computed in SQL/Python, never
guessed by an LLM.** The recipe engine runs offline with the standard library only; the conversational RAG layer adds Groq + embeddings (optional, see below).

## Run it (Python 3.10+; the recipe engine needs no packages, the RAG layer uses the optional ones in requirements.txt)

```bash
git clone <this repo> && cd nosh
python scripts/download_data.py      # downloads + unzips the 3 JSON files into ./data (53 MB zip)
python -m nosh.build                 # parses ingredients, builds data/nosh.db (SQLite + FTS5), ~1 minute, one time
python -m nosh.cli                   # command-line chat
python -m nosh.web                   # or a web UI at http://127.0.0.1:8000
python -m eval.run_eval              # 42 verified checks over 11 test conversations -> eval/RESULTS.md, eval/transcripts.md
python -m unittest discover -s tests # unit tests for normalization / dietary groups / intent parsing
```

Try (type `help` in the chat for more):

| Task | Say |
|---|---|
| 1 search + follow-up | `Find a penne pasta recipe.` then `I cannot have dairy.` then `show the ingredients of #2` |
| 1 statistics | `How many recipes contain both garlic and onion? What percentage of the dataset is that?` |
| 1 ranking | `Among recipes containing tomato, which five have the fewest instruction steps?` |
| 2 matching | `I have eggs, onions, tomatoes, and rice. Suggest five recipes, no peanuts, at most six steps, fewest extra ingredients.` then `Exclude eggs too` |
| 3 substitution | `Take this banana bread recipe and remove eggs.` / `Replace cream in this pasta recipe without dairy.` then `use applesauce instead` |

Optional: put `GROQ_API_KEY=...` in a git-ignored `.env` file (see `.env.example`; `GEMINI_API_KEY` also works) and messages the rule-based parser cannot classify are re-parsed by the LLM
into the same structured intent (`nosh/llm.py`). The LLM never writes answers. The Groq path was smoke-tested (model `openai/gpt-oss-120b`); the Gemini path is untested because no key was available
while developing; everything reported below was produced without it.

## Nosh Assistant: the RAG chatbot layer (what was added)

The original recipe engine is untouched (its 42 evaluation checks still pass). A conversational layer now sits on top of it:

```
message --> router (Groq, small model) --+--> recipe  : deterministic engine -> verified facts -> Groq writes the answer (numbers re-checked)
                                         +--> docs    : hybrid retrieval (+ SQL for tables) over YOUR uploads -> grounded, cited answer
                                         +--> general : Groq from its own knowledge, with conversation memory
```

* **Document ingestion** (`nosh/rag/parse.py`): PDF, DOCX, TXT/MD, HTML, CSV/TSV, XLSX, JSON/JSONL. Scanned PDFs are detected and reported (no OCR) instead of silently indexed as empty.
* **Chunking** (`chunk.py`): structure-aware (headings/pages kept as labels), ~900 characters with overlap; tables get a schema chunk plus row-group chunks.
* **Embeddings** (`embed.py`): MiniLM-L6 (384-d) via `fastembed` (ONNX), falling back to `sentence-transformers`, then to a dependency-free hashed lexical embedder, so the app always starts.
* **Retrieval** (`docstore.py`): dense cosine + BM25 (SQLite FTS5) fused with reciprocal-rank fusion, near-duplicate suppression, neighbour expansion, a confidence gate, and per-session isolation. Brute-force cosine over a cached float32 matrix is fast for tens of thousands of chunks.
* **Tables / datasets**: uploaded tables are also loaded into SQLite and Groq writes a SQL query that is executed **read-only behind an authorizer** (one SELECT, only that session's tables, 6 s limit). Counts, sums, averages and rankings are therefore computed, never guessed.
* **Recipe search** is semantic too: `python -m nosh.rag.recipe_vec` (about 15 min, optional) embeds all 122,893 recipes; search then fuses BM25 with similarity while every dietary/step/ingredient filter stays exact SQL.
* **No hallucination**: document answers may only use retrieved excerpts and must cite them (`[1]`); with nothing relevant the assistant says it could not find it. Recipe answers are written from the engine's facts, then every number in the reply is checked against those facts (retry, then fall back to the engine's own wording). Allergy/diet disclaimers and warnings are preserved.
* **Conversation**: last turns are sent to the model, older turns are summarised, and the router rewrites follow-ups ("and what about the battery?") into standalone questions. Default answers are short; "explain in detail / step by step" switches to long form.
* **Streaming UI**: answers stream token by token, render Markdown (tables, lists, code), show source chips, and files can be attached with the paperclip or dragged in.

Notes on behaviour you may notice: result lists are introduced by one short Groq-written sentence (validated: no recipe names, no unsupported claims such as "healthy" or "cozy", no diet the user did not ask for) with everything else generated deterministically from verified results; statistics, matching and substitution answers are written by Groq from the engine's facts and re-checked number by number. First run downloads the ~90 MB MiniLM model; `python -m nosh.rag.recipe_vec` takes roughly 15 minutes once. Dietary flags are conservative: they also scan every comma-separated part and parenthetical of an ingredient line ("1 cup milk (or chicken broth)" counts as meat for vegetarian filters).

Setup for this layer: `pip install -r requirements.txt` (all optional - without them the app falls back gracefully), put `GROQ_API_KEY` in `.env`, then `python -m nosh.web`.
Offline (no key) the assistant still routes, answers recipe questions with the engine's wording, and answers document questions extractively.
Tests: `python -m unittest discover -s tests` (includes `tests/test_rag.py`, which needs no network).

## How it works (decisions)

```
raw JSON --build--> SQLite: recipes(id,title,instructions,n_steps,flags) + ing(raw,qty,unit,name,alts,flags) + FTS5 index
user text --nlu.py--> Intent --session.py--> store.py / matcher.py / substitute.py --> answer with filters, counts, ids, evidence
```

* **IDs and originals.** Stable id = `<source>:<original key>` (e.g. `ar:hzvAPQ...`). Title, ingredient lines and instructions are stored
  verbatim (only the scraper noise `ADVERTISEMENT` and section headers are removed). 2,229 raw records lacking title, ingredients or
  instructions are dropped and reported; 2,090 exact duplicates are kept in statistics and hidden in result lists.
* **Retrieval.** SQLite FTS5 / BM25 (porter stemming, title weighted 8x over ingredients) for dish names and natural language; exact
  ingredient and dietary filters in SQL. A search is a `Filter` object; follow-ups ("no dairy", "at most 6 steps") are merged into it, so
  newly stated restrictions apply to the active search until the user says `start over`. Statistics run on the *complete* filtered set,
  never on the top-k hits.
* **Normalization (`nosh/ingredients.py`).** Strip quantities/units/prep words/parentheses, singularize, map synonyms (scallion = green
  onion, aubergine = eggplant ...). Names that are a *different product* are fused into one token (`tomato_paste`, `peanut_butter`,
  `green_onion`, `coconut_milk`, `olive_oil`) so "tomato" does not match tomato paste and "butter" does not match peanut butter, while
  "cherry tomato" does match "tomato". "X or Y" lines keep both alternatives. Matching = contiguous token run, so `egg` also matches
  `egg white` (shown to the user as the raw line).
* **Dietary restrictions.** A lexicon of 13 groups (dairy, egg, peanut, tree nut, gluten, soy, sesame, fish, shellfish, meat, pork,
  alcohol, honey; vegetarian/vegan are unions) classifies every ingredient line once at build time. Definite hits are hard exclusions;
  vague items (margarine, chocolate, broth) are *uncertain* and surfaced as warnings, never silently passed. Text-only, not medical advice.
* **Statistics.** SQL COUNT / Python mean+median / top ingredients / ranking by steps. Every answer prints the filters, the number of
  recipes analysed, the denominator and the rule. *Step rule:* one step per non-empty instruction line; if the instructions are one
  block, one step per sentence. *Ingredient count:* number of non-blank ingredient lines.
* **Matching (Task 2).** Hard constraints (excluded ingredients/groups, max steps) filter first. Ranking is lexicographic and shown to
  the user: (1) number of *your* ingredients used, (2) fewest extra required ingredients (or fewest steps, per preference), (3) steps, (4) id.
  Salt/pepper/water are assumed available. Quantities are compared only when units are convertible (volume, mass, or same unit);
  otherwise "quantity not verified". If nothing passes, it says **NO MATCH** and lists alternatives each labelled `VIOLATES: ...`.
* **Substitution (Task 3).** Candidates come from a small curated table of culinary roles (~16 ingredients; *general knowledge, labelled
  as such*), filtered by the restriction and by presence in the dataset. Local evidence per candidate: (a) the ~60 recipes most similar to the
  target (title words + ingredient Jaccard) that lack the original ingredient but contain the candidate, (b) recipes whose title is
  labelled eggless / vegan / dairy-free for the same dish. Strength strong/weak/none, plus a *conflict* flag when the candidate is just a normal
  ingredient of the dish. It outputs cited ids with the verbatim supporting line, an updated ingredient list, rewritten steps, predicted
  effects (labelled predictions), and warns about other lines that still violate the restriction. No web search is used; with no local
  evidence it says so instead of presenting a prediction as fact.
* **Unsupported questions.** Calories, nutrition, cooking time, servings, ratings: refused (the dataset has none) rather than estimated.

## Layout

`nosh/` ingredients.py (parsing, normalization, dietary lexicon) · build.py · store.py (filters, FTS, stats) · matcher.py (Task 2) ·
substitute.py (Task 3) · nlu.py (rules) · session.py (state + answers) · llm.py (optional) · cli.py · web.py ·
`eval/` run_eval.py, RESULTS.md, transcripts.md, REPORT.md (what worked / failed, one-shot comparison) · `tests/`.

## AI tool disclosure

This project was written with Claude Code (Anthropic) as a coding assistant, under my direction: it generated most of the code and the first
drafts of the curated substitution table and lexicons; I reviewed the design, and the evaluation checks were written to verify outputs independently
of the code that produced them. (Edit this paragraph to describe your own involvement accurately before submitting.)

## Web UI
The page (nosh/static/index.html) follows the Nosh Assistant mockup: dark sidebar, chat with recipe cards, promo panel. Logo and photos come from letsnosh.io (nosh/static/img). Card photos are illustrative because the dataset has none. Recipe ids are hidden on screen (SHOW_IDS-style: they stay in the API/CLI).
