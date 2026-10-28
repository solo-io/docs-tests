#!/usr/bin/env python3
"""A scenario runs on Linux unless its manifest entry names another `runner:`."""
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import doc_test_run  # noqa: E402


def _repo_with_manifest(scenarios: str):
    repo = pathlib.Path(tempfile.mkdtemp())
    docs_tests = pathlib.Path(tempfile.mkdtemp())
    for rel in ("documentation/a.md", "documentation/b.md"):
        page = repo / f"content/docs/kubernetes/main/{rel}"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("---\ntitle: x\n---\n")
    manifest = docs_tests / "products/p/kubernetes/tests.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("version: 1\nmode: kubernetes\nscenarios:\n" + scenarios)
    return repo, docs_tests


class RunnerTests(unittest.TestCase):
    def test_linux_is_the_default_and_others_are_kept(self):
        repo, docs_tests = _repo_with_manifest(
            "  a:\n    page: documentation/a.md\n    path: a\n"
            "  b:\n    runner: macos\n    page: documentation/b.md\n    path: b\n")
        cases, _, _ = doc_test_run.build_test_cases_from_manifests(repo, docs_tests, repo / "out")
        self.assertEqual(sorted((c.name, c.runner) for c in cases), [("a", "linux"), ("b", "macos")])

    def test_unknown_runner_is_an_error(self):
        repo, docs_tests = _repo_with_manifest(
            "  a:\n    runner: freebsd\n    page: documentation/a.md\n    path: a\n")
        with self.assertRaisesRegex(RuntimeError, "runner: freebsd"):
            doc_test_run.build_test_cases_from_manifests(repo, docs_tests, repo / "out")

    def test_host_runner_names(self):
        for system, expected in (("Linux", "linux"), ("Darwin", "macos"), ("Windows", "windows")):
            with mock.patch.object(doc_test_run.platform, "system", return_value=system):
                self.assertEqual(doc_test_run.host_runner(), expected)


if __name__ == "__main__":
    unittest.main()
