#!/usr/bin/env python3
"""The fixer's gate: an agent's rewrite of needs-update markup is checked, not trusted."""
import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import annotations as A  # noqa: E402
import annotation_fix_check as C  # noqa: E402

PAGE = '''---
title: Feature
---

Create the alpha resource so that the gateway routes alpha traffic.

```sh {paths="alpha"}
kubectl apply -f alpha.yaml
kubectl wait --for=condition=Ready pod/alpha
kubectl get pod alpha -o yaml
```

{{< doc-test paths="alpha" >}}
kubectl get pod alpha
{{< /doc-test >}}

Apply beta.

```sh {paths="beta"}
kubectl apply -f beta.yaml
```
'''

REL = "content/docs/feature.md"
ADIR = "products/demo/annotations"


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args)


def sh(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


class FixCheckTests(unittest.TestCase):
    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp())     # content repo
        self.root = pathlib.Path(tempfile.mkdtemp())     # docs-tests
        (self.repo / REL).parent.mkdir(parents=True)
        (self.repo / REL).write_text(PAGE)
        sh(self.root, "init", "-q")
        quiet(A.main, ["export", "--repo", str(self.repo), "--annotations", str(self.root / ADIR)])
        quiet(A.main, ["strip", "--repo", str(self.repo)])
        sh(self.root, "add", "-A")
        sh(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        # The content changes enough that alpha's block and its hidden check no longer attach.
        self.rewrite("kubectl apply -f alpha.yaml\nkubectl wait --for=condition=Ready pod/alpha\nkubectl get pod alpha -o yaml",
                     "helm install alpha ./chart")
        self._refresh()
        sh(self.root, "add", "-A")
        sh(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "refresh")
        self.pending = A.pending(self.root / ADIR)

    def _refresh(self):
        path = A.annotation_path(self.root / ADIR, REL)
        ann = A.load_annotation(path)
        new, _ = A.refresh_one((self.repo / REL).read_text(), ann, REL, preview=True)
        A.write_annotation(path, new, ann)

    def rewrite(self, old, new):
        p = self.repo / REL
        p.write_text(p.read_text().replace(old, new))

    def agent_fixes(self, hidden_body="kubectl get pod alpha", extra=""):
        """What a correct agent does: annotate, put the markup back by hand, save, drop the stale entries."""
        out = self.root / "out/edit.md"
        quiet(A.main, ["annotate", "--repo", str(self.repo), "--annotations", str(self.root / ADIR),
                       "--page", REL, "--out", str(out)])
        text = out.read_text().replace("```sh\nhelm install alpha ./chart\n```",
                                       '```sh {paths="alpha"}\nhelm install alpha ./chart\n```\n\n'
                                       '{{< doc-test paths="alpha" >}}\n' + hidden_body + extra + '\n{{< /doc-test >}}')
        out.write_text(text)
        quiet(A.main, ["save", "--repo", str(self.repo), "--annotations", str(self.root / ADIR),
                       "--page", REL, "--edited", str(out)])
        path = A.annotation_path(self.root / ADIR, REL)
        ann = A.load_annotation(path)
        old = A.load_annotation(path)
        for k in ("tags", "hidden"):
            ann[k] = [e for e in ann.get(k, []) if e.get("status") != A.NEEDS_UPDATE]
        A.write_annotation(path, ann, old)
        (self.root / "out/edit.md").unlink()

    def run_check(self, summary, private=False, changes=None):
        return C.check(self.root, self.repo, ADIR, "HEAD", self.pending, summary, private, changes)

    def test_a_small_edit_to_a_check_passes_and_is_reported(self):
        self.agent_fixes(hidden_body="kubectl get pod alpha-v2")
        changes = []
        self.assertEqual(self.run_check({"fixed": [{"page": REL, "label": "alpha"}]}, changes=changes), [])
        self.assertEqual([(c["page"], c["label"]) for c in changes], [(REL, "alpha")])
        self.assertGreaterEqual(changes[0]["similarity"], C.MIN_EDIT_SIMILARITY)

    def test_a_rewritten_check_fails(self):
        """Re-anchoring is the agent's job; replacing the test is a person's."""
        self.agent_fixes(hidden_body='curl -s "http://${INGRESS_GW_ADDRESS}/health" | grep -q ok\nkubectl logs deploy/other')
        errors = self.run_check({"fixed": [{"page": REL, "label": "alpha"}]})
        self.assertTrue(any("rewritten" in e for e in errors), errors)

    def test_an_unchanged_check_is_not_reported(self):
        self.agent_fixes()
        changes = []
        self.run_check({"fixed": [{"page": REL, "label": "alpha"}]}, changes=changes)
        self.assertEqual(changes, [])

    def test_a_deleted_annotation_file_needs_every_piece_removed(self):
        A.annotation_path(self.root / ADIR, REL).unlink()
        errors = self.run_check({"unresolved": [{"page": REL, "label": "alpha", "reason": "gone"}]})
        self.assertTrue(any("deleted" in e for e in errors), errors)
        summary = {"removed": [{"page": REL, "label": l, "reason": "page section deleted"} for l in ("alpha", "beta")]}
        self.assertEqual(self.run_check(summary), [])

    def test_pending_lists_the_needs_update_markup(self):
        self.assertEqual({(r["page"], r["kind"], r["label"]) for r in self.pending},
                         {(REL, "tag", "alpha"), (REL, "hidden", "alpha")})

    def test_a_correct_fix_passes(self):
        self.agent_fixes()
        self.assertEqual(self.run_check({"fixed": [{"page": REL, "label": "alpha"}]}), [])

    def test_doing_nothing_fails(self):
        errors = self.run_check({"fixed": []})
        self.assertTrue(any("not listed under `unresolved`" in e for e in errors), errors)

    def test_doing_nothing_but_saying_why_passes(self):
        summary = {"unresolved": [{"page": REL, "label": "alpha", "reason": "the alpha section was rewritten"}]}
        self.assertEqual(self.run_check(summary), [])

    def test_unresolved_needs_a_reason(self):
        errors = self.run_check({"unresolved": [{"page": REL, "label": "alpha"}]})
        self.assertTrue(any("reason" in e for e in errors), errors)

    def test_dropping_markup_unlisted_fails_and_listed_passes(self):
        path = A.annotation_path(self.root / ADIR, REL)
        ann = A.load_annotation(path)
        old = A.load_annotation(path)
        ann["tags"] = [t for t in ann["tags"] if t["paths"] != "alpha"]
        ann["hidden"] = []
        A.write_annotation(path, ann, old)
        errors = self.run_check({})
        self.assertTrue(any("disappeared" in e for e in errors), errors)
        summary = {"removed": [{"page": REL, "label": "alpha", "reason": "section deleted"},
                               {"page": REL, "label": "alpha", "reason": "its check went with it"}]}
        self.assertEqual(self.run_check(summary), [])

    def test_a_change_outside_the_annotations_fails(self):
        self.agent_fixes()
        (self.root / "scripts").mkdir()
        (self.root / "scripts/annotations.py").write_text("# rewritten by the agent\n")
        errors = self.run_check({"fixed": [{"page": REL, "label": "alpha"}]})
        self.assertTrue(any("changed outside" in e for e in errors), errors)

    def test_private_page_prose_in_a_check_fails(self):
        self._previews_off()
        self.agent_fixes(extra="\n# Create the alpha resource so that the gateway routes alpha traffic.")
        errors = self.run_check({"fixed": [{"page": REL, "label": "alpha"}]}, private=True)
        self.assertTrue(any("repeats a sentence" in e for e in errors), errors)

    def test_private_fix_without_prose_passes(self):
        self._previews_off()
        self.agent_fixes()
        self.assertEqual(self.run_check({"fixed": [{"page": REL, "label": "alpha"}]}, private=True), [])

    def test_private_previews_must_stay_off(self):
        self.agent_fixes()   # previews were never off here
        errors = self.run_check({"fixed": [{"page": REL, "label": "alpha"}]}, private=True)
        self.assertTrue(any("previews" in e for e in errors), errors)

    def _previews_off(self):
        path = A.annotation_path(self.root / ADIR, REL)
        ann = A.load_annotation(path)
        ann["previews"] = False
        for k in ("tags",):
            for t in ann.get(k, []):
                t.pop("preview", None)
        for h in ann.get("hidden", []):
            (h.get("after") or {}).get("block_before", {}) and h["after"]["block_before"].pop("preview", None)
        (path).write_text(A.dump_annotation(ann))
        sh(self.root, "add", "-A")
        sh(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "previews off")


if __name__ == "__main__":
    unittest.main()
