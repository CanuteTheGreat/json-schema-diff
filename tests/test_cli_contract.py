"""Shared CLI contract (docs/CLI-CONTRACT.md): exit codes, --json schema, --quiet, stderr for errors."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

import contract_support as cs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "schema_diff.py")
sys.path.insert(0, ROOT)
import schema_diff as tool  # noqa: E402

SCHEMA = cs.load_schema(ROOT, "json-schema-diff")
OLD = os.path.join(ROOT, "old_schema.json")
BREAKING = os.path.join(ROOT, "new_schema_breaking.json")
COMPATIBLE = os.path.join(ROOT, "new_schema_compatible.json")


def cli(*args):
    return cs.run_cli(TOOL, args)


class TestExitCodes(unittest.TestCase):
    def test_clean_is_0(self):
        self.assertEqual(cli(OLD, OLD)[0], 0)

    def test_findings_is_1(self):
        self.assertEqual(cli(OLD, BREAKING)[0], 1)

    def test_compatible_change_only_fails_with_fail_on_compatible(self):
        self.assertEqual(cli(OLD, COMPATIBLE)[0], 0)
        self.assertEqual(cli(OLD, COMPATIBLE, "--fail-on", "compatible")[0], 1)

    def test_fail_on_never_is_0(self):
        self.assertEqual(cli(OLD, BREAKING, "--fail-on", "never")[0], 0)

    def test_bad_args_is_2_with_message_on_stderr(self):
        for a in ((), (OLD,), (OLD, OLD, "--no-such-flag"), (OLD, OLD, "--fail-on", "bogus")):
            rc, out, err = cli(*a)
            self.assertEqual(rc, 2, a)
            self.assertEqual(out, "", a)
            self.assertIn("usage:", err)

    def test_missing_file_is_2(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-schema-file.json")
        for a in ((missing, OLD), (OLD, missing)):
            rc, out, err = cli(*a)
            self.assertEqual((rc, out), (2, ""), a)
            self.assertIn("error:", err)

    def test_invalid_json_is_2(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as f:
            f.write("{not json")
        rc, out, err = cli(OLD, path)
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("error:", err)

    def test_unexpected_error_is_3_on_stderr_not_a_traceback_exit_1(self):
        buf = io.StringIO()
        with mock.patch.object(tool, "_main", side_effect=RuntimeError("boom")), redirect_stderr(buf):
            self.assertEqual(tool.main([OLD, OLD]), 3)
        self.assertIn("RuntimeError: boom", buf.getvalue())


class TestJson(unittest.TestCase):
    def check(self, args, code):
        rc, out, err = cli(*args, "--json")
        self.assertEqual(rc, code, err)
        doc = json.loads(out)  # exactly one JSON document, nothing else on stdout
        self.assertEqual(cs.validate(doc, SCHEMA), [])
        self.assertEqual(doc["exit_code"], rc)
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["tool"], "json-schema-diff")
        self.assertEqual(doc["version"], tool.__version__)
        return doc

    def test_clean(self):
        self.assertEqual(self.check((OLD, OLD), 0)["summary"], {"breaking": 0, "compatible": 0, "info": 0})

    def test_findings(self):
        doc = self.check((OLD, BREAKING), 1)
        self.assertGreater(doc["summary"]["breaking"], 0)

    def test_fail_on_never_reports_the_gate_result_in_exit_code(self):
        doc = self.check((OLD, BREAKING, "--fail-on", "never"), 0)
        self.assertGreater(doc["summary"]["breaking"], 0)

    def test_usage_and_input_errors_leave_stdout_empty(self):
        for a in (("--json",), (OLD, os.path.join(tempfile.gettempdir(), "no-such-schema-file.json"), "--json")):
            rc, out, err = cli(*a)
            self.assertEqual((rc, out), (2, ""), a)
            self.assertNotEqual(err, "")

    def test_schema_rejects_a_document_without_the_contract_keys(self):
        self.assertNotEqual(cs.validate({"breaking": []}, SCHEMA), [])
        good = {"schema_version": 1, "tool": "json-schema-diff", "version": "1", "exit_code": 0, "mode": "schema",
                "breaking": [], "compatible": [], "info": [],
                "summary": {"breaking": 0, "compatible": 0, "info": 0}}
        self.assertEqual(cs.validate(good, SCHEMA), [])
        self.assertNotEqual(cs.validate(dict(good, exit_code=9), SCHEMA), [])


class TestQuiet(unittest.TestCase):
    def test_quiet_prints_nothing_but_keeps_exit_code(self):
        for new, code in ((OLD, 0), (BREAKING, 1)):
            for extra in ((), ("--json",), ("--markdown",)):
                for flag in ("--quiet", "-q"):
                    rc, out, err = cli(OLD, new, flag, *extra)
                    self.assertEqual((rc, out), (code, ""), (flag, extra, new))

    def test_quiet_info_is_a_different_option_and_still_prints(self):
        rc, out, err = cli(OLD, BREAKING, "--quiet-info")
        self.assertEqual(rc, 1)
        self.assertIn("BREAKING", out)

    def test_without_quiet_the_report_is_printed(self):
        self.assertIn("No differences found", cli(OLD, OLD)[1])


if __name__ == "__main__":
    unittest.main()
