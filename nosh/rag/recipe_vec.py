"""Dense vectors for the recipe collection (title + main ingredients) for semantic recipe search.

    python -m nosh.rag.recipe_vec          # ~8 minutes on CPU, writes data/recipe_vec.npz  (optional; search works without it)

Used by Store.search to fuse semantic similarity with BM25, so vague requests ("something light and fresh with chickpeas")
find sensible recipes.  All hard filters (dietary, steps, excluded ingredients) are still applied exactly in SQL afterwards.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import numpy as np

from ..build import DATA_DIR, DB_PATH
from .embed import get_embedder

PATH = os.path.join(DATA_DIR, "recipe_vec.npz")


def build(batch: int = 4000):
    db = sqlite3.connect(DB_PATH)
    rows = db.execute("SELECT rid, title FROM recipes ORDER BY rid").fetchall()
    ing = {}
    for rid, nm in db.execute("SELECT rid, group_concat(replace(name,'_',' '), ', ') FROM (SELECT rid, name FROM ing WHERE name<>'' AND staple=0 ORDER BY rid, idx) GROUP BY rid"):
        ing[rid] = nm
    emb = get_embedder()
    print(f"embedding {len(rows):,} recipes with {emb.name}", flush=True)
    texts_all = [f"{t}. {', '.join((ing.get(r) or '').split(', ')[:3])}" for r, t in rows]
    order = np.argsort([len(t) for t in texts_all])          # similar lengths together -> far less padding
    rids = np.array([r[0] for r in rows], dtype=np.int32)
    out = np.zeros((len(rows), emb.dim), dtype=np.float16)
    t0 = time.time()
    for i in range(0, len(rows), batch):
        idx = order[i:i + batch]
        out[idx] = emb.embed([texts_all[j] for j in idx], batch_size=128).astype(np.float16)
        done = min(i + batch, len(rows))
        print(f"  {done:,}/{len(rows):,}  {done / (time.time() - t0):.0f}/s", flush=True)
    np.savez(PATH, rids=rids, vecs=out, model=np.array(emb.name))
    print("saved", PATH)


class RecipeVectors:
    """Lazy loader; `top(query, n)` -> list of (rid, cosine)."""
    _inst = None

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = cls()
        return cls._inst

    def __init__(self):
        self.ok = False
        if not os.path.exists(PATH):
            return
        z = np.load(PATH, allow_pickle=False)
        self.rids = z["rids"]
        self.vecs = z["vecs"].astype(np.float32)
        self.model = str(z["model"])
        self.ok = True

    def top(self, query: str, n: int = 300):
        if not self.ok:
            return []
        emb = get_embedder()
        if emb.name != self.model:          # vectors were built with a different backend -> do not mix
            return []
        q = emb.embed_query(query)
        sims = self.vecs @ q
        idx = np.argpartition(-sims, min(n, len(sims) - 1))[:n]
        idx = idx[np.argsort(-sims[idx])]
        return [(int(self.rids[i]), float(sims[i])) for i in idx]


if __name__ == "__main__":
    build()
