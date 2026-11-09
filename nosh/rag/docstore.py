"""Per-session document store: chunks + embeddings + FTS5 in SQLite, hybrid retrieval, and safe SQL over uploaded tables.

Retrieval = dense cosine (MiniLM) + BM25 (FTS5), fused with reciprocal-rank fusion, near-duplicate suppression and
neighbour expansion.  Uploaded tables are also loaded as real SQLite tables so quantitative questions are answered by
executing SQL (read-only, authorizer-restricted), never by an LLM doing arithmetic.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field

import numpy as np

from ..build import DATA_DIR
from .chunk import Chunk, chunk_sections, infer_type, table_chunks
from .embed import get_embedder
from .parse import ParsedDoc, parse_file

DB_PATH = os.path.join(DATA_DIR, "docs.db")
STOP = set("""a an the and or of to in on for with is are was were be been it this that these those what which who whom how when where why
do does did can could would should will about from by as at into than then there their its it's i me my you your we our please tell show give
find explain say said mention mentioned document documents file files pdf""".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY, sid TEXT, name TEXT, kind TEXT, size INTEGER, n_chunks INTEGER,
    n_rows INTEGER, tables TEXT, warnings TEXT, model TEXT, created REAL);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, ord INTEGER, label TEXT, kind TEXT, text TEXT, emb BLOB);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(text, label, content='chunks', content_rowid='id', tokenize='porter unicode61');
"""


@dataclass
class Hit:
    chunk_id: int
    doc_id: str
    doc_name: str
    label: str
    text: str
    score: float
    dense: float = 0.0
    fts: bool = False
    kind: str = "text"


@dataclass
class Retrieval:
    hits: list = field(default_factory=list)
    confident: bool = False
    best_dense: float = 0.0


def _sql_ident(name: str, used: set) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name).strip().lower()).strip("_") or "col"
    if s[0].isdigit():
        s = "c_" + s
    base, i = s, 2
    while s in used:
        s = f"{base}_{i}"
        i += 1
    used.add(s)
    return s


def _convert(v, typ):
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        if typ == "INTEGER":
            return int(s.replace(",", ""))
        if typ == "REAL":
            return float(s.replace(",", "").rstrip("%"))
    except Exception:
        return None
    return s


class DocStore:
    def __init__(self, path: str = DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.emb = get_embedder()
        self._cache: dict[str, tuple] = {}

    # ------------------------------------------------------------------ documents
    def list(self, sid: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM docs WHERE sid=? ORDER BY created", (sid,)).fetchall()
        out = []
        for r in rows:
            first = self.db.execute("SELECT text FROM chunks WHERE doc_id=? ORDER BY ord LIMIT 1", (r["id"],)).fetchone()
            out.append({"preview": (first[0][:160].replace("\n", " ") if first else ""), "id": r["id"], "name": r["name"], "kind": r["kind"], "size": r["size"], "chunks": r["n_chunks"],
                        "rows": r["n_rows"], "tables": json.loads(r["tables"] or "[]"), "warnings": json.loads(r["warnings"] or "[]")})
        return out

    def add(self, sid: str, name: str, data: bytes) -> dict:
        parsed = parse_file(name, data)
        chunks: list[Chunk] = chunk_sections(parsed.sections)
        tables_meta, n_rows = [], 0
        doc_id = uuid.uuid4().hex[:12]
        for k, t in enumerate(parsed.tables):
            chunks += table_chunks(t)
            n_rows += len(t.rows)
            tables_meta.append(self._load_table(doc_id, k, t))
        if not chunks:
            raise ValueError("nothing to index")
        # contextual header so a chunk is findable by its document/section name too
        texts = [f"{name}" + (f" - {c.label}" if c.label else "") + f"\n{c.text}" for c in chunks]
        vecs = self.emb.embed(texts, batch_size=64)
        with self.lock:
            self.db.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (doc_id, sid, name, parsed.kind, len(data), len(chunks), n_rows, json.dumps(tables_meta),
                             json.dumps(parsed.warnings), self.emb.name, time.time()))
            for i, (c, v) in enumerate(zip(chunks, vecs)):
                cur = self.db.execute("INSERT INTO chunks(doc_id, ord, label, kind, text, emb) VALUES (?,?,?,?,?,?)",
                                      (doc_id, i, c.label, c.kind, c.text, v.astype(np.float32).tobytes()))
                self.db.execute("INSERT INTO chunk_fts(rowid, text, label) VALUES (?,?,?)", (cur.lastrowid, c.text, f"{name} {c.label}"))
            self.db.commit()
            self._cache.pop(sid, None)
        return next(d for d in self.list(sid) if d["id"] == doc_id)

    def remove(self, sid: str, doc_id: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT tables FROM docs WHERE id=? AND sid=?", (doc_id, sid)).fetchone()
            if not row:
                return False
            for t in json.loads(row["tables"] or "[]"):
                self.db.execute(f'DROP TABLE IF EXISTS "{t["sql_name"]}"')
            ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
            for cid in ids:
                self.db.execute("INSERT INTO chunk_fts(chunk_fts, rowid, text, label) SELECT 'delete', id, text, label FROM chunks WHERE id=?", (cid,))
            self.db.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            self.db.execute("DELETE FROM docs WHERE id=?", (doc_id,))
            self.db.commit()
            self._cache.pop(sid, None)
        return True

    def purge(self, days: float = 14.0) -> int:
        """Delete documents (and their tables/vectors) older than `days`; called at server start."""
        cutoff = time.time() - days * 86400
        with self.lock:
            old = [(r["sid"], r["id"]) for r in self.db.execute("SELECT sid, id FROM docs WHERE created < ?", (cutoff,))]
        for sid, did in old:
            self.remove(sid, did)
        return len(old)

    def clear(self, sid: str):
        for d in self.list(sid):
            self.remove(sid, d["id"])

    # ------------------------------------------------------------------ tables
    def _load_table(self, doc_id: str, k: int, t) -> dict:
        used: set = set()
        cols = []
        for i, c in enumerate(t.columns):
            typ = infer_type([r[i] for r in t.rows[:2000]])
            cols.append({"name": str(c), "sql": _sql_ident(c, used), "type": typ})
        tname = f"tbl_{doc_id}_{k}"
        with self.lock:
            self.db.execute(f'CREATE TABLE "{tname}" (' + ", ".join(f'"{c["sql"]}" {c["type"]}' for c in cols) + ")")
            ph = ",".join("?" * len(cols))
            batch = []
            for r in t.rows:
                batch.append([_convert(r[i], cols[i]["type"]) for i in range(len(cols))])
                if len(batch) >= 5000:
                    self.db.executemany(f'INSERT INTO "{tname}" VALUES ({ph})', batch)
                    batch = []
            if batch:
                self.db.executemany(f'INSERT INTO "{tname}" VALUES ({ph})', batch)
            self.db.commit()
        return {"sql_name": tname, "display": t.name, "columns": cols, "n_rows": len(t.rows)}

    def table_schema(self, sid: str) -> str:
        """Compact schema description (with value hints) handed to the text-to-SQL prompt."""
        parts = []
        for d in self.list(sid):
            for t in d["tables"]:
                parts.append(f'TABLE "{t["sql_name"]}"  -- from file "{d["name"]}" ({t["display"]}), {t["n_rows"]:,} rows')
                for c in t["columns"]:
                    hint = ""
                    with self.lock:
                        if c["type"] in ("INTEGER", "REAL"):
                            r = self.db.execute(f'SELECT MIN("{c["sql"]}"), MAX("{c["sql"]}") FROM "{t["sql_name"]}"').fetchone()
                            hint = f"  range {r[0]} .. {r[1]}"
                        else:
                            vals = [x[0] for x in self.db.execute(f'SELECT DISTINCT "{c["sql"]}" FROM "{t["sql_name"]}" LIMIT 13')]
                            if len(vals) <= 12:
                                hint = "  values: " + ", ".join(repr(v) for v in vals[:12])
                    parts.append(f'  "{c["sql"]}" {c["type"]}  -- {c["name"]}{hint}')
                with self.lock:
                    rows = self.db.execute(f'SELECT * FROM "{t["sql_name"]}" LIMIT 3').fetchall()
                parts.append("  sample rows: " + " ; ".join(str(tuple(r)) for r in rows)[:600])
        return "\n".join(parts)

    def has_tables(self, sid: str) -> bool:
        return any(d["tables"] for d in self.list(sid))

    def run_sql(self, sid: str, sql: str, limit: int = 60, timeout: float = 6.0):
        """Execute ONE read-only SELECT over this session's tables. Returns (columns, rows, truncated)."""
        sql = sql.strip().rstrip(";").strip()
        if not re.match(r"(?is)^(select|with)\b", sql) or ";" in sql or not sqlite3.complete_statement(sql + ";"):
            raise ValueError("only a single SELECT statement is allowed")
        allowed = {t["sql_name"] for d in self.list(sid) for t in d["tables"]}
        if not allowed:
            raise ValueError("no tables are available")
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        try:
            def auth(action, a1, a2, dbname, src):
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_READ and a1 in allowed:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            conn.set_authorizer(auth)
            deadline = time.time() + timeout
            conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 20000)
            cur = conn.execute(sql)
            cols = [c[0] for c in cur.description or []]
            rows = cur.fetchmany(limit + 1)
            return cols, [list(r) for r in rows[:limit]], len(rows) > limit
        finally:
            conn.close()

    # ------------------------------------------------------------------ retrieval
    def _matrix(self, sid: str):
        if sid in self._cache:
            return self._cache[sid]
        with self.lock:
            rows = self.db.execute("SELECT c.id, c.emb FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE d.sid=? AND d.model=?",
                                   (sid, self.emb.name)).fetchall()
        if not rows:
            self._cache[sid] = ([], np.zeros((0, self.emb.dim), dtype=np.float32))
        else:
            ids = [r[0] for r in rows]
            m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
            self._cache[sid] = (ids, m)
        return self._cache[sid]

    @staticmethod
    def _fts_query(q: str) -> str | None:
        toks = [t for t in re.findall(r"[A-Za-z0-9]+", q.lower()) if t not in STOP and len(t) > 1]
        toks = list(dict.fromkeys(toks))[:16]
        return " OR ".join(f'"{t}"' for t in toks) if toks else None

    def search(self, sid: str, query: str, k: int = 6, doc_ids: list | None = None, expand: bool = True) -> Retrieval:
        ids, M = self._matrix(sid)
        if not ids:
            return Retrieval()
        allowed = None
        if doc_ids:
            with self.lock:
                q = ",".join("?" * len(doc_ids))
                allowed = {r[0] for r in self.db.execute(f"SELECT id FROM chunks WHERE doc_id IN ({q})", doc_ids)}
        qv = self.emb.embed_query(query)
        sims = M @ qv
        order = np.argsort(-sims)[:60]
        dense = [(ids[i], float(sims[i])) for i in order if allowed is None or ids[i] in allowed][:40]
        dense_map = dict(dense)
        fq = self._fts_query(query)
        bm: list[int] = []
        if fq:
            with self.lock:
                try:
                    rows = self.db.execute(
                        "SELECT f.rowid FROM chunk_fts f JOIN chunks c ON c.id=f.rowid JOIN docs d ON d.id=c.doc_id "
                        "WHERE chunk_fts MATCH ? AND d.sid=? ORDER BY bm25(chunk_fts, 1.0, 0.4) LIMIT 40", (fq, sid)).fetchall()
                    bm = [r[0] for r in rows if allowed is None or r[0] in allowed]
                except sqlite3.OperationalError:
                    bm = []
        # reciprocal-rank fusion
        fused: dict[int, float] = {}
        for rank, (cid, _) in enumerate(dense):
            fused[cid] = fused.get(cid, 0) + 1.0 / (60 + rank)
        for rank, cid in enumerate(bm):
            fused[cid] = fused.get(cid, 0) + 1.0 / (60 + rank)
        ranked = sorted(fused, key=lambda c: -fused[c])
        # near-duplicate suppression using the dense vectors
        pos = {cid: i for i, cid in enumerate(ids)}
        chosen: list[int] = []
        for cid in ranked:
            if all(float(M[pos[cid]] @ M[pos[o]]) < 0.93 for o in chosen):
                chosen.append(cid)
            if len(chosen) >= k:
                break
        best = max([dense_map.get(c, 0.0) for c in chosen] or [0.0])
        bm_set = set(bm)
        hits = self._fetch(chosen, fused, dense_map, bm_set)
        if expand:
            hits = self._expand(hits[:2]) + hits[2:]
        confident = best >= (0.30 if self.emb.backend != "hash" else 0.18) or any(h.fts for h in hits[:3])
        return Retrieval(hits, confident, best)

    def _fetch(self, cids, fused, dense_map, bm_set) -> list[Hit]:
        if not cids:
            return []
        with self.lock:
            q = ",".join("?" * len(cids))
            rows = {r["id"]: r for r in self.db.execute(
                f"SELECT c.id, c.doc_id, c.label, c.text, c.kind, d.name FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE c.id IN ({q})", cids)}
        return [Hit(c, rows[c]["doc_id"], rows[c]["name"], rows[c]["label"], rows[c]["text"], fused.get(c, 0.0), dense_map.get(c, 0.0),
                    c in bm_set, rows[c]["kind"]) for c in cids if c in rows]

    def _expand(self, hits: list[Hit]) -> list[Hit]:
        """Add the neighbouring chunk (previous one) to top hits so answers aren't cut mid-thought."""
        out = []
        for h in hits:
            if h.kind != "text":
                out.append(h)
                continue
            with self.lock:
                row = self.db.execute("SELECT ord FROM chunks WHERE id=?", (h.chunk_id,)).fetchone()
                nxt = self.db.execute("SELECT text FROM chunks WHERE doc_id=? AND ord=? AND kind='text'", (h.doc_id, row["ord"] + 1)).fetchone() if row else None
            if nxt and len(h.text) + len(nxt[0]) < 2400:
                h = Hit(h.chunk_id, h.doc_id, h.doc_name, h.label, h.text + "\n" + nxt[0], h.score, h.dense, h.fts, h.kind)
            out.append(h)
        return out
