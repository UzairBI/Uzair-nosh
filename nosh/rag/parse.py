"""Document parsing: bytes + filename -> ParsedDoc (text sections and/or tables).

Supported: txt md rst log, pdf (pypdf), docx (stdlib zip/xml), html/htm, csv/tsv, xlsx (openpyxl), json/jsonl.
Parsing never guesses: if a file has no extractable text (e.g. a scanned PDF) it says so in `warnings`.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

MAX_BYTES = 40 * 1024 * 1024
TEXT_EXT = {"txt", "md", "markdown", "rst", "log", "text"}


@dataclass
class Table:
    name: str
    columns: list
    rows: list          # list of lists (strings or python scalars)


@dataclass
class ParsedDoc:
    name: str
    kind: str                                       # "text" | "table" | "mixed"
    sections: list = field(default_factory=list)    # [(label, text)]
    tables: list = field(default_factory=list)      # [Table]
    warnings: list = field(default_factory=list)


class UnsupportedFile(Exception):
    pass


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


_HEAD = re.compile(r"^(#{1,6})\s+(.*)$", re.M)


def _md_sections(text: str):
    """Split on markdown headings, keeping a breadcrumb label."""
    matches = list(_HEAD.finditer(text))
    if not matches:
        return [("", text)]
    out, trail = [], []
    if matches[0].start() > 0 and text[:matches[0].start()].strip():
        out.append(("", text[:matches[0].start()]))
    for i, m in enumerate(matches):
        level, title = len(m.group(1)), m.group(2).strip()
        trail = trail[:level - 1] + [title]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((" > ".join(trail), text[m.end():end]))
    return out


class _Strip(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip, self.title = [], 0, ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            self.parts.append("\n## ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _read_table_text(text: str, name: str, delim: str | None = None) -> Table:
    sample = text[:20000]
    if delim is None:
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except Exception:
            delim = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise UnsupportedFile("the table is empty")
    header = [h.strip() or f"col{i + 1}" for i, h in enumerate(rows[0])]
    body = [r + [""] * (len(header) - len(r)) if len(r) < len(header) else r[:len(header)] for r in rows[1:]]
    return Table(name, header, body)


def parse_file(name: str, data: bytes) -> ParsedDoc:
    if len(data) > MAX_BYTES:
        raise UnsupportedFile(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if not data:
        raise UnsupportedFile("the file is empty")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else "txt"
    doc = ParsedDoc(name=name, kind="text")

    if ext in TEXT_EXT:
        doc.sections = _md_sections(_decode(data))
    elif ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise UnsupportedFile("PDF support needs the 'pypdf' package (pip install pypdf)")
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise UnsupportedFile("the PDF is password-protected")
        for i, page in enumerate(reader.pages, 1):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                doc.sections.append((f"p.{i}", t))
        if not doc.sections:
            doc.warnings.append("No text could be extracted - this looks like a scanned/image-only PDF (OCR is not enabled).")
    elif ext == "docx":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ET.fromstring(z.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        W = "{%s}" % ns["w"]
        lines = []
        body = root.find("w:body", ns)
        for el in (body if body is not None else []):
            if el.tag == W + "p":
                txt = "".join(t.text or "" for t in el.iter(W + "t")).strip()
                st = el.find("w:pPr/w:pStyle", ns)
                style = (st.get(W + "val") if st is not None else "") or ""
                if txt:
                    lines.append(("## " + txt) if style.lower().startswith("heading") else txt)
            elif el.tag == W + "tbl":
                for tr in el.iter(W + "tr"):
                    cells = ["".join(t.text or "" for t in tc.iter(W + "t")).strip() for tc in tr.iter(W + "tc")]
                    lines.append(" | ".join(cells))
        doc.sections = _md_sections("\n\n".join(lines))
    elif ext in ("html", "htm"):
        p = _Strip()
        p.feed(_decode(data))
        doc.sections = _md_sections(re.sub(r"\n{3,}", "\n\n", "".join(p.parts)))
    elif ext in ("csv", "tsv"):
        doc.kind = "table"
        doc.tables = [_read_table_text(_decode(data), name.rsplit(".", 1)[0], "\t" if ext == "tsv" else None)]
    elif ext in ("xlsx", "xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise UnsupportedFile("Excel support needs the 'openpyxl' package (pip install openpyxl)")
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows = [["" if c is None else c for c in r] for r in ws.iter_rows(values_only=True)]
            rows = [r for r in rows if any(str(c).strip() for c in r)]
            if len(rows) < 1:
                continue
            header = [str(h).strip() or f"col{i + 1}" for i, h in enumerate(rows[0])]
            body = [list(r) + [""] * (len(header) - len(r)) if len(r) < len(header) else list(r)[:len(header)] for r in rows[1:]]
            doc.tables.append(Table(f"{name.rsplit('.', 1)[0]}_{ws.title}", header, body))
        if not doc.tables:
            raise UnsupportedFile("the workbook has no data")
        doc.kind = "table"
    elif ext in ("json", "jsonl", "ndjson"):
        text = _decode(data)
        try:
            obj = [json.loads(l) for l in text.splitlines() if l.strip()] if ext != "json" else json.loads(text)
        except Exception as e:
            raise UnsupportedFile(f"invalid JSON ({e})")
        if isinstance(obj, dict) and len(obj) and all(isinstance(v, dict) for v in obj.values()):
            obj = [{"_key": k, **v} for k, v in obj.items()]          # {id: {...}} -> rows (like the recipe files)
        if isinstance(obj, list) and obj and all(isinstance(r, dict) for r in obj):
            cols = list(dict.fromkeys(k for r in obj[:2000] for k in r))
            rows = [[json.dumps(r.get(c), ensure_ascii=False) if isinstance(r.get(c), (list, dict)) else r.get(c, "") for c in cols] for r in obj]
            doc.kind = "table"
            doc.tables = [Table(name.rsplit(".", 1)[0], cols, rows)]
        else:
            doc.sections = [("", json.dumps(obj, indent=2, ensure_ascii=False))]
    else:
        # unknown extension: accept it only if it decodes as mostly-printable text
        text = _decode(data)
        if sum(ch.isprintable() or ch in "\n\t\r" for ch in text[:5000]) < 0.95 * max(1, len(text[:5000])):
            raise UnsupportedFile(f"'.{ext}' files are not supported (try PDF, DOCX, TXT, MD, CSV, XLSX, JSON or HTML)")
        doc.sections = _md_sections(text)

    if doc.kind == "text" and not any(t.strip() for _, t in doc.sections) and not doc.warnings:
        raise UnsupportedFile("no readable text found in the file")
    return doc
