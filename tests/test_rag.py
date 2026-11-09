"""Offline tests for the RAG layer (no network, no model download): python -m unittest discover -s tests"""
import io
import os
import tempfile
import unittest
import zipfile

os.environ["NOSH_EMBED"] = "hash"          # dependency-free lexical embeddings -> deterministic, no downloads

from nosh.assistant import Assistant, _norm_nums, md_table  # noqa: E402
from nosh.rag.chunk import TARGET, chunk_text, infer_type, table_chunks  # noqa: E402
from nosh.rag.docstore import DocStore  # noqa: E402
from nosh.rag.embed import Embedder  # noqa: E402
from nosh.rag.parse import UnsupportedFile, parse_file  # noqa: E402

POLICY = """# Returns
Items can be returned within 30 days of purchase with a receipt. Refunds go to the original payment method.

# Shipping
Orders ship within 2 business days. Express shipping costs 12 dollars and arrives in 3 days.

# Warranty
The machine has a 2 year warranty covering motor defects. Water damage is excluded.
"""
CSV = b"month,region,revenue,units\nJan,North,120000,30\nJan,South,90000,22\nFeb,North,150000,41\nFeb,South,70000,15\n"


def make_docx(paragraphs):
    body = "".join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs)
    xml = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


class Parsing(unittest.TestCase):
    def test_markdown_sections_keep_headings(self):
        d = parse_file("p.md", POLICY.encode())
        self.assertEqual([l for l, _ in d.sections], ["Returns", "Shipping", "Warranty"])

    def test_csv_becomes_table(self):
        d = parse_file("s.csv", CSV)
        self.assertEqual(d.kind, "table")
        self.assertEqual(d.tables[0].columns, ["month", "region", "revenue", "units"])
        self.assertEqual(len(d.tables[0].rows), 4)

    def test_docx(self):
        d = parse_file("r.docx", make_docx(["Revenue grew 12 percent.", "Costs fell."]))
        self.assertIn("Revenue grew", d.sections[0][1])

    def test_json_records_become_table(self):
        d = parse_file("x.json", b'[{"a":1,"b":"x"},{"a":2,"b":"y"}]')
        self.assertEqual(d.kind, "table")
        self.assertEqual(d.tables[0].columns, ["a", "b"])

    def test_rejects_binary_and_empty(self):
        with self.assertRaises(UnsupportedFile):
            parse_file("x.bin", bytes(range(256)) * 20)
        with self.assertRaises(UnsupportedFile):
            parse_file("e.txt", b"")


class Chunking(unittest.TestCase):
    def test_chunks_are_bounded_and_overlapping(self):
        text = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(30))
        cs = chunk_text(text, "sec")
        self.assertGreater(len(cs), 3)
        self.assertTrue(all(len(c.text) <= TARGET * 1.4 for c in cs))
        self.assertTrue(all(c.label == "sec" for c in cs))

    def test_long_paragraph_is_split(self):
        cs = chunk_text("x" * 5000)
        self.assertGreater(len(cs), 3)

    def test_type_inference_and_table_chunks(self):
        self.assertEqual(infer_type(["1", "2", "3"]), "INTEGER")
        self.assertEqual(infer_type(["1.5", "2"]), "REAL")
        self.assertEqual(infer_type(["a", "1"]), "TEXT")
        d = parse_file("s.csv", CSV)
        cs = table_chunks(d.tables[0])
        self.assertEqual(cs[0].kind, "schema")
        self.assertIn("revenue", cs[0].text)


class Store(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.ds = DocStore(os.path.join(cls.tmp, "t.db"))
        cls.pol = cls.ds.add("s1", "policy.md", POLICY.encode())
        cls.csv = cls.ds.add("s1", "sales.csv", CSV)

    def test_hybrid_retrieval_finds_the_right_section(self):
        r = self.ds.search("s1", "how many days do I have to return an item?", k=3)
        self.assertTrue(r.confident)
        self.assertEqual(r.hits[0].label, "Returns")
        r = self.ds.search("s1", "is water damage covered by the warranty", k=3)
        self.assertEqual(r.hits[0].label, "Warranty")

    def test_sessions_are_isolated(self):
        self.assertEqual(self.ds.search("other", "returns", k=3).hits, [])
        self.assertEqual(self.ds.list("other"), [])

    def test_unrelated_query_is_not_confident(self):
        r = self.ds.search("s1", "quantum chromodynamics lattice gauge", k=3)
        self.assertFalse(r.confident)

    def test_sql_runs_and_is_exact(self):
        t = self.csv["tables"][0]["sql_name"]
        cols, rows, trunc = self.ds.run_sql("s1", f'SELECT region, SUM(revenue) FROM "{t}" GROUP BY region ORDER BY 2 DESC')
        self.assertEqual(rows[0], ["North", 270000])
        self.assertFalse(trunc)

    def test_sql_is_locked_down(self):
        t = self.csv["tables"][0]["sql_name"]
        for bad in ("DROP TABLE " + t, "SELECT * FROM docs", "SELECT 1; SELECT 2", "PRAGMA table_info(docs)",
                    f'DELETE FROM "{t}"', "ATTACH DATABASE 'x.db' AS x"):
            with self.assertRaises(Exception, msg=bad):
                self.ds.run_sql("s1", bad)
        with self.assertRaises(Exception):                       # another session's tables are invisible
            self.ds.run_sql("other", f'SELECT * FROM "{t}"')

    def test_remove_cleans_up(self):
        info = self.ds.add("s2", "tmp.csv", CSV)
        t = info["tables"][0]["sql_name"]
        self.assertTrue(self.ds.remove("s2", info["id"]))
        self.assertEqual(self.ds.list("s2"), [])
        with self.assertRaises(Exception):
            self.ds.run_sql("s2", f'SELECT * FROM "{t}"')


class OfflineAssistant(unittest.TestCase):
    """Without an LLM the assistant still routes sensibly and answers extractively - it never invents text."""
    @classmethod
    def setUpClass(cls):
        cls.ds = DocStore(os.path.join(tempfile.mkdtemp(), "t.db"))
        cls.ds.add("s1", "policy.md", POLICY.encode())
        from nosh.store import Store as RecipeStore
        cls.store = RecipeStore()

    def answer(self, a, msg):
        route, out = "", ""
        for ev in a.respond(msg):
            if ev["t"] == "route":
                route = ev["route"]
            elif ev["t"] == "delta":
                out += ev["x"]
        return route, out

    def test_document_question_is_answered_from_the_document(self):
        a = Assistant(self.store, self.ds, None, "s1")
        route, out = self.answer(a, "What does the document say about returns within 30 days?")
        self.assertEqual(route, "docs")
        self.assertIn("30 days", out)

    def test_recipe_question_goes_to_the_engine(self):
        a = Assistant(self.store, self.ds, None, "nodocs")
        route, out = self.answer(a, "How many recipes contain both garlic and onion?")
        self.assertEqual(route, "recipe")
        from nosh.store import Filter
        self.assertIn(f"{self.store.count(Filter(include=('garlic', 'onion'))):,}", out)

    def test_general_question_without_llm_is_honest(self):
        a = Assistant(self.store, self.ds, None, "nodocs")
        route, out = self.answer(a, "hello there")
        self.assertEqual(route, "general")
        self.assertIn("GROQ_API_KEY", out)


class Guards(unittest.TestCase):
    def test_number_normalisation(self):
        n = _norm_nums("Revenue 4,200,000.50 and 12% growth")
        self.assertIn("4200000.50", n)
        self.assertIn("12", n)

    def test_md_table(self):
        t = md_table(["a", "b"], [[1, 2.123456], [3, "x|y"]])
        self.assertIn("| a | b |", t)
        self.assertIn("2.1235", t)
        self.assertIn("x/y", t)

    def test_hash_embeddings_are_normalised_and_similar_texts_are_close(self):
        e = Embedder("hash")
        v = e.embed(["return policy thirty days", "return policy within thirty days", "banana bread recipe"])
        self.assertAlmostEqual(float((v[0] * v[0]).sum()), 1.0, places=4)
        self.assertGreater(float(v[0] @ v[1]), float(v[0] @ v[2]))


if __name__ == "__main__":
    unittest.main()
