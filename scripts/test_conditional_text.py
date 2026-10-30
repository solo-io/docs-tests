#!/usr/bin/env python3
"""conditional-text gates match the product, plus any --condition names.

A site's tests run with the mode as the product (`kubernetes`), while its
snippets gate on the product name (include-if="agentgateway"). Without a way
to name the product too, every such snippet rendered empty in a test script:
`--namespace  \\` and `apiVersion:` with nothing after them.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from doc_test_extract import Extractor  # noqa: E402

TEXT = (
    '{{< conditional-text include-if="agentgateway" >}}agw{{< /conditional-text >}}'
    '{{< conditional-text include-if="gm" >}}gm{{< /conditional-text >}}'
    '{{< conditional-text exclude-if="standalone" >}}k8s{{< /conditional-text >}}'
)


def extractor(conditions=None, product="kubernetes"):
    context = {"product": product, "version": "latest"}
    if conditions is not None:
        context["conditions"] = conditions
    return Extractor(repo_root=pathlib.Path(tempfile.mkdtemp()),
                     definition={"main_file": "x.md", "sources": [], "context": context})


class ConditionalTextTests(unittest.TestCase):
    def test_product_only(self):
        self.assertEqual(extractor()._resolve_conditional_text_blocks(TEXT), "k8s")

    def test_condition_adds_a_match(self):
        self.assertEqual(extractor(["agentgateway"])._resolve_conditional_text_blocks(TEXT), "agwk8s")

    def test_condition_also_excludes(self):
        self.assertEqual(extractor(["standalone"])._resolve_conditional_text_blocks(TEXT), "")

    def test_no_product_and_no_condition_drops_include_blocks(self):
        self.assertEqual(extractor(product=None)._resolve_conditional_text_blocks(TEXT), "k8s")


if __name__ == "__main__":
    unittest.main()
