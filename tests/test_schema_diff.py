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
        r = json.loads(p.stdout)
        self.assertEqual(p.returncode, 1 if r["breaking"] else 0, p.stderr)  # exit 1 on BREAKING by default
        return r

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
        self.assert_breaking(old, new, "user.tags.[]", "null no longer accepted", {"user": {"tags": [None]}})
        old = obj({"n": {"type": ["string", "integer"]}})
        self.assert_breaking(old, obj({"n": {"type": "string"}}), "n", "type narrowed", {"n": 3})

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


class TestExternalRefs(unittest.TestCase):
    """$refs to other local files: each test writes an old/ and a new/ directory with the
    same file layout, like two checkouts of an API's schema folder."""

    def tree(self, files):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d)
        for name, content in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
            with open(os.path.join(d, name), "w") as f:
                f.write(content if isinstance(content, str) else json.dumps(content))
        return d

    def run_trees(self, old_files, new_files, root="root.json"):
        old_dir, new_dir = self.tree(old_files), self.tree(new_files)
        return run(os.path.join(old_dir, root), os.path.join(new_dir, root), "--json")

    @staticmethod
    def valid_with(files, doc, root="root.json"):
        """Real validation: jsonschema resolves the same file refs through a registry."""
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
        registry = Registry().with_resources(
            (name, Resource.from_contents(c, default_specification=DRAFT202012)) for name, c in files.items() if name != root)
        return Draft202012Validator(files[root], registry=registry).is_valid(doc)

    def test_a_change_inside_a_referenced_file_is_reported_where_it_is_used(self):
        root = {"type": "object", "properties": {"user": {"$ref": "defs/user.json"},
                                                  "owner": {"$ref": "defs/user.json#/properties/name"}}}
        old_user = {"type": "object", "properties": {"name": {"type": ["string", "null"]}, "id": {"$ref": "common.json#/$defs/Id"}}}
        new_user = {"type": "object", "properties": {"name": {"type": "string"}, "id": {"$ref": "common.json#/$defs/Id"}}}
        old_common = {"$defs": {"Id": {"type": ["integer", "string"]}}}
        new_common = {"$defs": {"Id": {"type": "integer"}}}
        old = {"root.json": root, "defs/user.json": old_user, "defs/common.json": old_common}
        new = {"root.json": root, "defs/user.json": new_user, "defs/common.json": new_common}
        witness = {"user": {"name": None, "id": "x"}, "owner": None}
        self.assertTrue(self.valid_with(old, witness))
        self.assertFalse(self.valid_with(new, witness))
        p = self.run_trees(old, new)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        found = {(f["path"], f["message"]) for f in json.loads(p.stdout)["breaking"]}
        paths = {path for path, _ in found}
        self.assertTrue({"user.name", "user.id", "owner"} <= paths, found)
        self.assertTrue(all("null no longer accepted" in m or "type narrowed" in m for _, m in found), found)

    def test_identical_trees_and_refs_between_files_that_loop(self):
        files = {"root.json": {"$ref": "node.json"},
                 "node.json": {"type": "object", "properties": {"next": {"$ref": "node.json"}, "leaf": {"$ref": "leaf.json"}}},
                 "leaf.json": {"type": "object", "properties": {"up": {"$ref": "node.json"}, "v": {"type": "integer"}}}}
        p = self.run_trees(files, files)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["summary"], {"breaking": 0, "compatible": 0, "info": 0})
        new = json.loads(json.dumps(files))
        new["leaf.json"]["properties"]["v"]["maximum"] = 3
        p = self.run_trees(files, new)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("leaf.v", {f["path"] for f in json.loads(p.stdout)["breaking"]})

    def test_openapi_components_in_a_separate_file(self):
        def spec(schemas_file):
            return {"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "paths": {"/u": {"post": {
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": schemas_file + "#/User"}}}},
                "responses": {"200": {"description": "ok"}}}}}}
        old = {"api.json": spec("schemas.json"), "schemas.json": {"User": {"type": "object", "properties": {"age": {"type": "integer"}}}}}
        new = {"api.json": spec("schemas.json"), "schemas.json": {"User": {"type": "object", "properties": {"age": {"type": "integer", "minimum": 18}}}}}
        p = self.run_trees(old, new, root="api.json")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertTrue(any("minimum" in f["message"] for f in json.loads(p.stdout)["breaking"]), p.stdout)

    def test_a_missing_referenced_file_is_a_usage_error_and_remote_refs_are_never_fetched(self):
        files = {"root.json": {"type": "object", "properties": {"a": {"$ref": "missing.json"}}}}
        p = self.run_trees(files, files)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("missing.json", p.stderr)
        # .invalid never resolves; the tool must not even try (it has no network code at all).
        files = {"root.json": {"type": "object", "properties": {"a": {"$ref": "https://schemas.example.invalid/a.json"}}}}
        p = self.run_trees(files, files)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class TestCompatible(Case):
    def test_type_widened_including_integer_to_number(self):
        self.assert_compatible(obj({"v": {"type": "string"}}), obj({"v": {"type": ["string", "null"]}}),
                               "v", "null now accepted", [{"v": "x"}])
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

    def test_help_documents_fail_on_breaking_alias(self):
        p = run("--help")
        self.assertEqual(p.returncode, 0, p.stderr)
        text = " ".join(p.stdout.split())
        self.assertIn("--fail-on-breaking", text)
        self.assertIn("1.x flag, kept as an alias", text)

    def test_fail_on_breaking_gate_and_default_exit(self):
        old, new = obj({"a": {}}), obj({"a": {}}, ["a"])
        self.assertEqual(diff(old, new).returncode, 1)                        # a CI gate by default
        self.assertEqual(diff(old, new, "--fail-on-breaking").returncode, 1)  # the 1.x flag still works
        self.assertEqual(diff(old, new, "--fail-on", "never").returncode, 0)  # report only
        self.assertEqual(diff(old, old).returncode, 0)
        relaxed = obj({"a": {}, "b": {}})
        self.assertEqual(diff(old, relaxed).returncode, 0)
        self.assertEqual(diff(old, relaxed, "--fail-on", "compatible").returncode, 1)

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


class TestKeywords(Case):
    def test_const(self):
        self.assert_breaking(obj({"k": {"type": "string"}}), obj({"k": {"const": "a"}}), "k", "const newly added", {"k": "b"})
        self.assert_breaking(obj({"k": {"const": "a"}}), obj({"k": {"const": "b"}}), "k", "const changed", {"k": "a"})
        self.assert_compatible(obj({"k": {"const": "a"}}), obj({"k": {}}), "k", "const removed", [{"k": "a"}])

    def test_openapi_30_nullable(self):
        # nullable is OpenAPI 3.0 vocabulary; the plain validator ignores it, so this is checked by message only
        r = self.findings(obj({"n": {"type": "string", "nullable": True}}), obj({"n": {"type": "string"}}))
        self.assertTrue(any(f["path"] == "n" and "null no longer accepted" in f["message"] for f in r["breaking"]), r)
        r = self.findings(obj({"n": {"type": "string"}}), obj({"n": {"type": "string", "nullable": True}}))
        self.assertTrue(any("null now accepted" in f["message"] for f in r["compatible"]), r)

    def test_dependent_required(self):
        old = {"type": "object", "properties": {"card": {}, "cvc": {}, "zip": {}}, "dependentRequired": {"card": ["cvc"]},
               "$defs": {}}
        new = dict(old, dependentRequired={"card": ["cvc", "zip"]})
        self.assert_breaking(old, new, "card", "['zip'] are now also required", {"card": 1, "cvc": 2})
        self.assert_compatible(new, old, "card", "no longer requires ['zip']", [{"card": 1, "cvc": 2, "zip": 3}])
        d7 = {"type": "object", "dependencies": {"a": ["b"]}}
        self.assert_breaking({"type": "object"}, d7, "a", "now also required", {"a": 1})

    def test_one_of_any_of_variants(self):
        cat = {"type": "object", "title": "Cat", "properties": {"kind": {"const": "cat"}, "lives": {"type": "integer"}},
               "required": ["kind"]}
        dog = {"type": "object", "title": "Dog", "properties": {"kind": {"const": "dog"}}, "required": ["kind"]}
        fish = {"type": "object", "title": "Fish", "properties": {"kind": {"const": "fish"}}, "required": ["kind"]}
        old = {"oneOf": [cat, dog]}
        self.assert_breaking(old, {"oneOf": [cat]}, "oneOf[1]", "oneOf variant removed", {"kind": "dog"})
        r = self.findings(old, {"oneOf": [cat, dog, fish]})
        self.assertTrue(any(f["path"] == "oneOf[2]" and "oneOf variant added" in f["message"] for f in r["info"]), r)
        self.assertEqual(r["breaking"], [])
        cat2 = dict(cat, properties=dict(cat["properties"], lives={"type": "integer", "maximum": 9}))
        self.assert_breaking(old, {"oneOf": [cat2, dog]}, "oneOf[0].lives", "maximum constraint newly added",
                             {"kind": "cat", "lives": 12})
        self.assert_compatible({"anyOf": [cat, dog]}, {"anyOf": [cat, dog, fish]}, "anyOf[2]", "anyOf variant added",
                               [{"kind": "cat"}])
        self.assert_breaking({"type": "object"}, {"type": "object", "anyOf": [cat, dog]}, "anyOf", "anyOf newly added",
                             {"kind": "fish"})
        self.assert_compatible({"anyOf": [cat, dog]}, {}, "anyOf", "anyOf removed", [{"kind": "cat"}])

    def test_ref_variants_are_paired_by_name(self):
        defs = {"A": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
                "B": {"type": "object", "properties": {"y": {"type": "string"}}, "required": ["y"]}}
        old = {"$defs": defs, "oneOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}]}
        new_defs = dict(defs, A={"type": "object", "properties": {"x": {"type": "string", "maxLength": 3}}, "required": ["x"]})
        new = {"$defs": new_defs, "oneOf": [{"$ref": "#/$defs/A"}, {"$ref": "#/$defs/B"}]}
        self.assert_breaking(old, new, "oneOf[0].x", "maxLength constraint newly added", {"x": "abcdef"})

    def test_all_of(self):
        base = {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}
        audit = {"type": "object", "properties": {"created": {"type": "string"}}, "required": ["created"]}
        self.assert_breaking({"allOf": [base]}, {"allOf": [base, audit]}, "allOf[1]", "allOf subschema added", {"id": 1})
        self.assert_compatible({"allOf": [base, audit]}, {"allOf": [base]}, "allOf[1]", "allOf subschema removed",
                               [{"id": 1, "created": "x"}])
        base2 = dict(base, properties={"id": {"type": "integer", "minimum": 1}})
        self.assert_breaking({"allOf": [base, audit]}, {"allOf": [base2, audit]}, "allOf[0].id", "minimum constraint newly added",
                             {"id": 0, "created": "x"})


def spec(paths, schemas=None, swagger=False):
    if swagger:
        return {"swagger": "2.0", "info": {"title": "t", "version": "1"}, "paths": paths, "definitions": schemas or {}}
    return {"openapi": "3.0.3", "info": {"title": "t", "version": "1"}, "paths": paths,
            "components": {"schemas": schemas or {}}}


USER = {"type": "object", "properties": {"id": {"type": "integer"}, "email": {"type": "string"},
                                         "role": {"type": "string", "enum": ["admin", "member"]}},
        "required": ["id", "email"]}


def op(req_schema=None, resp_schema=None, params=(), body_required=True):
    o = {"responses": {"200": {"description": "ok", "content": {"application/json": {"schema": resp_schema or
                                                                                      {"$ref": "#/components/schemas/User"}}}}}}
    if req_schema is not None:
        o["requestBody"] = {"required": body_required, "content": {"application/json": {"schema": req_schema}}}
    if params:
        o["parameters"] = list(params)
    return o


class TestOpenAPI(Case):
    def oa(self, old, new, *extra):
        p = diff(old, new, *extra)
        self.assertIn(p.returncode, (0, 1), p.stderr)
        r = json.loads(p.stdout)
        self.assertEqual(r["mode"], "openapi")
        return p, {(f["path"], f["message"]): f["severity"] for k in ("breaking", "compatible", "info") for f in r[k]}

    def test_endpoints_operations_and_parameters(self):
        old = spec({"/users": {"get": op(params=[{"name": "limit", "in": "query", "schema": {"type": "integer"}}]),
                               "post": op({"$ref": "#/components/schemas/User"})},
                    "/legacy": {"get": op()}}, {"User": USER})
        new = spec({"/users": {"get": op(params=[{"name": "limit", "in": "query", "required": True,
                                                  "schema": {"type": "integer", "maximum": 100}},
                                                 {"name": "X-Tenant", "in": "header", "required": True,
                                                  "schema": {"type": "string"}}])},
                    "/users/{id}": {"get": op(params=[{"name": "id", "in": "path", "required": True,
                                                       "schema": {"type": "integer"}}])}}, {"User": USER})
        p, f = self.oa(old, new)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(f[("/legacy", "endpoint removed")], "BREAKING")
        self.assertEqual(f[("/users/{id}", "endpoint added")], "compatible")
        self.assertEqual(f[("POST /users", "operation removed")], "BREAKING")
        self.assertEqual(f[("GET /users query parameter 'limit'", "parameter became required")], "BREAKING")
        self.assertEqual(f[("GET /users header parameter 'X-Tenant'",
                            "new required parameter -- existing requests don't send it")], "BREAKING")
        self.assertTrue(any(k[0] == "GET /users query parameter 'limit'" and "maximum constraint newly added" in k[1]
                            and v == "BREAKING" for k, v in f.items()), f)

    def test_request_and_response_directions(self):
        user2 = json.loads(json.dumps(USER))
        user2["properties"]["role"]["enum"].append("guest")          # response: new enum value breaks clients
        user2["required"] = ["id"]                                    # response: email no longer guaranteed
        user2["properties"]["nickname"] = {"type": "string"}         # response: extra field is fine
        req_old = {"type": "object", "properties": {"email": {"type": "string"}, "plan": {"type": "string"}}}
        req_new = {"type": "object", "properties": {"email": {"type": "string", "maxLength": 50}}, "required": ["email"]}
        old = spec({"/users": {"post": op(req_old, body_required=False)}}, {"User": USER})
        new = spec({"/users": {"post": op(req_new, body_required=True)}}, {"User": user2})
        _, f = self.oa(old, new)
        r = "POST /users response 200 application/json"
        q = "POST /users requestBody application/json"
        self.assertEqual(f[(r + ".role", "enum values added: ['guest'] -- clients that switch on the value may not handle them")], "BREAKING")
        self.assertEqual(f[(r + ".email", "field no longer guaranteed (removed from 'required') -- consumers relying on it break")], "BREAKING")
        self.assertEqual(f[(r + ".nickname", "new optional field added")], "compatible")
        self.assertEqual(f[("POST /users requestBody", "request body became required")], "BREAKING")
        self.assertEqual(f[(q + ".email", "field newly marked 'required' -- existing producers that omit it will now fail validation")], "BREAKING")
        self.assertEqual(f[(q + ".plan", "optional field removed -- consumers that read it will silently get nothing instead of an error")], "compatible")
        self.assertTrue(any(k[0] == q + ".email" and "maxLength" in k[1] and v == "BREAKING" for k, v in f.items()))
        # the reverse change: a narrower response is compatible
        _, f = self.oa(new, old)
        self.assertEqual(f[(r + ".role", "enum values removed: ['guest'] -- previously-valid values now rejected")], "compatible")

    def test_responses_and_content_types(self):
        o = op()
        o["responses"]["404"] = {"description": "nope"}
        old = spec({"/x": {"get": o}}, {"User": USER})
        n = op()
        n["responses"] = {"201": n["responses"]["200"]}
        n["responses"]["201"]["content"]["application/xml"] = {"schema": {"type": "string"}}
        new = spec({"/x": {"get": n}}, {"User": USER, "Extra": {"type": "string"}})
        _, f = self.oa(old, new)
        self.assertEqual(f[("GET /x response 200", "response removed")], "BREAKING")
        self.assertEqual(f[("GET /x response 404", "response removed")], "info")
        self.assertEqual(f[("GET /x response 201", "response added -- make sure clients handle it")], "info")
        self.assertEqual(f[("components.schemas.Extra", "schema component added")], "info")

    def test_swagger_2(self):
        user = {"$ref": "#/definitions/User"}
        old = spec({"/u": {"post": {"parameters": [{"name": "body", "in": "body", "schema": user},
                                                   {"name": "q", "in": "query", "type": "string"}],
                                    "responses": {"200": {"description": "ok", "schema": user}}}}}, {"User": USER},
                   swagger=True)
        u2 = json.loads(json.dumps(USER))
        u2["properties"]["id"] = {"type": "string"}
        new = spec({"/u": {"post": {"parameters": [{"name": "body", "in": "body", "required": True, "schema": user},
                                                   {"name": "q", "in": "query", "type": "string", "enum": ["a"]}],
                                    "responses": {"200": {"description": "ok", "schema": user}}}}}, {"User": u2},
                   swagger=True)
        p, f = self.oa(old, new)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(f[("POST /u requestBody", "request body became required")], "BREAKING")
        self.assertTrue(any(k[0] == "POST /u query parameter 'q'" and "enum constraint newly added" in k[1] for k in f), f)
        self.assertTrue(any(k[0] == "POST /u response 200 application/json.id" and "type changed" in k[1]
                            and v == "BREAKING" for k, v in f.items()), f)

    def test_mixed_inputs_and_markdown(self):
        a, b = write(spec({}, {})), write(obj({}))
        try:
            p = run(a, b)
            self.assertEqual(p.returncode, 2)
            self.assertIn("one file is an OpenAPI", p.stderr)
        finally:
            os.unlink(a)
            os.unlink(b)
        a, b = write(spec({"/a": {"get": op()}}, {"User": USER})), write(spec({}, {"User": USER}))
        try:
            p = run(a, b, "--markdown")
        finally:
            os.unlink(a)
            os.unlink(b)
        self.assertEqual(p.returncode, 1)
        self.assertIn("**Breaking (1)**", p.stdout)
        self.assertIn("- `/a`: endpoint removed", p.stdout)

    def test_version(self):
        self.assertRegex(run("--version").stdout, r"^json-schema-diff \d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
