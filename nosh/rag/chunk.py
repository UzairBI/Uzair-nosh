"""Structure-aware chunking.

Text: paragraphs are packed into ~TARGET-char chunks with sentence overlap; headings/pages stay attached as `label`.
Tables: one schema chunk (columns, types, ranges, sample rows) + row-group chunks (header repeated), capped so huge
datasets stay cheap - exact analytics on tables go through SQL (see analytics.py), not through chunks.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TARGET = 900        # characters per chunk (~200 tokens)
OVERLAP = 160
MAX_ROW_CHUNKS = 3000
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


@dataclass
class Chunk:
    text: str
    label: str = ""
    kind: str = "text"   # text | schema | rows


def _units(text: str) -> list[str]:
    """Paragraphs; over-long paragraphs are split into sentences (and, as a last resort, hard-wrapped)."""
    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        para = re.sub(r"[ \t]+", " ", para.replace("\r", "")).strip()
        if not para:
            continue
        if len(para) <= TARGET:
            out.append(para)
            continue
        for s in _SENT.split(para):
            while len(s) > TARGET * 1.3:
                cut = s.rfind(" ", 0, TARGET)
                cut = cut if cut > TARGET // 2 else TARGET
                out.append(s[:cut].strip())
                s = s[cut:].strip()
            if s:
                out.append(s)
    return out


def chunk_text(text: str, label: str = "") -> list[Chunk]:
    units = _units(text)
    chunks, cur, size = [], [], 0
    for u in units:
        if cur and size + len(u) + 1 > TARGET:
            chunks.append("\n".join(cur))
            # overlap: carry the tail of the previous chunk
            tail, tl = [], 0
            for x in reversed(cur):
                if tl + len(x) > OVERLAP:
                    break
                tail.insert(0, x)
                tl += len(x)
            cur, size = tail, tl
        cur.append(u)
        size += len(u) + 1
    if cur and "\n".join(cur).strip():
        last = "\n".join(cur)
        if chunks and len(last) < 120 and chunks[-1].endswith(last[-60:]):
            pass
        else:
            chunks.append(last)
    return [Chunk(c, label) for c in chunks if c.strip()]


def chunk_sections(sections) -> list[Chunk]:
    out = []
    for label, text in sections:
        out += chunk_text(text, label)
    return out


# ---------------------------------------------------------------------------------------------- tables
def infer_type(values) -> str:
    vals = [str(v).strip() for v in values if str(v).strip() != ""][:500]
    if not vals:
        return "TEXT"
    def isint(x):
        try:
            int(x.replace(",", ""))
            return "." not in x
        except Exception:
            return False
    def isfloat(x):
        try:
            float(x.replace(",", "").rstrip("%"))
            return True
        except Exception:
            return False
    if all(isint(v) for v in vals):
        return "INTEGER"
    if all(isfloat(v) for v in vals):
        return "REAL"
    return "TEXT"


def table_chunks(table, rows_per_chunk: int = 12) -> list[Chunk]:
    cols = table.columns
    n = len(table.rows)
    types = {c: infer_type([r[i] for r in table.rows[:500]]) for i, c in enumerate(cols)}
    lines = [f"Table '{table.name}': {n:,} rows, {len(cols)} columns.", "Columns: " + ", ".join(f"{c} ({types[c].lower()})" for c in cols)]
    for i, c in enumerate(cols):
        vals = [r[i] for r in table.rows if str(r[i]).strip() != ""]
        if types[c] in ("INTEGER", "REAL") and vals:
            try:
                nums = [float(str(v).replace(",", "").rstrip("%")) for v in vals]
                lines.append(f"- {c}: min {min(nums):g}, max {max(nums):g}, mean {sum(nums) / len(nums):.4g}")
            except Exception:
                pass
        else:
            distinct = list(dict.fromkeys(str(v) for v in vals[:3000]))
            if 0 < len(distinct) <= 12:
                lines.append(f"- {c}: values {', '.join(distinct)}")
    lines.append("Sample rows:")
    for r in table.rows[:3]:
        lines.append(" | ".join(str(x)[:60] for x in r))
    chunks = [Chunk("\n".join(lines), f"schema of {table.name}", "schema")]
    step = rows_per_chunk
    if n > MAX_ROW_CHUNKS * step:
        step = -(-n // MAX_ROW_CHUNKS)      # ceil: keep chunk count bounded for very large tables
    head = " | ".join(cols)
    for i in range(0, n, step):
        part = table.rows[i:i + step]
        body = "\n".join(" | ".join(str(x)[:80] for x in r) for r in part)
        chunks.append(Chunk(f"{head}\n{body}", f"{table.name} rows {i + 1}-{i + len(part)}", "rows"))
    return chunks
