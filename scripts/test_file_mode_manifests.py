#!/usr/bin/env python3
"""Tests for `doc_test_run.py --file`, which must see manifest scenarios.

Full discovery reads front matter AND manifests, but `--file` used to read front
matter only. Once an area moved to a manifest and its front matter was stripped,
a PR's changed-file selection logged "No test metadata" and dropped the area's
tests, and the per-test run (`--file X --test Y`) failed with "No test named"
for a test that full discovery had just listed.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

RUNNER = pathlib.Path(__file__).resolve().parent / "doc_test_run.py"
PAGE_REL = "content/docs/standalone/latest/documentation/buffer.md"

MANIFEST = """version: 1
mode: standalone
scenarios:
  buffer:
    page: documentation/buffer.md
    path: buffer
"""
FRONT_MATTER_TWIN = """---
title: Buffer
test:
  buffer:
  - file: content/docs/standalone/latest/documentation/buffer.md
    path: buffer
---

```sh {paths="buffer"}
echo hi
```
"""


def build(page_text: str):
    repo = pathlib.Path(tempfile.mkdtemp())
    docs_tests = pathlib.Path(tempfile.mkdtemp())
    page = repo / PAGE_REL
    page.parent.mkdir(parents=True)
    page.write_text(page_text)
    manifest = docs_tests / "products/p/standalone/tests.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(MANIFEST)
    return repo, docs_tests


def list_tests(repo, docs_tests, *extra):
    out = subprocess.run(
        [sys.executable, str(RUNNER), "--repo-root", str(repo), "--docs-tests-root", str(docs_tests),
         "--no-verbose", "--list-tests", *extra],
        capture_output=True, text=True, check=True,
    ).stdout
    return [(t["file"], t["test"]) for t in json.loads(out)]


class FileModeManifestTests(unittest.TestCase):
    def test_a_manifest_only_page_is_found_by_file(self):
        repo, docs_tests = build('---\ntitle: Buffer\n---\n\n```sh {paths="buffer"}\necho hi\n```\n')
        self.assertEqual(list_tests(repo, docs_tests, "--file", PAGE_REL), [(PAGE_REL, "buffer")])

    def test_file_and_test_select_a_manifest_scenario(self):
        repo, docs_tests = build('---\ntitle: Buffer\n---\n')
        self.assertEqual(
            list_tests(repo, docs_tests, "--file", PAGE_REL, "--test", "buffer"), [(PAGE_REL, "buffer")]
        )

    def test_the_manifest_wins_over_a_front_matter_twin_without_duplicating(self):
        repo, docs_tests = build(FRONT_MATTER_TWIN)
        self.assertEqual(list_tests(repo, docs_tests, "--file", PAGE_REL), [(PAGE_REL, "buffer")])

    def test_file_mode_matches_full_discovery(self):
        repo, docs_tests = build(FRONT_MATTER_TWIN)
        self.assertEqual(list_tests(repo, docs_tests, "--file", PAGE_REL), list_tests(repo, docs_tests))


if __name__ == "__main__":
    unittest.main()
