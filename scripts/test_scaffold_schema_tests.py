#!/usr/bin/env python3
"""Tests for scaffold_schema_tests.py: which pages get a schema test, how the
entry is keyed and named, and where it lands in the manifest."""
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import scaffold_schema_tests as S  # noqa: E402

RESOURCE = '''```yaml
kubectl apply -f- <<EOF
apiVersion: v1
kind: Service
metadata:
  name: httpbin
EOF
```
'''

CONFIG = '''```yaml
cat <<'EOF' > config.yaml
binds:
- port: 3000
EOF
```
'''

MANIFEST = '''# A comment that must survive.
version: 1
mode: {mode}
prerequisites:
  httpbin:
    page: install.md
    path: all
scenarios:
  covered:
    {key}: {covered}
    path: all
  zebra:
    page: zebra.md
    path: all
'''

CONFIG_SCHEMA = {"type": "object", "properties": {"binds": {"type": "array"}, "routes": {"type": "array"}}}


def page(title: str, body: str, extra: str = "") -> str:
    return f"---\ntitle: {title}\n{extra}---\n\n{body}"


class ScaffoldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.repo, self.dt = root / "site", root / "docs-tests"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel: str, text: str, base: pathlib.Path = None):
        p = (base or self.repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def manifest(self, mode: str, by_source: bool = False) -> pathlib.Path:
        text = MANIFEST.format(mode=mode, key="source" if by_source else "page",
                               covered="pages/covered.md" if by_source else "covered.md")
        rel = f"products/demo/{mode}/tests.yaml"
        self.write(rel, text, self.dt)
        return self.dt / rel

    def run_scaffold(self, *extra) -> dict:
        report = pathlib.Path(self.tmp.name) / "report.json"
        args = ["--repo", str(self.repo), "--product", "demo", "--docs-tests-root", str(self.dt), "--date", "2026-11-04",
                "--report", str(report), *extra]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(S.main(args), 0)
        return json.loads(report.read_text())

    def test_adds_only_untested_pages_with_something_to_check(self):
        m = self.manifest("kubernetes")
        root = "content/docs/kubernetes/latest"
        self.write(f"{root}/covered.md", page("Covered", RESOURCE))
        self.write(f"{root}/new-feature.md", page("New", RESOURCE))
        self.write(f"{root}/concept.md", page("Concept", "Prose only.\n"))
        self.write(f"{root}/opted-out.md", page("Skip", RESOURCE, "test: skip\n"))

        report = self.run_scaffold()

        self.assertEqual([e["name"] for e in report["added"]], ["new-feature-schema"])
        self.assertEqual([s["page"] for s in report["skipped"]], [f"{root}/concept.md"])
        data = yaml.safe_load(m.read_text())
        self.assertEqual(data["scenarios"]["new-feature-schema"],
                         {"type": "schema", "page": "new-feature.md", "path": "all", "scaffolded": "2026-11-04"})
        text = m.read_text()
        self.assertTrue(text.startswith("# A comment that must survive."))
        # alphabetical: after `covered`, before `zebra`
        self.assertLess(text.index("covered:"), text.index("new-feature-schema:"))
        self.assertLess(text.index("new-feature-schema:"), text.index("zebra:"))

    def test_dry_run_leaves_the_manifest_alone(self):
        m = self.manifest("kubernetes")
        before = m.read_text()
        self.write("content/docs/kubernetes/latest/new-feature.md", page("New", RESOURCE))
        report = self.run_scaffold("--dry-run")
        self.assertEqual(len(report["added"]), 1)
        self.assertEqual(m.read_text(), before)

    def test_one_entry_covers_latest_and_main(self):
        self.manifest("kubernetes")
        for v in ("latest", "main"):
            self.write(f"content/docs/kubernetes/{v}/new-feature.md", page("New", RESOURCE))
        report = self.run_scaffold()
        self.assertEqual(len(report["added"]), 1)

    def test_wrapper_page_keyed_by_source_when_the_manifest_is(self):
        m = self.manifest("kubernetes", by_source=True)
        self.write("assets/pages/new-feature.md", RESOURCE)
        for v in ("latest", "main"):
            self.write(f"content/docs/kubernetes/{v}/guides/new-feature.md",
                       page("New", '{{< reuse "pages/new-feature.md" >}}\n'))
        self.run_scaffold()
        entry = yaml.safe_load(m.read_text())["scenarios"]["new-feature-schema"]
        self.assertEqual(entry, {"type": "schema", "source": "pages/new-feature.md", "path": "all",
                                 "scaffolded": "2026-11-04"})

    # Consumer repos run this suite in jobs that install only PyYAML; loading
    # the config schema needs jsonschema. docs-tests' own CI installs it.
    @unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
    def test_standalone_config_needs_the_config_schema(self):
        self.manifest("standalone")
        self.write("content/docs/standalone/latest/config-guide.md", page("Config", CONFIG))
        self.assertEqual(self.run_scaffold("--dry-run")["added"], [])
        schema = pathlib.Path(self.tmp.name) / "config.json"
        schema.write_text(json.dumps(CONFIG_SCHEMA))
        added = self.run_scaffold("--dry-run", "--config-schema", str(schema))["added"]
        self.assertEqual([e["name"] for e in added], ["config-guide-schema"])

    def test_name_collision_adds_the_parent_folder(self):
        m = self.manifest("kubernetes")
        root = "content/docs/kubernetes/latest"
        self.write(f"{root}/llm/overview.md", page("A", RESOURCE))
        self.write(f"{root}/mcp/overview.md", page("B", RESOURCE))
        self.write(f"{root}/mcp/setup/_index.md", page("C", RESOURCE))
        self.run_scaffold()
        names = set(yaml.safe_load(m.read_text())["scenarios"])
        self.assertTrue({"overview-schema", "mcp-overview-schema", "setup-schema"} <= names, names)

    def test_insert_after_the_last_scenario(self):
        text = "scenarios:\n  a:\n    page: a.md\n\nskip:\n- page: x.md\n"
        out = S.insert_scenarios(text, [("b-schema", "  b-schema:\n    type: schema\n")])
        self.assertEqual(out, "scenarios:\n  a:\n    page: a.md\n  b-schema:\n    type: schema\n\nskip:\n- page: x.md\n")


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
class SchemaCheckScaffoldPageTest(unittest.TestCase):
    """doc_test_schema_check.py --file: a PR checks a new page before the nightly
    scaffold adds its test, without changing the docs-tests checkout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.repo, self.dt, self.crds = root / "site", root / "docs-tests", root / "crds"
        self.crds.mkdir()
        self.manifest = self.dt / "products/demo/standalone/tests.yaml"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text("version: 1\nmode: standalone\nscenarios:\n  old-schema:\n    type: schema\n"
                                 "    page: old.md\n    path: all\n", encoding="utf-8")
        self.schema = root / "config.json"
        self.schema.write_text(json.dumps(dict(CONFIG_SCHEMA, required=["binds", "routes"])))
        self.page = "content/docs/standalone/latest/new-feature.md"
        p = self.repo / self.page
        p.parent.mkdir(parents=True)
        p.write_text(page("New", CONFIG), encoding="utf-8")   # no `routes`: fails the schema

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, *extra) -> "subprocess.CompletedProcess":
        import subprocess
        script = pathlib.Path(__file__).resolve().parent / "doc_test_schema_check.py"
        return subprocess.run(
            [sys.executable, str(script), "--repo-root", str(self.repo), "--crd-dir", str(self.crds),
             "--config-schema", str(self.schema), "--no-apiserver", "--docs-tests-root", str(self.dt),
             "--docs-glob", "content/docs/**/*.md", "--jobs", "1", *extra],
            capture_output=True, text=True)

    def test_new_page_is_untested_without_file(self):
        r = self.check()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No 'schema'-type scenarios found", r.stderr)

    def test_file_checks_the_new_page_and_leaves_docs_tests_alone(self):
        before = self.manifest.read_text()
        r = self.check("--file", self.page)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn(f"FAILED: {self.page}::new-feature-schema", r.stderr)
        self.assertIn("'routes' is a required property", r.stderr)
        self.assertEqual(self.manifest.read_text(), before)

    def test_no_scaffold_checks_only_existing_tests(self):
        r = self.check("--file", self.page, "--no-scaffold")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No 'schema'-type scenarios found", r.stderr)

if __name__ == "__main__":
    unittest.main()
