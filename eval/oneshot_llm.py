"""OPTIONAL: ask a plain LLM (no retrieval, no tools) the same questions and compare with the grounded answers.

    set GEMINI_API_KEY=...   (free Google AI Studio key)
    python -m eval.oneshot_llm

The grounded numbers come from the SQL store; the LLM numbers come from the model's memory.  Not run in CI because it
needs a key; the committed eval/RESULTS.md does not depend on it.
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nosh.store import Filter, Store  # noqa: E402

KEY = os.environ.get("GEMINI_API_KEY")
if not KEY:
    sys.exit("set GEMINI_API_KEY to run the one-shot baseline")

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")


def ask(q):
    body = {"contents": [{"parts": [{"text": q}]}], "generationConfig": {"temperature": 0}}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={KEY}",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["candidates"][0]["content"]["parts"][0]["text"]


store = Store()
q = "In the Recipe Box dataset (eightportions.com, ~125k recipes from AllRecipes, Epicurious and Food Network), how many recipes contain both garlic and onion? Give a number."
print("QUESTION:", q)
print("\nONE-SHOT LLM:\n", ask(q))
print(f"\nGROUNDED (SQL over {store.total:,} parsed recipes): {store.count(Filter(include=('garlic', 'onion'))):,}")
