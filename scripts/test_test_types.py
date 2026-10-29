#!/usr/bin/env python3
"""Only `schema` and `functional` are test types.

`live` and `credentialed` were removed: no scenario in any product used them,
and having them listed made it unclear how to test a page that calls an
external service. Such a page is `functional` against an in-cluster stand-in.
Any other `type:`, in a manifest or in front matter, is an error. An old `--types`
value only warns, so a workflow that still passes one keeps running.
"""
import pathlib
import subprocess
import sys
import tempfile
import unittest

RUNNER = pathlib.Path(__file__).resolve().parent / "doc_test_run.py"
PAGE_REL = "content/docs/standalone/latest/documentation/buffer.md"
PAGE = '---\ntitle: Buffer\n---\n\n```sh {paths="buffer"}\necho hi\n```\n'


def build(manifest_type: str = "", front_matter_type: str = ""):
    repo = pathlib.Path(tempfile.mkdtemp())
    docs_tests = pathlib.Path(tempfile.mkdtemp())
    page = repo / PAGE_REL
    page.parent.mkdir(parents=True)
    if front_matter_type:
        page.write_text(
            "---\ntitle: Buffer\ntest:\n  buffer:\n"
            f"    type: {front_matter_type}\n    steps:\n"
            f"    - file: {PAGE_REL}\n      path: buffer\n---\n\n"
            '```sh {paths="buffer"}\necho hi\n```\n'
        )
    else:
        page.write_text(PAGE)
        manifest = docs_tests / "products/p/standalone/tests.yaml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "version: 1\nmode: standalone\nscenarios:\n  buffer:\n"
            + (f"    type: {manifest_type}\n" if manifest_type else "")
            + "    page: documentation/buffer.md\n    path: buffer\n"
        )
    return repo, docs_tests


def list_tests(repo, docs_tests, *extra):
    return subprocess.run(
        [sys.executable, str(RUNNER), "--repo-root", str(repo), "--docs-tests-root", str(docs_tests),
         "--no-verbose", "--list-tests", *extra],
        capture_output=True, text=True,
    )


class TestTypes(unittest.TestCase):
    def test_schema_and_functional_are_accepted(self):
        repo, docs_tests = build("[schema, functional]")
        self.assertEqual(list_tests(repo, docs_tests).returncode, 0)

    def test_unknown_manifest_type_is_an_error(self):
        for removed in ("live", "credentialed"):
            with self.subTest(type=removed):
                result = list_tests(*build(removed))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"type: {removed}", result.stderr)

    def test_unknown_front_matter_type_is_an_error(self):
        result = list_tests(*build(front_matter_type="live"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("type: live", result.stderr)

    def test_unknown_types_filter_is_a_warning(self):
        """A workflow still passing `functional,live` keeps working until it is updated."""
        result = list_tests(*build(), "--types", "functional,live")
        self.assertEqual(result.returncode, 0)
        self.assertIn("live", result.stderr)
        self.assertIn("buffer", result.stdout)


if __name__ == "__main__":
    unittest.main()
