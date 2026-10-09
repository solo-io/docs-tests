#!/usr/bin/env python3
"""A scenario whose test markup did not attach is skipped with a warning, not run short.

Without this, a tag that stayed needs update would leave its block out of the
generated script, and the scenario would pass having skipped a step.
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import annotations as A  # noqa: E402
import doc_test_run  # noqa: E402

PAGE = '''---
title: Feature
test:
  alpha:
  - path: alpha
  beta:
  - path: beta
---

Apply alpha.

```sh {paths="alpha"}
kubectl apply -f alpha.yaml
kubectl wait --for=condition=Ready pod/alpha
kubectl get pod alpha -o yaml
```

Apply beta.

```sh {paths="beta"}
kubectl apply -f beta.yaml
```
'''

OTHER = '''---
title: Other
---

```sh {paths="alpha"}
echo "an unrelated alpha on a page the scenarios never read"
```
'''

REL = "content/docs/kubernetes/main/feature.md"


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args)


class SkipTests(unittest.TestCase):
    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp())
        self.adir = pathlib.Path(tempfile.mkdtemp())
        for rel, text in ((REL, PAGE), ("content/docs/other.md", OTHER)):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        quiet(A.main, ["export", "--repo", str(self.repo), "--annotations", str(self.adir)])
        quiet(A.main, ["strip", "--repo", str(self.repo)])
        self.summary = self.repo / "summary.md"

    def attach_and_run(self):
        report = self.repo / "attach.json"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.summary)}):
            quiet(A.main, ["attach", "--repo", str(self.repo), "--annotations", str(self.adir),
                           "--report", str(report), "--summary"])
        argv = ["doc_test_run.py", "--repo-root", str(self.repo), "--generate-only",
                "--annotation-report", str(report), "--report-file", "out/report.yaml"]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.summary)}):
            code = quiet(doc_test_run.main)
        return code, yaml.safe_load((self.repo / "out/report.yaml").read_text())

    def rewrite(self, old, new):
        p = self.repo / REL
        p.write_text(p.read_text().replace(old, new))

    def test_report_directory_is_created(self):
        """CI starts from a fresh checkout: the report's directory does not exist yet."""
        report = self.repo / "out/annotations/attach-report.json"
        self.assertFalse(report.parent.exists())
        code = quiet(A.main, ["attach", "--repo", str(self.repo), "--annotations", str(self.adir), "--report", str(report)])
        self.assertEqual(code, 0)
        self.assertTrue(report.is_file())

    def test_everything_attaches_nothing_skipped(self):
        code, report = self.attach_and_run()
        self.assertEqual(code, 0)
        self.assertNotIn("skipped_needs_update", report)
        self.assertFalse(self.summary.exists())

    def test_rewritten_block_skips_only_its_scenario(self):
        self.rewrite("kubectl apply -f alpha.yaml\nkubectl wait --for=condition=Ready pod/alpha\nkubectl get pod alpha -o yaml",
                     "helm install alpha ./chart")
        code, report = self.attach_and_run()
        self.assertEqual(code, 0)   # a warning, not a failure
        self.assertEqual(list(report["skipped_needs_update"]), [f"{REL}::alpha"])
        self.assertEqual(report["skipped_needs_update"][f"{REL}::alpha"]["markup"][0]["paths"], "alpha")
        summary = self.summary.read_text()
        self.assertIn(f"`{REL}::alpha`", summary)
        self.assertIn("needs-update", summary)
        # beta still generated
        self.assertTrue(any(p.name.endswith(".sh") and "beta" in p.name for p in (self.repo / "out").rglob("*.sh")))

    def test_unattached_markup_on_a_page_it_never_reads_is_ignored(self):
        other = self.repo / "content/docs/other.md"
        other.write_text(other.read_text().replace("an unrelated alpha", "something else entirely now"))
        code, report = self.attach_and_run()
        self.assertNotIn("skipped_needs_update", report)
        self.assertIn("content/docs/other.md", self.summary.read_text())   # still listed

    def test_close_match_runs_and_is_listed(self):
        self.rewrite("kubectl get pod alpha -o yaml", "kubectl get pod alpha -o yaml\nkubectl logs alpha")
        code, report = self.attach_and_run()
        self.assertNotIn("skipped_needs_update", report)
        self.assertIn("| close |", self.summary.read_text())


SCHEMA_PAGE = '''---
title: Policy
test:
  policy:
    type: schema
    steps:
    - path: policy
---

```yaml {paths="policy"}
kubectl apply -f- <<EOF
apiVersion: example.dev/v1
kind: Policy
metadata:
  name: p
spec:
  timeout: 10s
EOF
```
'''

CRD = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
spec:
  names: {kind: Policy}
  versions:
  - name: v1
    served: true
    schema:
      openAPIV3Schema:
        type: object
        properties:
          apiVersion: {type: string}
          kind: {type: string}
          metadata: {type: object}
          spec:
            type: object
            properties:
              timeout: {type: string}
"""


# Consumer repos run this suite in jobs that install only PyYAML; the schema
# check needs jsonschema and exits early without it. docs-tests' own CI installs
# it, so these always run there.
@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
class SchemaSkipTests(unittest.TestCase):
    """The schema check, too, skips a scenario whose markup did not attach."""

    REL = "content/docs/kubernetes/main/policy.md"

    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp())
        self.adir = pathlib.Path(tempfile.mkdtemp())
        self.crds = pathlib.Path(tempfile.mkdtemp())
        (self.crds / "policy.yaml").write_text(CRD)
        (self.repo / self.REL).parent.mkdir(parents=True)
        (self.repo / self.REL).write_text(SCHEMA_PAGE)
        quiet(A.main, ["export", "--repo", str(self.repo), "--annotations", str(self.adir)])
        quiet(A.main, ["strip", "--repo", str(self.repo)])
        page = self.repo / self.REL
        page.write_text(page.read_text().replace(
            "apiVersion: example.dev/v1\nkind: Policy\nmetadata:\n  name: p\nspec:\n  timeout: 10s",
            "helm upgrade --install policy ./chart"))

    def run_check(self, with_report):
        import doc_test_schema_check
        report = self.repo / "attach.json"
        quiet(A.main, ["attach", "--repo", str(self.repo), "--annotations", str(self.adir), "--report", str(report)])
        argv = ["doc_test_schema_check.py", "--repo-root", str(self.repo), "--crd-dir", str(self.crds),
                "--report-file", str(self.repo / "out/schema.yaml")]
        if with_report:
            argv += ["--annotation-report", str(report)]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            code = quiet(doc_test_schema_check.main)
        return code, yaml.safe_load((self.repo / "out/schema.yaml").read_text())

    def test_without_the_report_it_fails_for_the_wrong_reason(self):
        code, report = self.run_check(with_report=False)
        self.assertEqual(code, 1)
        self.assertIn("found no recognizable custom resource", report["tests"][f"{self.REL}::policy"]["error"])

    def test_with_the_report_it_is_skipped(self):
        code, report = self.run_check(with_report=True)
        self.assertEqual(code, 0)
        self.assertEqual(report["tests"], {})
        self.assertIn(f"{self.REL}::policy", report["skipped_needs_update"])


class ManifestSkipTests(unittest.TestCase):
    """`skip:` in tests.yaml replaces front matter `test: skip`."""

    def test_skipped_pages_count_as_covered(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        docs_tests = pathlib.Path(tempfile.mkdtemp())
        for version in ("latest", "main"):
            page = repo / f"content/docs/kubernetes/{version}/documentation/section/_index.md"
            page.parent.mkdir(parents=True)
            page.write_text("---\ntitle: Section\n---\n")
        manifest = docs_tests / "products/p/kubernetes/tests.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("version: 1\nmode: kubernetes\nskip:\n- page: documentation/section/_index.md\n"
                            "- page: documentation/gone.md\nscenarios: {}\n")
        _, _, docs = doc_test_run.build_test_cases_from_manifests(repo, docs_tests, repo / "out")
        self.assertEqual(docs, [f"content/docs/kubernetes/{v}/documentation/section/_index.md" for v in ("latest", "main")])

    def test_dir_skips_every_page_under_it(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        docs_tests = pathlib.Path(tempfile.mkdtemp())
        for rel in ("reference/cli/a.md", "reference/cli/sub/b.md", "reference/api.md"):
            page = repo / f"content/docs/kubernetes/latest/{rel}"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("---\ntitle: x\n---\n")
        manifest = docs_tests / "products/p/kubernetes/tests.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("version: 1\nmode: kubernetes\nskip:\n- dir: reference/cli/\n- dir: reference/gone/\nscenarios: {}\n")
        _, _, docs = doc_test_run.build_test_cases_from_manifests(repo, docs_tests, repo / "out")
        self.assertEqual(docs, [f"content/docs/kubernetes/latest/reference/cli/{p}" for p in ("a.md", "sub/b.md")])


class ManifestNameTests(unittest.TestCase):
    """Two pages in one mode can declare the same scenario name."""

    def test_name_field_and_missing_page(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        docs_tests = pathlib.Path(tempfile.mkdtemp())
        for version in ("latest", "main"):
            for rel in ("documentation/a.md", "documentation/b.md"):
                page = repo / f"content/docs/kubernetes/{version}/{rel}"
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text("---\ntitle: x\n---\n")
        manifest = docs_tests / "products/p/kubernetes/tests.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "version: 1\nmode: kubernetes\nscenarios:\n"
            "  headers:\n    page: documentation/a.md\n    path: headers\n"
            "  b/headers:\n    name: headers\n    page: documentation/b.md\n    path: headers\n"
            # a page renamed to .txt, which the runner never discovers
            "  gone:\n    page: documentation/gone.md\n    path: gone\n")
        cases, claimed, _ = doc_test_run.build_test_cases_from_manifests(repo, docs_tests, repo / "out")
        self.assertEqual(sorted((c.document.name, c.name) for c in cases),
                         [("a.md", "headers"), ("a.md", "headers"), ("b.md", "headers"), ("b.md", "headers")])
        self.assertIn(("content/docs/kubernetes/main/documentation/b.md", "headers"), claimed)


class DocsTestsRootTests(unittest.TestCase):
    """Without a docs-tests root the runner read no manifests, and every
    manifest-only scenario vanished from a PR run's listing without an error."""

    def test_defaults_to_this_checkout(self):
        import doc_test_extract
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCS_TESTS_ROOT", None)
            root = doc_test_extract.resolve_docs_tests_root(None)
        self.assertEqual(root, pathlib.Path(__file__).resolve().parent.parent)
        self.assertTrue(any(root.glob(doc_test_run.MANIFEST_RELATIVE_GLOB)))

    def test_explicit_and_env_win(self):
        import doc_test_extract
        with mock.patch.dict(os.environ, {"DOCS_TESTS_ROOT": "/env/root"}):
            self.assertEqual(doc_test_extract.resolve_docs_tests_root(None), pathlib.Path("/env/root"))
            self.assertEqual(doc_test_extract.resolve_docs_tests_root("/flag/root"), pathlib.Path("/flag/root"))


class ReportTests(unittest.TestCase):
    """A skip survives merging shards and shows as a warning, not a pass."""

    def test_merge_keeps_skips_and_summary_warns(self):
        import merge_test_results
        import report_summary
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "1").mkdir()
        (d / "2").mkdir()
        (d / "1/test-results.yaml").write_text(yaml.safe_dump({
            "tested_documents": ["a.md"], "tests": {"a.md::x": {"status": "passed", "checks": []}}}))
        (d / "2/test-results.yaml").write_text(yaml.safe_dump({
            "tested_documents": ["b.md"], "tests": {}, "skipped_needs_update": {"b.md::y": {"markup": []}}}))
        quiet(merge_test_results.merge, d, d / "merged.yaml")
        merged = yaml.safe_load((d / "merged.yaml").read_text())
        self.assertIn("b.md::y", merged["skipped_needs_update"])
        header = report_summary.generate_summary(merged).split("\n")[0]
        self.assertIn("1 passed | 1 need update | 2 total", header)
        main, _ = report_summary.generate_slack_blocks(merged)
        self.assertIn("need update", main["text"])


if __name__ == "__main__":
    unittest.main()
