# Contributing to reflective-index

Thanks for your interest. This is a solo-maintained project, so review is best-effort (see [SUPPORT.md](SUPPORT.md)).
Open an issue before a large change so the direction can be agreed first.

## Setup

reflective-index needs Python 3.10+ and has no runtime dependencies (the standard library only).

```sh
pip install -e .        # or run in place: PYTHONPATH=src python3 -m reflective_index.cli ROOT stats
```

Swift support uses the toolchain's own `sourcekit-lsp` and, with `--swift-calls`, `swift build` and `libIndexStore`.
It needs Xcode or a Swift toolchain; without one, Swift files are listed as unparsed.

## Tests

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

The tests are `unittest` modules in `tests/` (`test_python.py`, `test_swift.py`, `test_indexstore.py`, `test_mcp.py`).
Swift fixtures are in `tests/fixtures/swift/`.

## Conventions

- **Standard library only.** A runtime dependency needs a strong justification.
- **Certain, or counted as unresolved.** An edge is added only when its target resolves for sure; never guess a call.
- **The schema is documented.** A change to node kinds, edge kinds or provenance updates [docs/SCHEMA.md](docs/SCHEMA.md).
- **The README stays true.** Update the README with any change to a command, a language or a tool.
- **Commits** are one short imperative sentence describing the change, with the pull request number appended on
  merge, for example `callers and callees say when they may be short (#2)`.

## Pull requests

Keep one change per pull request, run the tests above, and fill in the pull request template.
