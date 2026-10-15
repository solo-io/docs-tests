#!/usr/bin/env python3
"""Sites that are not agentgateway resolve their version tree and page links.

kgateway lays its docs out as content/docs/envoy/<version>/ and keeps shared
page bodies in assets/kgw-docs/pages/. Both used to be agentgateway-only:
the version came from a `kubernetes|standalone` pattern, so every kgateway
script fell back to the --version default (2.2.x) and installed 2.2.9 for
latest and main alike; and link targets were looked up only under
assets/agw-docs/pages/.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import doc_test_extract  # noqa: E402
import doc_test_run  # noqa: E402


class InferVersionTest(unittest.TestCase):
    def infer(self, *files):
        return doc_test_run.infer_version_from_sources([{"file": f} for f in files], "2.2.x")

    def test_agentgateway_modes_unchanged(self):
        self.assertEqual(self.infer("content/docs/kubernetes/latest/install/helm.md"), "latest")
        self.assertEqual(self.infer("content/docs/standalone/main/configuration/backends.md"), "main")
        # The hub layout carries the product before the mode.
        self.assertEqual(
            self.infer("content/en/agentgateway/kubernetes/latest/documentation/x.md"), "latest"
        )

    def test_other_modes_use_the_docs_layout(self):
        self.assertEqual(self.infer("content/docs/envoy/latest/quickstart.md"), "latest")
        self.assertEqual(self.infer("content/docs/envoy/main/install/sample-app.md"), "main")

    def test_named_mode_wins_over_layout(self):
        # A chain that mixes layouts keeps the old answer when one source matches it.
        self.assertEqual(
            self.infer("content/docs/envoy/main/a.md", "content/docs/kubernetes/latest/b.md"), "latest"
        )

    def test_no_version_falls_back(self):
        self.assertEqual(self.infer("assets/kgw-docs/pages/a.md"), "2.2.x")


class PageRootsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        page = self.root / "assets/kgw-docs/pages/traffic-management/direct-response.md"
        page.parent.mkdir(parents=True)
        page.write_text("body\n")
        self.page = page
        main = self.root / "content/docs/envoy/latest/traffic-management/direct-response.md"
        main.parent.mkdir(parents=True)
        main.write_text('---\ntitle: x\n---\n{{< reuse "kgw-docs/pages/traffic-management/direct-response.md" >}}\n')
        self.extractor = doc_test_extract.Extractor(
            self.root,
            {
                "name": "t",
                "main_file": "content/docs/envoy/latest/traffic-management/direct-response.md",
                "context": {"version": "latest", "product": "kubernetes"},
                "sources": [],
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_route_for_a_kgw_docs_page(self):
        self.assertEqual(
            self.extractor._route_for_file(self.page), "/traffic-management/direct-response/"
        )

    def test_link_target_finds_a_kgw_docs_page(self):
        self.assertEqual(
            self.extractor._find_by_route_like_target("/traffic-management/direct-response/"),
            self.page.resolve(),
        )


if __name__ == "__main__":
    unittest.main()
