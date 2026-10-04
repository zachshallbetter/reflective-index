"""The Python extractor, linking, queries, slices, replace and the CSG export, on a small throwaway repo."""
import json
import tempfile
import textwrap
import unittest
from pathlib import Path

from reflective_index import SCHEMA, Index
from reflective_index.graph import permits

FILES = {
    "pkg/__init__.py": "",
    "pkg/store.py": '''
        import os


        def load(path):
            return read(path)


        def read(path):
            return open(path).read()


        class Store:
            def get(self, key):
                return self._lookup(key)

            @staticmethod
            def _lookup(key):
                return key
    ''',
    "app.py": '''
        from pkg.store import load, Store
        import pkg.store as st


        def main():
            load("x")
            st.read("y")
            Store().get(1)
            unknown_call()

            def helper():
                return load("z")
            return helper()
    ''',
    "broken.py": "def oops(:\n",
}


class PythonIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for rel, src in FILES.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(src).lstrip("\n"))
        self.idx = Index(self.root, cache=self.root / "cache.json").build()

    def tearDown(self):
        self.tmp.cleanup()

    def test_nodes_and_kinds(self):
        ids = set(self.idx.nodes)
        for nid in ("py:pkg/store.py", "py:pkg/store.py::load", "py:pkg/store.py::Store",
                    "py:pkg/store.py::Store.get", "py:pkg/store.py::Store._lookup", "py:app.py::main.helper"):
            self.assertIn(nid, ids)
        self.assertEqual(self.idx.nodes["py:pkg/store.py::Store.get"]["kind"], "method")
        self.assertEqual(self.idx.nodes["py:pkg/store.py::Store"]["kind"], "class")
        self.assertEqual(self.idx.stats()["unparsed"], ["broken.py"])

    def test_decorators_are_in_the_span(self):
        span = self.idx.nodes["py:pkg/store.py::Store._lookup"]["span"]
        self.assertEqual(self.idx.read("py:pkg/store.py::Store._lookup", numbered=False).splitlines()[0].strip(), "@staticmethod")
        self.assertLess(span["start_line"], span["end_line"])

    def test_calls_resolve_only_when_certain(self):
        callees = {n["id"] for n in self.idx.callees("py:app.py::main")}
        self.assertEqual(callees, {"py:pkg/store.py::load", "py:pkg/store.py::read", "py:pkg/store.py::Store"})
        self.assertEqual({n["id"] for n in self.idx.callees("py:pkg/store.py::Store.get")}, {"py:pkg/store.py::Store._lookup"})
        # the nested helper's call belongs to the helper, not to main
        self.assertEqual({n["id"] for n in self.idx.callees("py:app.py::main.helper")}, {"py:pkg/store.py::load"})
        self.assertGreaterEqual(self.idx.unresolved, 1)          # unknown_call() is counted, not guessed
        callers = {n["id"] for n in self.idx.callers("py:pkg/store.py::load")}
        self.assertEqual(callers, {"py:app.py::main", "py:app.py::main.helper"})

    def test_imports_and_contains(self):
        rels = {(e["from"], e["to"], e["relationship"]) for e in self.idx.edges}
        self.assertIn(("py:app.py", "py:pkg/store.py", "imports"), rels)
        self.assertIn(("py:pkg/store.py::Store", "py:pkg/store.py::Store.get", "contains"), rels)

    def test_find_outline_and_node(self):
        self.assertEqual([n["name"] for n in self.idx.outline("pkg/store.py")], ["load", "read", "Store", "get", "_lookup"])
        self.assertEqual(self.idx.node("Store.get")["id"], "py:pkg/store.py::Store.get")
        self.assertIsNone(self.idx.node("nothing-like-this"))

    def test_slice_has_edges_and_respects_access(self):
        s = self.idx.slice("py:pkg/store.py::load", radius=1)
        self.assertIn("py:app.py::main", {n["id"] for n in s["nodes"]})
        self.assertTrue(all(e["from"] in {n["id"] for n in s["nodes"]} for e in s["edges"]))
        self.assertIsNone(self.idx.slice("py:pkg/store.py::load", access="public"))   # nodes are member-level
        self.assertTrue(permits("administrator", "member") and not permits("public", "member"))

    def test_replace_checks_before_writing(self):
        path = self.root / "pkg/store.py"
        before = path.read_text()
        bad = self.idx.replace("py:pkg/store.py::read", "def read(path:\n    return 1\n")
        self.assertFalse(bad["ok"])
        renamed = self.idx.replace("py:pkg/store.py::read", "def other(path):\n    return 1\n")
        self.assertFalse(renamed["ok"])
        self.assertEqual(path.read_text(), before)
        ok = self.idx.replace("py:pkg/store.py::read", "def read(path):\n    with open(path) as f:\n        return f.read()\n")
        self.assertTrue(ok["ok"], ok)
        self.assertIn("with open(path) as f:", path.read_text())
        self.assertIn("py:pkg/store.py::read", self.idx.nodes)
        # a method keeps its class indentation
        ok = self.idx.replace("py:pkg/store.py::Store.get", "def get(self, key):\n    return key\n")
        self.assertTrue(ok["ok"], ok)
        self.assertIn("    def get(self, key):\n        return key\n", path.read_text())

    def test_cache_and_csg(self):
        again = Index(self.root, cache=self.root / "cache.json").build()
        self.assertEqual(set(again.nodes), set(self.idx.nodes))
        g = json.loads(json.dumps(self.idx.csg()))
        self.assertEqual(g["schema_version"], SCHEMA)
        self.assertEqual(g["provenance"]["kind"], "extractor")
        self.assertTrue(g["provenance"]["source_hash"].startswith("sha256:"))
        ids = {n["id"] for n in g["nodes"]}
        self.assertTrue(all(e["from"] in ids and e["to"] in ids for e in g["edges"]))       # no dangling edges
        self.assertTrue(all(n["span"]["start_line"] <= n["span"]["end_line"] for n in g["nodes"]))


if __name__ == "__main__":
    unittest.main()
