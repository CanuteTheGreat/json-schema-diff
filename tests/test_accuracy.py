"""Detection accuracy against an independent oracle (the `jsonschema` validator).

For every single-keyword edit of a realistic schema, a pool of documents (a valid baseline
plus every field varied one at a time through edge values, missing, wrong type and an extra
key) is validated against the old and the new schema with jsonschema:

  * request / plain-schema direction: the change is breaking iff some document the OLD schema
    accepts is rejected by the NEW one;
  * response direction (OpenAPI response body): breaking iff the NEW schema allows a document
    the OLD one didn't (a consumer written against the old contract can receive it).

The tool must report BREAKING exactly when the oracle finds such a witness: a miss is a false
negative, a BREAKING with no witness is a false positive -- except for the few contract
conventions the README states on purpose (CONVENTIONS below), where pure validation and the
API contract disagree.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

from jsonschema import Draft202012Validator

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "schema_diff.py")

BASE = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 10},
        "age": {"type": "integer", "minimum": 0, "maximum": 120},
        "kind": {"enum": ["a", "b", "c"]},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "flag": {"type": "boolean"},
        "score": {"type": "number", "exclusiveMinimum": 0},
    },
    "required": ["name"],
}

VARIANTS = {
    "name": ["", "a", "abc", "abcdefgh", "abcdefghij", "abcdefghijkl", "ABC", 5, None],
    "age": [-10, -5, -1, 0, 5, 10, 50, 100, 110, 120, 121, 130, 150, 5.5, "x", None],
    "kind": ["a", "b", "c", "d", 1],
    "tags": [[], ["x"], ["x", "y"], ["x", "y", "z"], ["a", "b", "c", "d"], ["a", "b", "c", "d", "e", "f"], [1]],
    "flag": [True, False, "yes"],
    "score": [-1, 0, 0.5, 5, "x"],
}
BASE_DOC = {"name": "abc", "age": 50, "kind": "a", "tags": ["x"], "flag": True, "score": 5}


def pool():
    docs = [dict(BASE_DOC), {k: v for k, v in BASE_DOC.items() if k != "name"}, dict(BASE_DOC, zzz=1)]
    for field, values in VARIANTS.items():
        docs.append({k: v for k, v in BASE_DOC.items() if k != field})
        for v in values:
            docs.append(dict(BASE_DOC, **{field: v}))
    return docs


POOL = pool()


def edit(fn, base=None):
    def make():
        s = copy.deepcopy(base or BASE)
        fn(s)
        return s
    return make


P = lambda s: s["properties"]  # noqa: E731
CLOSED = dict(copy.deepcopy(BASE), additionalProperties=False)

EDITS = {
    "require age": edit(lambda s: s["required"].append("age")),
    "unrequire name": edit(lambda s: s.__setitem__("required", [])),
    "enum remove c": edit(lambda s: P(s)["kind"].__setitem__("enum", ["a", "b"])),
    "enum add d": edit(lambda s: P(s)["kind"]["enum"].append("d")),
    "enum dropped": edit(lambda s: P(s)["kind"].pop("enum")),
    "const kind": edit(lambda s: P(s)["kind"].__setitem__("const", "a")),
    "minimum raised": edit(lambda s: P(s)["age"].__setitem__("minimum", 10)),
    "minimum lowered": edit(lambda s: P(s)["age"].__setitem__("minimum", -5)),
    "maximum lowered": edit(lambda s: P(s)["age"].__setitem__("maximum", 100)),
    "maximum raised": edit(lambda s: P(s)["age"].__setitem__("maximum", 150)),
    "maximum removed": edit(lambda s: P(s)["age"].pop("maximum")),
    "maxLength lowered": edit(lambda s: P(s)["name"].__setitem__("maxLength", 5)),
    "maxLength raised": edit(lambda s: P(s)["name"].__setitem__("maxLength", 20)),
    "minLength raised": edit(lambda s: P(s)["name"].__setitem__("minLength", 3)),
    "minLength removed": edit(lambda s: P(s)["name"].pop("minLength")),
    "maxItems lowered": edit(lambda s: P(s)["tags"].__setitem__("maxItems", 2)),
    "maxItems raised": edit(lambda s: P(s)["tags"].__setitem__("maxItems", 5)),
    "items type changed": edit(lambda s: P(s)["tags"]["items"].__setitem__("type", "integer")),
    "integer to number": edit(lambda s: P(s)["age"].__setitem__("type", "number")),
    "age nullable": edit(lambda s: P(s)["age"].__setitem__("type", ["integer", "null"])),
    "flag to string": edit(lambda s: P(s)["flag"].__setitem__("type", "string")),
    "flag type removed": edit(lambda s: P(s)["flag"].pop("type")),
    "pattern added": edit(lambda s: P(s)["name"].__setitem__("pattern", "^[a-z]+$")),
    "closed": edit(lambda s: s.__setitem__("additionalProperties", False)),
    "reopened": edit(lambda s: s.pop("additionalProperties"), CLOSED),
    "optional removed (open)": edit(lambda s: P(s).pop("flag")),
    "optional removed (closed)": edit(lambda s: P(s).pop("flag"), CLOSED),
    "required removed": edit(lambda s: (P(s).pop("name"), s.__setitem__("required", []))),
    "exclusiveMinimum to minimum": edit(lambda s: (P(s)["score"].pop("exclusiveMinimum"),
                                                   P(s)["score"].__setitem__("minimum", 0))),
    "minimum to exclusiveMinimum": edit(lambda s: (P(s)["age"].pop("minimum"),
                                                   P(s)["age"].__setitem__("exclusiveMinimum", 0))),
    "exclusiveMinimum raised": edit(lambda s: P(s)["score"].__setitem__("exclusiveMinimum", 1)),
    "no change": edit(lambda s: None),
}


# (edit, direction) -> (expected BREAKING?, why). Every other case is judged by the oracle alone.
CONVENTIONS = {
    ("required removed", "request"): (True, "README: a removed required field breaks consumers depending on it"),
    ("optional removed (closed)", "response"): (True, "README: a removed field in a response breaks clients"),
    ("reopened", "response"): (False, "tolerant reader: consumers ignore undeclared extra response fields"),
    ("undo optional removed (open)", "request"): (False, "README: a new optional field is compatible"),
}


def oracle_witness(accepts, rejects):
    a, r = Draft202012Validator(accepts), Draft202012Validator(rejects)
    for doc in POOL:
        if a.is_valid(doc) and not r.is_valid(doc):
            return doc
    return None


def write(obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    return path


def tool(old, new):
    a, b = write(old), write(new)
    try:
        p = subprocess.run([sys.executable, TOOL, a, b, "--json"], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(a)
        os.unlink(b)
    return p, json.loads(p.stdout)


def openapi(schema):
    return {"openapi": "3.1.0", "info": {"title": "t", "version": "1"},
            "paths": {"/x": {"get": {"responses": {"200": {"description": "ok",
                                                           "content": {"application/json": {"schema": schema}}}}}}}}


class TestOracle(unittest.TestCase):
    def check(self, name, old, new, response):
        if response:
            witness = oracle_witness(new, old)
            p, d = tool(openapi(old), openapi(new))
        else:
            witness = oracle_witness(old, new)
            p, d = tool(old, new)
        breaking = d["summary"]["breaking"] > 0
        self.assertEqual(p.returncode, 1 if breaking else 0)
        conv = CONVENTIONS.get((name, "response" if response else "request"))
        if conv is not None:
            self.assertEqual(breaking, conv[0], "%s: %s; tool said %r" % (name, conv[1], d))
            return
        if witness is not None:
            self.assertTrue(breaking, "false negative: %s (%s) not BREAKING though %r is accepted by the %s "
                            "schema and rejected by the %s one; tool said %r"
                            % (name, "response" if response else "request", witness,
                               "new" if response else "old", "old" if response else "new", d))
        else:
            self.assertFalse(breaking, "false positive: %s (%s) reported BREAKING but no document in the pool "
                             "tells old and new apart that way: %r" % (name, "response" if response else "request",
                                                                       d["breaking"]))

    def test_request_direction(self):
        for name, make in EDITS.items():
            with self.subTest(edit=name):
                base = CLOSED if name in ("reopened", "optional removed (closed)") else BASE
                self.check(name, base, make(), response=False)

    def test_response_direction(self):
        for name, make in EDITS.items():
            with self.subTest(edit=name):
                base = CLOSED if name in ("reopened", "optional removed (closed)") else BASE
                self.check(name, base, make(), response=True)

    def test_reverse_edits(self):
        # every edit undone (new -> old) must be judged by the same oracle
        for name, make in EDITS.items():
            with self.subTest(edit="undo " + name):
                base = CLOSED if name in ("reopened", "optional removed (closed)") else BASE
                self.check("undo " + name, make(), base, response=False)


if __name__ == "__main__":
    unittest.main()
