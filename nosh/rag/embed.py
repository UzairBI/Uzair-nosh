"""Text embeddings with graceful fallbacks.

Backends (first that works wins):
  fastembed  - ONNX all-MiniLM-L6-v2 (384-d), fast on CPU, no torch needed   (pip install fastembed)
  st         - sentence-transformers all-MiniLM-L6-v2 (same model, via torch)
  hash       - feature-hashed TF-IDF-style vectors; purely lexical, zero dependencies (offline fallback)

All vectors are L2-normalised float32, so cosine similarity == dot product.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import threading

import numpy as np

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DIM = 384
_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder:
    def __init__(self, prefer: str | None = None):
        self.lock = threading.Lock()
        self.backend = None
        self.model = None
        self.dim = DIM
        order = [prefer] if prefer else (os.environ.get("NOSH_EMBED", "").split(",") if os.environ.get("NOSH_EMBED") else ["fastembed", "st", "hash"])
        for b in order:
            if self._try(b.strip()):
                break

    @property
    def name(self) -> str:
        return f"{self.backend}:{MODEL if self.backend != 'hash' else 'hash384'}"

    def _try(self, b: str) -> bool:
        try:
            if b == "fastembed":
                from fastembed import TextEmbedding
                self.model = TextEmbedding(MODEL, threads=os.cpu_count() or 4)
            elif b == "st":
                from sentence_transformers import SentenceTransformer
                self.model = SentenceTransformer(MODEL, device="cpu")
            elif b == "hash":
                self.model = None
            else:
                return False
            self.backend = b
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    def embed(self, texts: list[str], batch_size: int = 128) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        with self.lock:
            if self.backend == "fastembed":
                v = np.array(list(self.model.embed(texts, batch_size=batch_size)), dtype=np.float32)
            elif self.backend == "st":
                v = np.asarray(self.model.encode(texts, batch_size=batch_size, normalize_embeddings=False), dtype=np.float32)
            else:
                v = np.stack([_hash_vec(t, self.dim) for t in texts])
        n = np.linalg.norm(v, axis=1, keepdims=True)
        return v / np.maximum(n, 1e-9)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


def _hash_vec(text: str, dim: int) -> np.ndarray:
    """Deterministic lexical embedding: hashed unigrams + bigrams with sublinear tf (no corpus statistics needed)."""
    toks = _TOKEN.findall(text.lower())
    v = np.zeros(dim, dtype=np.float32)
    counts: dict[str, int] = {}
    for i, t in enumerate(toks):
        counts[t] = counts.get(t, 0) + 1
        if i:
            b = toks[i - 1] + "_" + t
            counts[b] = counts.get(b, 0) + 1
    for g, c in counts.items():
        h = int(hashlib.md5(g.encode()).hexdigest()[:8], 16)
        v[h % dim] += (1.0 + math.log(c)) * (1 if (h >> 31) & 1 else -1)
    return v


_EMB: Embedder | None = None
_EMB_LOCK = threading.Lock()


def get_embedder() -> Embedder:
    global _EMB
    with _EMB_LOCK:
        if _EMB is None:
            _EMB = Embedder()
        return _EMB
