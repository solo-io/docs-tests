#!/usr/bin/env python3
"""A manifest can name its own version trees and where they live.

The enterprise kgateway hub lays content out as content/en/kgateway/2.4.x/,
with no mode folder and trees named by release. Before `versions:` and the
`{version}` placeholder in content_root, discovery looked only for
<content_root>/<mode>/latest and /main, found neither, and listed no tests.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import doc_test_run as R  # noqa: E402


class VersionRootTests(unittest.TestCase):
    def test_default_layout(self):
        self.assertEqual(R.version_root("content/docs", "envoy", "latest"), "content/docs/envoy/latest")

    def test_placeholder_layout(self):
        self.assertEqual(R.version_root("content/en/kgateway/{version}", "envoy", "2.4.x"),
                         "content/en/kgateway/2.4.x")

    def test_default_versions(self):
        self.assertEqual(R.tested_versions({}), R.TESTED_VERSIONS)

    def test_manifest_versions(self):
        self.assertEqual(R.tested_versions({"versions": ["2.4.x"]}), ("2.4.x",))

    def test_latest_resolves_through_the_site_config(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        (repo / "hugo-x.toml").write_text('[params]\ndefaultVersion = "2.4.x"\n')
        manifest = {"versions": ["latest"], "version_config": "hugo-x.toml"}
        self.assertEqual(R.tested_versions(manifest, repo), ("2.4.x",))
        # Without the repo (or the config), `latest` stays as written.
        self.assertEqual(R.tested_versions(manifest), ("latest",))

    def test_version_comes_from_a_release_named_tree(self):
        self.assertEqual(
            R.infer_version_from_sources([{"file": "content/en/kgateway/2.4.x/quickstart.md"}], "main"),
            "2.4.x")


class DiscoveryTests(unittest.TestCase):
    def test_scenarios_and_skips_resolve_in_a_release_named_tree(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        dt = pathlib.Path(tempfile.mkdtemp())
        (repo / "hugo-kgateway.toml").write_text('[params]\ndefaultVersion = "2.4.x"\n')
        for rel in ("content/en/kgateway/2.4.x/quickstart.md",
                    "content/en/kgateway/2.4.x/_index.md",
                    "content/en/kgateway/2.3.x/quickstart.md"):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text("---\ntitle: x\n---\n")
        m = dt / "products/kgateway-enterprise/envoy/tests.yaml"
        m.parent.mkdir(parents=True)
        m.write_text(
            "version: 1\nmode: envoy\ncontent_root: content/en/kgateway/{version}\n"
            "versions: [latest]\nversion_config: hugo-kgateway.toml\nprerequisites: {}\nskip:\n- page: _index.md\n"
            "scenarios:\n  quickstart:\n    page: quickstart.md\n    path: quickstart\n")
        cases, _claimed, docs = R.build_test_cases_from_manifests(repo, dt, pathlib.Path(tempfile.mkdtemp()))
        self.assertEqual([c.document.relative_to(repo).as_posix() for c in cases],
                         ["content/en/kgateway/2.4.x/quickstart.md"])
        self.assertIn("content/en/kgateway/2.4.x/_index.md", docs)
        self.assertNotIn("content/en/kgateway/2.3.x/quickstart.md", docs)


if __name__ == "__main__":
    unittest.main()
