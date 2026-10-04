"""Swift facts from sourcekit-lsp, the Swift toolchain's language server, spoken to over stdio.

Structure, one file at a time (`extract`), from `textDocument/documentSymbol`. It's syntactic, so a file needs no build:
  nodes     module, class, struct, enum, protocol, extension, function, method (initializers are methods), and
            property for a property written over several lines (a computed one, like a SwiftUI view's `body`, or one
            set by a closure): code runs in it, so calls are made from it
  contains  parent → child
Calls, for a SwiftPM package: `build_index` builds it with indexing on, and indexstore.py reads the store it writes.

Ids:
  swift:<path>                              the file
  swift:<path>::<Type>                      a type (nested: Outer.Inner)
  swift:<path>::<Type>.<member>             a method, with its argument labels: Robin.greet(), Robin.init(name:)
  swift:<path>::<function>                  a top-level function
  swift:<path>::extension <Type>            an extension (a second one in the file: extension <Type> #2)
A member declared in an extension belongs to its type: swift:<path>::<Type>.<member>, contained by the extension.
"""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import threading
from typing import Any

LANGUAGE = "swift"

# LSP SymbolKind → node kind. A one-line property, an enum case or a variable isn't a node (Python has none either);
# a property written over several lines is (PROPERTY_KINDS), since code runs in it.
KINDS = {5: "class", 23: "struct", 10: "enum", 11: "protocol", 3: "extension", 6: "method", 9: "method",
         12: "function"}
PROPERTY_KINDS = {7, 13}
TYPE_KINDS = {"class", "struct", "enum", "protocol", "extension"}


class LSPError(RuntimeError):
    pass


class LSP:
    """One sourcekit-lsp process over stdio, kept for the whole build (its first request costs about half a second)."""

    def __init__(self, root: str, args: list[str] | None = None):
        cmd = ["xcrun", "sourcekit-lsp"] if os.uname().sysname == "Darwin" else ["sourcekit-lsp"]
        self.proc = subprocess.Popen(cmd + list(args or []), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.lock = threading.Lock()
        self.next_id = 0
        self.request("initialize", {"processId": os.getpid(), "rootUri": "file://" + os.path.abspath(root),
                                    "capabilities": {"textDocument": {
                                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}}}})
        self.notify("initialized", {})

    def _send(self, msg: dict[str, Any]) -> None:
        body = json.dumps(msg).encode()
        assert self.proc.stdin is not None
        self.proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.proc.stdin.flush()

    def _recv(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        headers: dict[str, str] = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise LSPError("sourcekit-lsp exited")
            line = line.decode().strip()
            if not line:
                break
            k, _, v = line.partition(":")
            headers[k.lower()] = v.strip()
        return json.loads(self.proc.stdout.read(int(headers["content-length"])))

    def notify(self, method: str, params: dict[str, Any]) -> None:
        with self.lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> Any:
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            while True:
                m = self._recv()
                if m.get("id") == rid and "method" not in m:
                    if "error" in m:
                        raise LSPError(str(m["error"].get("message", m["error"]))[:200])
                    return m.get("result")
                if "method" in m and "id" in m:   # a request from the server (progress, config): answer it empty
                    self._send({"jsonrpc": "2.0", "id": m["id"], "result": None})

    def close(self) -> None:
        try:
            self.request("shutdown", {})
            self.notify("exit", {})
        except (LSPError, OSError, BrokenPipeError):
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                if pipe:
                    pipe.close()
            except OSError:
                pass


_shared: LSP | None = None


def _server() -> LSP:
    """The structure pass's server: rootless (documentSymbol needs no build settings), one per process."""
    global _shared
    if _shared is None:
        _shared = LSP(os.getcwd())
        atexit.register(_shared.close)
    return _shared


def symbols(rel: str, source: str, lsp: LSP | None = None) -> list[dict[str, Any]]:
    """documentSymbol for this text, opened under a synthetic URI and closed again (nothing read from disk)."""
    s = lsp or _server()
    uri = "file:///reflective-index/" + rel.replace(os.sep, "/")
    s.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "swift", "version": 1, "text": source}})
    try:
        return s.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}) or []
    finally:
        s.notify("textDocument/didClose", {"textDocument": {"uri": uri}})


def extract(rel: str, source: str, access: str, lsp: LSP | None = None) -> dict[str, Any]:
    mod_id = f"swift:{rel}"
    lines = source.splitlines(keepends=True)
    line_bytes = [len(ln.encode()) for ln in lines]

    def size(s: int, e: int) -> int:
        return sum(line_bytes[s - 1:e])

    nodes = [{"id": mod_id, "name": os.path.splitext(os.path.basename(rel))[0], "kind": "module",
              "span": {"file": rel, "start_line": 1, "end_line": max(1, len(lines))},
              "minimum_access": access, "size_bytes": sum(line_bytes)}]
    contains: list[list[str]] = []
    seen: dict[str, int] = {}

    def unique(nid: str) -> str:
        n = seen.get(nid, 0) + 1
        seen[nid] = n
        return nid if n == 1 else f"{nid} #{n}"

    def visit(syms: list[dict[str, Any]], type_path: str, parent: str) -> None:
        for s in syms:
            kind = KINDS.get(s.get("kind"))
            name = str(s.get("name") or "")
            start = s["range"]["start"]["line"] + 1
            end = max(start, s["range"]["end"]["line"] + 1)
            if s.get("kind") in PROPERTY_KINDS and end > start:
                kind = "property"
            if not kind or not name:
                continue
            if kind == "extension":
                qual, member_path = f"extension {name}", name
            elif kind in TYPE_KINDS:
                qual = member_path = f"{type_path}.{name}" if type_path else name
            else:
                qual, member_path = (f"{type_path}.{name}" if type_path else name), type_path
                if kind == "method" and not type_path:
                    kind = "function"
                if kind == "function" and type_path:
                    kind = "method"
            if kind == "property":
                qual = f"{type_path}.{name}" if type_path else name
            nid = unique(f"{mod_id}::{qual}")
            nodes.append({"id": nid, "name": name if kind != "extension" else qual, "kind": kind,
                          "span": {"file": rel, "start_line": start, "end_line": end},
                          "minimum_access": access, "size_bytes": size(start, end)})
            contains.append([parent, nid])
            if kind in TYPE_KINDS:
                visit(s.get("children") or [], member_path, nid)

    visit(symbols(rel, source, lsp), "", mod_id)
    return {"language": LANGUAGE, "module": mod_id, "nodes": nodes, "contains": contains, "imports": {}, "from_imports": {}, "calls": []}


# ── a SwiftPM package's index store (read by indexstore.py) ─────────────────────────────────────────────────────────────

def find_packages(root: str, rels: list[str]) -> list[str]:
    """Directories (relative to root) with a Package.swift that hold indexed Swift files."""
    dirs = set()
    for rel in rels:
        d = os.path.dirname(rel)
        while True:
            if os.path.isfile(os.path.join(root, d, "Package.swift")):
                dirs.add(d)
                break
            if not d:
                break
            d = os.path.dirname(d)
    return sorted(dirs)


def build_index(root: str, package: str, scratch: str, timeout: int = 1800) -> bool:
    """`swift build` into its own scratch path (never the package's own .build), which writes the index store."""
    r = subprocess.run(["swift", "build", "--enable-index-store", "--package-path", os.path.join(root, package),
                        "--scratch-path", scratch], capture_output=True, timeout=timeout)
    return r.returncode == 0
