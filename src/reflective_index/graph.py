"""The index: extract every file, link the facts into one graph, answer questions about it.

Calls are linked only where the binding is certain: a def in the same module, `from m import f`,
`import m as x; x.f()`, `self.f()` / `cls.f()` inside a class, and `Class()` to the class. Everything else
is counted as unresolved, never guessed. The per-file facts are cached by content hash, so a rebuild
re-reads only what changed.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from . import python_extractor

SCHEMA = "rr-csg-0.1+ri-ext-draft"
ACCESS_ORDER = ("public", "member", "owner", "administrator")   # reflective-rust's AccessLevel, lowest first
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", ".build", "DerivedData",
             ".reflective-index"}
MAX_FILE_BYTES = 2_000_000
EXTRACTORS = {".py": python_extractor}


def permits(access: str, required: str) -> bool:
    """reflective-rust's AccessLevel::permits: a caller sees what needs its level or lower."""
    return ACCESS_ORDER.index(access) >= ACCESS_ORDER.index(required)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Index:
    def __init__(self, root: str | os.PathLike, cache: str | os.PathLike | None = None, access: str = "member",
                 skip: Iterable[str] = ()):
        self.root = Path(root).resolve()
        self.cache_path = Path(cache or os.environ.get("REFLECTIVE_INDEX_CACHE")
                               or self.root / ".reflective-index" / "cache.json")
        self.access = access
        self.skip = set(SKIP_DIRS) | set(skip)
        self.files: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.unresolved = 0

    # ── building ──────────────────────────────────────────────────────────────────────────────────
    def source_files(self) -> list[str]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in self.skip and not d.startswith("."))
            for f in sorted(filenames):
                if os.path.splitext(f)[1] in EXTRACTORS:
                    out.append(os.path.relpath(os.path.join(dirpath, f), self.root))
        return out

    def build(self) -> "Index":
        old: dict[str, Any] = {}
        try:
            cached = json.loads(self.cache_path.read_text())
            if cached.get("schema") == SCHEMA and cached.get("access") == self.access:
                old = cached.get("files", {})
        except (OSError, ValueError):
            pass
        self.files = {}
        for rel in self.source_files():
            try:
                data = (self.root / rel).read_bytes()
            except OSError:
                continue
            if len(data) > MAX_FILE_BYTES:
                continue
            h = _sha(data)
            if rel in old and old[rel].get("hash") == h:
                self.files[rel] = old[rel]
                continue
            ext = EXTRACTORS[os.path.splitext(rel)[1]]
            try:
                self.files[rel] = {"hash": h, "facts": ext.extract(rel, data.decode("utf-8", "replace"), self.access)}
            except (SyntaxError, ValueError, RecursionError) as exc:
                self.files[rel] = {"hash": h, "error": f"{type(exc).__name__}: {exc}"[:200]}
        self._link()
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"schema": SCHEMA, "access": self.access, "files": self.files}))
        except OSError:
            pass
        return self

    def _resolve_module(self, dotted: str, importer: str) -> str | None:
        """A dotted import to a file in the repo: a package path from the root, or a sibling module (scripts that
        import each other by bare name after putting their own directory on sys.path)."""
        rel_path = dotted.replace(".", "/")
        for cand in (f"{rel_path}.py", f"{rel_path}/__init__.py", os.path.join(os.path.dirname(importer), f"{rel_path}.py")):
            cand = os.path.normpath(cand)
            if "facts" in self.files.get(cand, {}):
                return cand
        return None

    def _link(self) -> None:
        self.nodes, self.edges, self.unresolved = {}, [], 0
        for f in self.files.values():
            for n in f.get("facts", {}).get("nodes", []):
                self.nodes[n["id"]] = n
        seen: set[tuple[str, str, str]] = set()

        def edge(a: str, b: str, rel: str) -> None:
            if a in self.nodes and b in self.nodes and (a, b, rel) not in seen:
                seen.add((a, b, rel))
                self.edges.append({"from": a, "to": b, "relationship": rel, "minimum_access": self.access})

        for rel, f in self.files.items():
            facts = f.get("facts")
            if not facts:
                continue
            mod = f"py:{rel}"
            for a, b in facts["contains"]:
                edge(a, b, "contains")
            alias_mod: dict[str, str] = {}
            for alias, dotted in facts["imports"].items():
                target = self._resolve_module(dotted, rel)
                if target:
                    alias_mod[alias] = target
                    edge(mod, f"py:{target}", "imports")
            from_map: dict[str, str] = {}
            for local, (dotted, name) in facts["from_imports"].items():
                target = self._resolve_module(dotted, rel)
                if target:
                    from_map[local] = f"py:{target}::{name}"
                    edge(mod, f"py:{target}", "imports")
            for caller, d in facts["calls"]:
                target = None
                if "name" in d:
                    nm = d["name"]
                    target = f"{mod}::{nm}" if f"{mod}::{nm}" in self.nodes else from_map.get(nm)
                elif d["base"] in ("self", "cls") and d.get("cls"):
                    target = f"{mod}::{d['cls']}.{d['attr']}"
                elif d["base"] in alias_mod:
                    target = f"py:{alias_mod[d['base']]}::{d['attr']}"
                if target and target in self.nodes:
                    edge(caller, target, d.get("rel", "calls"))
                else:
                    self.unresolved += 1

    # ── queries ───────────────────────────────────────────────────────────────────────────────────
    def visible(self, n: dict[str, Any], access: str | None = None) -> bool:
        return permits(access or self.access, n["minimum_access"])

    def node(self, ref: str) -> dict[str, Any] | None:
        """A node by id, or by a name that matches exactly one node."""
        if ref in self.nodes:
            return self.nodes[ref]
        hits = self.find(ref)
        return hits[0] if len(hits) == 1 else None

    def find(self, name: str, limit: int = 50) -> list[dict[str, Any]]:
        exact = [n for n in self.nodes.values() if n["name"] == name or n["id"].endswith("::" + name)]
        if exact:
            return exact[:limit]
        low = name.lower()
        return [n for n in self.nodes.values() if low in n["id"].lower()][:limit]

    def outline(self, rel: str) -> list[dict[str, Any]]:
        rel = os.path.normpath(rel)
        return sorted((n for n in self.nodes.values() if n["span"]["file"] == rel and n["kind"] != "module"),
                      key=lambda n: n["span"]["start_line"])

    def _related(self, nid: str, relationship: str, incoming: bool) -> list[dict[str, Any]]:
        key, other = ("to", "from") if incoming else ("from", "to")
        return [self.nodes[e[other]] for e in self.edges if e[key] == nid and e["relationship"] == relationship]

    def callers(self, nid: str) -> list[dict[str, Any]]:
        return self._related(nid, "calls", True)

    def callees(self, nid: str) -> list[dict[str, Any]]:
        return self._related(nid, "calls", False)

    def read(self, nid: str, numbered: bool = True) -> str:
        n = self.nodes[nid]
        lines = (self.root / n["span"]["file"]).read_text(errors="replace").splitlines()
        s, e = n["span"]["start_line"], n["span"]["end_line"]
        chunk = lines[s - 1:e]
        return "\n".join(f"{s + i:>6}  {ln}" for i, ln in enumerate(chunk)) if numbered else "\n".join(chunk)

    def slice(self, nid: str, radius: int = 1, access: str | None = None) -> dict[str, Any] | None:
        """The node and everything within `radius` edges of it that `access` may see, with the edges between
        them (reflective-rust-agent's slice_around returns nodes only; an agent needs the why)."""
        access = access or self.access
        if nid not in self.nodes or not self.visible(self.nodes[nid], access):
            return None
        adj: dict[str, set[str]] = defaultdict(set)
        for e in self.edges:
            if permits(access, e["minimum_access"]):
                adj[e["from"]].add(e["to"])
                adj[e["to"]].add(e["from"])
        seen, q = {nid}, deque([(nid, 0)])
        while q:
            cur, d = q.popleft()
            if d == radius:
                continue
            for nxt in adj[cur]:
                if nxt not in seen and self.visible(self.nodes[nxt], access):
                    seen.add(nxt)
                    q.append((nxt, d + 1))
        return {"target": nid, "radius": radius, "access": access,
                "nodes": [self.nodes[i] for i in sorted(seen)],
                "edges": [e for e in self.edges if e["from"] in seen and e["to"] in seen
                          and permits(access, e["minimum_access"])]}

    def replace(self, nid: str, new_source: str) -> dict[str, Any]:
        """Swap one symbol's source (its whole span, decorators included) and re-parse the file. Refuses, leaving
        the file untouched, when the result doesn't parse or no longer defines the same symbol."""
        n = self.nodes.get(nid)
        if not n or n["kind"] == "module":
            return {"ok": False, "error": "not a class, function or method in the index"}
        rel = n["span"]["file"]
        path = self.root / rel
        lines = path.read_text().splitlines(keepends=True)
        s, e = n["span"]["start_line"], n["span"]["end_line"]
        indent = len(lines[s - 1]) - len(lines[s - 1].lstrip())
        body = new_source.rstrip("\n").splitlines()
        lead = min((len(ln) - len(ln.lstrip()) for ln in body if ln.strip()), default=0)
        body = [(" " * indent + ln[lead:]) if ln.strip() else "" for ln in body]
        new_text = "".join(lines[:s - 1]) + "\n".join(body) + "\n" + "".join(lines[e:])
        ext = EXTRACTORS[os.path.splitext(rel)[1]]
        try:
            facts = ext.extract(rel, new_text, self.access)
        except SyntaxError as exc:
            return {"ok": False, "error": f"the file wouldn't parse: {exc}"}
        if not any(x["id"] == nid and x["kind"] == n["kind"] for x in facts["nodes"]):
            return {"ok": False, "error": f"the new source must still define {n['kind']} {n['name']}"}
        path.write_text(new_text)
        self.build()
        return {"ok": True, "id": nid, "lines": [s, s + len(body) - 1]}

    def stats(self) -> dict[str, Any]:
        kinds: dict[str, int] = defaultdict(int)
        for n in self.nodes.values():
            kinds[n["kind"]] += 1
        rels: dict[str, int] = defaultdict(int)
        for e in self.edges:
            rels[e["relationship"]] += 1
        return {"root": str(self.root), "files": len(self.files),
                "unparsed": sorted(r for r, f in self.files.items() if "error" in f),
                "nodes": dict(kinds), "edges": dict(rels), "unresolved_calls": self.unresolved}

    def csg(self) -> dict[str, Any]:
        """The whole graph in reflective-rust's exchange shape (see docs/SCHEMA.md for the draft extensions)."""
        digest = hashlib.sha256("".join(f"{r}:{f['hash']}" for r, f in sorted(self.files.items())).encode()).hexdigest()
        langs = sorted({f["facts"]["language"] for f in self.files.values() if "facts" in f})
        return {"schema_version": SCHEMA,
                "provenance": {"kind": "extractor", "extractor": "reflective-index", "languages": langs,
                               "source_hash": f"sha256:{digest}"},
                "nodes": sorted(self.nodes.values(), key=lambda n: n["id"]), "edges": self.edges}
