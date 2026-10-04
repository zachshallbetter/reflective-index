# reflective-index

A local semantic index of a codebase: every class, function and method, where it lives, and how they connect.
Agents and tools can read one function instead of a 12,000-line file, and ask "who calls this?" without
searching text.

The index is a graph in the shape of [reflective-rust](../reflective-rust)'s Compiler Semantic Graph
(`rr-csg-0.1`): nodes with spans and a minimum access level, typed edges, and a provenance. That means the
same access-aware slicing works on it. The extensions it needs beyond Rust (class and method nodes, and an
`extractor` provenance for syntax-level facts) are documented as a draft in [docs/SCHEMA.md](docs/SCHEMA.md).

- **Local.** It reads files on this machine. Nothing is sent anywhere.
- **No dependencies.** The Python standard library only (Python 3.10+).
- **Certain or silent.** A call is linked only when its target can be resolved for sure. The rest are counted
  as unresolved, never guessed.
- **Incremental.** Per-file facts are cached by content hash, so a rebuild only re-reads what changed.

## Languages

| Language | Extractor | Nodes | Edges |
|---|---|---|---|
| Python | standard library `ast` | module, class, function, method | contains, imports, calls, inherits |
| Swift | planned: `sourcekit-lsp` (symbols, references, call hierarchy) | | |

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

A symbol is its id (`py:path/file.py::Class.method`) or a name that matches exactly one symbol.

### As an MCP server

```json
{"mcpServers": {"reflective-index": {"command": "reflective-index-mcp", "args": ["."]}}}
```

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

On titan-node (342 Python files): 3,874 symbols, 4,784 resolved calls, about 6 s for a cold build and
0.4 s warm.

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```
