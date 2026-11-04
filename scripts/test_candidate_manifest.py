#!/usr/bin/env python3
"""candidate_manifest.py: the agent's drafted and declined rows become manifest edits,
on the candidate's own scenario only, and a bad row changes nothing."""
import pathlib
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import candidate_manifest as M  # noqa: E402

MANIFEST = '''# Comment that must survive.
version: 1
prerequisites:
  gateway:
    page: setup/gateway.md
    path: all
  httpbin:
    page: install/sample-app.md
    path: install-httpbin
scenarios:
  # A hand-written scenario, left alone.
  other:
    page: other.md
    path: other
  new-page-schema:
    type: schema
    page: new-page.md
    path: all
    scaffolded: "2026-11-04"
  zebra-schema:
    type: schema
    page: zebra.md
    path: all
'''

KEY = "content/docs/kubernetes/latest/new-page.md::new-page-schema::functional"
ENTRY = {"type": "schema", "page": "new-page.md", "path": "all", "scaffolded": "2026-11-04"}
CANDIDATES = [{"key": KEY, "page": "content/docs/kubernetes/latest/new-page.md", "test": "new-page-schema",
               "candidate": {"manifest": "products/demo/kubernetes/tests.yaml", "scenario": "new-page-schema",
                             "scaffolded": "2026-11-04", "entry": ENTRY}}]


class CandidateManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.path = self.root / "products/demo/kubernetes/tests.yaml"
        self.path.parent.mkdir(parents=True)
        self.path.write_text(MANIFEST)

    def tearDown(self):
        self.tmp.cleanup()

    def scenarios(self):
        return yaml.safe_load(self.path.read_text())["scenarios"]

    def test_drafted_row_adds_the_functional_test(self):
        row = {"key": KEY, "kind": "drafted", "fix": True,
               "scenario": {"needs": ["gateway", "httpbin"], "path": "new-page",
                            "assert": ['curl -sf localhost:15020/metrics | grep "x 1"']}}
        edits = M.apply_rows(self.root, CANDIDATES, [row])
        self.assertEqual(list(edits), ["products/demo/kubernetes/tests.yaml"])
        entry = self.scenarios()["new-page-schema"]
        self.assertEqual(entry, {"type": ["schema", "functional"], "page": "new-page.md", "path": "new-page",
                                 "scaffolded": "2026-11-04", "needs": ["gateway", "httpbin"],
                                 "assert": ['curl -sf localhost:15020/metrics | grep "x 1"']})
        text = self.path.read_text()
        self.assertTrue(text.startswith("# Comment that must survive."))
        self.assertIn("  # A hand-written scenario, left alone.", text)
        self.assertEqual(self.scenarios()["zebra-schema"], {"type": "schema", "page": "zebra.md", "path": "all"})

    def test_declined_row_records_the_public_reason(self):
        row = {"key": KEY, "kind": "declined", "fix": False, "reason": "private detail",
               "public_reason": "Needs an AWS account."}
        M.apply_rows(self.root, CANDIDATES, [row])
        entry = self.scenarios()["new-page-schema"]
        self.assertEqual(entry["type"], "schema")
        self.assertEqual(entry["functional"], "Needs an AWS account.")

    def test_a_second_attempt_that_declines_leaves_no_trace_of_the_draft(self):
        drafted = {"key": KEY, "kind": "drafted", "fix": True,
                   "scenario": {"needs": ["gateway"], "path": "new-page", "assert": ["true"]}}
        M.apply_rows(self.root, CANDIDATES, [drafted])
        M.apply_rows(self.root, CANDIDATES, [{"key": KEY, "kind": "declined", "public_reason": "Flaky on kind."}])
        self.assertEqual(self.scenarios()["new-page-schema"], dict(ENTRY, functional="Flaky on kind."))

    def test_revert_entry_restores_the_base_and_records_why(self):
        drafted = {"key": KEY, "kind": "drafted", "fix": True, "scenario": {"needs": ["gateway"], "assert": ["true"]}}
        M.apply_rows(self.root, CANDIDATES, [drafted])
        out = M.revert_entry(self.path.read_text(), MANIFEST, "new-page-schema", "Did not pass on a cluster.")
        scenarios = yaml.safe_load(out)["scenarios"]
        self.assertEqual(scenarios["new-page-schema"], dict(ENTRY, functional="Did not pass on a cluster."))
        self.assertEqual(scenarios["other"], {"page": "other.md", "path": "other"})

    def test_unknown_prerequisite_changes_nothing(self):
        row = {"key": KEY, "kind": "drafted", "fix": True, "scenario": {"needs": ["istio"], "assert": ["true"]}}
        with self.assertRaisesRegex(ValueError, "not in `prerequisites:`"):
            M.apply_rows(self.root, CANDIDATES, [row])
        self.assertEqual(self.path.read_text(), MANIFEST)

    def test_unknown_field_changes_nothing(self):
        row = {"key": KEY, "kind": "drafted", "fix": True, "scenario": {"type": "functional", "assert": ["true"]}}
        with self.assertRaisesRegex(ValueError, "unknown field `type`"):
            M.apply_rows(self.root, CANDIDATES, [row])
        self.assertEqual(self.path.read_text(), MANIFEST)

    def test_row_for_something_that_is_not_a_candidate_fails(self):
        row = {"key": "content/docs/kubernetes/latest/other.md::other::functional", "kind": "declined",
               "public_reason": "x"}
        with self.assertRaisesRegex(ValueError, "not a candidate"):
            M.apply_rows(self.root, CANDIDATES, [row])

    def test_failure_rows_are_ignored(self):
        self.assertEqual(M.apply_rows(self.root, CANDIDATES, [{"key": "x", "kind": "content"}]), {})
        self.assertEqual(self.path.read_text(), MANIFEST)


if __name__ == "__main__":
    unittest.main()
