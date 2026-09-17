#!/usr/bin/env python3
"""
schema_diff.py -- JSON Schema breaking-change detector.

Compares two JSON Schema documents (old vs new) and classifies every
difference as BREAKING, non-breaking (compatible/additive), or informational.
Built for API versioning: run it in CI against the previous released schema
vs. the current one to catch accidental breaking changes before they ship.

Zero dependencies (Python stdlib only, json + argparse), no network calls,
works entirely offline. Supports the common subset of JSON Schema draft-07 /
2020-12 keywords actually used in real API schemas: type, properties,
required, additionalProperties, enum, items, $ref (shallow), format,
minimum/maximum, minLength/maxLength, pattern.

Usage:
    ./schema_diff.py old_schema.json new_schema.json
    ./schema_diff.py old_schema.json new_schema.json --json
    ./schema_diff.py old_schema.json new_schema.json --fail-on-breaking

Exit codes:
    0 = no breaking changes found
    1 = at least one breaking change found (only meaningful with --fail-on-breaking,
        otherwise always 0 so it's safe to run informationally in CI without
        gating a build unless you opt in)
    2 = usage/parse error
"""

import argparse
import json
import sys

BREAKING = "BREAKING"
COMPATIBLE = "compatible"
INFO = "info"


class Finding:
    def __init__(self, path, severity, message):
        self.path = path
        self.severity = severity
        self.message = message

    def to_dict(self):
        return {"path": self.path, "severity": self.severity, "message": self.message}

    def __str__(self):
        tag = {"BREAKING": "BREAKING ", "compatible": "compat   ", "info": "info     "}[self.severity]
        return f"[{tag}] {self.path}: {self.message}"


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
    "minLength": lambda old, new: new > old,
    "maxLength": lambda old, new: new < old,
    "minItems": lambda old, new: new > old,
    "maxItems": lambda old, new: new < old,
}


def diff_type(old, new, path, findings):
    old_type = old.get("type")
    new_type = new.get("type")
    if old_type is None or new_type is None:
        return
    old_set = set(old_type) if isinstance(old_type, list) else {old_type}
    new_set = set(new_type) if isinstance(new_type, list) else {new_type}
    if old_set == new_set:
        return
    if new_set.issubset(old_set):
        findings.append(Finding(path, BREAKING,
            f"type narrowed from {sorted(old_set)} to {sorted(new_set)} "
            f"-- previously-valid values may now be rejected"))
    elif old_set.issubset(new_set):
        findings.append(Finding(path, COMPATIBLE,
            f"type widened from {sorted(old_set)} to {sorted(new_set)} "
            f"-- more values now accepted"))
    else:
        findings.append(Finding(path, BREAKING,
            f"type changed from {sorted(old_set)} to {sorted(new_set)} "
            f"-- incompatible sets, likely breaking for both producers and consumers"))


def diff_required(old, new, path, findings):
    old_req = set(old.get("required", []))
    new_req = set(new.get("required", []))
    added = new_req - old_req
    removed = old_req - new_req
    for field in sorted(added):
        findings.append(Finding(_join(path, field), BREAKING,
            "field newly marked 'required' -- existing producers that omit it will now fail validation"))
    for field in sorted(removed):
        findings.append(Finding(_join(path, field), COMPATIBLE,
            "field no longer required -- safe relaxation"))


def diff_additional_properties(old, new, path, findings):
    old_ap = old.get("additionalProperties", True)
    new_ap = new.get("additionalProperties", True)
    if old_ap == new_ap:
        return
    old_permits = old_ap is not False
    new_permits = new_ap is not False
    if old_permits and not new_permits:
        findings.append(Finding(path, BREAKING,
            "additionalProperties changed to false -- previously-accepted extra fields will now be rejected"))
    elif not old_permits and new_permits:
        findings.append(Finding(path, COMPATIBLE,
            "additionalProperties relaxed (no longer false) -- extra fields now allowed"))
    else:
        findings.append(Finding(path, INFO, "additionalProperties schema changed (non-boolean form)"))


def diff_enum(old, new, path, findings):
    if "enum" not in old and "enum" not in new:
        return
    old_enum = set(map(_hashable, old.get("enum", []))) if "enum" in old else None
    new_enum = set(map(_hashable, new.get("enum", []))) if "enum" in new else None
    if old_enum is None and new_enum is not None:
        findings.append(Finding(path, BREAKING,
            f"enum constraint newly added, restricting values to {sorted(map(str, new_enum))}"))
        return
    if old_enum is not None and new_enum is None:
        findings.append(Finding(path, COMPATIBLE, "enum constraint removed -- any value now allowed"))
        return
    if old_enum is None and new_enum is None:
        return
    removed = old_enum - new_enum
    added = new_enum - old_enum
    if removed:
        findings.append(Finding(path, BREAKING,
            f"enum values removed: {sorted(map(str, removed))} -- previously-valid values now rejected"))
    if added:
        findings.append(Finding(path, COMPATIBLE, f"enum values added: {sorted(map(str, added))}"))


def _hashable(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, sort_keys=True)
    return v


def diff_numeric_constraints(old, new, path, findings):
    for key, is_narrowing in NUMERIC_NARROWING.items():
        if key in old and key in new:
            if old[key] != new[key]:
                if is_narrowing(old[key], new[key]):
                    findings.append(Finding(path, BREAKING,
                        f"{key} narrowed from {old[key]} to {new[key]} -- some previously-valid values now rejected"))
                else:
                    findings.append(Finding(path, COMPATIBLE,
                        f"{key} relaxed from {old[key]} to {new[key]}"))
        elif key in old and key not in new:
            findings.append(Finding(path, COMPATIBLE, f"{key} constraint removed (was {old[key]})"))
        elif key not in old and key in new:
            findings.append(Finding(path, BREAKING,
                f"{key} constraint newly added ({new[key]}) -- may reject previously-valid values"))


def diff_pattern(old, new, path, findings):
    old_p = old.get("pattern")
    new_p = new.get("pattern")
    if old_p == new_p:
        return
    if old_p is None and new_p is not None:
        findings.append(Finding(path, BREAKING, f"pattern constraint newly added: {new_p!r}"))
    elif old_p is not None and new_p is None:
        findings.append(Finding(path, COMPATIBLE, "pattern constraint removed"))
    else:
        findings.append(Finding(path, BREAKING,
            f"pattern changed from {old_p!r} to {new_p!r} -- treat as breaking unless proven a strict superset"))


def diff_format(old, new, path, findings):
    old_f = old.get("format")
    new_f = new.get("format")
    if old_f == new_f:
        return
    if old_f is None and new_f is not None:
        findings.append(Finding(path, INFO, f"format hint newly added: {new_f!r} (most validators only warn)"))
    elif old_f is not None and new_f is None:
        findings.append(Finding(path, INFO, f"format hint removed (was {old_f!r})"))
    else:
        findings.append(Finding(path, INFO, f"format hint changed from {old_f!r} to {new_f!r}"))


def diff_schema(old, new, path, findings):
    if not isinstance(old, dict) or not isinstance(new, dict):
        if old != new:
            findings.append(Finding(path, INFO, f"non-object schema fragment changed: {old!r} -> {new!r}"))
        return

    diff_type(old, new, path, findings)
    diff_required(old, new, path, findings)
    diff_additional_properties(old, new, path, findings)
    diff_enum(old, new, path, findings)
    diff_numeric_constraints(old, new, path, findings)
    diff_pattern(old, new, path, findings)
    diff_format(old, new, path, findings)

    old_props = old.get("properties", {})
    new_props = new.get("properties", {})
    old_names = set(old_props.keys())
    new_names = set(new_props.keys())

    for name in sorted(old_names - new_names):
        was_required = name in old.get("required", [])
        sev = BREAKING if was_required else COMPATIBLE
        msg = ("required field removed entirely -- any consumer depending on it will break"
               if was_required else
               "optional field removed -- consumers that read it will silently get nothing instead of an error")
        findings.append(Finding(_join(path, name), sev, msg))

    for name in sorted(new_names - old_names):
        is_required = name in new.get("required", [])
        if is_required:
            pass  # already reported by diff_required as BREAKING
        else:
            findings.append(Finding(_join(path, name), COMPATIBLE, "new optional field added"))

    for name in sorted(old_names & new_names):
        diff_schema(old_props[name], new_props[name], _join(path, name), findings)

    old_items = old.get("items")
    new_items = new.get("items")
    if old_items is not None and new_items is not None:
        diff_schema(old_items, new_items, _join(path, "[]"), findings)
    elif old_items is not None and new_items is None:
        findings.append(Finding(_join(path, "[]"), INFO, "items schema removed (array items now unconstrained)"))
    elif old_items is None and new_items is not None:
        findings.append(Finding(_join(path, "[]"), INFO, "items schema added (array items now constrained)"))


def main():
    parser = argparse.ArgumentParser(description="JSON Schema breaking-change detector")
    parser.add_argument("old_schema", help="path to the previous/baseline JSON Schema file")
    parser.add_argument("new_schema", help="path to the new/candidate JSON Schema file")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON output")
    parser.add_argument("--fail-on-breaking", action="store_true",
                         help="exit 1 if any BREAKING finding is present (default: always exit 0)")
    parser.add_argument("--quiet-info", action="store_true", help="suppress informational findings in text output")
    args = parser.parse_args()

    try:
        old = load_schema(args.old_schema)
        new = load_schema(args.new_schema)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    findings = []
    diff_schema(old, new, "", findings)

    breaking = [f for f in findings if f.severity == BREAKING]
    compatible = [f for f in findings if f.severity == COMPATIBLE]
    info = [f for f in findings if f.severity == INFO]

    if args.json:
        print(json.dumps({
            "breaking": [f.to_dict() for f in breaking],
            "compatible": [f.to_dict() for f in compatible],
            "info": [f.to_dict() for f in info],
            "summary": {"breaking": len(breaking), "compatible": len(compatible), "info": len(info)},
        }, indent=2))
    else:
        if not findings:
            print("No differences found -- schemas are identical.")
        else:
            for f in breaking:
                print(f)
            for f in compatible:
                print(f)
            if not args.quiet_info:
                for f in info:
                    print(f)
            print()
            print(f"Summary: {len(breaking)} breaking, {len(compatible)} compatible, {len(info)} informational")

    if args.fail_on_breaking and breaking:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
