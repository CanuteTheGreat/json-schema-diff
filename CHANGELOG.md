# Changelog

All notable changes. Versions are git tags; each tag has a Forgejo release with checksums.

## Unreleased

- `minimum`/`exclusiveMinimum` and `maximum`/`exclusiveMaximum` are judged together: `exclusiveMinimum: 0` -> `minimum: 0` is a relaxation (was reported BREAKING as "minimum newly added"), and a rewrite with the same effect is info.
- Tests: oracle-based accuracy suite (~30 edits x request/response/undo, judged by `jsonschema` against a document pool), mutation testing (nightly), claims ledger.

## 1.2.0 - 2026-10-16

- **OpenAPI 3.x and Swagger 2.0 input**: paths, operations, parameters, request bodies and responses are compared,
  with request schemas judged from the client's side and response schemas from the consumer's side.
- **`oneOf`/`anyOf`/`allOf`/`not`**, `const`, `nullable`, `dependentRequired`/`dependencies`, exclusive bounds and
  min/maxProperties; composition variants are paired by `$ref` name, title, const or type so changes inside
  them are reported.
- `--markdown` changelog output.
- **Exit codes now follow the shared convention:** BREAKING changes exit 1 by default (`--fail-on`, default
  `breaking`; `never` for report-only). 1.x exited 0 unless `--fail-on-breaking` was given.
- MIT license and pay-what-you-want sections in the README.
- Installable as a package: `pipx install git+https://git.canutethegreat.com/CanuteTheGreat/json-schema-diff.git`
  puts a `json-schema-diff` command on PATH (pyproject.toml, MIT license metadata).
- `--version` flag.
- Exit codes follow the shared convention: 0 = clean, 1 = findings at/above the gate, 2 = error or bad usage.
- Every tag now publishes a Forgejo release with this changelog, the wheel, the sdist and SHA256SUMS.
- CI installs the package with pip in a clean venv and checks `--version`/`--help` on every push.

## 1.1.0 - 2026-10-16

- Follow local $refs (incl. recursive), integer->number is a widening, type added/removed, optional-field removal from closed objects is breaking, '$' root path; add CI tests validated with jsonschema
- Add MIT license

## 1.0.0 - 2026-09-17

- First public release.
