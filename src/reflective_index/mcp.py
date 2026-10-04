"""A Model Context Protocol server over stdio (newline-delimited JSON-RPC 2.0), standard library only.

  reflective-index-mcp ROOT [--allow-edit]

Tools: outline, find, callers, callees, read_symbol, slice, stats, and with --allow-edit replace_symbol.
Read-only by default. The index is rebuilt before each call, which costs a cache check unless files changed.

An MCP client, in a project's .mcp.json:
  {"mcpServers": {"reflective-index": {"command": "reflective-index-mcp", "args": ["."]}}}
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .cli import brief
from .graph import Index

PROTOCOL = "2025-06-18"
_SYM = {"type": "string", "description": "A symbol id (py:path/file.py::Class.method) or a name that matches one symbol"}
TOOLS: list[dict[str, Any]] = [
    {"name": "outline", "description": "A file's classes, functions and methods with line ranges. Use instead of reading a large file.",
     "inputSchema": {"type": "object", "properties": {"file": {"type": "string", "description": "Path relative to the repo root"}},
                     "required": ["file"]}},
    {"name": "find", "description": "Symbols by name: exact matches, else substring matches on the id.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "callers", "description": "Every function or method whose call to this symbol could be resolved with certainty.",
     "inputSchema": {"type": "object", "properties": {"symbol": _SYM}, "required": ["symbol"]}},
    {"name": "callees", "description": "What this function or method calls, where the target could be resolved with certainty.",
     "inputSchema": {"type": "object", "properties": {"symbol": _SYM}, "required": ["symbol"]}},
    {"name": "read_symbol", "description": "One symbol's source, line-numbered: a single function instead of the whole file.",
     "inputSchema": {"type": "object", "properties": {"symbol": _SYM}, "required": ["symbol"]}},
    {"name": "slice", "description": "A symbol and its neighbourhood within `radius` edges (contains, imports, calls), with the edges, as JSON.",
     "inputSchema": {"type": "object", "properties": {"symbol": _SYM, "radius": {"type": "integer", "minimum": 0, "maximum": 3}},
                     "required": ["symbol"]}},
    {"name": "stats", "description": "Files, symbols and edges by kind, and how many calls couldn't be resolved.",
     "inputSchema": {"type": "object", "properties": {}}},
]
EDIT_TOOL = {"name": "replace_symbol",
             "description": "Replace one class, function or method's full source (decorators included). Refused, file untouched, "
                            "if the file wouldn't parse or the symbol would no longer be defined.",
             "inputSchema": {"type": "object", "properties": {"symbol": _SYM, "source": {"type": "string"}},
                             "required": ["symbol", "source"]}}


class Server:
    def __init__(self, root: str, allow_edit: bool = False):
        self.index = Index(root)
        self.allow_edit = allow_edit

    def tools(self) -> list[dict[str, Any]]:
        return TOOLS + ([EDIT_TOOL] if self.allow_edit else [])

    def _sym(self, ref: str) -> dict[str, Any]:
        n = self.index.node(ref)
        if not n:
            hits = self.index.find(ref, limit=8)
            raise ValueError(f"no single symbol matches {ref!r}" + (": " + ", ".join(h["id"] for h in hits) if hits else ""))
        return n

    def call(self, name: str, args: dict[str, Any]) -> str:
        idx = self.index.build()
        if name == "outline":
            return "\n".join(brief(n) for n in idx.outline(args["file"])) or "no symbols (not indexed, or empty)"
        if name == "find":
            return "\n".join(brief(n) for n in idx.find(args["name"])) or "no matches"
        if name in ("callers", "callees"):
            n = self._sym(args["symbol"])
            return "\n".join(brief(x) for x in getattr(idx, name)(n["id"])) or f"no resolved {name}"
        if name == "read_symbol":
            return idx.read(self._sym(args["symbol"])["id"])
        if name == "slice":
            return json.dumps(idx.slice(self._sym(args["symbol"])["id"], int(args.get("radius", 1))), indent=1)
        if name == "stats":
            return json.dumps(idx.stats(), indent=1)
        if name == "replace_symbol" and self.allow_edit:
            return json.dumps(idx.replace(self._sym(args["symbol"])["id"], args["source"]))
        raise ValueError(f"unknown tool {name!r}")

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:          # a notification (notifications/initialized and the like): no reply
            return None
        try:
            if method == "initialize":
                result: Any = {"protocolVersion": msg.get("params", {}).get("protocolVersion", PROTOCOL),
                               "capabilities": {"tools": {}},
                               "serverInfo": {"name": "reflective-index", "version": __version__}}
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                p = msg.get("params", {})
                try:
                    result = {"content": [{"type": "text", "text": self.call(p.get("name", ""), p.get("arguments") or {})}]}
                except (ValueError, KeyError, OSError) as exc:
                    result = {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
            elif method == "ping":
                result = {}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the server
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)[:500]}}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = next((a for a in argv if not a.startswith("--")), ".")
    server = Server(root, allow_edit="--allow-edit" in argv)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        reply = server.handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
