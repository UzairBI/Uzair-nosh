# Nosh: Recipe Box Chatbot

A conversational assistant over the Recipe Box dataset (122,893 recipes). It searches recipes, answers statistics, matches recipes to the ingredients you have, and suggests substitutions for restricted or missing ingredients. It can also answer questions about documents you upload.

Answers are grounded in the data. Numbers are computed in SQL/Python, never guessed by an LLM.

## Features
- **Search**: full-text search (SQLite FTS5/BM25) with dietary filters and follow-ups like "no dairy"
- **Statistics**: counts, percentages and rankings over the full filtered set
- **Ingredient matching**: ranks recipes by how many of your ingredients they use, with fewest extra ingredients
- **Substitutions**: suggestions backed by evidence from similar recipes, with rewritten steps
- **RAG over uploads** (optional): PDF, DOCX, TXT, HTML, CSV, XLSX and JSON, with cited answers

## Quick Start
Requires Python 3.10+. The core engine needs no extra packages.

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
python scripts/download_data.py   # download dataset
python -m nosh.build              # build the SQLite database (one time)
python -m nosh.cli                # command-line chat
python -m nosh.web                # web UI at http://127.0.0.1:8000
```

## Optional: LLM Layer
```bash
pip install -r requirements.txt
```
Add `GROQ_API_KEY=<your-key>` to a git-ignored `.env` file. Without a key, the app still works offline using its rule-based engine.

## Tests
```bash
python -m unittest discover -s tests
python -m eval.run_eval
```

## Project Layout
`nosh/` (engine, NLU, web, RAG) · `eval/` (evaluation) · `tests/` · `scripts/`


Built for the GenAI Intern assignment.
