# Graph schema: `rr-csg-0.1+ri-ext-draft`

reflective-index writes reflective-rust's Compiler Semantic Graph exchange format
(`reflective-rust-csg`, schema `rr-csg-0.1`), with two extensions. Both are drafts, proposed for
reflective-rust rather than defined here.

## Unchanged from `rr-csg-0.1`

```text
graph  { schema_version, provenance, nodes[], edges[] }
node   { id, name, kind, span { file, start_line, end_line }, minimum_access, size_bytes }
edge   { from, to, relationship, minimum_access }
```

- `minimum_access` uses reflective-rust's `AccessLevel`, lowest first: `public`, `member`, `owner`, `administrator`.
  A caller sees a node or edge when its own level is at least the required one.
- Every edge's `from` and `to` name nodes in the same graph (no dangling edges), and every span has
  `start_line <= end_line`.

## Extension 1: node kinds beyond Rust's

`rr-csg-0.1` kinds are Rust's: crate, module, struct, enum, field, function, trait, impl. Other languages need:

| Kind | Meaning |
|---|---|
| `class` | a class (Python, Swift) |
| `method` | a function defined in a class body |
| `struct` | a Swift struct |
| `enum` | a Swift enum |
| `protocol` | a Swift protocol |
| `extension` | a Swift extension; the members it declares belong to the extended type |

`module` and `function` keep their rr-csg meaning.

## Extension 2: `extractor` provenance

`rr-csg-0.1` has two provenance kinds: `compiler`, a compiler-certified extractor, and `fixture`, hand-authored.
Facts read from syntax by a parser are neither, so reflective-index uses a third:

```json
{"kind": "extractor", "extractor": "reflective-index", "languages": ["python"], "source_hash": "sha256:…"}
```

- Not compiler-certified. A Python call edge is a syntactic fact resolved only where the name binding is certain.
- `source_hash` covers every indexed file's content hash, so a graph can be matched to the tree it came from.

## Ids

```text
py:<path>                      a module
py:<path>::<Class>             a class
py:<path>::<function>          a module-level function
py:<path>::<Class>.<method>    a method
py:<path>::<outer>.<inner>     a nested function
```

Swift:

```text
swift:<path>                         a file
swift:<path>::<Type>                 a type (nested: Outer.Inner)
swift:<path>::<Type>.<member>        a method or initializer, with its argument labels: Model.load(from:), Model.init()
swift:<path>::<function>             a top-level function
swift:<path>::extension <Type>       an extension (a second one in the same file: extension <Type> #2)
```

## Edge relationships

| Relationship | From → to |
|---|---|
| `contains` | module → class/function, class → method, function → nested function |
| `imports` | module → module (repository modules only) |
| `calls` | function/method → function, method or class (construction). Swift: from a SwiftPM package's index store |
| `inherits` | class → base class |
