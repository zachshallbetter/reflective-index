"""The MCP server's JSON-RPC handling, without a subprocess."""
import json
import tempfile
import unittest
from pathlib import Path

from reflective_index.mcp import Server


class McpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "m.py").write_text("def a():\n    return b()\n\n\ndef b():\n    return 1\n")
        (root / "pets.py").write_text("class Robin:\n    def go(self):\n        return 1\n\n\n"
                                      "class Hale:\n    def go(self):\n        return 2\n")
        self.server = Server(str(root))
        self.server.index.cache_path = root / "cache.json"

    def tearDown(self):
        self.tmp.cleanup()

    def rpc(self, method, params=None, mid=1):
        return self.server.handle({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})

    def call(self, name, **args):
        return self.rpc("tools/call", {"name": name, "arguments": args})["result"]

    def test_initialize_and_list(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
        self.assertEqual(init["serverInfo"]["name"], "reflective-index")
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertIn("read_symbol", names)
        self.assertNotIn("replace_symbol", names)          # read-only unless --allow-edit
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools(self):
        self.assertIn("py:m.py::b", self.call("callees", symbol="a")["content"][0]["text"])
        self.assertIn("py:m.py::a", self.call("callers", symbol="b")["content"][0]["text"])
        self.assertIn("return b()", self.call("read_symbol", symbol="py:m.py::a")["content"][0]["text"])
        sl = json.loads(self.call("slice", symbol="a", radius=1)["content"][0]["text"])
        self.assertEqual(sl["target"], "py:m.py::a")
        bad = self.call("read_symbol", symbol="nope")
        self.assertTrue(bad["isError"])
        self.assertEqual(self.rpc("no/such")["error"]["code"], -32601)

    def test_an_ambiguous_name_answers_with_the_candidates(self):
        out = self.call("callers", symbol="go")
        self.assertTrue(out["isError"])
        text = out["content"][0]["text"]
        self.assertIn("py:pets.py::Robin.go", text)
        self.assertIn("py:pets.py::Hale.go", text)
        self.assertFalse(self.call("callees", symbol="Hale.go").get("isError"))

    def test_swift_calls_flag(self):
        self.assertFalse(self.server.index.swift_calls)
        self.assertTrue(Server(self.tmp.name, swift_calls=True).index.swift_calls)

    def test_edit_only_when_allowed(self):
        self.assertTrue(self.call("replace_symbol", symbol="a", source="def a():\n    return 2\n")["isError"])
        self.server.allow_edit = True
        out = json.loads(self.call("replace_symbol", symbol="a", source="def a():\n    return 2\n")["content"][0]["text"])
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
