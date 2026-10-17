"""The finding-by-finding contract of json-schema-diff (found as mutation-testing survivors): every
OpenAPI 3 / Swagger 2 operation-level change (parameters by location, required flips, request bodies
and content types, responses by status class, $ref'd parameters/bodies/responses, path-level
parameters, components), oneOf/anyOf/allOf variant pairing (by $ref, title, const, discriminator,
type), and the text / Markdown / JSON outputs and exit codes. The tool runs as a subprocess.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "schema_diff.py")
sys.path.insert(0, HERE)
import specs_contract as specs  # noqa: E402

REMOVED_COMPONENT = "schema component removed (breaking only where it was referenced; see the operations above)"
NEW_REQUIRED = "new required parameter -- existing requests don't send it"
NARROWED = "narrowed from %s to %s -- some previously-valid values now rejected"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, doc):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))
        return path

    def run_tool(self, old, new, *args):
        return subprocess.run([sys.executable, TOOL, self.write("old.json", old), self.write("new.json", new)]
                              + list(args), capture_output=True, text=True, timeout=60)

    def findings(self, old, new, *args):
        p = self.run_tool(old, new, "--json", *args)
        self.assertIn(p.returncode, (0, 1), p.stderr)
        d = json.loads(p.stdout)
        return p.returncode, d, {sev: [(x["path"], x["message"]) for x in d[sev]]
                                 for sev in ("breaking", "compatible", "info")}


class TestOpenAPI3(Base):
    def test_every_operation_level_change(self):
        code, d, f = self.findings(specs.OAS_OLD, specs.OAS_NEW)
        self.assertEqual((code, d["mode"], d["summary"]), (1, "openapi", {"breaking": 13, "compatible": 7, "info": 4}))
        self.assertEqual(f["breaking"], [
            ("/owners", "endpoint removed"),
            ("GET /pets header parameter 'tenant'", NEW_REQUIRED),
            ("GET /pets path parameter 'region'", NEW_REQUIRED),  # path parameters are always required
            ("GET /pets header parameter 'X-Old'", "parameter removed"),
            ("GET /pets header parameter 'X-Trace'", "parameter became required"),  # overrides the path-level $ref
            ("GET /pets query parameter 'limit'", "maximum " + NARROWED % (100, 50)),
            ("GET /pets query parameter 'q'", "parameter became required"),
            ("GET /pets response 500 application/json", "response content type removed"),
            ("POST /pets requestBody application/xml", "request content type no longer accepted"),
            ("POST /pets response 201 text/plain", "response content type removed"),
            ("DELETE /pets", "operation removed"),
            ("PATCH /pets requestBody", "request body became required"),
            ("PATCH /pets response 202", "response removed"),  # a 2xx: clients relied on it
        ])
        self.assertEqual(f["compatible"], [
            ("/vets", "endpoint added"),
            ("GET /pets query parameter 'fields'", "new optional parameter"),
            ("GET /pets query parameter 'sort'", "parameter removed -- the server now ignores it"),
            ("GET /pets query parameter 'page'", "parameter no longer required"),
            ("PUT /pets", "operation added"),
            ("POST /pets requestBody", "request body no longer required"),  # via components.requestBodies
            ("POST /pets requestBody text/plain", "request content type added"),
        ])
        self.assertEqual(f["info"], [
            ("GET /pets response 404", "response removed"),
            ("GET /pets response 429", "response added -- make sure clients handle it"),
            ("components.schemas.Legacy", REMOVED_COMPONENT),
            ("components.schemas.Owner", "schema component added"),
        ])

    def test_identical_specs_and_untouched_references(self):
        code, d, f = self.findings(specs.OAS_OLD, specs.OAS_OLD)
        self.assertEqual((code, d["summary"]), (0, {"breaking": 0, "compatible": 0, "info": 0}))
        p = self.run_tool(specs.OAS_OLD, specs.OAS_OLD)
        self.assertEqual(p.stdout, "No differences found -- schemas are identical.\n")

    def test_response_schemas_are_judged_from_the_consumer_side(self):
        old = json.loads(json.dumps(specs.OAS_OLD))
        new = json.loads(json.dumps(specs.OAS_OLD))
        new["components"]["schemas"]["Pet"] = {"type": "object", "properties": {"name": {"type": "string"}}}
        code, d, f = self.findings(old, new)
        no_longer = "field no longer guaranteed (removed from 'required') -- consumers relying on it break"
        gone = "field removed from the response -- consumers that read it get nothing"
        self.assertEqual(f["breaking"], [  # responses: the $ref'd schema reaches consumers through both operations
            ("GET /pets response 200 application/json.[].name", no_longer),
            ("GET /pets response 200 application/json.[].tag", gone),
            ("POST /pets response 201 application/json.name", no_longer),
            ("POST /pets response 201 application/json.tag", gone),
        ])
        self.assertEqual(f["compatible"], [  # the same edit, seen by a request body, only relaxes it
            ("POST /pets requestBody application/json.name", "field no longer required -- safe relaxation"),
            ("POST /pets requestBody application/json.tag",
             "optional field removed -- consumers that read it will silently get nothing instead of an error"),
        ])


class TestSwagger2(Base):
    def test_every_operation_level_change(self):
        code, d, f = self.findings(specs.SW_OLD, specs.SW_NEW)
        self.assertEqual((code, d["mode"]), (1, "openapi"))
        self.assertEqual(f["breaking"], [
            ("GET /items query parameter 'ids'.[]", "type changed from ['string'] to ['integer'] -- incompatible sets, "
                                                    "likely breaking for both producers and consumers"),
            ("GET /items query parameter 'n'", "maximum " + NARROWED % (10, 5)),
            ("GET /items query parameter 's'", "minLength " + NARROWED % (1, 2)),
            ("GET /items query parameter 's'", "pattern changed from '^a' to '^b' -- treat as breaking unless proven a "
                                               "strict superset"),
            ("POST /items requestBody application/json.qty", "field newly marked 'required' -- existing producers "
                                                             "that omit it will now fail validation"),
        ])
        self.assertEqual(f["compatible"], [
            ("GET /items query parameter 's'", "enum values added: ['abc']"),
            ("GET /items query parameter 's'", "maxLength relaxed from 5 to 10"),
            ("GET /items response 200 application/json.qty", "field now always present (newly required)"),
            ("POST /items requestBody", "request body no longer required"),
        ])
        self.assertEqual(f["info"], [
            ("GET /items query parameter 'n'", "format hint changed from 'int32' to 'int64'"),
            ("GET /items response default", "response removed"),  # not a 2xx
            ("components.schemas.Gone", REMOVED_COMPONENT),
            ("components.schemas.New", "schema component added"),
        ])

    def test_renaming_the_body_parameter_is_not_a_change(self):
        # a Swagger 2.0 body parameter's name never goes over the wire
        old = json.loads(json.dumps(specs.SW_OLD))
        new = json.loads(json.dumps(specs.SW_OLD))
        new["paths"]["/items"]["post"]["parameters"][0]["name"] = "payload"
        code, d, f = self.findings(old, new)
        self.assertEqual((code, d["summary"]), (0, {"breaking": 0, "compatible": 0, "info": 0}))
        new["paths"]["/items"]["post"]["parameters"] = []
        code, d, f = self.findings(old, new)
        self.assertEqual(f["breaking"], [("POST /items requestBody application/json",
                                          "request content type no longer accepted")])
        self.assertEqual(f["compatible"], [("POST /items requestBody", "request body no longer required")])


class TestComposition(Base):
    def test_variants_paired_by_ref(self):
        defs_o = {"Cat": {"title": "Cat", "type": "object", "properties": {"meow": {"type": "string"}}},
                  "Dog": {"type": "object", "properties": {"bark": {"type": "string"}}}}
        defs_n = dict(defs_o, Dog={"type": "object", "required": ["bark"], "properties": {"bark": {"type": "string"}}},
                      Bird={"type": "object"})
        old = {"$defs": defs_o, "oneOf": [{"$ref": "#/$defs/Cat"}, {"$ref": "#/$defs/Dog"}]}
        new = {"$defs": defs_n, "oneOf": [{"$ref": "#/$defs/Cat"}, {"$ref": "#/$defs/Bird"}, {"$ref": "#/$defs/Dog"}]}
        code, d, f = self.findings(old, new)
        self.assertEqual(f["breaking"], [("oneOf[2].bark", "field newly marked 'required' -- existing producers "
                                                           "that omit it will now fail validation")])
        self.assertEqual(f["info"], [("oneOf[1]", "oneOf variant added -- more values accepted, but documents "
                                                  "matching an existing variant AND the new one now fail oneOf "
                                                  "(verify the variants don't overlap)")])

    def test_variants_paired_by_title_type_const_and_discriminator(self):
        old = {"anyOf": [{"title": "A", "type": "string", "maxLength": 5}, {"type": "integer", "maximum": 3},
                         {"const": "x"}, {"properties": {"kind": {"const": "k1"}, "v": {"type": "string"}}}]}
        new = {"anyOf": [{"properties": {"kind": {"const": "k1"}, "v": {"type": "integer"}}}, {"const": "x"},
                         {"type": "integer", "maximum": 2}, {"title": "A", "type": "string", "maxLength": 4},
                         {"type": "boolean"}]}
        code, d, f = self.findings(old, new)
        self.assertEqual(f["breaking"], [
            ("anyOf[3]", "maxLength " + NARROWED % (5, 4)),
            ("anyOf[2]", "maximum " + NARROWED % (3, 2)),
            ("anyOf[0].v", "type changed from ['string'] to ['integer'] -- incompatible sets, likely breaking for "
                           "both producers and consumers"),
        ])
        self.assertEqual(f["compatible"], [("anyOf[4]", "anyOf variant added -- more values accepted")])
        # a const variant edited in place, alone, is paired too
        code, d, f = self.findings({"oneOf": [{"const": "a"}, {"type": "string", "minLength": 1}]},
                                   {"oneOf": [{"const": "b"}, {"type": "string", "minLength": 1}]})
        self.assertEqual(f, {"breaking": [("oneOf[0]", "const changed from 'a' to 'b'")], "compatible": [], "info": []})

    def test_keywords_added_and_removed(self):
        code, d, f = self.findings({"type": "object"}, {"type": "object", "anyOf": [], "allOf": [{"required": ["a"]}],
                                                         "oneOf": [{"type": "object"}], "not": {"type": "null"}})
        self.assertEqual(f["breaking"], [("oneOf", "oneOf newly added -- values must now also match 1 subschema(s)"),
                                         ("allOf", "allOf newly added -- values must now also match all of 1 subschema(s)")])
        self.assertEqual(f["compatible"], [("anyOf", "anyOf newly added -- values must now also match 0 subschema(s)")])
        self.assertEqual(f["info"], [("not", "'not' subschema changed -- review by hand")])
        code, d, f = self.findings({"allOf": [{"required": ["a"]}, {"required": ["b"]}], "not": {"type": "null"}},
                                   {"allOf": [{"required": ["a"]}, {"required": ["c"]}, {"minProperties": 1}]})
        self.assertEqual(f["breaking"], [("allOf[1]", "allOf subschema added -- values must satisfy one more constraint"),
                                         ("allOf[2]", "allOf subschema added -- values must satisfy one more constraint")])
        self.assertEqual(f["compatible"], [("allOf[1]", "allOf subschema removed -- one constraint fewer")])
        self.assertEqual(f["info"], [("not", "'not' subschema changed -- review by hand")])
        code, d, f = self.findings({"oneOf": [{"type": "string"}, {"type": "integer"}]}, {"type": "string"})
        self.assertEqual(f["compatible"], [("oneOf", "oneOf removed (was 2 subschema(s)) -- fewer constraints")])
        code, d, f = self.findings({"oneOf": [{"type": "string"}, {"type": "integer"}]},
                                   {"oneOf": [{"type": "string"}]})
        self.assertEqual(f["breaking"], [("oneOf[1]", "oneOf variant removed -- values that only matched it are now "
                                                      "rejected")])


class TestOutputs(Base):
    OLD = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}, "a": {"type": "string"},
                                                               "f": {"type": "string", "format": "date"}}}
    NEW = {"type": "object", "required": ["id", "b"], "properties": {"id": {"type": "string"}, "b": {"type": "string"},
                                                                    "c": {"type": "string"},
                                                                    "f": {"type": "string", "format": "date-time"}}}

    def test_text(self):
        p = self.run_tool(self.OLD, self.NEW)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stdout, "\n".join([
            "[BREAKING ] b: field newly marked 'required' -- existing producers that omit it will now fail validation",
            "[compat   ] a: optional field removed -- consumers that read it will silently get nothing instead of an error",
            "[compat   ] c: new optional field added",
            "[info     ] f: format hint changed from 'date' to 'date-time'",
            "",
            "Summary: 1 breaking, 2 compatible, 1 informational", ""]))
        p = self.run_tool(self.OLD, self.NEW, "--quiet-info")
        self.assertNotIn("[info", p.stdout)
        self.assertIn("Summary: 1 breaking, 2 compatible, 1 informational", p.stdout)

    def test_markdown(self):
        p = self.run_tool(self.OLD, self.NEW, "--markdown")
        old, new = os.path.join(self.tmp, "old.json"), os.path.join(self.tmp, "new.json")
        self.assertEqual(p.stdout, "\n".join([
            "### API changes: `%s` -> `%s`" % (old, new), "",
            "**Breaking (1)**", "",
            "- `b`: field newly marked 'required' -- existing producers that omit it will now fail validation", "",
            "**Compatible (2)**", "",
            "- `a`: optional field removed -- consumers that read it will silently get nothing instead of an error",
            "- `c`: new optional field added", "",
            "**Informational (1)**", "",
            "- `f`: format hint changed from 'date' to 'date-time'", "", ""]))
        p = self.run_tool(self.OLD, self.NEW, "--markdown", "--quiet-info")
        self.assertNotIn("Informational", p.stdout)
        p = self.run_tool(self.OLD, self.OLD, "--markdown")
        self.assertTrue(p.stdout.endswith("\n\nNo differences.\n"), p.stdout)

    def test_json(self):
        code, d, f = self.findings(self.OLD, self.NEW)
        self.assertEqual(set(d), {"version", "mode", "breaking", "compatible", "info", "summary"})
        self.assertEqual((d["mode"], d["summary"]), ("schema", {"breaking": 1, "compatible": 2, "info": 1}))
        self.assertEqual(d["breaking"][0], {"path": "b", "severity": "BREAKING", "message": f["breaking"][0][1]})
        self.assertEqual(d["info"][0]["severity"], "info")
        self.assertEqual(d["compatible"][0]["severity"], "compatible")

    def test_exit_codes(self):
        compat_only = ({"type": "object"}, {"type": "object", "properties": {"x": {"type": "string"}}})
        for args, breaking_rc, compat_rc in ((), 1, 0), (("--fail-on", "breaking"), 1, 0), \
                (("--fail-on", "compatible"), 1, 1), (("--fail-on", "never"), 0, 0), \
                (("--fail-on", "never", "--fail-on-breaking"), 1, 0), (("--fail-on-breaking",), 1, 0):
            with self.subTest(args=args):
                self.assertEqual(self.run_tool(self.OLD, self.NEW, *args).returncode, breaking_rc)
                self.assertEqual(self.run_tool(*compat_only, *args).returncode, compat_rc)
        info_only = ({"format": "date"}, {"format": "date-time"})
        self.assertEqual(self.run_tool(*info_only, "--fail-on", "compatible").returncode, 0)

    def test_errors(self):
        p = self.run_tool("{not json", self.NEW)
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.startswith("error: "), p.stderr)
        p = subprocess.run([sys.executable, TOOL, os.path.join(self.tmp, "missing.json"), self.write("n.json", {})],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("error: [Errno 2] No such file or directory", p.stderr)
        p = self.run_tool(specs.OAS_OLD, self.NEW)
        self.assertEqual((p.returncode, p.stderr), (2, "error: one file is an OpenAPI/Swagger spec and the other a "
                                                       "plain JSON Schema\n"))
        p = self.run_tool({"openapi": "3.1.0"}, {"openapi": "3.1.0"}, "--json")  # no paths: plain schemas
        self.assertEqual(json.loads(p.stdout)["mode"], "schema")


if __name__ == "__main__":
    unittest.main()
