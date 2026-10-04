"""Python facts from the standard library's `ast`: one file at a time, no cross-file resolution.

What it records per file:
  nodes         module, class, function, method (nested functions as outer.inner)
  contains      parent → child
  imports       local alias → dotted module           (`import m`, `import m as x`)
  from_imports  local name → (dotted module, name)    (`from m import f as g`)
  calls         (caller id, descriptor): a bare name `f()`, or `base.attr()` with a plain-name base
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

    def own_calls(fn: ast.AST) -> list[ast.Call]:
        """Calls in this function's body, not in functions or classes nested inside it (those are their own nodes)."""
        out, stack = [], list(ast.iter_child_nodes(fn))
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) and n is not fn:
                if isinstance(n, ast.Lambda):
                    stack.extend(ast.iter_child_nodes(n))
                continue
            if isinstance(n, ast.Call):
                out.append(n)
            stack.extend(ast.iter_child_nodes(n))
        return out

    def visit(body: list[ast.stmt], prefix: str, parent: str, cls: str | None) -> None:
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{n.name}"
                nid = add(n, qual, "method" if cls else "function", parent)
                for c in own_calls(n):
                    d = call_desc(c)
                    if d:
                        calls.append([nid, {**d, "cls": cls, "line": c.lineno}])
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

    # imports anywhere in the file (function-local `import x` inside a branch binds too)
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imports.setdefault(a.asname or a.name.split(".")[0], a.name if a.asname else a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            for a in n.names:
                if a.name != "*":
                    from_imports.setdefault(a.asname or a.name, [n.module, a.name])
    visit(tree.body, "", mod_id, None)
    return {"language": LANGUAGE, "nodes": nodes, "contains": contains, "imports": imports,
            "from_imports": from_imports, "calls": calls}
