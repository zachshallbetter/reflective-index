"""The index: extract every file, link the facts into one graph, answer questions about it.

Calls are linked only where the binding is certain: a def in the same module, `from m import f`,
`import m as x; x.f()`, `self.f()` / `cls.f()` inside a class, and `Class()` to the class. Everything else
is counted as unresolved, never guessed, and the count is kept per caller and per called name so an answer can say
when it may be short. A function named without being called (`x = m.f`, `to_thread(m.f, a)`) is a `refs` edge,
resolved the same way. The per-file facts are cached by content hash, so a rebuild
re-reads only what changed.
"""
from __future__ import annotations

import builtins
import hashlib
import json
import os
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

from . import python_extractor, swift_extractor

SCHEMA = "rr-csg-0.1+ri-ext-draft"
ACCESS_ORDER = ("public", "member", "owner", "administrator")   # reflective-rust's AccessLevel, lowest first
FACTS_VERSION = 3   # bump when an extractor's facts change shape, so cached facts are re-read
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "build", "dist", ".build", "DerivedData",
             ".reflective-index"}
MAX_FILE_BYTES = 2_000_000
EXTRACTORS = {".py": python_extractor, ".swift": swift_extractor}


def permits(access: str, required: str) -> bool:
    """reflective-rust's AccessLevel::permits: a caller sees what needs its level or lower."""
    return ACCESS_ORDER.index(access) >= ACCESS_ORDER.index(required)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Index:
    def __init__(self, root: str | os.PathLike, cache: str | os.PathLike | None = None, access: str = "member",
                 skip: Iterable[str] = (), swift_calls: bool = False):
        self.root = Path(root).resolve()
        self.cache_path = Path(cache or os.environ.get("REFLECTIVE_INDEX_CACHE")
                               or self.root / ".reflective-index" / "cache.json")
        self.access = access
        self.skip = set(SKIP_DIRS) | set(skip)
        self.files: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.unresolved = 0
        self.unresolved_in: dict[str, int] = defaultdict(int)     # caller id → calls in it that couldn't be resolved
        self.unresolved_named: dict[str, int] = defaultdict(int)  # called name → unresolved calls by that name
        # Swift calls come from a SwiftPM package's index store, which needs a build: only when asked for, cached by
        # the package's file hashes (swift_extractor.package_calls).
        self.swift_calls = swift_calls
        self.package_edges: dict[str, dict[str, Any]] = {}

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
        old_pkg: dict[str, Any] = {}
        try:
            cached = json.loads(self.cache_path.read_text())
            if (cached.get("schema") == SCHEMA and cached.get("access") == self.access
                    and cached.get("facts_version") == FACTS_VERSION):
                old = cached.get("files", {})
                old_pkg = cached.get("swift_calls", {})
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
            except (SyntaxError, ValueError, RecursionError, swift_extractor.LSPError, OSError) as exc:
                self.files[rel] = {"hash": h, "error": f"{type(exc).__name__}: {exc}"[:200]}
        self.package_edges = self._swift_packages(old_pkg) if self.swift_calls else {}
        self._link()
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({"schema": SCHEMA, "facts_version": FACTS_VERSION,
                                                   "access": self.access, "files": self.files,
                                                   "swift_calls": self.package_edges}))
        except OSError:
            pass
        return self

    def _swift_packages(self, old: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Each SwiftPM package's call edges: reused while none of its Swift files changed, else from a fresh build of
        its index store (in the cache directory, never the package's own .build)."""
        swift = [r for r in self.files if r.endswith(".swift") and "facts" in self.files[r]]
        out: dict[str, dict[str, Any]] = {}
        for pkg in swift_extractor.find_packages(str(self.root), swift):
            mine = sorted(r for r in swift if pkg == "" or r.startswith(pkg + os.sep))
            key = _sha((f"v{FACTS_VERSION}" + "".join(self.files[r]["hash"] for r in mine)).encode())
            if old.get(pkg, {}).get("key") == key:
                out[pkg] = old[pkg]
                continue
            scratch = str(self.cache_path.parent / "swiftpm" / (pkg.replace(os.sep, "_") or "root"))
            if not swift_extractor.build_index(str(self.root), pkg, scratch):
                out[pkg] = {"key": None, "edges": [], "outside": 0, "unplaced": {}, "error": "swift build failed"}
                continue
            functions, spans = {}, []
            for r in mine:
                facts = self.files[r]["facts"]
                for n in facts["nodes"]:
                    if n["kind"] in ("function", "method", "property"):
                        spans.append((r, n["span"]["start_line"], n["span"]["end_line"], n["id"]))
                        if n["id"] in facts.get("anchors", {}):
                            functions[n["id"]] = (r, facts["anchors"][n["id"]])
            edges, outside, unplaced = swift_extractor.package_calls(str(self.root), pkg, functions, spans, scratch)
            out[pkg] = {"key": key, "edges": edges, "outside": outside, "unplaced": unplaced}
        return out

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
        self.unresolved_in, self.unresolved_named = defaultdict(int), defaultdict(int)
        self.unplaced: dict[str, list[list[Any]]] = {}
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
            mod = facts.get("module", f"py:{rel}")
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
            def resolve(d: dict[str, Any]) -> str | None:
                if "name" in d:
                    nm = d["name"]
                    return f"{mod}::{nm}" if f"{mod}::{nm}" in self.nodes else from_map.get(nm)
                if d["base"] in ("self", "cls") and d.get("cls"):
                    return f"{mod}::{d['cls']}.{d['attr']}"
                if d["base"] in alias_mod:
                    return f"py:{alias_mod[d['base']]}::{d['attr']}"
                return None

            def outside(d: dict[str, Any]) -> bool:
                """Known to leave the repo: a builtin, or something from a module that isn't in it (the standard library,
                a package). Not a gap in the answer, so not counted as one."""
                if "name" in d:
                    nm = d["name"]
                    return (nm in facts["from_imports"] and nm not in from_map) or (
                        nm not in facts["from_imports"] and hasattr(builtins, nm))
                return d["base"] in facts["imports"] and d["base"] not in alias_mod

            for caller, d in facts["calls"]:
                target = resolve(d)
                if target and target in self.nodes:
                    edge(caller, target, d.get("rel", "calls"))
                elif not outside(d):
                    self.unresolved += 1
                    if d.get("rel", "calls") == "calls":
                        self.unresolved_in[caller] += 1
                        self.unresolved_named[d.get("name") or d.get("attr")] += 1
            for caller, d in facts.get("refs", []):
                target = resolve(d)
                if target and self.nodes.get(target, {}).get("kind") in ("function", "method") and target != caller:
                    edge(caller, target, "refs")
        for pkg in self.package_edges.values():   # Swift: already resolved by the index store
            for a, b, r in pkg.get("edges", []):
                edge(a, b, r)
            self.unplaced.update(pkg.get("unplaced", {}))

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
        """Exact matches first: the name (a Swift one with or without its argument labels), or the qualified name or
        its tail (`tldr`, `ReplyLines.tldr`, `ReplyLines.tldr(_:)`). Else substring matches on the id."""
        def quals(n: dict[str, Any]) -> tuple[str, str]:
            q = n["id"].split("::", 1)[1] if "::" in n["id"] else ""
            return q, q.split("(", 1)[0]

        exact = [n for n in self.nodes.values()
                 if name in (n["name"], n["name"].split("(", 1)[0], *quals(n))
                 or any(q.endswith("." + name) for q in quals(n))]
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

    def referrers(self, nid: str) -> list[dict[str, Any]]:
        """What names this function without a resolved call to it: a use as a value, or a call the index saw only as
        a reference."""
        called = {n["id"] for n in self.callers(nid)}
        return [n for n in self._related(nid, "refs", True) if n["id"] not in called]

    def referenced(self, nid: str) -> list[dict[str, Any]]:
        called = {n["id"] for n in self.callees(nid)}
        return [n for n in self._related(nid, "refs", False) if n["id"] not in called]

    def gaps(self, nid: str, which: str) -> list[str]:
        """Why a callers or callees answer may be short. An empty list from the index means nothing about the code
        unless this says it's complete, so every known gap is spelled out."""
        n = self.nodes[nid]
        notes: list[str] = []
        confirm = "an empty or short list doesn't mean none"
        if nid.startswith("py:"):
            if which == "callers":
                k = self.unresolved_named.get(n["name"], 0)
                if k:
                    notes.append(f"{k} call{'s' if k != 1 else ''} named `{n['name']}` couldn't be resolved to a definition, "
                                 f"so {confirm} (confirm with a text search for `{n['name']}`).")
            else:
                k = self.unresolved_in.get(nid, 0)
                if k:
                    notes.append(f"{k} call{'s' if k != 1 else ''} in this {n['kind']} couldn't be resolved (a local name, "
                                 f"a parameter, a method on an object); {confirm} (read it with read_symbol).")
        elif nid.startswith("swift:"):
            if not self.package_edges:
                notes.append("Swift calls weren't indexed (build with --swift-calls); the list is empty for that reason.")
            elif which == "callers":
                places = self.unplaced.get(nid, [])
                if places:
                    where = ", ".join(f"{f}:{ln}" for f, ln in places[:8]) + (" …" if len(places) > 8 else "")
                    notes.append(f"also named at {where}, inside code the index doesn't model (a closure in a stored "
                                 f"property, a one-line property, a top-level statement); read those lines.")
                notes.append("Swift links come from the compiler's index for this package only: code in other targets "
                             "(an app, widget or extension built by Xcode) isn't seen.")
            elif n["kind"] == "property":
                notes.append("calls made from a property aren't traced (only from functions and methods); "
                             "read it with read_symbol.")
            elif not self.callees(nid) and not self.referenced(nid):
                notes.append(f"the compiler's index gave no calls for this {n['kind']}; {confirm} (read it).")
        return notes

    def report(self, nid: str, which: str, fmt: Any = None) -> str:
        """callers or callees as text for an agent: the resolved ones, then what names it without a resolved call,
        then why the answer may be short."""
        fmt = fmt or (lambda x: x["id"])
        calls = self.callers(nid) if which == "callers" else self.callees(nid)
        refs = self.referrers(nid) if which == "callers" else self.referenced(nid)
        out = [fmt(x) for x in calls] or [f"no resolved {which}"]
        if refs:
            out.append("names it without a resolved call (a use as a value, or a call seen only as a reference):"
                       if which == "callers" else
                       "names without a resolved call (used as a value, or called through a local name):")
            out += ["  " + fmt(x) for x in refs]
        out += ["note: " + g for g in self.gaps(nid, which)]
        return "\n".join(out)

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
        if not rel.endswith(".py"):
            # The re-parse check is what makes replace safe, and Swift's symbol reader takes broken code without a word.
            return {"ok": False, "error": "replace is for Python symbols only (Swift has no parse check here yet)"}
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
                "nodes": dict(kinds), "edges": dict(rels), "unresolved_calls": self.unresolved,
                "unplaced_references": sum(len(v) for v in self.unplaced.values())}

    def csg(self) -> dict[str, Any]:
        """The whole graph in reflective-rust's exchange shape (see docs/SCHEMA.md for the draft extensions)."""
        digest = hashlib.sha256("".join(f"{r}:{f['hash']}" for r, f in sorted(self.files.items())).encode()).hexdigest()
        langs = sorted({f["facts"]["language"] for f in self.files.values() if "facts" in f})
        return {"schema_version": SCHEMA,
                "provenance": {"kind": "extractor", "extractor": "reflective-index", "languages": langs,
                               "source_hash": f"sha256:{digest}"},
                "nodes": sorted(self.nodes.values(), key=lambda n: n["id"]), "edges": self.edges}
