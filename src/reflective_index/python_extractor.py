"""Python facts from the standard library's `ast`: one file at a time, no cross-file resolution.

What it records per file:
  nodes         module, class, function, method (nested functions as outer.inner)
  contains      parent → child
  imports       local alias → dotted module           (`import m`, `import m as x`)
  from_imports  local name → (dotted module, name)    (`from m import f as g`)
  calls         (caller id, descriptor): a bare name `f()`, or `base.attr()` with a plain-name base
                a local name bound to a function and then called (`f = f or m.g; f()`) is a call to it, with "via"
  refs          (caller id, descriptor): a function named but not called there, so used as a value: `x = m.f`,
                `to_thread(m.f, a)`, `{"k": f}`. Only names that could be a def (this module's top-level defs, from-imports,
                imported modules' attributes, self/cls attributes); the module itself is the caller at top level.
  unresolved_in (caller id → count) is computed when linking, not here
Resolution of imports and calls happens in graph.Index once every file is known.
"""
from __future__ import annotations

import ast
from typing import Any

LANGUAGE = "python"


def module_name(rel: str) -> str:
    return rel[:-3].replace("\\", "/").replace("/", ".")


def extract(rel: str, source: str, access: str) -> dict[str, Any]:
    tree = ast.parse(source)
    mod_id = f"py:{rel}"
    lines = source.splitlines(keepends=True)
    # byte length of each line, so a node's size is a slice sum (ast.get_source_segment rescans the whole
    # source per call, which is quadratic on a 12,000-line file)
    line_bytes = [len(ln.encode()) for ln in lines]

    def size(s: int, e: int) -> int:
        return sum(line_bytes[s - 1:e])

    nodes = [{"id": mod_id, "name": module_name(rel).rsplit(".", 1)[-1], "kind": "module",
              "span": {"file": rel, "start_line": 1, "end_line": max(1, len(lines))},
              "minimum_access": access, "size_bytes": sum(line_bytes)}]
    contains: list[list[str]] = []
    imports: dict[str, str] = {}
    from_imports: dict[str, list[str]] = {}
    calls: list[list[Any]] = []
    refs: list[list[Any]] = []

    def start(n: ast.AST) -> int:
        decs = getattr(n, "decorator_list", None) or []
        return min([n.lineno] + [d.lineno for d in decs])

    def add(n: ast.AST, qual: str, kind: str, parent: str) -> str:
        nid = f"{mod_id}::{qual}"
        s, e = start(n), n.end_lineno or n.lineno
        nodes.append({"id": nid, "name": qual.rsplit(".", 1)[-1], "kind": kind,
                      "span": {"file": rel, "start_line": s, "end_line": e},
                      "minimum_access": access, "size_bytes": size(s, e)})
        contains.append([parent, nid])
        return nid

    def call_desc(c: ast.Call) -> dict[str, Any] | None:
        f = c.func
        if isinstance(f, ast.Name):
            return {"name": f.id}
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            return {"base": f.value.id, "attr": f.attr}
        return None

    def own_nodes(roots: list[ast.AST]) -> list[ast.AST]:
        """Everything under these, not in functions or classes nested inside (those are their own nodes); a lambda's
        body belongs to the function it's written in."""
        out, stack = [], list(roots)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            out.append(n)
            stack.extend(ast.iter_child_nodes(n))
        return out

    # names a value reference could resolve through (anything else can't be a def in the repo)
    top_defs: set[str] = set()

    def collect_defs(body: list[ast.stmt]) -> None:
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                top_defs.add(n.name)
            elif isinstance(n, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.While)):
                for part in ("body", "orelse", "finalbody"):
                    collect_defs(getattr(n, part, []) or [])
                for h in getattr(n, "handlers", []) or []:
                    collect_defs(h.body)

    def ref_desc(n: ast.AST, cls: str | None) -> dict[str, Any] | None:
        """A name that could be a def in the repo, as a call descriptor, or None."""
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and (
                n.value.id in imports or (cls and n.value.id in ("self", "cls"))):
            return {"base": n.value.id, "attr": n.attr, "cls": cls}
        if isinstance(n, ast.Name) and (n.id in top_defs or n.id in from_imports):
            return {"name": n.id}
        return None

    def record(nid: str, roots: list[ast.AST], cls: str | None) -> None:
        """This scope's calls, and the functions it names without calling (used as values)."""
        found = own_nodes(roots)
        called = {id(c.func) for c in found if isinstance(c, ast.Call)}
        # A local name bound to a function and then called is a call to that function: `f = f or m.g; f(x)` calls
        # m.g wherever the default is what runs (a caller can still pass its own).
        aliases: dict[str, list[dict[str, Any]]] = {}
        for n in found:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                vals = n.value.values if isinstance(n.value, ast.BoolOp) else (
                    [n.value.body, n.value.orelse] if isinstance(n.value, ast.IfExp) else [n.value])
                for v in vals:
                    d = ref_desc(v, cls)
                    if d:
                        aliases.setdefault(n.targets[0].id, []).append(d)
        for n in found:
            if isinstance(n, ast.Call):
                d = call_desc(n)
                if d and d.get("name") in aliases and d["name"] not in top_defs and d["name"] not in from_imports:
                    for a in aliases[d["name"]]:
                        calls.append([nid, {**a, "via": d["name"], "line": n.lineno}])
                elif d:
                    calls.append([nid, {**d, "cls": cls, "line": n.lineno}])
            elif id(n) in called or not isinstance(getattr(n, "ctx", None), ast.Load):
                continue
            elif (d := ref_desc(n, cls)) is not None:
                refs.append([nid, {**d, "line": n.lineno}])

    def visit(body: list[ast.stmt], prefix: str, parent: str, cls: str | None) -> None:
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{n.name}"
                nid = add(n, qual, "method" if cls else "function", parent)
                # the body and the defaults, not the decorators (those run once, at definition)
                record(nid, list(n.body) + list(n.args.defaults) + [d for d in n.args.kw_defaults if d], cls)
                visit(n.body, f"{qual}.", nid, None)
            elif isinstance(n, ast.ClassDef):
                qual = f"{prefix}{n.name}"
                nid = add(n, qual, "class", parent)
                for b in n.bases:
                    if isinstance(b, ast.Name):
                        calls.append([nid, {"name": b.id, "rel": "inherits", "line": n.lineno}])
                    elif isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name):
                        calls.append([nid, {"base": b.value.id, "attr": b.attr, "rel": "inherits", "line": n.lineno}])
                visit(n.body, f"{qual}.", nid, qual)
            elif isinstance(n, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.While)):
                # defs under `if TYPE_CHECKING:`, try/except fallbacks and the like still count
                for part in ("body", "orelse", "finalbody"):
                    visit(getattr(n, part, []) or [], prefix, parent, cls)
                for h in getattr(n, "handlers", []) or []:
                    visit(h.body, prefix, parent, cls)
            elif parent == mod_id and not isinstance(n, (ast.Import, ast.ImportFrom)):
                # module level: dispatch tables and registrations (`TOOLS = {"x": run_x}`) name functions here
                record(mod_id, [n], None)

    # imports anywhere in the file (function-local `import x` inside a branch binds too)
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imports.setdefault(a.asname or a.name.split(".")[0], a.name if a.asname else a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                if a.name != "*":
                    from_imports.setdefault(a.asname or a.name, [n.module, a.name])
    collect_defs(tree.body)
    visit(tree.body, "", mod_id, None)
    return {"language": LANGUAGE, "nodes": nodes, "contains": contains, "imports": imports,
            "from_imports": from_imports, "calls": calls, "refs": refs}
