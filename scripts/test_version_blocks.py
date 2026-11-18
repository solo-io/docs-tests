#!/usr/bin/env python3
"""version gates nest the way Hugo nests them.

An inline {{% version %}} link inside a gated section used to close the outer
gate early: the regex paired the outer opening tag with the inner closing tag,
so the rest of an old-version section (its Helm values and `helm upgrade`)
leaked into the latest test. Found on the enterprise Entra OBO page.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from doc_test_extract import Extractor  # noqa: E402


def extractor(version="latest"):
    return Extractor(repo_root=pathlib.Path(tempfile.mkdtemp()),
                     definition={"main_file": "x.md", "sources": [],
                                 "context": {"product": "kubernetes", "version": version}})


NESTED = (
    '{{< version include-if="2.3.x" >}}\n'
    'old intro {{% version include-if="2.3.x" %}}a{{% /version %}}{{% version exclude-if="2.3.x" %}}b{{% /version %}}\n'
    'helm upgrade old\n'
    '{{< /version >}}\n'
    '{{< version exclude-if="2.3.x" >}}\n'
    'new {{% version exclude-if="2.3.x" %}}c{{% /version %}}\n'
    '{{< /version >}}\n'
)


class VersionBlockTests(unittest.TestCase):
    def test_nested_gate_keeps_outer_exclusion(self):
        out = extractor("latest")._strip_version_blocks(NESTED)
        self.assertNotIn("helm upgrade old", out)
        self.assertNotIn("old intro", out)
        self.assertIn("new c", out)

    def test_nested_gate_in_included_section(self):
        out = extractor("2.3.x")._strip_version_blocks(NESTED)
        self.assertIn("old intro a\nhelm upgrade old", out)
        self.assertNotIn("new", out)

    def test_flat_gates_unchanged(self):
        text = '{{< version include-if="latest" >}}x{{< /version >}}{{< version include-if="main" >}}y{{< /version >}}'
        self.assertEqual(extractor("latest")._strip_version_blocks(text), "x")

    def test_no_version_keeps_every_block(self):
        self.assertIn("helm upgrade old", extractor(None)._strip_version_blocks(NESTED))


if __name__ == "__main__":
    unittest.main()
