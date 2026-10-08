#!/usr/bin/env python3
"""Tests for a manifest step's `tag_snippet:`.

Some consuming sites keep test markup out of their content entirely, so their
snippets carry no paths= tags, and untagged blocks are never selected. The case
that prompted this is an enterprise install snippet reached through a rebased
page: without tags the install step extracted nothing, and every test ran
against a cluster with no control plane. tag_snippet tags the snippet in memory
instead, skips placeholder blocks, and runs a verify file after the last block.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from doc_test_extract import Extractor  # noqa: E402

REL = "content/en/p/kubernetes/latest/documentation/install.md"
SNIPPET = "agw-docs/snippets/get-started.md"
VERIFY = "products/p/kubernetes/install/verify.sh"

CONSUMER_SNIPPET = (
    "1. Set the key.\n"
    "   ```sh\n   export LICENSE_KEY=<your-key>\n   ```\n\n"
    "2. Install CRDs.\n"
    "   ```sh\n   kubectl apply -f crds.yaml\n   ```\n\n"
    "3. Install.\n"
    "   ```sh\n   helm install ent --set key=${LICENSE_KEY}\n   ```\n\n"
    "   ```console\n   NAME  READY\n   ```\n"
)


def build(snippet_text=CONSUMER_SNIPPET, reuse=True):
    consumer = pathlib.Path(tempfile.mkdtemp())
    upstream = pathlib.Path(tempfile.mkdtemp())
    docs_tests = pathlib.Path(tempfile.mkdtemp())
    cp = consumer / REL
    cp.parent.mkdir(parents=True)
    cp.write_text('---\ntitle: I\n---\n\n{{< rebase file="agw-docs/kubernetes/documentation/install.md" >}}\n')
    up = upstream / "content/docs/kubernetes/latest/documentation/install.md"
    up.parent.mkdir(parents=True)
    body = f'{{{{< reuse "{SNIPPET}" >}}}}\n' if reuse else "Nothing reused here.\n"
    up.write_text(
        "---\ntitle: I\n---\n\n" + body + "\n## Uninstall\n\n```sh\nhelm uninstall ent\n```\n"
    )
    cs = consumer / "assets" / SNIPPET
    cs.parent.mkdir(parents=True)
    cs.write_text(snippet_text)
    vf = docs_tests / VERIFY
    vf.parent.mkdir(parents=True)
    vf.write_text("YAMLTest -f - <<'EOF'\n- name: wait for ent\nEOF\n")
    return consumer, upstream, docs_tests


def select(consumer, upstream, docs_tests, spec):
    source = {"file": REL, "paths": ["standard"]}
    if spec is not None:
        source["tag_snippet"] = spec
    e = Extractor(
        repo_root=consumer,
        definition={"main_file": REL, "sources": [source],
                    "context": {"product": "kubernetes", "version": "latest"}},
        docs_tests_root=docs_tests,
        upstream_root=upstream, upstream_version_for={"latest": "latest"},
    )
    e.walk()
    return [b.content.strip() for b in e.select_blocks()]


SPEC = {"file": SNIPPET, "exclude": ["^export LICENSE_KEY=<"], "verify": VERIFY}


class TagSnippetTests(unittest.TestCase):
    def test_without_tag_snippet_an_untagged_snippet_selects_nothing(self):
        """Documents the old behaviour, so the regression is visible if it returns."""
        self.assertEqual(select(*build(), spec=None), [])

    def test_untagged_runnable_blocks_are_selected_in_order(self):
        blocks = select(*build(), spec=SPEC)
        self.assertEqual(blocks[0], "kubectl apply -f crds.yaml")
        self.assertEqual(blocks[1], "helm install ent --set key=${LICENSE_KEY}")

    def test_an_excluded_placeholder_block_is_not_run(self):
        self.assertFalse(any("<your-key>" in b for b in select(*build(), spec=SPEC)))

    def test_output_blocks_are_not_tagged(self):
        self.assertFalse(any("NAME  READY" in b for b in select(*build(), spec=SPEC)))

    def test_blocks_outside_the_snippet_are_not_tagged(self):
        """The page's own untagged Uninstall block must never run."""
        self.assertFalse(any("helm uninstall" in b for b in select(*build(), spec=SPEC)))

    def test_verify_runs_after_the_last_snippet_block(self):
        blocks = select(*build(), spec=SPEC)
        self.assertIn("wait for ent", blocks[-1])
        self.assertEqual(len(blocks), 3)

    def test_already_tagged_fences_are_left_alone(self):
        text = CONSUMER_SNIPPET.replace("   ```sh\n   kubectl apply", '   ```sh {paths="other"}\n   kubectl apply')
        self.assertFalse(any("crds.yaml" in b for b in select(*build(text), spec=SPEC)))

    def test_a_snippet_with_nothing_to_tag_fails_loudly(self):
        with self.assertRaises(ValueError):
            select(*build("Prose only, no blocks.\n"), spec=SPEC)

    def test_a_snippet_the_page_does_not_reuse_fails_loudly(self):
        with self.assertRaises(ValueError):
            select(*build(reuse=False), spec=SPEC)

    def test_a_missing_verify_file_fails_loudly(self):
        with self.assertRaises(FileNotFoundError):
            select(*build(), spec={**SPEC, "verify": "products/p/missing.sh"})


if __name__ == "__main__":
    unittest.main()
