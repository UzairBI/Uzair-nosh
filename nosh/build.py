"""Build the SQLite database (recipes + parsed ingredients + FTS5 index) from the three raw JSON files.

    python -m nosh.build            # reads data/recipes_raw_nosource_*.json, writes data/nosh.db
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time

from . import ingredients as ing

DATA_DIR = os.environ.get("NOSH_DATA", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
DB_PATH = os.path.join(DATA_DIR, "nosh.db")
FILES = {"ar": "recipes_raw_nosource_ar.json", "epi": "recipes_raw_nosource_epi.json", "fn": "recipes_raw_nosource_fn.json"}
SRC_NAMES = {"ar": "AllRecipes", "epi": "Epicurious", "fn": "Food Network"}

_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


JUNK_TITLES = {"title", "untitled", "no title", "recipe", "recipe title", "new recipe", "test", "test recipe", "tst", "qa",
               "sample", "example", "asdf", "n/a", "na", "none", "null", "tbd", "lorem ipsum"}


def is_junk(title: str, instructions: str) -> bool:
    """Placeholder / test records scraped from the source sites ('Title', 'Tst', 'QA', instructions that are just ';')."""
    t = re.sub(r"[\s.:\-_!]+", " ", title.lower()).strip()
    if t in JUNK_TITLES or not re.search(r"[A-Za-z]", title):
        return True
    return len(re.findall(r"[A-Za-z]", instructions)) < 12      # no real instructions


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def is_junk_content(title: str, instructions: str, lines: list, named_lines: int) -> bool:
    """Records whose body is not real recipe data: no ingredient has a name ('1 cup'), the ingredients or the instructions are just
    the title repeated ('Blooming Color Centerpiece' / 'no recipes available'), or the instructions are keyboard mash / one repeated word."""
    t = _norm(title)
    if named_lines == 0:
        return True
    if re.match(r"(no recipes? available|qa ?test\w*)$", t):
        return True
    if lines and all(_norm(x) == t for x in lines):
        return True
    ins = _norm(instructions)
    if ins == t or ins.replace(t, "").strip() == "":
        return True
    return len(set(re.findall(r"[a-z]{2,}", instructions.lower()))) < 3        # 'method method method', 'sdkfhiashf;hSAFASF'


def split_steps(instructions: str) -> list[str]:
    """Step-splitting rule (documented in README): one step per non-empty line; if the whole text is a
    single line, one step per sentence."""
    lines = [l for l in (x.strip() for x in instructions.split("\n")) if l]
    if len(lines) != 1:
        return lines
    parts = [s.strip() for s in _SENT.split(lines[0]) if s.strip()]
    return parts or lines


def count_steps(instructions: str) -> int:
    return len(split_steps(instructions))


SCHEMA = """
PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
CREATE TABLE recipes(rid INTEGER PRIMARY KEY, id TEXT UNIQUE, src TEXT, title TEXT, instructions TEXT,
                     n_ing INTEGER, n_core INTEGER, n_steps INTEGER, flags INTEGER, uflags INTEGER, dup INTEGER DEFAULT 0);
CREATE TABLE ing(rid INTEGER, idx INTEGER, raw TEXT, qty REAL, unit TEXT, name TEXT, alts TEXT,
                 flags INTEGER, uflags INTEGER, staple INTEGER);
CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
"""


def build(data_dir: str = DATA_DIR, db_path: str = DB_PATH, limit: int | None = None) -> None:
    t0 = time.time()
    if os.path.exists(db_path):
        os.remove(db_path)
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    rid = 0
    junk = 0
    dropped = {k: 0 for k in FILES}
    seen = {}
    dup = 0
    raw_counts = {}
    for src, fname in FILES.items():
        path = os.path.join(data_dir, fname)
        if not os.path.exists(path):
            sys.exit(f"missing {path} - run `python scripts/download_data.py` first")
        with open(path, encoding="utf8") as f:
            data = json.load(f)
        raw_counts[src] = len(data)
        rows, irows = [], []
        for n, (key, r) in enumerate(data.items()):
            if limit and n >= limit:
                break
            title = (r.get("title") or "").strip()
            instr = (r.get("instructions") or "").replace("\r", "").strip()
            raw_lines = r.get("ingredients") or []
            lines = [c for c in (ing.clean_line(x) for x in raw_lines) if c]
            if not title or not instr or not lines:
                dropped[src] += 1
                continue
            rid += 1                           # junk records still consume an id, so ids stay aligned with recipe_vec.npz
            if is_junk(title, instr):
                dropped[src] += 1
                junk += 1
                continue
            sflags = suflags = 0
            n_core = 0
            parsed = []
            for i, ln in enumerate(lines):
                q, u, alts = ing.parse_line(ln)
                ts, tm = ing.tail_groups(ln)           # conservative: flags from every segment/alternative of the line
                if not alts:
                    sflags |= ts
                    suflags |= tm
                    parsed.append((rid, i, ln, q, u, "", "", ts, tm, 0))
                    continue
                s, m = ing.line_groups(alts)
                s, m = s | ts, (m | tm) & ~(s | ts)
                stp = 1 if ing.is_staple(alts) else 0
                n_core += 0 if stp else 1
                sflags |= s
                suflags |= m
                parsed.append((rid, i, ln, q, u, alts[0], " " + " | ".join(alts) + " ", s, m, stp))
            if is_junk_content(title, instr, lines, sum(1 for p in parsed if p[5])):
                dropped[src] += 1
                junk += 1
                continue
            sig = (title.lower(), tuple(sorted(p[5] for p in parsed)))
            d = 1 if sig in seen else 0
            seen[sig] = 1
            dup += d
            rows.append((rid, f"{src}:{key}", src, title, instr, len(lines), n_core, count_steps(instr),
                         sflags, suflags & ~sflags, d))
            irows.extend(parsed)
        db.executemany("INSERT INTO recipes VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT INTO ing VALUES (?,?,?,?,?,?,?,?,?,?)", irows)
        db.commit()
        print(f"  {src}: {raw_counts[src]} raw, {dropped[src]} dropped (missing title/ingredients/instructions)  [{time.time()-t0:.0f}s]", flush=True)
        del data
    print("indexing ...", flush=True)
    db.executescript("""
        CREATE INDEX ing_rid ON ing(rid);
        CREATE INDEX ing_name ON ing(name);
        CREATE INDEX rec_src ON recipes(src);
        CREATE VIRTUAL TABLE fts USING fts5(title, ingredients, tokenize='porter unicode61', content='', contentless_delete=0);
    """)
    db.execute("""INSERT INTO fts(rowid, title, ingredients)
                  SELECT r.rid, r.title, (SELECT group_concat(replace(replace(name,'_',' '),'|',' '),' ') FROM ing WHERE ing.rid=r.rid)
                  FROM recipes r""")
    db.execute("INSERT INTO meta VALUES('raw_counts', ?)", (json.dumps(raw_counts),))
    db.execute("INSERT INTO meta VALUES('dropped', ?)", (json.dumps(dropped),))
    db.execute("INSERT INTO meta VALUES('exact_duplicates', ?)", (str(dup),))
    db.execute("INSERT INTO meta VALUES('junk_dropped', ?)", (str(junk),))
    print(f"  placeholder/test records dropped: {junk}")
    db.commit()
    db.execute("ANALYZE")
    db.close()
    print(f"done: {rid - junk} recipes in {time.time()-t0:.0f}s -> {db_path}")


if __name__ == "__main__":
    build(limit=int(sys.argv[1]) if len(sys.argv) > 1 else None)
