#!/usr/bin/env python3
"""
schema_diff.py -- JSON Schema and OpenAPI breaking-change detector (json-schema-diff).

Compares two JSON Schema documents -- or two OpenAPI 3.x / Swagger 2.0 specs --
(old vs new) and classifies every difference as BREAKING, non-breaking
(compatible/additive), or informational. Built for API versioning: run it in CI
against the previous released contract vs. the current one to catch accidental
breaking changes before they ship.

Zero dependencies (Python stdlib only, json + argparse), no network calls,
works entirely offline. JSON Schema keywords understood: type, properties,
required, additionalProperties, enum, const, items, local $ref (any JSON
pointer, incl. #/definitions, #/$defs, #/components/schemas, recursive
schemas), oneOf / anyOf / allOf / not, nullable (OpenAPI 3.0),
dependentRequired / dependencies, format, minimum/maximum,
exclusiveMinimum/exclusiveMaximum, minLength/maxLength, minItems/maxItems,
pattern. YAML specs need converting to JSON first (e.g. `yq -o json`).

OpenAPI mode (detected from the top-level "openapi"/"swagger" key) compares
paths, operations, parameters, request bodies and responses. Request schemas
are judged from the client's side (a narrower schema rejects requests that
used to work), response schemas from the consumer's side (a wider schema --
a new enum value, a field no longer required -- breaks clients).

Usage:
    json-schema-diff old_schema.json new_schema.json
    json-schema-diff old_schema.json new_schema.json --json
    json-schema-diff openapi-v1.json openapi-v2.json --fail-on breaking
    json-schema-diff old.json new.json --markdown >> CHANGELOG.md

Exit codes:
    0 = nothing at or above --fail-on (default: breaking)
    1 = at least one breaking change (or compatible change, with --fail-on compatible)
    2 = usage/parse error
"""

__version__ = "1.2.0"

import argparse
import json
import sys

BREAKING = "BREAKING"
COMPATIBLE = "compatible"
INFO = "info"
SWAP = {BREAKING: COMPATIBLE, COMPATIBLE: BREAKING, INFO: INFO}
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


class Finding:
    def __init__(self, path, severity, message):
        self.path = path
        self.severity = severity
        self.message = message

    def to_dict(self):
        return {"path": _fmt_path(self.path), "severity": self.severity, "message": self.message}

    def __str__(self):
        tag = {"BREAKING": "BREAKING ", "compatible": "compat   ", "info": "info     "}[self.severity]
        return f"[{tag}] {_fmt_path(self.path)}: {self.message}"


class Ctx:
    """Where findings go, plus how to judge them: direction "request" (the schema validates input:
    narrowing breaks) or "response" (the schema describes output: widening breaks consumers)."""

    def __init__(self, findings, roots, direction="request"):
        self.findings, self.roots, self.direction = findings, roots, direction

    def add(self, path, sev, message, response_sev=None):
        if self.direction == "response":
            sev = response_sev if response_sev is not None else SWAP[sev]
        self.findings.append(Finding(path, sev, message))

    def child(self, direction=None):
        return Ctx(self.findings, self.roots, direction or self.direction)


def load_schema(path):
    with open(path) as f:
        return json.load(f)


def _fmt_path(path):
    return path if path else "$"


def _join(path, key):
    return f"{path}.{key}" if path else key


NUMERIC_NARROWING = {
    "minimum": lambda old, new: new > old,
    "maximum": lambda old, new: new < old,
    "exclusiveMinimum": lambda old, new: new > old,
    "exclusiveMaximum": lambda old, new: new < old,
    "minLength": lambda old, new: new > old,
    "maxLength": lambda old, new: new < old,
    "minItems": lambda old, new: new > old,
    "maxItems": lambda old, new: new < old,
    "minProperties": lambda old, new: new > old,
    "maxProperties": lambda old, new: new < old,
}


def _types(schema):
    t = schema.get("type")
    if t is None:
        return None
    s = set(t) if isinstance(t, list) else {t}
    if schema.get("nullable") is True:
        s.add("null")
    return s


def diff_type(old, new, path, ctx):
    old_set, new_set = _types(old), _types(new)
    if old_set is None and new_set is None:
        return
    if old_set is None:
        ctx.add(path, BREAKING, f"type constraint newly added ({new.get('type')!r}) -- values of other types now rejected")
        return
    if new_set is None:
        ctx.add(path, COMPATIBLE, f"type constraint removed (was {old.get('type')!r})")
        return
    if old_set == new_set:
        return

    def covers(a, b):
        # every value of the types in b is accepted by a ("number" includes "integer")
        a = set(a) | ({"integer"} if "number" in a else set())
        return set(b) <= a

    if old_set - {"null"} == new_set - {"null"}:
        if "null" in old_set:
            ctx.add(path, BREAKING, "null no longer accepted (nullable/type 'null' removed)")
        else:
            ctx.add(path, COMPATIBLE, "null now accepted (nullable/type 'null' added)")
    elif covers(old_set, new_set) and not covers(new_set, old_set):
        ctx.add(path, BREAKING, f"type narrowed from {sorted(old_set)} to {sorted(new_set)} "
                                f"-- previously-valid values may now be rejected")
    elif covers(new_set, old_set):
        ctx.add(path, COMPATIBLE, f"type widened from {sorted(old_set)} to {sorted(new_set)} -- more values now accepted")
    else:
        ctx.add(path, BREAKING, f"type changed from {sorted(old_set)} to {sorted(new_set)} "
                                f"-- incompatible sets, likely breaking for both producers and consumers",
                response_sev=BREAKING)


def diff_required(old, new, path, ctx):
    old_req = set(old.get("required", []))
    new_req = set(new.get("required", []))
    for field in sorted(new_req - old_req):
        ctx.add(_join(path, field), BREAKING,
                "field newly marked 'required' -- existing producers that omit it will now fail validation"
                if ctx.direction == "request" else "field now always present (newly required)")
    for field in sorted(old_req - new_req):
        ctx.add(_join(path, field), COMPATIBLE,
                "field no longer required -- safe relaxation" if ctx.direction == "request" else
                "field no longer guaranteed (removed from 'required') -- consumers relying on it break")


def diff_additional_properties(old, new, path, ctx):
    old_ap = old.get("additionalProperties", True)
    new_ap = new.get("additionalProperties", True)
    if old_ap == new_ap:
        return
    old_permits = old_ap is not False
    new_permits = new_ap is not False
    if old_permits and not new_permits:
        ctx.add(path, BREAKING, "additionalProperties changed to false -- previously-accepted extra fields will now be rejected")
    elif not old_permits and new_permits:
        ctx.add(path, COMPATIBLE, "additionalProperties relaxed (no longer false) -- extra fields now allowed",
                response_sev=INFO)
    else:
        ctx.add(path, INFO, "additionalProperties schema changed (non-boolean form)")


def _hashable(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, sort_keys=True)
    return v


def diff_enum(old, new, path, ctx):
    if "enum" not in old and "enum" not in new:
        return
    old_enum = set(map(_hashable, old.get("enum", []))) if "enum" in old else None
    new_enum = set(map(_hashable, new.get("enum", []))) if "enum" in new else None
    if old_enum is None:
        ctx.add(path, BREAKING, f"enum constraint newly added, restricting values to {sorted(map(str, new_enum))}")
        return
    if new_enum is None:
        ctx.add(path, COMPATIBLE, "enum constraint removed -- any value now allowed")
        return
    removed = old_enum - new_enum
    added = new_enum - old_enum
    if removed:
        ctx.add(path, BREAKING, f"enum values removed: {sorted(map(str, removed))} -- previously-valid values now rejected")
    if added:
        ctx.add(path, COMPATIBLE, f"enum values added: {sorted(map(str, added))}"
                + ("" if ctx.direction == "request" else " -- clients that switch on the value may not handle them"))


def diff_const(old, new, path, ctx):
    if "const" not in old and "const" not in new:
        return
    if "const" not in old:
        ctx.add(path, BREAKING, f"const newly added ({new['const']!r}) -- only that value is accepted now")
    elif "const" not in new:
        ctx.add(path, COMPATIBLE, f"const removed (was {old['const']!r})")
    elif _hashable(old["const"]) != _hashable(new["const"]):
        ctx.add(path, BREAKING, f"const changed from {old['const']!r} to {new['const']!r}", response_sev=BREAKING)


def _dependent_required(schema):
    out = dict(schema.get("dependentRequired") or {})
    for k, v in (schema.get("dependencies") or {}).items():
        if isinstance(v, list):
            out.setdefault(k, v)
    return {k: set(v) for k, v in out.items()}


def diff_dependent_required(old, new, path, ctx):
    o, n = _dependent_required(old), _dependent_required(new)
    for key in sorted(set(o) | set(n)):
        added = n.get(key, set()) - o.get(key, set())
        removed = o.get(key, set()) - n.get(key, set())
        if added:
            ctx.add(_join(path, key), BREAKING, f"when '{key}' is present, {sorted(added)} are now also required")
        if removed:
            ctx.add(_join(path, key), COMPATIBLE, f"'{key}' no longer requires {sorted(removed)}")


def _bound(schema, inclusive_key, exclusive_key, lower):
    """Effective numeric bound as (value, exclusive), or None (draft-06+ numeric exclusive*):
    the stricter of the inclusive and the exclusive keyword when both are given."""
    best = None
    for key, excl in ((inclusive_key, False), (exclusive_key, True)):
        v = schema.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        if best is None or (v > best[0] if lower else v < best[0]):
            best = (v, excl)
        elif v == best[0]:
            best = (v, best[1] or excl)
    return best


def diff_numeric_bounds(old, new, path, ctx):
    """minimum/exclusiveMinimum (and maximum/exclusiveMaximum) judged together, so replacing
    `exclusiveMinimum: 0` with `minimum: 0` is a relaxation, not 'minimum newly added'."""
    for inc, exc, lower in (("minimum", "exclusiveMinimum", True), ("maximum", "exclusiveMaximum", False)):
        if old.get(inc) == new.get(inc) and old.get(exc) == new.get(exc):
            continue
        if any(isinstance(sch.get(k), bool) for sch in (old, new) for k in (inc, exc)):
            continue  # draft-04 boolean exclusive*: reported as info by diff_numeric_constraints
        ob, nb = _bound(old, inc, exc, lower), _bound(new, inc, exc, lower)
        name = lambda b: exc if b[1] else inc  # noqa: E731
        if ob == nb:
            ctx.add(path, INFO, f"{inc}/{exc} rewritten with the same effect ({name(ob)} {ob[0]})")
        elif ob is None:
            ctx.add(path, BREAKING, f"{name(nb)} constraint newly added ({nb[0]}) -- may reject previously-valid values")
        elif nb is None:
            ctx.add(path, COMPATIBLE, f"{name(ob)} constraint removed (was {ob[0]})")
        else:
            narrower = (nb[0] > ob[0] if lower else nb[0] < ob[0]) or (nb[0] == ob[0] and nb[1] and not ob[1])
            if name(ob) == name(nb):
                change = f"{name(nb)} {'narrowed' if narrower else 'relaxed'} from {ob[0]} to {nb[0]}"
            else:
                change = (f"{'lower' if lower else 'upper'} bound {'narrowed' if narrower else 'relaxed'} from "
                          f"{name(ob)} {ob[0]} to {name(nb)} {nb[0]}")
            if narrower:
                ctx.add(path, BREAKING, change + " -- some previously-valid values now rejected")
            else:
                ctx.add(path, COMPATIBLE, change)


def diff_numeric_constraints(old, new, path, ctx):
    diff_numeric_bounds(old, new, path, ctx)
    for key, is_narrowing in NUMERIC_NARROWING.items():
        o, n = old.get(key), new.get(key)
        if key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum") and not (
                isinstance(o, bool) or isinstance(n, bool)):
            continue  # judged together by diff_numeric_bounds
        if isinstance(o, bool) or isinstance(n, bool):  # draft-04 boolean exclusiveMinimum: compare as info
            if o != n:
                ctx.add(path, INFO, f"{key} changed from {o!r} to {n!r}")
            continue
        if key in old and key in new:
            if o != n:
                if is_narrowing(o, n):
                    ctx.add(path, BREAKING, f"{key} narrowed from {o} to {n} -- some previously-valid values now rejected")
                else:
                    ctx.add(path, COMPATIBLE, f"{key} relaxed from {o} to {n}")
        elif key in old:
            ctx.add(path, COMPATIBLE, f"{key} constraint removed (was {o})")
        elif key in new:
            ctx.add(path, BREAKING, f"{key} constraint newly added ({n}) -- may reject previously-valid values")


def diff_pattern(old, new, path, ctx):
    old_p, new_p = old.get("pattern"), new.get("pattern")
    if old_p == new_p:
        return
    if old_p is None:
        ctx.add(path, BREAKING, f"pattern constraint newly added: {new_p!r}")
    elif new_p is None:
        ctx.add(path, COMPATIBLE, "pattern constraint removed")
    else:
        ctx.add(path, BREAKING, f"pattern changed from {old_p!r} to {new_p!r} -- treat as breaking unless proven a strict "
                                "superset", response_sev=BREAKING)


def diff_format(old, new, path, ctx):
    old_f, new_f = old.get("format"), new.get("format")
    if old_f == new_f:
        return
    if old_f is None:
        ctx.add(path, INFO, f"format hint newly added: {new_f!r} (most validators only warn)")
    elif new_f is None:
        ctx.add(path, INFO, f"format hint removed (was {old_f!r})")
    else:
        ctx.add(path, INFO, f"format hint changed from {old_f!r} to {new_f!r}")


def resolve_ref(schema, root, seen=()):
    """Follow a local "$ref" (any JSON pointer, e.g. "#/definitions/X", "#/$defs/X",
    "#/components/schemas/X", "#") within root. Sibling keywords next to $ref (allowed in
    2020-12) are merged over the target. Returns (schema, refs_followed); cycles and
    non-local refs are left as-is."""
    followed = []
    while isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if not ref.startswith("#") or ref in seen or ref in followed:
            break
        target = root
        try:
            for part in [p for p in ref[1:].split("/") if p]:
                part = part.replace("~1", "/").replace("~0", "~")
                target = target[int(part)] if isinstance(target, list) else target[part]
        except (KeyError, IndexError, ValueError, TypeError):
            break
        followed.append(ref)
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        schema = dict(target, **siblings) if isinstance(target, dict) else target
    return schema, followed


def _canon(schema, root):
    s, _ = resolve_ref(schema, root)
    return json.dumps(s, sort_keys=True)


def _label(schema, root):
    """A name to pair variants by: the $ref target, else title, else const/type."""
    if isinstance(schema, dict):
        if isinstance(schema.get("$ref"), str):
            return "ref:" + schema["$ref"].rsplit("/", 1)[-1]
        s, _ = resolve_ref(schema, root)
        if isinstance(s, dict):
            if s.get("title"):
                return "title:" + str(s["title"])
            if "const" in s:
                return "const:" + json.dumps(s["const"])
            props = (s.get("properties") or {})
            for p, ps in props.items():  # a discriminator-style const property
                if isinstance(ps, dict) and "const" in ps:
                    return "disc:%s=%s" % (p, json.dumps(ps["const"]))
            if "type" in s:
                return "type:" + json.dumps(s["type"])
    return None


def diff_composition(old, new, path, ctx, seen):
    for kw in ("oneOf", "anyOf", "allOf"):
        o, n = old.get(kw), new.get(kw)
        if o is None and n is None:
            continue
        kpath = _join(path, kw)
        if o is None:
            ctx.add(kpath, BREAKING if kw != "anyOf" or len(n) else COMPATIBLE,
                    f"{kw} newly added -- values must now also match {'all of ' if kw == 'allOf' else ''}"
                    f"{len(n)} subschema(s)")
            continue
        if n is None:
            ctx.add(kpath, COMPATIBLE, f"{kw} removed (was {len(o)} subschema(s)) -- fewer constraints")
            continue
        oc = [_canon(s, ctx.roots[0]) for s in o]
        nc = [_canon(s, ctx.roots[1]) for s in n]
        unmatched_o = [i for i, c in enumerate(oc) if c not in nc]
        unmatched_n = [j for j, c in enumerate(nc) if c not in oc]
        pairs = []
        for i in list(unmatched_o):  # pair changed variants by label so their bodies are diffed
            lo = _label(o[i], ctx.roots[0])
            for j in unmatched_n:
                if lo is not None and lo == _label(n[j], ctx.roots[1]):
                    pairs.append((i, j))
                    unmatched_o.remove(i)
                    unmatched_n.remove(j)
                    break
        if len(unmatched_o) == 1 and len(unmatched_n) == 1:  # one variant edited in place
            pairs.append((unmatched_o.pop(), unmatched_n.pop()))
        for i, j in pairs:
            diff_schema(o[i], n[j], f"{kpath}[{j}]", ctx, seen=seen)
        for i in unmatched_o:
            if kw == "allOf":
                ctx.add(f"{kpath}[{i}]", COMPATIBLE, "allOf subschema removed -- one constraint fewer")
            else:
                ctx.add(f"{kpath}[{i}]", BREAKING, f"{kw} variant removed -- values that only matched it are now rejected")
        for j in unmatched_n:
            if kw == "allOf":
                ctx.add(f"{kpath}[{j}]", BREAKING, "allOf subschema added -- values must satisfy one more constraint")
            elif kw == "anyOf":
                ctx.add(f"{kpath}[{j}]", COMPATIBLE, "anyOf variant added -- more values accepted")
            else:
                ctx.add(f"{kpath}[{j}]", INFO if ctx.direction == "request" else BREAKING,
                        "oneOf variant added -- more values accepted, but documents matching an existing variant "
                        "AND the new one now fail oneOf (verify the variants don't overlap)")
    if "not" in old or "not" in new:
        if _canon(old.get("not"), ctx.roots[0]) != _canon(new.get("not"), ctx.roots[1]):
            ctx.add(_join(path, "not"), INFO, "'not' subschema changed -- review by hand")


def diff_schema(old, new, path, findings_or_ctx, roots=None, seen=()):
    """Public entry point: diff_schema(old, new, path, findings) for whole schemas."""
    if isinstance(findings_or_ctx, Ctx):
        ctx = findings_or_ctx
    else:
        ctx = Ctx(findings_or_ctx, roots or (old, new))
    old, old_refs = resolve_ref(old, ctx.roots[0])
    new, new_refs = resolve_ref(new, ctx.roots[1])
    pair = (tuple(old_refs), tuple(new_refs))
    if (old_refs or new_refs) and pair in seen:
        return  # recursive definition already being compared further up
    if old_refs or new_refs:
        seen = seen + (pair,)
    if old is True and isinstance(new, dict) or old is None and isinstance(new, dict):
        old = {}
    if new is True and isinstance(old, dict) or new is None and isinstance(old, dict):
        new = {}
    if not isinstance(old, dict) or not isinstance(new, dict):
        if old != new:
            ctx.add(path, INFO, f"non-object schema fragment changed: {old!r} -> {new!r}")
        return

    diff_type(old, new, path, ctx)
    diff_required(old, new, path, ctx)
    diff_additional_properties(old, new, path, ctx)
    diff_enum(old, new, path, ctx)
    diff_const(old, new, path, ctx)
    diff_dependent_required(old, new, path, ctx)
    diff_numeric_constraints(old, new, path, ctx)
    diff_pattern(old, new, path, ctx)
    diff_format(old, new, path, ctx)
    diff_composition(old, new, path, ctx, seen)

    old_props = old.get("properties", {}) or {}
    new_props = new.get("properties", {}) or {}
    closed = new.get("additionalProperties", True) is False
    for name in sorted(set(old_props) - set(new_props)):
        if name in old.get("required", []):
            ctx.add(_join(path, name), BREAKING, "required field removed entirely -- any consumer depending on it will break",
                    response_sev=BREAKING)
        elif ctx.direction == "response":
            ctx.add(_join(path, name), BREAKING, "field removed from the response -- consumers that read it get nothing",
                    response_sev=BREAKING)
        elif closed:
            ctx.add(_join(path, name), BREAKING, "optional field removed while additionalProperties is false -- "
                                                 "documents that still send it are now rejected")
        else:
            ctx.add(_join(path, name), COMPATIBLE, "optional field removed -- consumers that read it will silently get "
                                                   "nothing instead of an error")
    for name in sorted(set(new_props) - set(old_props)):
        if name not in new.get("required", []):
            ctx.add(_join(path, name), COMPATIBLE, "new optional field added", response_sev=COMPATIBLE)
    for name in sorted(set(old_props) & set(new_props)):
        diff_schema(old_props[name], new_props[name], _join(path, name), ctx, seen=seen)

    old_items, new_items = old.get("items"), new.get("items")
    if old_items is not None and new_items is not None:
        diff_schema(old_items, new_items, _join(path, "[]"), ctx, seen=seen)
    elif old_items is not None:
        ctx.add(_join(path, "[]"), INFO, "items schema removed (array items now unconstrained)")
    elif new_items is not None:
        ctx.add(_join(path, "[]"), INFO, "items schema added (array items now constrained)")


# --------------------------------------------------------------------------- OpenAPI

def is_openapi(doc):
    return isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc) and "paths" in doc


def _deref(obj, root):
    o, _ = resolve_ref(obj, root)
    return o if isinstance(o, dict) else {}


def _params(op, path_item, root):
    out = {}
    for p in (path_item.get("parameters") or []) + (op.get("parameters") or []):
        p = _deref(p, root)
        if p.get("name") and p.get("in"):
            out[(p["in"], p["name"])] = p  # operation-level overrides path-level
    return out


def _param_schema(p):
    if "schema" in p:
        return p["schema"]
    return {k: v for k, v in p.items() if k in ("type", "format", "enum", "items", "minimum", "maximum", "pattern",
                                                 "minLength", "maxLength")}  # Swagger 2.0 non-body parameter


def _bodies(op, root, swagger2):
    """{content type: schema} for the request body, plus whether it's required."""
    if swagger2:
        for p in op.get("parameters") or []:
            p = _deref(p, root)
            if p.get("in") == "body":
                return {"application/json": p.get("schema", {})}, bool(p.get("required"))
        return {}, False
    rb = _deref(op.get("requestBody") or {}, root)
    return {ct: m.get("schema", {}) for ct, m in (rb.get("content") or {}).items()}, bool(rb.get("required"))


def _responses(op, root, swagger2):
    out = {}
    for code, r in (op.get("responses") or {}).items():
        r = _deref(r, root)
        if swagger2:
            out[str(code)] = {"application/json": r["schema"]} if "schema" in r else {}
        else:
            out[str(code)] = {ct: m.get("schema", {}) for ct, m in (r.get("content") or {}).items()}
    return out


def diff_openapi(old, new, findings):
    ctx = Ctx(findings, (old, new))
    sw_o, sw_n = "swagger" in old, "swagger" in new
    o_paths, n_paths = old.get("paths") or {}, new.get("paths") or {}
    for p in sorted(set(o_paths) - set(n_paths)):
        ctx.add(p, BREAKING, "endpoint removed")
    for p in sorted(set(n_paths) - set(o_paths)):
        ctx.add(p, COMPATIBLE, "endpoint added")
    for p in sorted(set(o_paths) & set(n_paths)):
        oi, ni = _deref(o_paths[p], old), _deref(n_paths[p], new)
        for m in HTTP_METHODS:
            if m in oi and m not in ni:
                ctx.add(f"{m.upper()} {p}", BREAKING, "operation removed")
            elif m in ni and m not in oi:
                ctx.add(f"{m.upper()} {p}", COMPATIBLE, "operation added")
            elif m in oi:
                diff_operation(oi[m], ni[m], oi, ni, f"{m.upper()} {p}", ctx, sw_o, sw_n)
    o_comp = (old.get("components") or {}).get("schemas") or old.get("definitions") or {}
    n_comp = (new.get("components") or {}).get("schemas") or new.get("definitions") or {}
    for name in sorted(set(o_comp) - set(n_comp)):
        ctx.add(f"components.schemas.{name}", INFO, "schema component removed (breaking only where it was referenced; "
                                                     "see the operations above)")
    for name in sorted(set(n_comp) - set(o_comp)):
        ctx.add(f"components.schemas.{name}", INFO, "schema component added")


def diff_operation(o, n, oi, ni, path, ctx, sw_o, sw_n):
    req = ctx.child("request")
    op_, np_ = _params(o, oi, ctx.roots[0]), _params(n, ni, ctx.roots[1])
    for key in sorted(set(np_) - set(op_)):
        where = f"{path} {key[0]} parameter '{key[1]}'"
        if np_[key].get("required") or key[0] == "path":
            req.add(where, BREAKING, "new required parameter -- existing requests don't send it")
        else:
            req.add(where, COMPATIBLE, "new optional parameter")
    for key in sorted(set(op_) - set(np_)):
        if key[0] == "body":
            continue
        req.add(f"{path} {key[0]} parameter '{key[1]}'", BREAKING if key[0] in ("path", "header") else COMPATIBLE,
                "parameter removed" + (" -- the server now ignores it" if key[0] not in ("path", "header") else ""))
    for key in sorted(set(op_) & set(np_)):
        if key[0] == "body":
            continue
        where = f"{path} {key[0]} parameter '{key[1]}'"
        if np_[key].get("required") and not op_[key].get("required"):
            req.add(where, BREAKING, "parameter became required")
        elif op_[key].get("required") and not np_[key].get("required"):
            req.add(where, COMPATIBLE, "parameter no longer required")
        diff_schema(_param_schema(op_[key]), _param_schema(np_[key]), where, req)

    ob, ob_req = _bodies(o, ctx.roots[0], sw_o)
    nb, nb_req = _bodies(n, ctx.roots[1], sw_n)
    if nb_req and not ob_req:
        req.add(f"{path} requestBody", BREAKING, "request body became required")
    elif ob_req and not nb_req:
        req.add(f"{path} requestBody", COMPATIBLE, "request body no longer required")
    for ct in sorted(set(ob) - set(nb)):
        req.add(f"{path} requestBody {ct}", BREAKING, "request content type no longer accepted")
    for ct in sorted(set(nb) - set(ob)):
        req.add(f"{path} requestBody {ct}", COMPATIBLE, "request content type added")
    for ct in sorted(set(ob) & set(nb)):
        diff_schema(ob[ct], nb[ct], f"{path} requestBody {ct}", req)

    resp = ctx.child("response")
    orr, nrr = _responses(o, ctx.roots[0], sw_o), _responses(n, ctx.roots[1], sw_n)
    for code in sorted(set(orr) - set(nrr)):
        resp.add(f"{path} response {code}", BREAKING if code.startswith("2") else INFO,
                 "response removed", response_sev=BREAKING if code.startswith("2") else INFO)
    for code in sorted(set(nrr) - set(orr)):
        resp.add(f"{path} response {code}", INFO, "response added -- make sure clients handle it", response_sev=INFO)
    for code in sorted(set(orr) & set(nrr)):
        for ct in sorted(set(orr[code]) - set(nrr[code])):
            resp.add(f"{path} response {code} {ct}", BREAKING, "response content type removed", response_sev=BREAKING)
        for ct in sorted(set(orr[code]) & set(nrr[code])):
            diff_schema(orr[code][ct], nrr[code][ct], f"{path} response {code} {ct}", resp)


# --------------------------------------------------------------------------- output

def markdown(breaking, compatible, info, old_name, new_name):
    lines = [f"### API changes: `{old_name}` -> `{new_name}`", ""]
    for title, items in (("Breaking", breaking), ("Compatible", compatible), ("Informational", info)):
        if items:
            lines.append(f"**{title} ({len(items)})**")
            lines.append("")
            lines += [f"- `{_fmt_path(f.path)}`: {f.message}" for f in items]
            lines.append("")
    if not (breaking or compatible or info):
        lines.append("No differences.")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="json-schema-diff",
                                     description="JSON Schema / OpenAPI breaking-change detector",
                                     epilog="Exit codes: 0 nothing at/above --fail-on, 1 breaking (or compatible) "
                                            "changes, 2 usage/parse error.")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("old_schema", help="path to the previous/baseline JSON Schema or OpenAPI (JSON) file")
    parser.add_argument("new_schema", help="path to the new/candidate JSON Schema or OpenAPI (JSON) file")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON output")
    parser.add_argument("--markdown", action="store_true", help="emit a Markdown changelog section")
    parser.add_argument("--fail-on", choices=["breaking", "compatible", "never"], default="breaking",
                        help="exit 1 on BREAKING changes (default), on any non-info change, or never")
    parser.add_argument("--fail-on-breaking", action="store_true", help=argparse.SUPPRESS)  # 1.x flag, now the default
    parser.add_argument("--quiet-info", action="store_true", help="suppress informational findings in text output")
    args = parser.parse_args(argv)

    try:
        old = load_schema(args.old_schema)
        new = load_schema(args.new_schema)
    except (OSError, ValueError) as e:  # ValueError covers bad JSON and bad UTF-8
        print(f"error: {e}", file=sys.stderr)
        return 2

    findings = []
    mode = "openapi" if is_openapi(old) and is_openapi(new) else "schema"
    if is_openapi(old) != is_openapi(new):
        print("error: one file is an OpenAPI/Swagger spec and the other a plain JSON Schema", file=sys.stderr)
        return 2
    if mode == "openapi":
        diff_openapi(old, new, findings)
    else:
        diff_schema(old, new, "", findings)

    breaking = [f for f in findings if f.severity == BREAKING]
    compatible = [f for f in findings if f.severity == COMPATIBLE]
    info = [f for f in findings if f.severity == INFO]

    if args.json:
        print(json.dumps({
            "version": __version__, "mode": mode,
            "breaking": [f.to_dict() for f in breaking],
            "compatible": [f.to_dict() for f in compatible],
            "info": [f.to_dict() for f in info],
            "summary": {"breaking": len(breaking), "compatible": len(compatible), "info": len(info)},
        }, indent=2))
    elif args.markdown:
        print(markdown(breaking, compatible, [] if args.quiet_info else info, args.old_schema, args.new_schema))
    else:
        if not findings:
            print("No differences found -- schemas are identical.")
        else:
            for f in breaking + compatible + ([] if args.quiet_info else info):
                print(f)
            print()
            print(f"Summary: {len(breaking)} breaking, {len(compatible)} compatible, {len(info)} informational")

    if args.fail_on == "never" and not args.fail_on_breaking:
        return 0
    if breaking or (args.fail_on == "compatible" and compatible):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
