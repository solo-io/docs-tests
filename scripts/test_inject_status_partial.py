#!/usr/bin/env python3
"""A page with a skipped scenario loses the Verified badge.

A scenario whose test annotations did not attach is skipped and recorded under
`skipped_needs_update`, not under `tests`. Reading `tests` alone, a page whose
other scenarios passed would still be marked "passed" and show the badge, with
one of its examples untested.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import doc_test_inject_status as S  # noqa: E402

DOC = "content/docs/page.md"


class ComputeStatus(unittest.TestCase):
    def test_all_passed(self):
        tests = {f"{DOC}::a": {"status": "passed"}}
        self.assertEqual(S.compute_document_status(DOC, tests, {}), "passed")

    def test_passed_with_skipped_is_partial(self):
        tests = {f"{DOC}::a": {"status": "passed"}}
        skipped = {f"{DOC}::b": {"markup": []}}
        self.assertEqual(S.compute_document_status(DOC, tests, skipped), "partial")

    def test_only_skipped_is_partial(self):
        skipped = {f"{DOC}::b": {"markup": []}}
        self.assertEqual(S.compute_document_status(DOC, {}, skipped), "partial")

    def test_failed_wins_over_skipped(self):
        tests = {f"{DOC}::a": {"status": "failed"}}
        skipped = {f"{DOC}::b": {"markup": []}}
        self.assertEqual(S.compute_document_status(DOC, tests, skipped), "failed")

    def test_other_page_skip_does_not_count(self):
        tests = {f"{DOC}::a": {"status": "passed"}}
        skipped = {"content/docs/page-two.md::b": {"markup": []}}
        self.assertEqual(S.compute_document_status(DOC, tests, skipped), "passed")

    def test_no_results(self):
        self.assertIsNone(S.compute_document_status(DOC, {}, {}))


class ProcessDocuments(unittest.TestCase):
    def test_partial_written_to_front_matter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            page = root / DOC
            page.parent.mkdir(parents=True)
            page.write_text("---\ntitle: Page\n---\nBody\n", encoding="utf-8")
            results = {
                "tested_documents": [DOC],
                "tests": {f"{DOC}::a": {"status": "passed"}},
                "skipped_needs_update": {f"{DOC}::b": {"markup": []}},
            }
            status = S.process_documents(root, results, verbose=False)
            self.assertEqual(status, {DOC: "partial"})
            self.assertIn("test_status: partial", page.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
