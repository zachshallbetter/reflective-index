"""reflective-index: a local semantic index of a codebase, emitted as a CSG-shaped graph.

Nodes are a repository's symbols (modules, classes, functions, methods, and in Swift types, protocols and
extensions); edges say how they connect (contains, imports, calls, conforms). The graph follows
reflective-rust's Compiler Semantic Graph exchange format (`rr-csg-0.1`) with the draft extensions in
docs/SCHEMA.md, so the same access-aware slicing applies.
"""
from .graph import SCHEMA, Index

__all__ = ["Index", "SCHEMA"]
__version__ = "0.1.0"
