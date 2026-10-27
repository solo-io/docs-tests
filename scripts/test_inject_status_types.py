#!/usr/bin/env python3
"""`test_types` lists the kinds of test that ran on a passed page.

The Verified badge's tooltip reads it, so it must name only kinds that
actually ran (from the results, not from tests.yaml), come out in a stable
order, and disappear when the page stops passing.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import doc_test_inject_status as S  # noqa: E402

DOC = "content/docs/page.md"
PAGE = "---\ntitle: Page\n---\nBody\n"


def _run(tests, page=PAGE):
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / DOC).parent.mkdir(parents=True, exist_ok=True)
        (root / DOC).write_text(page, encoding="utf-8")
        S.process_documents(root, {"tested_documents": [DOC], "tests": tests}, verbose=False)
        _, fm, _ = S.parse_front_matter((root / DOC).read_text(encoding="utf-8"))
        return fm


class Types(unittest.TestCase):
    def test_passed_page_lists_kinds_in_badge_order(self):
        fm = _run({
            f"{DOC}::a::functional": {"status": "passed", "type": "functional"},
            f"{DOC}::a::schema": {"status": "passed", "type": "schema"},
            f"{DOC}::b": {"status": "passed", "type": "functional"},
        })
        self.assertEqual(fm["test_status"], "passed")
        self.assertEqual(fm["test_types"], ["schema", "functional"])

    def test_missing_type_counts_as_functional(self):
        fm = _run({f"{DOC}::a": {"status": "passed"}})
        self.assertEqual(fm["test_types"], ["functional"])

    def test_unknown_kind_sorts_after_known(self):
        fm = _run({
            f"{DOC}::a": {"status": "passed", "type": "zeta"},
            f"{DOC}::b": {"status": "passed", "type": "live"},
        })
        self.assertEqual(fm["test_types"], ["live", "zeta"])

    def test_failed_page_drops_a_stale_list(self):
        stale = "---\ntitle: Page\ntest_status: passed\ntest_types:\n- functional\n---\nBody\n"
        fm = _run({f"{DOC}::a": {"status": "failed", "type": "functional"}}, page=stale)
        self.assertEqual(fm["test_status"], "failed")
        self.assertNotIn("test_types", fm)


if __name__ == "__main__":
    unittest.main()
