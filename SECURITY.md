# Security Policy

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue. Use GitHub private vulnerability
reporting: <https://github.com/zachshallbetter/reflective-index/security/advisories/new>.

Include:

- a description of the issue and its impact,
- steps to reproduce (a minimal example is ideal),
- the affected commit or version.

This is a solo-maintained project. Responses and fixes are best-effort, with no SLA and no guaranteed response time.
Please give a reasonable window to ship a fix before any public disclosure.

## Scope

reflective-index is a local tool. It reads source files on the machine it runs on, writes a cache under its cache directory, and sends nothing over the network. Its MCP server speaks over stdio and is read-only unless started with `--allow-edit`, which adds `replace_symbol` (it rewrites one symbol's source in place). With `--swift-calls` it runs `swift build --enable-index-store` on the indexed Swift package. Reports about path handling (reading or writing outside the indexed root), unsafe edits through `replace_symbol`, or untrusted source causing code execution during indexing are in scope.

## Supported versions

While the version is 0.x (`pyproject.toml` declares 0.1.0), only the latest commit on `main` receives fixes. No versions are tagged or released yet.
