"""Tests for doc_test_type_check.py: page classification, findings, and --apply edits."""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_test_type_check as tc  # noqa: E402

MANIFEST = textwrap.dedent("""\
    version: 1
    mode: kubernetes
    prerequisites:
      gateway:
        page: documentation/setup/gateway.md
        path: all
    # Pages with deliberately no test.
    skip:
    - page: documentation/a/_index.md
    # Needs a cloud account.
    - page: documentation/b.md
    - source: agw-docs/pages/c.md
    scenarios:
      alpha:
        type:
        - schema
        - functional
        needs:
        - gateway
        source: agw-docs/pages/alpha.md
        path: alpha
      # beta's comment stays with beta.
      beta:
        type: schema
        page: documentation/beta.md
        path: beta
      gamma:
        type: functional
        page: documentation/gamma.md
        path: gamma
    """)


def finding(kind, scenario=None, ref=None, fix=None, severity="error"):
    return tc.Finding(manifest="products/p/kubernetes/tests.yaml", kind=kind, severity=severity,
                      ref=ref or {}, message="", scenario=scenario, fix=fix)


class Helpers(unittest.TestCase):
    def test_runnable_ignores_heredoc_body_and_comments(self):
        self.assertTrue(tc._runnable("kubectl apply -f- <<EOF\napiVersion: v1\nkind: X\nEOF\n"))
        self.assertFalse(tc._runnable("# just a note\n\n"))
        self.assertTrue(tc._runnable("curl -i localhost:8080\n"))

    def test_all_yaml(self):
        self.assertTrue(tc._all_yaml("binds:\n- port: 3000\n"))
        self.assertFalse(tc._all_yaml("kubectl apply -f- <<EOF\napiVersion: v1\nkind: X\nEOF\n"))
        self.assertFalse(tc._all_yaml('users:\n- "a:\\$b"\n'))   # \$ is not a YAML escape

    def test_is_resource(self):
        self.assertTrue(tc._is_resource({"apiVersion": "v1", "kind": "Service"}, "", "kubernetes", None))
        self.assertFalse(tc._is_resource({"binds": []}, "", "kubernetes", None))
        self.assertTrue(tc._is_resource({"binds": []}, "", "standalone", None))
        self.assertFalse(tc._is_resource({"policies": {}}, "", "standalone", None))


class PageFacts(unittest.TestCase):
    def facts(self, body, path="x", mode="kubernetes"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rel = f"content/docs/{mode}/main/page.md"
            (root / rel).parent.mkdir(parents=True)
            (root / rel).write_text("---\ntitle: t\n---\n" + textwrap.dedent(body), encoding="utf-8")
            return tc.page_facts(root, rel, path, "main", mode, root)

    def test_resource_and_command(self):
        f = self.facts("""\
            ```yaml
            kubectl apply -f- <<EOF
            apiVersion: agentgateway.dev/v1alpha1
            kind: AgentgatewayPolicy
            metadata:
              name: p
            EOF
            ```
            """)
        self.assertEqual((f.resources, f.page_commands, f.visible, f.unreadable), (1, 1, 1, 0))

    def test_unreadable_resource_still_counts(self):
        f = self.facts("""\
            ```yaml
            kubectl apply -f- <<EOF
            apiVersion: agentgateway.dev/v1alpha1
            kind: AgentgatewayPolicy
            spec:
              users:
              - "user:\\$apr1"
            EOF
            ```
            """)
        self.assertEqual((f.resources, f.unreadable), (1, 1))

    def test_prose_only_page(self):
        f = self.facts("Just words.\n")
        self.assertEqual((f.resources, f.page_commands, f.visible, f.selected), (0, 0, 0, 0))


class ApplyFixes(unittest.TestCase):
    def apply(self, findings):
        out, changes = tc.apply_fixes(MANIFEST, findings)
        return out, yaml.safe_load(out), changes

    def test_drop_one_of_two_types(self):
        out, data, _ = self.apply([finding("schema-empty", "alpha", {"source": "agw-docs/pages/alpha.md"})])
        self.assertEqual(data["scenarios"]["alpha"]["type"], "functional")
        self.assertIn("    type: functional\n    needs:", out)

    def test_last_type_moves_page_to_skip_with_its_comment(self):
        out, data, changes = self.apply([finding("schema-empty", "beta", {"page": "documentation/beta.md"})])
        self.assertNotIn("beta", data["scenarios"])
        self.assertIn({"page": "documentation/beta.md"}, data["skip"])
        self.assertNotIn("beta's comment", out)
        self.assertIn("added to skip", changes[0])

    def test_functional_with_resources_becomes_schema(self):
        _, data, _ = self.apply([finding("functional-empty", "gamma", {"page": "documentation/gamma.md"},
                                         fix="schema")])
        self.assertEqual(data["scenarios"]["gamma"]["type"], "schema")

    def test_skip_to_schema_takes_the_entry_comment_with_it(self):
        out, data, _ = self.apply([finding("skip-checkable", ref={"page": "documentation/b.md"})])
        self.assertNotIn({"page": "documentation/b.md"}, data["skip"])
        self.assertNotIn("Needs a cloud account", out)
        self.assertIn("# Pages with deliberately no test.", out)
        self.assertEqual(data["scenarios"]["b-schema"],
                         {"type": "schema", "page": "documentation/b.md", "path": "all"})
        self.assertEqual(list(data["scenarios"]), ["alpha", "b-schema", "beta", "gamma"])

    def test_skip_source_entry_and_index_page_name(self):
        _, data, _ = self.apply([finding("skip-checkable", ref={"source": "agw-docs/pages/c.md"}),
                                 finding("skip-checkable", ref={"page": "documentation/a/_index.md"})])
        self.assertIn("c-schema", data["scenarios"])
        self.assertIn("a-schema", data["scenarios"])
        self.assertEqual(data["skip"], [{"page": "documentation/b.md"}])

    def test_skip_checkable_records_its_new_scenario(self):
        f = finding("skip-checkable", ref={"page": "documentation/b.md"})
        self.apply([f])
        self.assertEqual(f.applied_as, "b-schema")

    def test_warnings_change_nothing(self):
        out, _, changes = self.apply([finding("could-be-functional", "beta", {"page": "documentation/beta.md"},
                                              severity="warning")])
        self.assertEqual((out, changes), (MANIFEST, []))


class Baseline(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "b.yaml")
            fs = [finding("skip-checkable", ref={"page": "x.md"}),
                  finding("could-be-functional", "s", {"page": "y.md"}, severity="warning")]
            tc.write_baseline(path, fs)
            self.assertEqual(tc.load_baseline(path), {tc.finding_key(fs[0])})


if __name__ == "__main__":
    unittest.main()
