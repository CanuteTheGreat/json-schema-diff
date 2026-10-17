"""End-to-end tests for schema_diff.py (run unmodified as a subprocess).

Every classification is checked against a real JSON Schema validator
(`jsonschema`, installed by ci/setup.sh): for each BREAKING finding the test
supplies a document that the OLD schema accepts and the NEW schema rejects,
and for each compatible change a document set that both schemas agree on (or
that only the new, wider schema accepts). So "breaking" is proven against
real validation behaviour, not just against the tool's own rules.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from jsonschema import Draft202012Validator, Draft7Validator

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "schema_diff.py")


def write(obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))
    return path


def run(*args):
    return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True, timeout=60)


def diff(old, new, *extra):
    a, b = write(old), write(new)
    try:
        return run(a, b, "--json", *extra)
    finally:
        os.unlink(a)
        os.unlink(b)


def valid(schema, doc):
    cls = Draft202012Validator if "$defs" in json.dumps(schema) else Draft7Validator
    return cls(schema).is_valid(doc)


def obj(props, required=(), **kw):
    s = {"type": "object", "properties": props}
    if required:
        s["required"] = list(required)
    s.update(kw)
    return s


class Case(unittest.TestCase):
    def findings(self, old, new):
        p = diff(old, new)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def assert_breaking(self, old, new, path, needle, witness):
        """witness: a document the old schema accepts and the new one rejects."""
        self.assertTrue(valid(old, witness), "witness must be valid under OLD")
        self.assertFalse(valid(new, witness), "witness must be rejected by NEW")
        r = self.findings(old, new)
        hits = [f for f in r["breaking"] if f["path"] == path and needle in f["message"]]
        self.assertTrue(hits, json.dumps(r, indent=1))
        return r

    def assert_compatible(self, old, new, path, needle, docs=()):
        """docs: documents accepted by OLD, which NEW must still accept."""
        for d in docs:
            self.assertTrue(valid(old, d) and valid(new, d), d)
        r = self.findings(old, new)
        self.assertEqual(r["breaking"], [], json.dumps(r, indent=1))
        hits = [f for f in r["compatible"] if f["path"] == path and needle in f["message"]]
        self.assertTrue(hits, json.dumps(r, indent=1))
        return r


class TestBreaking(Case):
    def test_type_narrowed_and_changed(self):
        self.assert_breaking(obj({"v": {"type": ["string", "integer"]}}), obj({"v": {"type": "string"}}),
                             "v", "type narrowed", {"v": 3})
        self.assert_breaking(obj({"v": {"type": "integer"}}), obj({"v": {"type": "string"}}),
                             "v", "type changed", {"v": 3})
        self.assert_breaking(obj({"v": {"type": "number"}}), obj({"v": {"type": "integer"}}),
                             "v", "type narrowed", {"v": 1.5})

    def test_type_newly_added(self):
        self.assert_breaking(obj({"v": {}}), obj({"v": {"type": "string"}}), "v", "type constraint newly added",
                             {"v": 1})

    def test_newly_required(self):
        self.assert_breaking(obj({"a": {"type": "string"}}), obj({"a": {"type": "string"}}, ["a"]),
                             "a", "newly marked 'required'", {})

    def test_new_required_field(self):
        self.assert_breaking(obj({}), obj({"b": {"type": "string"}}, ["b"]), "b", "newly marked 'required'", {})

    def test_required_field_removed(self):
        r = self.findings(obj({"a": {"type": "string"}}, ["a"]), obj({}))
        self.assertTrue(any(f["path"] == "a" and "required field removed" in f["message"] for f in r["breaking"]), r)

    def test_additional_properties_closed(self):
        self.assert_breaking(obj({"a": {}}), obj({"a": {}}, additionalProperties=False), "$",
                             "additionalProperties changed to false", {"a": 1, "extra": 2})

    def test_optional_field_removed_from_closed_object(self):
        old = obj({"a": {}, "b": {}}, additionalProperties=False)
        new = obj({"a": {}}, additionalProperties=False)
        self.assert_breaking(old, new, "b", "optional field removed while additionalProperties is false",
                             {"b": 1})

    def test_enum_removed_and_added_constraint(self):
        self.assert_breaking(obj({"s": {"enum": ["a", "b"]}}), obj({"s": {"enum": ["a"]}}), "s",
                             "enum values removed: ['b']", {"s": "b"})
        self.assert_breaking(obj({"s": {"type": "string"}}), obj({"s": {"type": "string", "enum": ["a"]}}), "s",
                             "enum constraint newly added", {"s": "z"})

    def test_numeric_and_length_narrowing(self):
        cases = [("minimum", 0, 18, {"type": "integer"}, 5), ("maximum", 100, 10, {"type": "integer"}, 50),
                 ("minLength", 1, 5, {"type": "string"}, "ab"), ("maxLength", 100, 3, {"type": "string"}, "abcd"),
                 ("minItems", 0, 2, {"type": "array"}, [1]), ("maxItems", 5, 1, {"type": "array"}, [1, 2])]
        for key, a, b, base, witness in cases:
            self.assert_breaking(obj({"v": dict(base, **{key: a})}), obj({"v": dict(base, **{key: b})}),
                                 "v", "%s narrowed from %s to %s" % (key, a, b), {"v": witness})
            self.assert_breaking(obj({"v": dict(base)}), obj({"v": dict(base, **{key: b})}),
                                 "v", "%s constraint newly added" % key, {"v": witness})

    def test_pattern_added_or_changed(self):
        self.assert_breaking(obj({"v": {"type": "string"}}), obj({"v": {"type": "string", "pattern": "^a"}}),
                             "v", "pattern constraint newly added", {"v": "b"})
        self.assert_breaking(obj({"v": {"type": "string", "pattern": "^a"}}),
                             obj({"v": {"type": "string", "pattern": "^b"}}), "v", "pattern changed", {"v": "a"})

    def test_nested_objects_and_array_items(self):
        old = obj({"user": obj({"tags": {"type": "array", "items": {"type": ["string", "null"]}}})})
        new = obj({"user": obj({"tags": {"type": "array", "items": {"type": "string"}}})})
        self.assert_breaking(old, new, "user.tags.[]", "type narrowed", {"user": {"tags": [None]}})

    def test_local_refs_definitions_and_defs(self):
        old = {"definitions": {"Id": {"type": ["integer", "string"]}}, "type": "object",
               "properties": {"id": {"$ref": "#/definitions/Id"}}}
        new = {"definitions": {"Id": {"type": "integer"}}, "type": "object",
               "properties": {"id": {"$ref": "#/definitions/Id"}}}
        self.assert_breaking(old, new, "id", "type narrowed", {"id": "x"})
        old = {"$defs": {"Status": {"enum": ["on", "off"]}}, "type": "object",
               "properties": {"s": {"$ref": "#/$defs/Status"}}}
        new = {"$defs": {"Status": {"enum": ["on"]}}, "type": "object", "properties": {"s": {"$ref": "#/$defs/Status"}}}
        self.assert_breaking(old, new, "s", "enum values removed", {"s": "off"})

    def test_ref_replaced_by_inline_schema(self):
        old = {"definitions": {"N": {"type": "integer", "maximum": 10}}, "type": "object",
               "properties": {"n": {"$ref": "#/definitions/N"}}}
        new = obj({"n": {"type": "integer", "maximum": 5}})
        self.assert_breaking(old, new, "n", "maximum narrowed from 10 to 5", {"n": 8})

    def test_recursive_schema_terminates(self):
        tree = {"$defs": {"Node": {"type": "object", "properties": {
            "name": {"type": "string"}, "children": {"type": "array", "items": {"$ref": "#/$defs/Node"}}}}},
            "$ref": "#/$defs/Node"}
        new = json.loads(json.dumps(tree))
        new["$defs"]["Node"]["properties"]["name"]["maxLength"] = 3
        r = self.assert_breaking(tree, new, "name", "maxLength constraint newly added", {"name": "long"})
        self.assertTrue(valid(tree, {"name": "a", "children": [{"name": "b", "children": []}]}))
        self.assertEqual(len(r["breaking"]), 1, r)


class TestCompatible(Case):
    def test_type_widened_including_integer_to_number(self):
        self.assert_compatible(obj({"v": {"type": "string"}}), obj({"v": {"type": ["string", "null"]}}),
                               "v", "type widened", [{"v": "x"}])
        self.assert_compatible(obj({"v": {"type": "integer"}}), obj({"v": {"type": "number"}}),
                               "v", "type widened", [{"v": 3}])

    def test_type_removed(self):
        self.assert_compatible(obj({"v": {"type": "string"}}), obj({"v": {}}), "v", "type constraint removed",
                               [{"v": "x"}])

    def test_relaxations(self):
        self.assert_compatible(obj({"a": {}}, ["a"]), obj({"a": {}}), "a", "no longer required", [{"a": 1}])
        self.assert_compatible(obj({"a": {}}, additionalProperties=False), obj({"a": {}}), "$",
                               "additionalProperties relaxed", [{"a": 1}])
        self.assert_compatible(obj({"s": {"enum": ["a"]}}), obj({"s": {"enum": ["a", "b"]}}), "s",
                               "enum values added", [{"s": "a"}])
        self.assert_compatible(obj({"s": {"enum": ["a"]}}), obj({"s": {}}), "s", "enum constraint removed",
                               [{"s": "a"}])
        self.assert_compatible(obj({"v": {"type": "integer", "maximum": 5}}),
                               obj({"v": {"type": "integer", "maximum": 50}}), "v", "maximum relaxed", [{"v": 5}])
        self.assert_compatible(obj({"v": {"type": "integer", "minimum": 5}}), obj({"v": {"type": "integer"}}), "v",
                               "minimum constraint removed", [{"v": 5}])
        self.assert_compatible(obj({"v": {"type": "string", "pattern": "^a"}}), obj({"v": {"type": "string"}}),
                               "v", "pattern constraint removed", [{"v": "a"}])

    def test_new_optional_and_removed_optional_in_open_object(self):
        self.assert_compatible(obj({"a": {}}), obj({"a": {}, "b": {"type": "string"}}), "b",
                               "new optional field added", [{"a": 1}])
        self.assert_compatible(obj({"a": {}, "b": {}}), obj({"a": {}}), "b", "optional field removed",
                               [{"a": 1, "b": 2}])


class TestInfoAndCli(Case):
    def test_format_and_items_are_info(self):
        r = self.findings(obj({"e": {"type": "string"}, "l": {"type": "array", "items": {"type": "string"}}}),
                          obj({"e": {"type": "string", "format": "email"}, "l": {"type": "array"}}))
        msgs = [f["message"] for f in r["info"]]
        self.assertTrue(any("format hint newly added: 'email'" in m for m in msgs), r)
        self.assertTrue(any("items schema removed" in m for m in msgs), r)
        self.assertEqual(r["breaking"], [])
        self.assertEqual(r["summary"], {"breaking": 0, "compatible": 0, "info": 2})

    def test_bundled_example_schemas(self):
        p = run(os.path.join(ROOT, "old_schema.json"), os.path.join(ROOT, "new_schema_breaking.json"),
                "--fail-on-breaking")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("[BREAKING ]", p.stdout)
        self.assertIn("[BREAKING ] $: additionalProperties changed to false", p.stdout)
        p = run(os.path.join(ROOT, "old_schema.json"), os.path.join(ROOT, "new_schema_compatible.json"),
                "--fail-on-breaking")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("Summary: 0 breaking", p.stdout)

    def test_fail_on_breaking_gate_and_default_exit(self):
        old, new = obj({"a": {}}), obj({"a": {}}, ["a"])
        self.assertEqual(diff(old, new).returncode, 0)                        # informational by default
        self.assertEqual(diff(old, new, "--fail-on-breaking").returncode, 1)  # CI gate
        self.assertEqual(diff(old, old, "--fail-on-breaking").returncode, 0)

    def test_text_output_identical_and_quiet_info(self):
        a, b, c = write(obj({"e": {}})), write(obj({"e": {"format": "email"}})), write(obj({"e": {}}))
        try:
            p = run(a, c)
            self.assertIn("No differences found", p.stdout)
            p = run(a, b)
            self.assertIn("[info     ] e: format hint newly added", p.stdout)
            p = run(a, b, "--quiet-info")
            self.assertNotIn("format hint", p.stdout)
            self.assertIn("Summary: 0 breaking, 0 compatible, 1 informational", p.stdout)
        finally:
            for x in (a, b, c):
                os.unlink(x)

    def test_parse_and_usage_errors_exit_2(self):
        good, bad, binary = write(obj({})), write("{not json"), write("")
        with open(binary, "wb") as f:
            f.write(b"\xff\xfe\x00garbage")
        try:
            for args in ((good, bad), (good, "/nonexistent.json"), (good, binary), (good,)):
                p = run(*args)
                self.assertEqual(p.returncode, 2, (args, p.stdout + p.stderr))
                self.assertNotIn("Traceback", p.stderr)
        finally:
            for x in (good, bad, binary):
                os.unlink(x)


if __name__ == "__main__":
    unittest.main()
