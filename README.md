# json-schema-diff

A JSON Schema breaking-change detector, built for API versioning.

Compares two JSON Schema documents -- or two **OpenAPI 3.x / Swagger 2.0** specs --
(old vs. new) and classifies every
difference as **BREAKING**, **compatible**, or **info** — so you can catch
an accidental breaking change to your API contract before you ship it,
instead of finding out from an angry integration partner.

Zero dependencies (Python stdlib only — `json` + `argparse`), no network
calls, works entirely offline. Supports the common subset of JSON Schema
(draft-07 / 2020-12) actually used in real API schemas: `type`,
`properties`, `required`, `additionalProperties`, `enum`, `items`, `format`,
`minimum`/`maximum`, `exclusiveMinimum`/`exclusiveMaximum`, `minLength`/`maxLength`,
`minItems`/`maxItems`, `pattern`, **`const`**, **`oneOf`/`anyOf`/`allOf`/`not`**,
**`nullable`** (OpenAPI 3.0), **`dependentRequired`**/`dependencies`, and local `$ref`s (any
JSON pointer: `#/definitions/...`, `#/$defs/...`, `#/components/schemas/...`, including
recursive schemas), which are followed so a change inside a shared definition is reported
at every place it's used. Python 3.8+. Specs in YAML need converting to JSON first
(e.g. `yq -o json spec.yaml > spec.json`).

## What it catches

- **Type narrowing** — `["string","integer"]` -> `"string"` or `number` -> `integer`
  (BREAKING), a `type` added where there was none (BREAKING), or a straight
  incompatible type swap (BREAKING); `integer` -> `number` is a widening (compatible)
- **Newly-required fields** — a field that used to be optional is now
  required; existing producers that omit it will fail validation (BREAKING)
- **Removed required fields** — a required field disappears entirely;
  any consumer depending on it breaks (BREAKING)
- **`additionalProperties` tightened to `false`** — previously-accepted
  extra fields get rejected (BREAKING); removing an optional field from an object
  with `additionalProperties: false` is BREAKING too (documents still sending it are
  rejected), while in an open object it's compatible
- **Enum values removed** — previously-valid values now rejected (BREAKING)
- **Numeric/length constraints narrowed** — `minimum` raised, `maximum`
  lowered, `maxLength` shortened, etc. (BREAKING); the reverse relaxations
  are flagged as compatible. `minimum`/`exclusiveMinimum` (and the maximum pair) are judged
  together, so `exclusiveMinimum: 0` -> `minimum: 0` is a relaxation
- **Pattern changed** — treated conservatively as BREAKING unless it's
  unchanged, since proving one regex is a strict superset of another is
  out of scope for a stdlib tool
- Everything with the opposite effect (new optional fields, relaxed
  constraints, added enum values, widened types, removed constraints) is
  classified **compatible**
- `format` hint changes are **info**-only, since most validators only warn
  on format anyway
- **Composition**: a `oneOf`/`anyOf` variant removed (BREAKING) or added (anyOf:
  compatible; oneOf: info, because an overlapping new variant makes some documents match
  twice), an `allOf` subschema added (BREAKING) or removed (compatible), and changes inside
  a variant -- variants are paired by `$ref` name, `title`, `const`/discriminator value or type
- `const` added or changed (BREAKING), `null` no longer accepted (`nullable`/`type: null`
  removed: BREAKING), new `dependentRequired` entries (BREAKING)

### OpenAPI mode

Detected automatically from the `openapi`/`swagger` key. It compares paths and operations
(removed endpoint/operation: BREAKING), parameters (new required parameter, parameter became
required, narrower parameter schema: BREAKING), request bodies (became required, content type
dropped, narrower schema: BREAKING) and responses (2xx response removed: BREAKING). **Request
schemas are judged from the client's side** (a narrower schema rejects requests that used to
work) and **response schemas from the consumer's side**: a new enum value, a field no longer
in `required`, a removed field or a widened type in a response breaks clients, while a new
optional response field doesn't.

## Install

```sh
pipx install git+https://git.canutethegreat.com/CanuteTheGreat/json-schema-diff.git        # latest release branch; puts `json-schema-diff` on PATH
pipx install git+https://git.canutethegreat.com/CanuteTheGreat/json-schema-diff.git@v1.2.1  # a pinned release
brew install canutethegreat/errant-tools/json-schema-diff   # after: brew tap canutethegreat/errant-tools https://git.canutethegreat.com/CanuteTheGreat/homebrew-errant-tools
scoop install errant-tools/json-schema-diff                 # after: scoop bucket add errant-tools https://git.canutethegreat.com/CanuteTheGreat/scoop-errant-tools
```

Or download the single file (stdlib-only Python 3.8+) and run it directly:
`curl -fsSLO https://errant.solutions/downloads/schema_diff.py && python3 schema_diff.py --help`.
Every [release](https://git.canutethegreat.com/CanuteTheGreat/json-schema-diff/releases) has a changelog, the wheel, the sdist and a `SHA256SUMS` file.
Not on PyPI yet, so plain `pipx install json-schema-diff` doesn't work until it's published there.

## Usage

```
json-schema-diff old_schema.json new_schema.json
json-schema-diff old_schema.json new_schema.json --json
json-schema-diff openapi-v1.json openapi-v2.json                 # OpenAPI/Swagger, detected automatically
json-schema-diff old.json new.json --markdown >> CHANGELOG.md     # changelog section
json-schema-diff old.json new.json --fail-on never                # report only
```

Exit codes (same convention as every Errant Solutions tool):

| Code | Meaning |
|---|---|
| 0 | nothing at or above `--fail-on` (default `breaking`) |
| 1 | at least one BREAKING change (or any compatible change with `--fail-on compatible`) |
| 2 | usage/parse error, or one file is OpenAPI and the other a plain schema |

1.x note: the tool used to exit 0 unless `--fail-on-breaking` was given; it now gates on
BREAKING by default (`--fail-on-breaking` is still accepted).

## Why this exists

Most API teams find out about a breaking schema change when a downstream
consumer's integration starts failing in production — usually days after
the change shipped, and often traced back to something as small as a field
quietly becoming `required` or an enum value getting dropped. Running this
in CI against the last-released schema and the current one turns that into
a merge-time check instead of a customer-facing incident.

## License / pricing

MIT — see LICENSE. Free; pay what you want if it caught a breaking change for you:
https://errant.solutions (payment link on the site).

## Tests

`tests/` checks every classification against a real validator (`jsonschema`): each
validation-level BREAKING finding comes with a document the old schema accepts and the new one
rejects, and `tests/test_accuracy.py` runs ~30 single-keyword edits in both directions (request
and response) against a document pool, failing on any false negative or false positive. The
contract conventions above (a removed required field, a removed response field, a new optional
field, extra response fields) are asserted explicitly there. OpenAPI mode is
covered with 3.0 and Swagger 2.0 fixtures. CI runs it on Python 3.8 and current Python, then
installs the package with pip and checks `--version`/`--help`.
