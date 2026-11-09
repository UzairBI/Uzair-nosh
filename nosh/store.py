"""Read access to the recipe database: filters, full-text search, statistics, recipe records."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
from dataclasses import dataclass, field, replace

from . import ingredients as ing
from .build import DB_PATH, SRC_NAMES, split_steps


@dataclass(frozen=True)
class Filter:
    """A conjunction of constraints over the recipe collection (the 'active search')."""
    text: tuple = ()          # dish-name / free-text words (used for ranking AND must match via FTS)
    include: tuple = ()       # normalized ingredient names that must all be present
    any_of: tuple = ()        # at least one of these ingredient names must be present
    exclude: tuple = ()       # normalized ingredient names that must be absent
    excl_mask: int = 0        # dietary groups that must be absent (dairy, egg, ...)
    title_has: tuple = ()     # words that must occur in the title
    max_steps: int | None = None
    min_steps: int | None = None
    src: str | None = None
    max_ingredients: int | None = None
    text_any: bool = False    # dish words matched with OR instead of AND (set when AND found nothing)
    semantic: str = ""        # whole natural-language request, used only for the semantic (vector) part of search ranking

    def describe(self) -> str:
        parts = []
        if self.text:
            parts.append("text: " + " ".join(self.text))
        if self.title_has:
            parts.append("title contains: " + ", ".join(self.title_has))
        if self.any_of:
            parts.append("contains any of: " + ", ".join(ing.display(x) for x in self.any_of))
        if self.include:
            parts.append("contains: " + ", ".join(ing.display(x) for x in self.include))
        if self.exclude:
            parts.append("without: " + ", ".join(ing.display(x) for x in self.exclude))
        if self.excl_mask:
            parts.append("free of: " + ing.describe_groups(self.excl_mask))
        if self.max_steps is not None:
            parts.append(f"at most {self.max_steps} steps")
        if self.min_steps is not None:
            parts.append(f"at least {self.min_steps} steps")
        if self.max_ingredients is not None:
            parts.append(f"at most {self.max_ingredients} ingredient lines")
        if self.src:
            parts.append("source: " + SRC_NAMES.get(self.src, self.src))
        return "; ".join(parts) if parts else "no filters (whole dataset)"

    def with_(self, **kw) -> "Filter":
        return replace(self, **kw)


_ESC = " ESCAPE '\\'"


class Store:
    def __init__(self, path: str = DB_PATH):
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} not found. Run: python scripts/download_data.py && python -m nosh.build")
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.total = self.db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        self.meta = {r["k"]: r["v"] for r in self.db.execute("SELECT * FROM meta")}

    # ------------------------------------------------------------------ SQL building
    @staticmethod
    def _term_clause(term: str):
        pats = ing.sql_like_patterns(term)
        sql = " OR ".join(f"alts LIKE ?{_ESC}" for _ in pats)
        return f"r.rid IN (SELECT rid FROM ing WHERE {sql})", pats

    def where(self, f: Filter, dedupe: bool = False, with_text: bool = True):
        cl, args = ["1=1"], []
        if with_text and f.text:
            toks = self._fts_tokens(f.text)
            if toks:
                cl.append("r.rid IN (SELECT rowid FROM fts WHERE fts MATCH ?)")
                args.append((" OR " if f.text_any else " AND ").join(f'"{t}"' for t in toks))
        for t in f.include:
            c, a = self._term_clause(t)
            cl.append(c)
            args += a
        if f.any_of:
            pats = [p for t in f.any_of for p in ing.sql_like_patterns(t)]
            cl.append("r.rid IN (SELECT rid FROM ing WHERE " + " OR ".join(f"alts LIKE ?{_ESC}" for _ in pats) + ")")
            args += pats
        for t in f.exclude:
            c, a = self._term_clause(t)
            cl.append("NOT " + c)
            args += a
        if f.excl_mask:
            cl.append("(r.flags & ?) = 0")
            args.append(f.excl_mask)
        for w in f.title_has:
            cl.append("r.title LIKE ?")
            args.append(f"%{w}%")
        if f.max_steps is not None:
            cl.append("r.n_steps <= ?")
            args.append(f.max_steps)
        if f.min_steps is not None:
            cl.append("r.n_steps >= ?")
            args.append(f.min_steps)
        if f.max_ingredients is not None:
            cl.append("r.n_ing <= ?")
            args.append(f.max_ingredients)
        if f.src:
            cl.append("r.src = ?")
            args.append(f.src)
        if dedupe:
            cl.append("r.dup = 0")
        return " AND ".join(cl), args

    def has_ingredient(self, term: str) -> bool:
        """Does any recipe have an ingredient matching this normalized name? (cheap existence check)"""
        pats = ing.sql_like_patterns(term)
        if not pats:
            return False
        cond = " OR ".join(f"alts LIKE ?{_ESC}" for _ in pats)
        return self.db.execute(f"SELECT 1 FROM ing WHERE {cond} LIMIT 1", pats).fetchone() is not None

    # ------------------------------------------------------------------ records
    def recipe(self, key) -> dict | None:
        """key = stable id string like 'ar:xyz' or integer rid."""
        if isinstance(key, int):
            r = self.db.execute("SELECT * FROM recipes WHERE rid=?", (key,)).fetchone()
        else:
            r = self.db.execute("SELECT * FROM recipes WHERE id=?", (key,)).fetchone()
        if not r:
            return None
        lines = self.db.execute("SELECT * FROM ing WHERE rid=? ORDER BY idx", (r["rid"],)).fetchall()
        return {
            "rid": r["rid"], "id": r["id"], "src": r["src"], "title": r["title"],
            "instructions": r["instructions"], "steps": split_steps(r["instructions"]),
            "n_steps": r["n_steps"], "n_ing": r["n_ing"], "flags": r["flags"], "uflags": r["uflags"],
            "ingredients": [dict(raw=l["raw"], qty=l["qty"], unit=l["unit"], name=l["name"],
                                 alts=[a.strip() for a in l["alts"].split("|") if a.strip()],
                                 flags=l["flags"], uflags=l["uflags"], staple=l["staple"]) for l in lines],
        }

    def brief(self, rids) -> list[dict]:
        rids = list(rids)
        if not rids:
            return []
        q = ",".join("?" * len(rids))
        rows = {r["rid"]: r for r in self.db.execute(f"SELECT rid,id,src,title,n_ing,n_core,n_steps,flags,uflags FROM recipes WHERE rid IN ({q})", rids)}
        return [dict(rows[i]) for i in rids if i in rows]

    def find_id(self, token: str) -> str | None:
        token = token.strip()
        r = self.db.execute("SELECT id FROM recipes WHERE id=?", (token,)).fetchone()
        if r:
            return r["id"]
        if len(token) >= 8:  # allow bare key without the source prefix
            r = self.db.execute("SELECT id FROM recipes WHERE id LIKE ? LIMIT 2", (f"%:{token}",)).fetchall()
            if len(r) == 1:
                return r[0]["id"]
        return None

    # ------------------------------------------------------------------ search
    @staticmethod
    def _fts_tokens(words) -> list[str]:
        out = []
        for w in words:
            for t in re.findall(r"[a-z0-9]+", str(w).lower()):
                if t not in out:
                    out.append(t)
        return out

    def search(self, f: Filter, limit: int = 5, offset: int = 0, dedupe: bool = True):
        """Ranked search: BM25 over title+ingredients fused (reciprocal-rank fusion) with MiniLM semantic similarity when the
        recipe vectors exist (python -m nosh.rag.recipe_vec). Every hard filter is still applied exactly in SQL.
        Returns (rids, total_matching, mode)."""
        vec = None
        sem = f.semantic or " ".join(f.text)
        if sem and offset + limit <= 300:
            from .rag.recipe_vec import RecipeVectors
            v = RecipeVectors.get()
            vec = v if v.ok else None
        if vec is None:
            return self._search_lexical(f, limit, offset, dedupe)
        lex_ids, total, mode = self._search_lexical(f, 300, 0, dedupe)
        if not f.text and not f.include:
            lex_ids = []                       # "filters only" order is not a relevance ranking: let the semantic side decide
        lexical_expected = bool(f.text or f.include)
        if lexical_expected and not lex_ids:
            return [], 0, "none"          # none of the requested words occurs in any matching recipe: report "nothing found", never a weak lookalike
        dense = [(r, s) for r, s in vec.top(sem, 400) if s >= 0.30]
        if not dense:
            return lex_ids[offset:offset + limit], total, mode
        wh, args = self.where(f, dedupe=dedupe, with_text=False)
        ids = [r for r, _ in dense]
        ok = {row[0] for row in self.db.execute(f"SELECT r.rid FROM recipes r WHERE {wh} AND r.rid IN ({','.join('?' * len(ids))})", args + ids)}
        dense_ids = [r for r in ids if r in ok]
        w_lex = 1.0 if mode == "all words" else 0.5      # an any-word match is noisy: trust the semantic ranking more
        fused: dict[int, float] = {}
        for rank, r in enumerate(lex_ids):
            fused[r] = fused.get(r, 0) + w_lex / (60 + rank)
        for rank, r in enumerate(dense_ids):
            fused[r] = fused.get(r, 0) + 1.0 / (60 + rank)
        ranked = sorted(fused, key=lambda r: -fused[r])
        return ranked[offset:offset + limit], max(total, len(fused)), ("semantic only" if (lexical_expected and not lex_ids) else mode + " + semantic")

    def _search_lexical(self, f: Filter, limit: int = 5, offset: int = 0, dedupe: bool = True):
        wh, args = self.where(f, dedupe=dedupe, with_text=False)
        words = list(f.text) + [t for i in f.include for t in ing.display(i).split()]
        toks = self._fts_tokens(words)
        if not toks:
            total = self.db.execute(f"SELECT COUNT(*) FROM recipes r WHERE {wh}", args).fetchone()[0]
            rows = self.db.execute(f"SELECT rid FROM recipes r WHERE {wh} ORDER BY n_ing, rid LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
            return [r[0] for r in rows], total, "filters only"
        for mode, joiner in (("all words", " AND "), ("any word", " OR ")):
            q = joiner.join(f'"{t}"' for t in toks)
            sql_from = f"fts JOIN recipes r ON r.rid = fts.rowid WHERE fts MATCH ? AND {wh}"
            if joiner == " AND ":
                total = self.db.execute(f"SELECT COUNT(*) FROM {sql_from}", [q] + args).fetchone()[0]
                if not total:
                    continue
            rows = self.db.execute(
                f"SELECT r.rid FROM {sql_from} ORDER BY bm25(fts, 8.0, 1.0), r.rid LIMIT ? OFFSET ?",
                [q] + args + [limit, offset]).fetchall()
            if joiner == " OR ":
                total = len(rows)       # exact counting of a broad any-word match is slow and not needed
            return [r[0] for r in rows], total, mode
        return [], 0, "none"

    # ------------------------------------------------------------------ statistics (all exact SQL / Python)
    def count(self, f: Filter) -> int:
        wh, args = self.where(f)
        return self.db.execute(f"SELECT COUNT(*) FROM recipes r WHERE {wh}", args).fetchone()[0]

    def numbers(self, f: Filter, col: str) -> list[int]:
        assert col in ("n_ing", "n_steps", "n_core")
        wh, args = self.where(f)
        return [r[0] for r in self.db.execute(f"SELECT {col} FROM recipes r WHERE {wh}", args)]

    def summarize(self, f: Filter, col: str) -> dict:
        v = self.numbers(f, col)
        if not v:
            return {"n": 0}
        return {"n": len(v), "mean": statistics.fmean(v), "median": statistics.median(v), "min": min(v), "max": max(v)}

    def top_ingredients(self, f: Filter, n: int = 10, skip_staples: bool = False, skip: tuple = ()):
        wh, args = self.where(f)
        extra = " AND staple=0" if skip_staples else ""
        skip_sql = "".join(" AND name <> ?" for _ in skip)
        rows = self.db.execute(
            f"SELECT name, COUNT(DISTINCT rid) c FROM ing WHERE name <> ''{extra}{skip_sql} AND rid IN (SELECT r.rid FROM recipes r WHERE {wh}) "
            f"GROUP BY name ORDER BY c DESC, name LIMIT ?", list(skip) + args + [n]).fetchall()
        return [(r["name"], r["c"]) for r in rows]

    def rank_steps(self, f: Filter, n: int = 5, fewest: bool = True):
        wh, args = self.where(f)
        order = "ASC" if fewest else "DESC"
        rows = self.db.execute(
            f"SELECT rid,id,title,n_steps,n_ing FROM recipes r WHERE {wh} ORDER BY n_steps {order}, n_ing {order}, id LIMIT ?",
            args + [n]).fetchall()
        res = [dict(r) for r in rows]
        ties = 0
        if res:
            ties = self.db.execute(f"SELECT COUNT(*) FROM recipes r WHERE {wh} AND n_steps = ?", args + [res[-1]["n_steps"]]).fetchone()[0]
        return res, ties
