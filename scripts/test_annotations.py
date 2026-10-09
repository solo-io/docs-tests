#!/usr/bin/env python3
"""Tests for test annotations: export, attach, close matching, refresh, and annotate/save.

The rule these protect: markup is either attached where the author meant it,
or reported as needs update. It is never attached somewhere else silently,
and a refresh never loses any.
"""
import contextlib
import io
import pathlib
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import annotations as A  # noqa: E402

APPLY = '''```yaml {paths="route"}
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: httpbin
  namespace: httpbin
spec:
  parentRefs:
  - name: agentgateway-proxy
  rules:
  - backendRefs:
    - name: httpbin
      port: 8000
```'''

CURL = '''```sh {paths="route"}
curl -i localhost:8080/headers \\
  -H "host: www.example.com" \\
  -H "x-request-id: 1" \\
  -H "x-team: docs" \\
  -H "x-env: test"
```'''

CHECK = '''{{< doc-test paths="route" >}}
YAMLTest -f - <<'EOF'
- name: route is accepted
EOF
{{< /doc-test >}}'''

PAGE = f'''---
title: Routes
weight: 10
test:
  route:
  - file: this.md
    path: route
---

Create a route.

{APPLY}

{CHECK}

Send a request.

{CURL}

The request succeeds.

{CHECK.replace("route is accepted", "request succeeds")}

## Cleanup

```sh
kubectl delete httproute httpbin -n httpbin
```
'''


def roundtrip(text):
    """Export, serialize, strip, attach. Returns (attached text, placements)."""
    page = A.parse(text)
    ann = yaml.safe_load(A.dump_annotation(A.to_annotation("p.md", page)))
    out, placements, _ = A.attach(page.text, ann)
    return out, placements


def annotation(text):
    return yaml.safe_load(A.dump_annotation(A.to_annotation("p.md", A.parse(text))))


def states(placements, kind=None):
    return [p.state for p in placements if kind is None or p.kind == kind]


def strip(text):
    return A.parse(text).text


class RoundTripTests(unittest.TestCase):
    """Strip then attach gives back the page byte for byte, so scripts do not change."""

    def assertRoundTrip(self, text):
        out, placements = roundtrip(text)
        self.assertEqual(out, text)
        self.assertTrue(all(s == A.EXACT for s in states(placements)), placements)

    def test_full_page(self):
        self.assertRoundTrip(PAGE)

    def test_stripped_page_has_no_markup(self):
        stripped = strip(PAGE)
        self.assertNotIn("paths=", stripped)
        self.assertNotIn("doc-test", stripped)
        self.assertNotIn("test:", stripped)
        self.assertFalse(A.parse(stripped).has_markup())

    def test_opener_forms(self):
        for opener in ('```sh {paths="a"}', '```sh {paths="a,b"}', '```yaml {linenos=table,paths="a"}',
                       '```yaml {hl_lines=[2], paths="a"}', '```sh {paths="a" hl_lines=[1]}',
                       '```sh,paths="a"', '```sh {paths="a"} ', '  ```sh {paths="a"}'):
            with self.subTest(opener=opener):
                self.assertRoundTrip(f"Intro.\n\n{opener}\necho hi\n```\n")

    def test_hidden_check_at_page_start_and_without_blank_line(self):
        self.assertRoundTrip(f"---\ntitle: x\n---\n{CHECK}\nText.\n{CHECK}\n")

    def test_page_that_ends_at_its_front_matter(self):
        self.assertRoundTrip("---\ntitle: x\ntest:\n  a: 1\n---")

    def test_no_trailing_newline_and_unclosed_fence(self):
        self.assertRoundTrip('Text\n\n```sh {paths="a"}\necho hi')

    def test_trailing_whitespace_survives_yaml(self):
        self.assertRoundTrip('Text\n\n{{< doc-test paths="a" >}}\necho "x"   \n\tindented\n{{< /doc-test >}}\n')

    def test_annotation_holds_no_prose(self):
        dumped = A.dump_annotation(A.to_annotation("p.md", A.parse(PAGE), preview=False))
        for prose in ("Create a route", "Send a request", "The request succeeds", "Cleanup", "HTTPRoute"):
            self.assertNotIn(prose, dumped)

    def test_attach_removes_inline_markup_first(self):
        """Mid-migration a page still has its markup; the result must depend on the annotation only."""
        ann = annotation(PAGE)
        ann["tags"] = ann["tags"][:1]
        out, _, _ = A.attach(PAGE, ann)
        self.assertEqual(out.count('paths="route"}'), 1)


class ExactTests(unittest.TestCase):
    def test_prose_edits_and_new_blocks_do_not_matter(self):
        ann = annotation(PAGE)
        edited = strip(PAGE).replace("Create a route.", "First, create an HTTPRoute.")
        edited = edited.replace("## Cleanup", "```sh\nkubectl get pods\n```\n\n## Cleanup")
        out, placements, _ = A.attach(edited, ann)
        self.assertEqual(set(states(placements)), {A.EXACT})
        self.assertEqual(strip(out), edited)
        self.assertEqual(out.count('{paths="route"}'), 2)

    def test_block_moved_unchanged_is_exact(self):
        ann = annotation(PAGE)
        stripped = strip(PAGE)
        curl = CURL.replace(' {paths="route"}', "")
        moved = stripped.replace(curl + "\n", "").replace("## Cleanup", curl + "\n\n## Cleanup")
        out, placements, _ = A.attach(moved, ann)
        self.assertEqual(states(placements, "tag"), [A.EXACT, A.EXACT])
        self.assertIn(CURL + "\n\n## Cleanup", out)


class CloseMatchTests(unittest.TestCase):
    def test_typo_fix_is_close(self):
        ann = annotation(PAGE)
        edited = strip(PAGE).replace("  name: httpbin\n  namespace", "  name: http-bin\n  namespace")
        out, placements, _ = A.attach(edited, ann)
        self.assertEqual(states(placements, "tag"), [A.CLOSE, A.EXACT])
        self.assertIn('```yaml {paths="route"}\napiVersion', out)

    def test_hidden_check_moves_with_its_block(self):
        ann = annotation(PAGE)
        edited = strip(PAGE).replace("port: 8000", "port: 8080")
        out, placements, _ = A.attach(edited, ann)
        self.assertEqual(states(placements, "hidden")[0], A.CLOSE)
        self.assertIn("port: 8080\n```\n\n{{< doc-test", out)

    def test_rewritten_block_needs_update(self):
        ann = annotation(PAGE)
        rewritten = "kubectl apply -f route.yaml\nkubectl wait --for=condition=Accepted httproute/httpbin"
        body = APPLY.split("\n", 1)[1].rsplit("\n", 1)[0]
        edited = strip(PAGE).replace(body, rewritten)
        out, placements, unplaced = A.attach(edited, ann)
        self.assertEqual(states(placements, "tag")[0], A.NEEDS_UPDATE)
        self.assertEqual(len(unplaced["tags"]), 1)
        self.assertEqual(out.count('{paths="route"}'), 1)

    def test_language_change_needs_update(self):
        ann = annotation(PAGE)
        edited = strip(PAGE).replace("```yaml\napiVersion", "```json\napiVersion").replace("port: 8000", "port: 8001")
        _, placements, _ = A.attach(edited, ann)
        self.assertEqual(states(placements, "tag")[0], A.NEEDS_UPDATE)

    def test_two_near_identical_candidates_need_update(self):
        """An edited block split into two similar blocks: no clear winner, so nothing moves."""
        ann = annotation(PAGE)
        body = APPLY.split("\n", 1)[1].rsplit("\n", 1)[0]
        a = body.replace("port: 8000", "port: 8001")
        b = body.replace("port: 8000", "port: 8002")
        edited = strip(PAGE).replace(body, a + "\n```\n\n```yaml\n" + b)
        _, placements, _ = A.attach(edited, ann)
        self.assertEqual(states(placements, "tag")[0], A.NEEDS_UPDATE)

    def test_two_edited_blocks_cannot_land_on_one(self):
        text = 'A\n\n```sh {paths="a"}\necho one\necho two\necho three\n```\n\nB\n\n```sh {paths="b"}\necho one\necho two\necho four\n```\n'
        ann = annotation(text)
        merged = "A\n\n```sh\necho one\necho two\necho three\necho four\n```\n\nB\n"
        _, placements, _ = A.attach(merged, ann)
        self.assertNotIn(A.CLOSE, states(placements, "tag"))

    def test_close_match_rule(self):
        new = [A.Block("x", "sh", ["1", "2", "3", "4", "5"], 0, 0, ""),
               A.Block("y", "sh", ["1", "2", "3", "9", "8"], 0, 0, "")]
        old = ["1", "2", "3", "4", "6"]
        j, best, second = A.close_match(old, "sh", new, [0, 1])
        self.assertEqual(j, 0)                          # 4/6 = 0.67 vs 3/7 = 0.43
        j, _, _ = A.close_match(["1", "2", "3", "x", "y"], "sh", new, [0, 1])
        self.assertIsNone(j)                            # 3/7 each: no margin, below 0.6


class SentenceAnchorTests(unittest.TestCase):
    """A check anchored to a sentence is found between the same two blocks."""

    def test_through_a_close_matched_neighbor(self):
        """The replay's largest needs-update source before this was handled."""
        ann = annotation(PAGE)
        edited = strip(PAGE).replace('-H "host: www.example.com"', '-H "host: www.example.org"')
        out, placements, _ = A.attach(edited, ann)
        self.assertEqual(states(placements, "hidden")[1], A.CLOSE)
        self.assertIn("The request succeeds.\n\n{{< doc-test", out)

    def test_reworded_sentence_is_close(self):
        ann = annotation(PAGE)
        edited = strip(PAGE).replace("The request succeeds.", "The request succeeds now.")
        out, placements, _ = A.attach(edited, ann)
        self.assertEqual(states(placements, "hidden")[1], A.CLOSE)
        self.assertIn("The request succeeds now.\n\n{{< doc-test", out)

    def test_rewritten_sentence_needs_update(self):
        ann = annotation(PAGE)
        edited = strip(PAGE).replace("The request succeeds.", "Expect a 200 response with the echoed headers.")
        _, placements, _ = A.attach(edited, ann)
        self.assertEqual(states(placements, "hidden")[1], A.NEEDS_UPDATE)

    def test_never_moves_past_another_block(self):
        """The same sentence after a different block is not a candidate."""
        ann = annotation(PAGE)
        stripped = strip(PAGE)
        curl = CURL.replace(' {paths="route"}', "")
        edited = stripped.replace(curl, "```sh\ncurl -v http://other\nexit 1\n```")
        self.assertNotEqual(edited, stripped)
        _, placements, _ = A.attach(edited, ann)
        self.assertEqual(states(placements, "hidden")[1], A.NEEDS_UPDATE)


class RefreshOneTests(unittest.TestCase):
    def test_records_new_fingerprints(self):
        ann = annotation(PAGE)
        edited = strip(PAGE).replace("port: 8000", "port: 8080")
        new, _ = A.refresh_one(edited, ann, "p.md", True)
        out, placements, _ = A.attach(edited, new)
        self.assertEqual(set(states(placements)), {A.EXACT})
        self.assertNotEqual(new["tags"][0]["fingerprint"], ann["tags"][0]["fingerprint"])

    def test_no_previews_is_sticky(self):
        ann = yaml.safe_load(A.dump_annotation(A.to_annotation("p.md", A.parse(PAGE), preview=False)))
        edited = strip(PAGE).replace("port: 8000", "port: 8080")
        new, _ = A.refresh_one(edited, ann, "p.md", True)      # a refresh run without --no-preview
        self.assertFalse(new["previews"])
        self.assertNotIn("preview", A.dump_annotation(new).replace("previews", ""))
        saved = A.save(strip(PAGE), PAGE, "p.md", new)          # a save without --no-preview
        self.assertNotIn("preview", A.dump_annotation(saved).replace("previews", ""))

    def test_keeps_needs_update_markup_and_reattaches_it_later(self):
        ann = annotation(PAGE)
        body = APPLY.split("\n", 1)[1].rsplit("\n", 1)[0]
        broken = strip(PAGE).replace(body, "kubectl apply -f route.yaml")
        new, _ = A.refresh_one(broken, ann, "p.md", True)
        self.assertEqual(A.count_markup(new), A.count_markup(ann))
        stale = [t for t in new["tags"] if t.get("status") == A.NEEDS_UPDATE]
        self.assertEqual(len(stale), 1)
        # The block comes back unchanged (a revert): the markup reattaches, exact.
        restored, placements = A.refresh_one(strip(PAGE), yaml.safe_load(A.dump_annotation(new)), "p.md", True)
        self.assertEqual(set(states(placements)), {A.EXACT})
        self.assertFalse(any(t.get("status") for t in restored["tags"]))


def cli(*argv):
    with contextlib.redirect_stdout(io.StringIO()):
        return A.main(list(argv))


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class RefreshCommandTests(unittest.TestCase):
    """End to end over a real git repo: edits, renames, and moves found by fingerprint."""

    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp())
        self.adir = pathlib.Path(tempfile.mkdtemp()) / "annotations"
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        self.page = "content/docs/traffic-management/routes.md"
        self.write(self.page, PAGE)
        self.write("content/docs/other.md", "No tests here.\n")
        self.commit()
        cli("export", "--repo", str(self.repo), "--annotations", str(self.adir))
        cli("strip", "--repo", str(self.repo))
        self.commit()
        self.assertEqual(cli("refresh", "--repo", str(self.repo), "--annotations", str(self.adir)), 0)

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "x", "--allow-empty")

    def refresh(self):
        return cli("refresh", "--repo", str(self.repo), "--annotations", str(self.adir))

    def files(self):
        return sorted(p.relative_to(self.adir).as_posix() for p in A.annotation_files(self.adir))

    def test_export_writes_one_file_per_page_with_markup(self):
        self.assertEqual(self.files(), [self.page + ".yaml"])
        self.assertIn("commit", yaml.safe_load((self.adir / "refresh-state.yaml").read_text()))

    def test_export_can_leave_scenario_definitions_to_manifests(self):
        self.write("content/docs/skipped.md", "---\ntitle: s\ntest: skip\n---\nBody.\n")
        out = pathlib.Path(tempfile.mkdtemp())
        git(self.repo, "checkout", "-q", "HEAD~1", "--", self.page)   # the page with its inline markup
        cli("export", "--repo", str(self.repo), "--annotations", str(out), "--front-matter", "skip-only")
        self.assertNotIn("front_matter", A.load_annotation(out / (self.page + ".yaml")))
        self.assertEqual(A.load_annotation(out / "content/docs/skipped.md.yaml")["front_matter"]["content"], "test: skip")

    def test_follows_a_git_rename(self):
        new = "content/docs/latest/traffic-management/routes.md"
        git(self.repo, "mv", self.page, "content/docs/x.md")
        (self.repo / new).parent.mkdir(parents=True)
        git(self.repo, "mv", "content/docs/x.md", new)
        self.commit()
        self.assertEqual(self.refresh(), 0)
        self.assertEqual(self.files(), [new + ".yaml"])
        self.assertEqual(A.load_annotation(self.adir / (new + ".yaml"))["source"], new)

    def test_follows_a_move_git_cannot_see(self):
        """Rewritten prose defeats rename detection; the tagged blocks still identify the page."""
        stripped = (self.repo / self.page).read_text()
        (self.repo / self.page).unlink()
        new = "content/docs/routing/basics.md"
        self.write(new, "---\ntitle: Basics\n---\n\nTotally new intro.\n\n" + stripped.split("Create a route.", 1)[1])
        self.commit()
        self.assertEqual(self.refresh(), 0)
        self.assertEqual(self.files(), [new + ".yaml"])

    def test_ambiguous_copies_are_not_guessed(self):
        """Versioned trees hold copies of a page; with two unannotated copies, neither is picked."""
        stripped = (self.repo / self.page).read_text()
        self.write("content/docs/a/copy.md", stripped)
        self.write("content/docs/b/copy.md", stripped)
        ann = A.load_annotation(self.adir / (self.page + ".yaml"))
        files = A.page_files(self.repo)
        index = A.fingerprint_index(self.repo, files)
        self.assertIsNone(A.find_moved_page(self.repo, ann, {self.page}, index))
        # One copy already has its own annotation: the other is the move.
        self.assertEqual(A.find_moved_page(self.repo, ann, {self.page, "content/docs/a/copy.md"}, index),
                         "content/docs/b/copy.md")

    def test_front_matter_test_survives_a_front_matter_edit(self):
        text = (self.repo / self.page).read_text().replace("weight: 10", "weight: 20")
        out, placements, _ = A.attach(text, A.load_annotation(self.adir / (self.page + ".yaml")))
        self.assertEqual(states(placements, "front_matter"), [A.CLOSE])
        self.assertEqual(yaml.safe_load(out.split("---")[1])["test"], {"route": [{"file": "this.md", "path": "route"}]})

    def test_deleted_page_keeps_its_annotation(self):
        (self.repo / self.page).unlink()
        self.commit()
        self.assertEqual(self.refresh(), 0)
        self.assertEqual(self.files(), [self.page + ".yaml"])

    def test_attach_command_round_trips(self):
        out = pathlib.Path(tempfile.mkdtemp())
        cli("attach", "--repo", str(self.repo), "--annotations", str(self.adir), "--out", str(out))
        self.assertEqual((out / self.page).read_text(), PAGE)

    def test_roundtrip_command(self):
        repo = pathlib.Path(tempfile.mkdtemp())
        (repo / "content").mkdir()
        (repo / "content/p.md").write_text(PAGE)
        self.assertEqual(cli("roundtrip", "--repo", str(repo)), 0)


class AnnotateSaveTests(unittest.TestCase):
    def test_annotate_then_save_adds_a_test(self):
        stripped = strip(PAGE)
        marked = stripped.replace("```sh\nkubectl delete", '```sh {paths="route"}\nkubectl delete')
        ann = A.save(stripped, marked, "p.md", None)
        self.assertEqual(len(ann["tags"]), 1)
        out, _, _ = A.attach(stripped, ann)
        self.assertEqual(out, marked)

    def test_save_refuses_page_edits(self):
        stripped = strip(PAGE)
        marked = stripped.replace("Create a route.", "Create the route.")
        with self.assertRaisesRegex(ValueError, "changes the page itself"):
            A.save(stripped, marked, "p.md", None)

    def test_save_keeps_needs_update_markup_until_it_is_replaced(self):
        old = annotation(PAGE)
        old["tags"][0]["status"] = A.NEEDS_UPDATE
        stripped = strip(PAGE)
        ann = A.save(stripped, stripped, "p.md", old)
        self.assertEqual([t.get("status") for t in ann["tags"]], [A.NEEDS_UPDATE])
        marked = stripped.replace("```yaml\napiVersion", '```yaml {paths="route"}\napiVersion')
        ann = A.save(stripped, marked, "p.md", old)
        self.assertEqual([t.get("status") for t in ann["tags"]], [None])


class SlackTests(unittest.TestCase):
    REPORT = {"markup_total": 10, "pages": 3, "pages_refreshed": 2,
              "sections": {"traffic-management": {"pages": 3, "markup": 10}},
              "needs_update": [{"page": "a.md", "kind": "tag", "label": "route"}],
              "close": [{"page": "b.md", "kind": "tag", "label": "rewrite", "score": 0.8}],
              "moved": [{"from": "c.md", "to": "d.md"}]}

    def texts(self, payload):
        return [b.get("text", {}).get("text") or b["elements"][0]["text"] for b in payload["blocks"]]

    def test_lists_every_needs_update_and_threads_unreviewed_changes(self):
        main, thread = A.slack_payloads(self.REPORT, "https://run")
        texts = self.texts(main)
        self.assertIn("9 attached | 1 need update | 10 total", texts[0])
        self.assertIn("`route` \u2014 tag  (_`a.md`_)", texts[2])
        self.assertIn("View workflow run", texts[-1])
        body = "\n".join(self.texts(thread))
        self.assertIn("rewrite", body)
        self.assertIn("c.md", body)

    def test_clean_day_has_no_thread(self):
        report = {**self.REPORT, "needs_update": [], "close": [], "moved": []}
        main, thread = A.slack_payloads(report, None)
        self.assertTrue(self.texts(main)[0].startswith("\u2705"))
        self.assertIsNone(thread)

    def test_failure(self):
        main, thread = A.slack_payloads(None, "https://run")
        self.assertIn("failed", self.texts(main)[0])
        self.assertIsNone(thread)


if __name__ == "__main__":
    unittest.main()
