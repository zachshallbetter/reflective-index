# reflective-index

A local semantic index of a codebase: every class, function and method, where it lives, and how they connect.
Agents and tools can read one function instead of a 12,000-line file, and ask "who calls this?" without
searching text.

The index is a graph in the shape of [reflective-rust](../reflective-rust)'s Compiler Semantic Graph
(`rr-csg-0.1`): nodes with spans and a minimum access level, typed edges, and a provenance. That means the
same access-aware slicing works on it. The extensions it needs beyond Rust (class and method nodes, and an
`extractor` provenance for syntax-level facts) are documented as a draft in [docs/SCHEMA.md](docs/SCHEMA.md).

- **Local.** It reads files on this machine. Nothing is sent anywhere.
- **No dependencies.** The Python standard library only (Python 3.10+). Swift uses the toolchain's own
  `sourcekit-lsp` (with Xcode or a Swift toolchain installed); without it, Swift files are listed as unparsed.
- **Certain, and says when it isn't complete.** A call is linked only when its target can be resolved for sure. The
  rest are counted as unresolved, never guessed, and a `callers` or `callees` answer ends with a note when such a gap
  could make it short, so an empty list is never mistaken for "none". A function named without being called (`x = m.f`,
  `to_thread(m.f, a)`, a dispatch table) is a `refs` edge, listed after the calls.
- **Incremental.** Per-file facts are cached by content hash, so a rebuild only re-reads what changed.

## Languages

| Language | Extractor | Nodes | Edges |
|---|---|---|---|
| Python | standard library `ast` | module, class, function, method | contains, imports, calls, refs, inherits |
| Swift | `sourcekit-lsp` over stdio: document symbols per file; for a SwiftPM package, calls and references read from the compiler's index store through the toolchain's `libIndexStore` (`--swift-calls`) | module, class, struct, enum, protocol, extension, function, method, property (one written over several lines, like a view's `body`) | contains, calls, refs |

## Use

```sh
pip install -e .        # or: PYTHONPATH=src python3 -m reflective_index.cli ...

reflective-index ROOT stats
reflective-index ROOT outline scripts/routines.py
reflective-index ROOT find similar
reflective-index ROOT callers 'py:scripts/routines.py::save'
reflective-index ROOT read 'py:scripts/routines.py::save'
reflective-index ROOT slice 'py:scripts/routines.py::save' 1
reflective-index ROOT csg > graph.json
```

A symbol is its id (`py:path/file.py::Class.method`, `swift:Sources/App/Model.swift::Model.load(from:)`) or a name
that matches exactly one symbol.

Swift calls are linked for SwiftPM packages only, and only when asked (`--swift-calls`, or `Index(..., swift_calls=True)`):
each package is built with `swift build --enable-index-store` into the cache directory (one scratch build per package,
incremental, never the package's own `.build`), and the index store that build writes is read directly through the
toolchain's `libIndexStore` (over `ctypes`, no language server). Each call is attributed to the innermost function,
method or multi-line property around it (a call in a closure belongs to the function the closure is in); a function
named without being called (`#selector(f)`, `let g = f`) is a `refs` edge; a place nothing modelled encloses is listed
in the answer's note. A call into the SDK or anything outside the tree isn't an edge. The edges are reused until one of
the package's Swift files changes. Files outside a package (an Xcode project's) get structure only.

### Reading an answer

```text
$ reflective-index ROOT callees 'py:scripts/tv_unlock.py::answer'
function py:scripts/tv_unlock.py::_db  (scripts/tv_unlock.py:46-56)
…
names without a resolved call (used as a value, or called through a local name):
  function py:scripts/proactive.py::add_card  (scripts/proactive.py:194-250)
note: 11 calls in this function couldn't be resolved (a local name, a parameter, a method on an object); an empty or short list doesn't mean none (read it with read_symbol).
```

Calls into a builtin or a module outside the repository aren't gaps, so they aren't counted.

### As an MCP server

```json
{"mcpServers": {"reflective-index": {"command": "reflective-index-mcp", "args": ["."]}}}
```

Add `"--swift-calls"` to the args for a Swift package's call edges (the first call builds its index store).
A name that matches more than one symbol is answered with the candidate ids, so a caller can pick one.

The tools are `outline`, `find`, `callers`, `callees`, `read_symbol`, `slice` and `stats`, all read-only.
Start it with `--allow-edit` to add `replace_symbol`. It swaps one symbol's source, re-parses the file, and
refuses, leaving the file untouched, when the result doesn't parse or no longer defines that symbol.

### As a library

```python
from reflective_index import Index

idx = Index("path/to/repo").build()
idx.callers("py:app.py::main")
idx.slice("py:app.py::main", radius=1, access="member")
```

## Numbers

On titan-node (343 Python files): 3,891 symbols, 6,531 resolved calls and 727 `refs`, about 2 s for a cold build and
0.2 s warm. On Jellybean's `mac/` package (269 Swift files) with `--swift-calls`: 7,186 symbols, 9,631 calls and 130
`refs`. A cold run is the build plus a few seconds to read the store (1 min 44 s here, with the package's scratch build
already present); after a Swift file changes, about 15 s (an incremental build and the read); otherwise 0.2 s.

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```
