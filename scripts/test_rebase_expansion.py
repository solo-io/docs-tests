#!/usr/bin/env python3
"""Tests for expanding {{< rebase >}} across repositories.

A consuming site whose pages are rebase shells has NOTHING extractable on disk:
the page body is one shortcode and the content lives in another repo, behind an
asset directory that is gitignored until the site builds. Before this, such a
page extracted to zero code blocks and any test over it would have passed having
run nothing -- the exact failure this framework exists to prevent.
"""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from doc_test_extract import Extractor  # noqa: E402

SHELL = '---\ntitle: Buffering\n---\n\n{{< rebase file="agw-docs/kubernetes/documentation/buffering.md" >}}\n'
UPSTREAM = (
    "---\ntitle: Buffering\n---\n\nIntro.\n\n"
    '```yaml {paths="buffering"}\nkind: Policy\n```\n\n'
    '```sh {paths="buffering"}\ncurl localhost\n```\n'
)


def build(consumer_rel: str):
    consumer = pathlib.Path(tempfile.mkdtemp())
    upstream = pathlib.Path(tempfile.mkdtemp())
    cp = consumer / consumer_rel
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(SHELL)
    up = upstream / "content/docs/kubernetes/latest/documentation/buffering.md"
    up.parent.mkdir(parents=True, exist_ok=True)
    up.write_text(UPSTREAM)
    return consumer, upstream, cp


class RebaseExpansionTests(unittest.TestCase):
    REL = "content/en/p/kubernetes/latest/documentation/buffering.md"
    VMAP = {"latest": "latest"}

    def extractor(self, consumer, upstream, **kw):
        return Extractor(
            repo_root=consumer,
            definition={"main_file": self.REL,
                        "sources": [{"file": self.REL, "path": "buffering"}],
                        "context": {"product": "kubernetes", "version": "latest"}},
            **kw,
        )

    def test_without_an_upstream_root_the_shell_yields_nothing(self):
        """Documents the old behaviour, so the regression is visible if it returns."""
        consumer, upstream, cp = build(self.REL)
        e = self.extractor(consumer, upstream)
        self.assertEqual(len(e.process_file(cp).code_blocks), 0)

    def test_with_an_upstream_root_the_upstream_blocks_appear(self):
        consumer, upstream, cp = build(self.REL)
        e = self.extractor(consumer, upstream, upstream_root=upstream, upstream_version_for=self.VMAP)
        blocks = e.process_file(cp).code_blocks
        self.assertEqual(len(blocks), 2)
        self.assertEqual({b.language for b in blocks}, {"yaml", "sh"})

    def test_blocks_stay_attributed_to_the_consuming_page(self):
        """Selectors are declared against the consumer page, so attribution must follow it."""
        consumer, upstream, cp = build(self.REL)
        e = self.extractor(consumer, upstream, upstream_root=upstream, upstream_version_for=self.VMAP)
        # .resolve() on both sides: macOS reports /var and /private/var for the
        # same tempdir, which is a symlink artifact, not an attribution difference.
        for b in e.process_file(cp).code_blocks:
            self.assertEqual(b.file_path.resolve(), cp.resolve())

    def test_a_version_outside_the_map_expands_to_nothing(self):
        """Rather than borrowing another version's content and implying coverage."""
        rel = "content/en/p/kubernetes/2.3.x/documentation/buffering.md"
        consumer, upstream, cp = build(rel)
        e = Extractor(
            repo_root=consumer,
            definition={"main_file": rel, "sources": [{"file": rel, "path": "buffering"}],
                        "context": {"product": "kubernetes", "version": "2.3.x"}},
            upstream_root=upstream, upstream_version_for=self.VMAP,
        )
        self.assertEqual(len(e.process_file(cp).code_blocks), 0)

    def test_a_missing_upstream_page_raises_rather_than_passing_empty(self):
        consumer, upstream, cp = build(self.REL)
        (upstream / "content/docs/kubernetes/latest/documentation/buffering.md").unlink()
        e = self.extractor(consumer, upstream, upstream_root=upstream, upstream_version_for=self.VMAP)
        with self.assertRaises(FileNotFoundError):
            e.process_file(cp)


if __name__ == "__main__":
    unittest.main()
