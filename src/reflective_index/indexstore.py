"""The compiler's index store, read through the toolchain's libIndexStore (its C API, over ctypes).

A Swift build with indexing on (`swift build --enable-index-store`, or any Xcode Debug build) writes, for every file it
compiles, each symbol occurrence with its roles: where a function is defined, and every place it's called or named.
Reading that is the whole call pass: no language server, nothing to wait for, a large package in seconds.

  occurrences(store, root)   (file rel, line, roles, usr, name) for every occurrence in a file under root
"""
from __future__ import annotations

import ctypes as C
import os
import shutil
import subprocess
from typing import Any, Iterator

# indexstore_symbol_role_t
DEFINITION = 1 << 1
REFERENCE = 1 << 2
CALL = 1 << 5
IMPLICIT = 1 << 8


class IndexStoreError(RuntimeError):
    pass


class _Str(C.Structure):
    _fields_ = [("data", C.c_char_p), ("length", C.c_size_t)]


def _s(r: _Str) -> str:
    return C.string_at(r.data, r.length).decode("utf-8", "replace") if r.data else ""


def library_path() -> str | None:
    """libIndexStore next to the Swift compiler in use (Xcode's toolchain on a Mac), or REFLECTIVE_INDEX_LIBINDEXSTORE."""
    env = os.environ.get("REFLECTIVE_INDEX_LIBINDEXSTORE")
    if env:
        return env
    swift = None
    if os.uname().sysname == "Darwin":
        try:
            swift = subprocess.run(["xcrun", "--find", "swift"], capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            swift = None
    swift = swift or shutil.which("swift")
    if not swift:
        return None
    lib = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(swift))), "lib")
    for name in ("libIndexStore.dylib", "libIndexStore.so"):
        if os.path.isfile(os.path.join(lib, name)):
            return os.path.join(lib, name)
    return None


_lib: Any = None


def _load() -> Any:
    global _lib
    if _lib is not None:
        return _lib
    path = library_path()
    if not path:
        raise IndexStoreError("libIndexStore not found (no Swift toolchain?)")
    lib = C.CDLL(path)
    V = C.c_void_p
    sig = {
        "indexstore_store_create": (V, [C.c_char_p, C.POINTER(V)]),
        "indexstore_store_dispose": (None, [V]),
        "indexstore_error_get_description": (C.c_char_p, [V]),
        "indexstore_store_units_apply_f": (C.c_bool, [V, C.c_uint, V, C.CFUNCTYPE(C.c_bool, V, _Str)]),
        "indexstore_unit_reader_create": (V, [V, C.c_char_p, C.POINTER(V)]),
        "indexstore_unit_reader_dispose": (None, [V]),
        "indexstore_unit_reader_dependencies_apply_f": (C.c_bool, [V, V, C.CFUNCTYPE(C.c_bool, V, V)]),
        "indexstore_unit_dependency_get_kind": (C.c_int, [V]),
        "indexstore_unit_dependency_get_name": (_Str, [V]),
        "indexstore_unit_dependency_get_filepath": (_Str, [V]),
        "indexstore_record_reader_create": (V, [V, C.c_char_p, C.POINTER(V)]),
        "indexstore_record_reader_dispose": (None, [V]),
        "indexstore_record_reader_occurrences_apply_f": (C.c_bool, [V, V, C.CFUNCTYPE(C.c_bool, V, V)]),
        "indexstore_occurrence_get_roles": (C.c_uint64, [V]),
        "indexstore_occurrence_get_symbol": (V, [V]),
        "indexstore_occurrence_get_line_col": (None, [V, C.POINTER(C.c_uint), C.POINTER(C.c_uint)]),
        "indexstore_symbol_get_name": (_Str, [V]),
        "indexstore_symbol_get_usr": (_Str, [V]),
    }
    for name, (res, args) in sig.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = res, args
    _lib = lib
    return lib


ACCESSORS = {"getter", "setter", "_read", "_modify", "willSet", "didSet"}
RECORD = 2   # indexstore_unit_dependency_kind_t: a record (the symbols of one source file)


def find_store(build_dir: str) -> str | None:
    """The index store a build wrote under its scratch path: an `index/store` directory holding a versioned store
    (`v5`). SwiftPM's layout varies by toolchain (`<triple>/debug/index/store`, `out/Products/Debug/index/store`), so
    it's found rather than assumed; with several, the most recently written."""
    hits = []
    for dirpath, dirnames, _files in os.walk(build_dir):
        if os.path.basename(dirpath) == "store" and os.path.basename(os.path.dirname(dirpath)) == "index" and any(
                d.startswith("v") and d[1:].isdigit() for d in dirnames):
            hits.append(dirpath)
            dirnames[:] = []
        elif dirpath.count(os.sep) - build_dir.count(os.sep) >= 5:
            dirnames[:] = []
    return max(hits, key=os.path.getmtime) if hits else None


def occurrences(store_path: str, root: str) -> Iterator[tuple[str, int, int, str, str]]:
    """(file relative to root, 1-based line, roles, symbol usr, symbol name) for every occurrence the store holds in a
    file under root. A record shared by several units (one file in two targets) is read once."""
    lib = _load()
    V = C.c_void_p
    err = V()
    store = lib.indexstore_store_create(store_path.encode(), C.byref(err))
    if not store:
        raise IndexStoreError((lib.indexstore_error_get_description(err) or b"can't open the index store").decode())
    root_abs = os.path.abspath(root)
    try:
        units: list[str] = []
        unit_cb = C.CFUNCTYPE(C.c_bool, V, _Str)(lambda _ctx, name: units.append(_s(name)) or True)
        lib.indexstore_store_units_apply_f(store, 0, None, unit_cb)
        records: dict[str, str] = {}
        for unit in units:
            reader = lib.indexstore_unit_reader_create(store, unit.encode(), C.byref(err))
            if not reader:
                continue

            def dep(_ctx: Any, d: Any) -> bool:
                if lib.indexstore_unit_dependency_get_kind(d) == RECORD:
                    path = _s(lib.indexstore_unit_dependency_get_filepath(d))
                    if path.startswith(root_abs + os.sep):
                        records.setdefault(_s(lib.indexstore_unit_dependency_get_name(d)), os.path.relpath(path, root_abs))
                return True

            dep_cb = C.CFUNCTYPE(C.c_bool, V, V)(dep)
            lib.indexstore_unit_reader_dependencies_apply_f(reader, None, dep_cb)
            lib.indexstore_unit_reader_dispose(reader)
        for record, rel in sorted(records.items(), key=lambda kv: kv[1]):
            reader = lib.indexstore_record_reader_create(store, record.encode(), C.byref(err))
            if not reader:
                continue
            found: list[tuple[str, int, int, str, str]] = []

            def occ(_ctx: Any, o: Any) -> bool:
                line, col = C.c_uint(), C.c_uint()
                lib.indexstore_occurrence_get_line_col(o, C.byref(line), C.byref(col))
                sym = lib.indexstore_occurrence_get_symbol(o)
                found.append((rel, line.value, lib.indexstore_occurrence_get_roles(o),
                              _s(lib.indexstore_symbol_get_usr(sym)), _s(lib.indexstore_symbol_get_name(sym))))
                return True

            occ_cb = C.CFUNCTYPE(C.c_bool, V, V)(occ)
            lib.indexstore_record_reader_occurrences_apply_f(reader, None, occ_cb)
            lib.indexstore_record_reader_dispose(reader)
            yield from found
    finally:
        lib.indexstore_store_dispose(store)


def edges(store_path: str, root: str, spans: list[tuple[str, int, int, str, str]]
          ) -> tuple[list[list[str]], int, dict[str, list[list[Any]]]]:
    """Call and reference edges among the nodes in `spans` (file rel, start line, end line, node id, node name).

    A symbol is one of our nodes when its definition sits inside that node's span under the same name (labels
    aside). Each call or reference is attributed to the innermost node around it: a `calls` edge for a call, `refs`
    for any other mention. Returns (edges, how many calls went to something outside the tree, unplaced: node id →
    [[file rel, line], …] where a node is named outside every node the index models)."""
    by_file: dict[str, list[tuple[int, int, str, str]]] = {}
    for f, s, e, nid, name in spans:
        by_file.setdefault(f, []).append((s, e, nid, name.split("(", 1)[0]))

    def innermost(rel: str, line: int, name: str | None = None) -> str | None:
        best = None
        for s, e, nid, base in by_file.get(rel, []):
            if s <= line <= e and (name is None or base == name) and (best is None or e - s < best[1] - best[0]):
                best = (s, e, nid)
        return best[2] if best else None

    occs = list(occurrences(store_path, root))
    node_of: dict[str, str] = {}
    for rel, line, roles, usr, name in occs:
        if roles & DEFINITION and usr not in node_of:
            # a computed property's getter is its own symbol (`getter:body`): reading the property calls it
            base = name.split(":", 1)[1] if name.split(":", 1)[0] in ACCESSORS else name
            nid = innermost(rel, line, base.split("(", 1)[0])
            if nid:
                node_of[usr] = nid
    out: list[list[str]] = []
    outside = 0
    unplaced: dict[str, list[list[Any]]] = {}
    for rel, line, roles, usr, _name in occs:
        if not roles & REFERENCE or (roles & IMPLICIT and not roles & CALL):
            continue
        target = node_of.get(usr)
        if target is None:
            outside += bool(roles & CALL)
            continue
        source = innermost(rel, line)
        if source is None:
            unplaced.setdefault(target, []).append([rel, line])
        elif source != target:
            out.append([source, target, "calls" if roles & CALL else "refs"])
    calls = {(a, b) for a, b, r in out if r == "calls"}
    seen: set[tuple[str, str, str]] = set()
    result = []
    for a, b, r in out:
        if (r == "refs" and (a, b) in calls) or (a, b, r) in seen:
            continue
        seen.add((a, b, r))
        result.append([a, b, r])
    return result, outside, unplaced
