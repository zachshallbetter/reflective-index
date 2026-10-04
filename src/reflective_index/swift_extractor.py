"""Swift facts from sourcekit-lsp, the Swift toolchain's language server, spoken to over stdio.

Structure, one file at a time (`extract`), from `textDocument/documentSymbol`. It's syntactic, so a file needs no build:
  nodes     module, class, struct, enum, protocol, extension, function, method (initializers are methods), and
            property for a property written over several lines (a computed one, like a SwiftUI view's `body`, or one
            set by a closure): code runs in it, so calls are made from it
  contains  parent → child
Calls, for a SwiftPM package (`package_calls`), from the package's index store (`swift build` writes it) through
`callHierarchy/outgoingCalls`: an edge only where the index names the callee's definition in the repository. Calls into
the SDK or anything else outside the tree aren't edges and aren't guesses. Then `textDocument/references` on every
function and method, each place it's named attributed to the innermost function, method or property around it: a
`refs` edge where outgoing calls had no `calls` edge (a call the call hierarchy missed, or a use as a value), and
the place itself kept as unplaced when nothing the index models encloses it.

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
                                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                                        "callHierarchy": {}}}})
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
    # Where each function's name is (0-based line, character), for the call pass: the index answers by position.
    anchors: dict[str, list[int]] = {}
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
            if kind in ("method", "function"):
                sel = s.get("selectionRange", s["range"])["start"]
                anchors[nid] = [sel["line"], sel["character"]]
            if kind in TYPE_KINDS:
                visit(s.get("children") or [], member_path, nid)

    visit(symbols(rel, source, lsp), "", mod_id)
    return {"language": LANGUAGE, "module": mod_id, "nodes": nodes, "contains": contains, "imports": {}, "from_imports": {}, "calls": [],
            "anchors": anchors}


# ── calls: a SwiftPM package's index store ─────────────────────────────────────────────────────────────

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


def package_calls(root: str, package: str, functions: dict[str, tuple[str, list[int]]],
                  spans: list[tuple[str, int, int, str]], scratch: str,
                  ready_timeout: float = 30.0, workers: int | None = None
                  ) -> tuple[list[list[str]], int, dict[str, list[list[Any]]]]:
    """Call and reference edges among this package's functions, from its index store, by several sourcekit-lsp servers
    at once (each answers one request at a time). `functions`: node id → (file rel, name position); `spans`: (file rel,
    start line, end line, node id) of every function, method and property. Returns (edges, how many calls led outside
    the tree, unplaced: target id → [[file rel, line], …] for places it's named that no function, method or property
    encloses)."""
    from concurrent.futures import ThreadPoolExecutor
    # Whole files to each server: opening a file makes its server type-check it, so a file shouldn't be opened by all.
    by_file: dict[str, dict[str, tuple[str, list[int]]]] = {}
    for nid, v in functions.items():
        by_file.setdefault(v[0], {})[nid] = v
    n = max(1, min(workers or max(1, (os.cpu_count() or 2) // 2), 8, len(by_file) or 1))
    chunks: list[dict[str, tuple[str, list[int]]]] = [{} for _ in range(n)]
    for f, fns in sorted(by_file.items(), key=lambda kv: -len(kv[1])):   # largest first, onto the lightest chunk
        min(chunks, key=len).update(fns)
    with ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda c: _package_calls_one(root, package, c, spans, scratch, ready_timeout), chunks))
    calls = {(a, b) for r in results for a, b, rel in r[0] if rel == "calls"}
    edges = [e for r in results for e in r[0] if e[2] == "calls" or (e[0], e[1]) not in calls]
    unplaced: dict[str, list[list[Any]]] = {}
    for r in results:
        unplaced.update(r[2])
    return edges, sum(r[1] for r in results), unplaced


def _package_calls_one(root: str, package: str, functions: dict[str, tuple[str, list[int]]],
                       spans: list[tuple[str, int, int, str]], scratch: str,
                       ready_timeout: float = 30.0) -> tuple[list[list[str]], int, dict[str, list[list[Any]]]]:
    """One server's share of package_calls: outgoing calls and references for these functions."""
    by_file: dict[str, list[tuple[int, int, str]]] = {}
    for f, s, e, nid in spans:
        by_file.setdefault(f, []).append((s, e, nid))

    def target(rel: str, line: int) -> str | None:
        best = None
        for s, e, nid in by_file.get(rel, []):
            if s <= line <= e and (best is None or e - s < best[1] - best[0]):
                best = (s, e, nid)
        return best[2] if best else None

    lsp = LSP(os.path.join(root, package), ["--scratch-path", scratch])
    edges: list[list[str]] = []
    outside = 0
    unplaced: dict[str, list[list[Any]]] = {}
    opened: set[str] = set()
    root_abs = os.path.abspath(root)

    def open_file(path: str) -> str:
        uri = "file://" + path
        if uri not in opened:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            lsp.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "swift", "version": 1,
                                                                "text": text}})
            opened.add(uri)
        return uri

    def in_tree(uri: str) -> str | None:
        path = str(uri)[len("file://"):]
        return os.path.relpath(path, root_abs) if path.startswith(root_abs + os.sep) else None

    def outgoing(nid: str, rel: str, line: int, ch: int) -> tuple[list[list[str]], int] | None:
        """This function's call edges and how many calls led outside the tree, or None when the server had nothing
        for it (not loaded yet, or no calls)."""
        try:
            uri = open_file(os.path.join(root_abs, rel))
            items = lsp.request("textDocument/prepareCallHierarchy",
                                {"textDocument": {"uri": uri}, "position": {"line": line, "character": ch}}) or []
            out = (lsp.request("callHierarchy/outgoingCalls", {"item": items[0]}) or []) if items else []
        except (LSPError, OSError):
            return None
        if not out:
            return None
        found, away = [], 0
        for call in out:
            to = call.get("to") or {}
            trel = in_tree(to.get("uri", ""))
            tline = (to.get("selectionRange") or to.get("range") or {}).get("start", {}).get("line", -1) + 1
            tnid = target(trel, tline) if trel else None
            if tnid and tnid != nid:
                found.append([nid, tnid, "calls"])
            elif not tnid:
                away += 1
        return found, away

    try:
        # The server loads the package and its index store in the background after it starts, and until it has, call
        # hierarchy answers nothing (or an item with no calls). Wait (up to ready_timeout) until one of the first
        # functions answers with calls; whatever still comes back empty is asked again at the end, once loaded.
        import time
        deadline = time.monotonic() + ready_timeout
        probe = list(functions.items())[:20]
        while probe and time.monotonic() < deadline:
            if any(outgoing(nid, rel, line, ch) is not None for nid, (rel, (line, ch)) in probe):
                break
            time.sleep(0.5)
        empty = []
        for nid, (rel, (line, ch)) in functions.items():
            got = outgoing(nid, rel, line, ch)
            if got is None:
                empty.append(nid)
            else:
                edges += got[0]
                outside += got[1]
        for nid in empty:
            rel, (line, ch) = functions[nid]
            got = outgoing(nid, rel, line, ch)
            if got:
                edges += got[0]
                outside += got[1]
        # Every place each function is named, attributed to what encloses it.
        for nid, (rel, (line, ch)) in functions.items():
            try:
                refs = lsp.request("textDocument/references",
                                   {"textDocument": {"uri": open_file(os.path.join(root_abs, rel))},
                                    "position": {"line": line, "character": ch},
                                    "context": {"includeDeclaration": False}}) or []
            except (LSPError, OSError):
                continue
            for r in refs:
                rrel = in_tree(r.get("uri", ""))
                if not rrel:
                    continue
                rline = r["range"]["start"]["line"] + 1
                src = target(rrel, rline)
                if src is None:
                    unplaced.setdefault(nid, []).append([rrel, rline])
                elif src != nid:
                    edges.append([src, nid, "refs"])
    finally:
        lsp.close()
    return edges, outside, unplaced
