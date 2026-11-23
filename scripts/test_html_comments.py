#!/usr/bin/env python3
"""Code blocks inside an HTML comment are not on the page.

kgateway's access-logging page parks two unfinished sections in
`<!-- TODO ... -->`. Hugo publishes nothing inside the comment, but the
extractor read the YAML there, and the schema check failed it on 2.1.x for a
`podTemplate.volumes` field that no reader is shown.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from doc_test_extract import Extractor  # noqa: E402

strip = Extractor._strip_html_comments

PAGE = (
    "Intro.\n"
    "<!-- TODO\n"
    "\n"
    "```yaml\n"
    "kind: Hidden\n"
    "```\n"
    "-->\n"
    "```yaml\n"
    "kind: Shown\n"
    "```\n"
)


class HtmlCommentTests(unittest.TestCase):
    def test_block_comment_removed_and_lines_kept(self):
        out = strip(PAGE)
        self.assertNotIn("kind: Hidden", out)
        self.assertIn("kind: Shown", out)
        self.assertEqual(out.count("\n"), PAGE.count("\n"))
        self.assertEqual(out.split("\n").index("kind: Shown"), PAGE.split("\n").index("kind: Shown"))

    def test_comment_inside_fence_is_code(self):
        text = "```html\n<!-- keep me -->\n<p>x</p>\n```\n"
        self.assertEqual(strip(text), text)

    def test_doc_test_include_directive_kept(self):
        text = 'Before\n<!-- doc-test-include file="t.sh" paths="a" -->\nAfter\n'
        self.assertIn('<!-- doc-test-include file="t.sh" paths="a" -->', strip(text))

    def test_inline_comment_removed(self):
        self.assertEqual(strip("a <!-- note --> b\n"), "a  b\n")

    def test_text_after_close_on_same_line_kept(self):
        out = strip("<!--\nhidden\n--> shown\n")
        self.assertNotIn("hidden", out)
        self.assertIn("shown", out)

    def test_no_comment_is_unchanged(self):
        text = "```yaml\nkind: A\n```\n"
        self.assertIs(strip(text), text)


if __name__ == "__main__":
    unittest.main()
