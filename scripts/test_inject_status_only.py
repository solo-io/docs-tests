#!/usr/bin/env python3
"""`--only` limits badge injection to the named documents.

A site rolling the Verified badge out one page at a time passes the full
results file plus `--only <page>`. Every other tested document must be left
exactly as it was, passed or not, and a path with no results must be reported
rather than silently producing no badge.
"""
import contextlib
import io
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import doc_test_inject_status as S  # noqa: E402

PILOT = "content/docs/pilot.md"
OTHER = "content/docs/other.md"
PAGE = "---\ntitle: Page\n---\nBody\n"


def _site(tmp: str) -> pathlib.Path:
    root = pathlib.Path(tmp)
    for doc in (PILOT, OTHER):
        page = root / doc
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(PAGE, encoding="utf-8")
    return root


RESULTS = {
    "tested_documents": [PILOT, OTHER],
    "tests": {
        f"{PILOT}::a": {"status": "passed"},
        f"{OTHER}::b": {"status": "passed"},
    },
}


class Only(unittest.TestCase):
    def test_only_named_document_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _site(tmp)
            status = S.process_documents(root, RESULTS, verbose=False, only=[PILOT])
            self.assertEqual(status, {PILOT: "passed"})
            self.assertIn("test_status: passed", (root / PILOT).read_text(encoding="utf-8"))
            self.assertEqual((root / OTHER).read_text(encoding="utf-8"), PAGE)

    def test_without_only_every_document_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _site(tmp)
            status = S.process_documents(root, RESULTS, verbose=False)
            self.assertEqual(status, {PILOT: "passed", OTHER: "passed"})

    def test_unknown_path_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _site(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = S.process_documents(
                    root, RESULTS, verbose=False, only=["content/docs/typo.md"]
                )
            self.assertEqual(status, {})
            self.assertIn("--only content/docs/typo.md: no test results", out.getvalue())
            self.assertEqual((root / PILOT).read_text(encoding="utf-8"), PAGE)


if __name__ == "__main__":
    unittest.main()
