#!/usr/bin/env python3
"""The mode comes from the page path, in either content layout.

A site with its own content_root (content/en/agentgateway/<mode>/...) has no
`docs` segment. Reading only content/docs/<mode>/ made every such page
`kubernetes`, so the schema check never validated a standalone config file.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from doc_test_run import _mode_segment  # noqa: E402


class ModeSegmentTests(unittest.TestCase):
    def test_docs_layout(self):
        self.assertEqual(_mode_segment("content/docs/standalone/main/x.md"), "standalone")

    def test_content_root_layout(self):
        self.assertEqual(_mode_segment("content/en/agentgateway/standalone/latest/x.md"), "standalone")
        self.assertEqual(_mode_segment("content/en/agentgateway/kubernetes/latest/x.md"), "kubernetes")

    def test_no_mode(self):
        self.assertEqual(_mode_segment("content/en/other/latest/x.md"), "")


if __name__ == "__main__":
    unittest.main()
