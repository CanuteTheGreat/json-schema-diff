"""Direct tests of schema_diff's small helpers, imported by their real module name (mutmut maps tests to
mutants through it): pointer escaping, canonical/hashable forms, $ref dereferencing of non-objects,
Swagger 2.0 non-body parameter schemas, request/response body extraction with a missing `schema`, `$ref`'d
path items and the Markdown "No differences" rule."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import schema_diff as sd  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_pointer_escape_escapes_tilde_before_slash(self):
        self.assertEqual(sd._pointer_escape("a~b/c"), "a~0b~1c")
        self.assertEqual(sd._pointer_escape("~1"), "~01")
        self.assertEqual(sd._pointer_escape("plain"), "plain")

    def test_hashable_is_order_independent_for_containers(self):
        self.assertEqual(sd._hashable({"b": 1, "a": [2]}), sd._hashable({"a": [2], "b": 1}))
        self.assertEqual(sd._hashable({"b": 1, "a": 2}), '{"a": 2, "b": 1}')
        self.assertEqual(sd._hashable([1, 2]), "[1, 2]")
        self.assertEqual(sd._hashable("text"), "text")
        self.assertEqual(sd._hashable(3), 3)
        self.assertIsNone(sd._hashable(None))

    def test_canon_sorts_keys_and_follows_refs(self):
        root = {"definitions": {"A": {"type": "string", "minLength": 1}}}
        self.assertEqual(sd._canon({"type": "string", "minLength": 1}, root), '{"minLength": 1, "type": "string"}')
        self.assertEqual(sd._canon({"$ref": "#/definitions/A"}, root), '{"minLength": 1, "type": "string"}')
        self.assertEqual(sd._canon({"b": 1, "a": 2}, {}), '{"a": 2, "b": 1}')

    def test_deref_only_returns_objects(self):
        root = {"definitions": {"S": "not an object", "O": {"x": 1}}}
        self.assertEqual(sd._deref({"$ref": "#/definitions/O"}, root), {"x": 1})
        self.assertEqual(sd._deref({"$ref": "#/definitions/S"}, root), {})
        self.assertEqual(sd._deref("text", root), {})
        self.assertEqual(sd._deref(["list"], root), {})
        self.assertEqual(sd._deref(None, root), {})
        self.assertEqual(sd._deref({"a": 1}, root), {"a": 1})

    def test_param_schema_keeps_exactly_the_swagger2_keywords(self):
        keep = {"type": "integer", "format": "int32", "enum": [1], "items": {"type": "string"}, "minimum": 1,
                "maximum": 9, "pattern": "^a", "minLength": 2, "maxLength": 3}
        self.assertEqual(sd._param_schema(dict(keep, name="n", **{"in": "query"}, required=True,
                                               description="d", default=1)), keep)
        for k, v in keep.items():
            self.assertEqual(sd._param_schema({k: v, "name": "n"}), {k: v}, k)
        self.assertEqual(sd._param_schema({"schema": {"type": "string"}, "type": "integer"}), {"type": "string"})

    def test_swagger2_body_without_schema_is_an_empty_schema(self):
        op = {"parameters": [{"in": "body", "name": "b", "required": True}]}
        self.assertEqual(sd._bodies(op, {}, True), ({"application/json": {}}, True))
        op = {"parameters": [{"in": "query", "name": "q"}, {"in": "body", "name": "b", "schema": {"type": "x"}}]}
        self.assertEqual(sd._bodies(op, {}, True), ({"application/json": {"type": "x"}}, False))
        self.assertEqual(sd._bodies({"parameters": [{"in": "query", "name": "q"}]}, {}, True), ({}, False))

    def test_swagger2_body_parameter_may_be_a_ref(self):
        root = {"parameters": {"B": {"in": "body", "name": "b", "schema": {"type": "object"}, "required": True}}}
        op = {"parameters": [{"$ref": "#/parameters/B"}]}
        self.assertEqual(sd._bodies(op, root, True), ({"application/json": {"type": "object"}}, True))

    def test_openapi3_body_content_without_schema_is_an_empty_schema(self):
        op = {"requestBody": {"required": True, "content": {"text/plain": {}, "application/json": {"schema": {"a": 1}}}}}
        self.assertEqual(sd._bodies(op, {}, False), ({"text/plain": {}, "application/json": {"a": 1}}, True))
        self.assertEqual(sd._bodies({}, {}, False), ({}, False))
        self.assertEqual(sd._bodies({"requestBody": {"content": {"x/y": {}}}}, {}, False), ({"x/y": {}}, False))

    def test_responses_content_without_schema_is_an_empty_schema(self):
        op = {"responses": {200: {"content": {"text/plain": {}, "application/json": {"schema": {"a": 1}}}}, "404": {}}}
        self.assertEqual(sd._responses(op, {}, False), {"200": {"text/plain": {}, "application/json": {"a": 1}}, "404": {}})
        op = {"responses": {"200": {"schema": {"type": "x"}}, "204": {}}}
        self.assertEqual(sd._responses(op, {}, True), {"200": {"application/json": {"type": "x"}}, "204": {}})

    def test_ref_path_items_are_followed_in_both_documents(self):
        def doc(required):
            return {"openapi": "3.0.0", "x-items": {"item": {"get": {"parameters": [
                        {"name": "q", "in": "query", "required": required, "schema": {"type": "string"}}]}}},
                    "paths": {"/p": {"$ref": "#/x-items/item"}}}
        findings = []
        sd.diff_openapi(doc(False), doc(True), findings)
        self.assertEqual([f.to_dict() for f in findings],
                         [{"path": "GET /p query parameter 'q'", "severity": "BREAKING",
                           "message": "parameter became required"}])

    def test_markdown_says_no_differences_only_when_there_are_none(self):
        class F:
            path = ["x"]
            message = "m"
        for kw in ({"breaking": [F()]}, {"compatible": [F()]}, {"info": [F()]}):
            args = dict(breaking=[], compatible=[], info=[])
            args.update(kw)
            self.assertNotIn("No differences.", sd.markdown(args["breaking"], args["compatible"], args["info"], "a", "b"), kw)
        self.assertIn("No differences.", sd.markdown([], [], [], "a", "b"))
        self.assertNotIn("No differences.", sd.markdown([F()], [F()], [F()], "a", "b"))


if __name__ == "__main__":
    unittest.main()
