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


class ShellTests(unittest.TestCase):
    def test_pwsh_scenario_gets_a_ps1_script(self):
        repo, docs_tests = _repo_with_manifest(
            "  a:\n    runner: windows\n    shell: pwsh\n    page: documentation/a.md\n    path: a\n"
            "  b:\n    page: documentation/b.md\n    path: b\n")
        cases, _, _ = doc_test_run.build_test_cases_from_manifests(repo, docs_tests, repo / "out")
        by_name = {c.name: c for c in cases}
        self.assertEqual((by_name["a"].shell, by_name["a"].script_path.suffix), ("pwsh", ".ps1"))
        self.assertEqual((by_name["b"].shell, by_name["b"].script_path.suffix), ("bash", ".sh"))
        self.assertEqual(doc_test_run.script_command(by_name["a"])[:2], ["pwsh", "-NoProfile"])

    def test_unknown_shell_is_an_error(self):
        repo, docs_tests = _repo_with_manifest(
            "  a:\n    shell: fish\n    page: documentation/a.md\n    path: a\n")
        with self.assertRaisesRegex(RuntimeError, "shell: fish"):
            doc_test_run.build_test_cases_from_manifests(repo, docs_tests, repo / "out")

    def test_pwsh_script_selects_powershell_fences_and_uses_pwsh_markers(self):
        repo = pathlib.Path(tempfile.mkdtemp()).resolve()
        page = repo / "content/docs/kubernetes/main/documentation/a.md"
        page.parent.mkdir(parents=True)
        page.write_text(
            "---\ntitle: x\n---\n\n"
            "```powershell {paths=\"a\"}\n$VERSION = \"v1\"\n```\n\n"
            "{{< doc-test paths=\"a\" >}}\nif (-not $VERSION) { throw \"no version\" }\n{{< /doc-test >}}\n\n"
            "```bash {paths=\"a\"}\necho bash-only\n```\n")
        definition = {
            "name": "a", "main_file": "content/docs/kubernetes/main/documentation/a.md", "shell": "pwsh",
            "sources": [{"file": "content/docs/kubernetes/main/documentation/a.md", "paths": ["a"]}],
            "output": {"script": "out/a.ps1", "manifest": "out/a.json"},
        }
        script_path = repo / "out/a.ps1"
        doc_test_run.generate_script_and_manifest(repo, definition, script_path, repo / "out/a.json")
        script = script_path.read_text()
        self.assertTrue(script.startswith("$ErrorActionPreference = 'Stop'"))
        self.assertIn('$VERSION = "v1"', script)
        self.assertIn("throw", script)
        self.assertNotIn("echo bash-only", script)
        self.assertIn("Set-Content -Path $env:DOC_TEST_STEP_FILE", script)


if __name__ == "__main__":
    unittest.main()
