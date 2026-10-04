"""Swift through sourcekit-lsp: structure per file (no build), and calls from a SwiftPM package's index store.
Skipped where the Swift toolchain isn't installed."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from reflective_index import swift_extractor
from reflective_index.graph import Index

FIX = Path(__file__).parent / "fixtures" / "swift"
HAS_SWIFT = shutil.which("xcrun") is not None or shutil.which("sourcekit-lsp") is not None


@unittest.skipUnless(HAS_SWIFT, "no Swift toolchain")
class TestSwiftStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = (FIX / "loose" / "Shapes.swift").read_text()
        cls.facts = swift_extractor.extract("loose/Shapes.swift", src, "member")
        cls.ids = {n["id"]: n for n in cls.facts["nodes"]}

    def test_ids_and_kinds(self):
        p = "swift:loose/Shapes.swift"
        want = {
            p: "module",
            f"{p}::Greeter": "protocol",
            f"{p}::Greeter.greet()": "method",
            f"{p}::Robin": "struct",
            f"{p}::Robin.init(name:)": "method",
            f"{p}::Robin.init()": "method",
            f"{p}::Robin.greet()": "method",
            f"{p}::Robin.helper()": "method",
            f"{p}::Robin.Mood": "enum",
            f"{p}::Robin.Mood.label()": "method",
            f"{p}::extension Robin": "extension",
            f"{p}::Robin.make()": "method",
            f"{p}::extension Robin #2": "extension",
            f"{p}::Robin.wave()": "method",
            f"{p}::Alex": "class",
            f"{p}::Alex.run()": "method",
            f"{p}::topLevel()": "function",
        }
        self.assertEqual({k: self.ids[k]["kind"] for k in want if k in self.ids}, want)
        self.assertNotIn(f"{p}::Robin.name", self.ids, "properties aren't nodes")

    def test_spans_and_containment(self):
        p = "swift:loose/Shapes.swift"
        robin = self.ids[f"{p}::Robin"]["span"]
        self.assertEqual((robin["start_line"], robin["end_line"]), (7, 17))
        self.assertIn([f"{p}::extension Robin", f"{p}::Robin.make()"], self.facts["contains"],
                      "a member from an extension belongs to its type, contained by the extension")
        self.assertIn([f"{p}::Robin", f"{p}::Robin.Mood"], self.facts["contains"])
        self.assertTrue(all(n["span"]["start_line"] <= n["span"]["end_line"] for n in self.facts["nodes"]))

    def test_in_the_index(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(FIX / "loose", Path(d) / "loose")
            idx = Index(d, cache=Path(d) / "c.json").build()
            self.assertEqual([n["id"] for n in idx.outline("loose/Shapes.swift")][:3],
                             ["swift:loose/Shapes.swift::Greeter", "swift:loose/Shapes.swift::Greeter.greet()",
                              "swift:loose/Shapes.swift::Robin"])
            self.assertIn("swift", idx.csg()["provenance"]["languages"])
            self.assertFalse(idx.replace("swift:loose/Shapes.swift::topLevel()", "func topLevel() -> Int {")["ok"],
                             "replace refuses Swift")


@unittest.skipUnless(HAS_SWIFT and os.environ.get("RI_SWIFT_CALLS", "1") != "0", "no Swift toolchain")
class TestSwiftCalls(unittest.TestCase):
    def test_calls_from_the_index_store(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(FIX / "Pkg", Path(d) / "Pkg")
            idx = Index(d, cache=Path(d) / ".ri" / "c.json", swift_calls=True).build()
            p = "swift:Pkg/Sources/Pkg/Calls.swift"
            calls = {(e["from"], e["to"]) for e in idx.edges if e["relationship"] == "calls"}
            self.assertIn((f"{p}::Jordan.plan()", f"{p}::Jordan.step()"), calls)
            self.assertIn((f"{p}::Jordan.plan()", f"{p}::Leo.count()"), calls)
            self.assertIn((f"{p}::Jordan.step()", f"{p}::helper(_:)"), calls)
            self.assertNotIn(f"{p}::Leo.count()", {a for a, _ in calls}, "Array.count is the SDK's: no edge")
            self.assertEqual([n["id"] for n in idx.callers(f"{p}::Jordan.step()")], [f"{p}::Jordan.plan()"])
            self.assertIn((f"{p}::Jordan.viaSelf()", f"{p}::Jordan.note()"), calls, "a static call through Self")
            # A computed property is a node; what it names reaches the callee as a reference, and the answer says so.
            self.assertEqual(idx.nodes[f"{p}::Jordan.total"]["kind"], "property")
            self.assertIn(f"{p}::Jordan.total", {n["id"] for n in idx.referrers(f"{p}::helper(_:)")})
            report = idx.report(f"{p}::helper(_:)", "callers")
            self.assertIn("names it without a resolved call", report)
            self.assertIn("other targets", report)
            self.assertIn("aren't traced", idx.report(f"{p}::Jordan.total", "callees"))
            # Unchanged files: the edges come from the cache, without another build.
            again = Index(d, cache=Path(d) / ".ri" / "c.json", swift_calls=True).build()
            self.assertEqual({(e["from"], e["to"]) for e in again.edges if e["relationship"] == "calls"}, calls)


if __name__ == "__main__":
    unittest.main()
