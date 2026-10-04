"""reflective-index ROOT <command> [args]

  stats                    files, nodes and edges by kind, unresolved calls
  outline FILE             a file's classes, functions and methods with line ranges
  find NAME                symbols by name (exact, else substring)
  callers SYMBOL           who calls it
  callees SYMBOL           what it calls
  read SYMBOL              its source, line-numbered
  slice SYMBOL [RADIUS]    it and its neighbourhood, with edges (JSON)
  csg                      the whole graph (JSON)

SYMBOL is an id (py:path/file.py::Class.method, swift:path/File.swift::Type.method()) or a name that matches exactly
one symbol. --swift-calls also links Swift calls for SwiftPM packages (it builds each package's index store once).
"""
from __future__ import annotations

import json
import sys

from .graph import Index


def brief(n: dict) -> str:
    sp = n["span"]
    return f"{n['kind']:<8} {n['id']}  ({sp['file']}:{sp['start_line']}-{sp['end_line']})"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv[:1] in (["-h"], ["--help"]) else 2
    swift_calls = "--swift-calls" in argv
    argv = [a for a in argv if a != "--swift-calls"]
    idx = Index(argv[0], swift_calls=swift_calls).build()
    cmd, args = argv[1], argv[2:]
    if cmd == "stats":
        print(json.dumps(idx.stats(), indent=2))
    elif cmd == "csg":
        print(json.dumps(idx.csg(), indent=1))
    elif cmd == "outline" and args:
        print("\n".join(brief(n) for n in idx.outline(args[0])))
    elif cmd == "find" and args:
        print("\n".join(brief(n) for n in idx.find(args[0])))
    elif cmd in ("callers", "callees", "read", "slice") and args:
        n = idx.node(args[0])
        if not n:
            print(f"no single symbol matches {args[0]!r}; try `find`", file=sys.stderr)
            return 1
        if cmd == "read":
            print(idx.read(n["id"]))
        elif cmd == "slice":
            print(json.dumps(idx.slice(n["id"], int(args[1]) if len(args) > 1 else 1), indent=1))
        else:
            print("\n".join(brief(x) for x in getattr(idx, cmd)(n["id"])))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
