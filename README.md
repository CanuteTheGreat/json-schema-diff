# json-schema-diff

A JSON Schema breaking-change detector, built for API versioning.

Compares two JSON Schema documents (old vs. new) and classifies every
difference as **BREAKING**, **compatible**, or **info** — so you can catch
an accidental breaking change to your API contract before you ship it,
instead of finding out from an angry integration partner.

Zero dependencies (Python stdlib only — `json` + `argparse`), no network
calls, works entirely offline. Supports the common subset of JSON Schema
(draft-07 / 2020-12) actually used in real API schemas: `type`,
`properties`, `required`, `additionalProperties`, `enum`, `items`, `format`,
`minimum`/`maximum`, `minLength`/`maxLength`, `minItems`/`maxItems`, `pattern`, and
local `$ref`s (`#/definitions/...`, `#/$defs/...`, including recursive schemas), which
are followed so a change inside a shared definition is reported at every place it's used.
Python 3.8+.

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
  are flagged as compatible
- **Pattern changed** — treated conservatively as BREAKING unless it's
  unchanged, since proving one regex is a strict superset of another is
  out of scope for a stdlib tool
- Everything with the opposite effect (new optional fields, relaxed
  constraints, added enum values, widened types, removed constraints) is
  classified **compatible**
- `format` hint changes are **info**-only, since most validators only warn
  on format anyway

## Usage

```
./schema_diff.py old_schema.json new_schema.json
./schema_diff.py old_schema.json new_schema.json --json
./schema_diff.py old_schema.json new_schema.json --fail-on-breaking   # CI gate
```

Exit codes: without `--fail-on-breaking`, always exits `0` (safe to run
informationally without gating a build). With `--fail-on-breaking`, exits
`1` if any BREAKING finding is present. Exits `2` on a usage/parse error.

## Why this exists

Most API teams find out about a breaking schema change when a downstream
consumer's integration starts failing in production — usually days after
the change shipped, and often traced back to something as small as a field
quietly becoming `required` or an enum value getting dropped. Running this
in CI against the last-released schema and the current one turns that into
a merge-time check instead of a customer-facing incident.
