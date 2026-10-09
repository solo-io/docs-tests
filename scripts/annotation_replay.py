#!/usr/bin/env python3
"""Measure close matching by replaying a content repo's history.

The answer key: a repo that still carries its markup inline shows, after every
commit, where the author actually left each tag and hidden check. For every
commit that edited a page with markup, this builds the annotation from the page
BEFORE the commit, attaches it to the page AFTER the commit with that page's
own markup removed, and compares where `annotations.attach` put each piece with
where the author put it.

Run it again whenever matching changes. The number that must not rise is
`close, wrong`: a test silently attached to the wrong block.

Usage: annotation_replay.py <repo> <since-date> [--ref origin/main] [--scope traffic-management] [--rows out.json]
"""
import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from annotations import CLOSE, EXACT, NEEDS_UPDATE, PAGE_GLOBS, attach, parse, to_annotation  # noqa: E402


def page_edits(repo, since, ref):
    log = subprocess.run(
        ["git", "-C", repo, "log", "--no-merges", f"--since={since}", "--diff-filter=MR", "-M",
         "--name-status", "--format=@%H %P", ref, "--", *PAGE_GLOBS],
        capture_output=True, text=True, check=True).stdout
    cur = None
    for line in log.splitlines():
        if line.startswith("@"):
            parts = line[1:].split()
            cur = (parts[0], parts[1] if len(parts) > 1 else None)
        elif line.strip() and cur and cur[1]:
            f = line.split("\t")
            if f[0].startswith("R"):
                yield cur[1], f[1], cur[0], f[2]
            elif f[0] == "M":
                yield cur[1], f[1], cur[0], f[1]


class Blobs:
    def __init__(self, repo):
        self.p = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def get(self, ref, path):
        self.p.stdin.write(f"{ref}:{path}\n".encode())
        self.p.stdin.flush()
        header = self.p.stdout.readline().decode()
        if header.endswith("missing\n"):
            return None
        data = self.p.stdout.read(int(header.split()[2]))
        self.p.stdout.read(1)
        return data.decode("utf-8", "replace")


def classify(state, at, truth, same_script=None):
    """truth: set of places the author left this markup (empty if removed).

    `same_script(at, truth)`, for hidden checks: True when the check landed
    elsewhere but no tagged block lies between the two spots, so the generated
    script is the same either way. Reported apart from `wrong`, never folded
    into `right`.
    """
    if state == NEEDS_UPDATE:
        return "missed" if truth else "skipped, author removed it"
    if at in truth:
        return "right"
    if truth and same_script and same_script(at, truth):
        return "elsewhere, same script"
    return "wrong" if truth else "attached, author removed it"


def selectors(paths):
    return {s.strip() for s in paths.split(",") if s.strip()}


def replay_pair(old_t, new_t, ctx):
    old = parse(old_t)
    if not old.tags and not old.hidden:
        return []
    new = parse(new_t)
    ann = to_annotation(ctx["file"], old)
    _, placements, _ = attach(new_t, ann)
    rows = []
    # A tag is right when it lands on a block that still carries any of its
    # selectors; the author may have added or removed selectors in the same edit.
    truth_tags = {}
    for t in new.tags:
        for sel in selectors(t["paths"]):
            truth_tags.setdefault(sel, set()).add(t["block"])
    tagged_openers = sorted(new.blocks[t["block"]].opener for t in new.tags)

    def same_script(at, truth):
        lo, hi = sorted((at, next(iter(truth))))
        return not any(lo < o <= hi for o in tagged_openers)

    truth_hidden = {}
    for h in new.hidden:
        truth_hidden.setdefault(h["content"], []).append(h["at"])
    tag_ps = [p for p in placements if p.kind == "tag"]
    hid_ps = [p for p in placements if p.kind == "hidden"]
    for t, p in zip(ann.get("tags", []), tag_ps):
        rows.append({**ctx, "kind": "tag", "state": p.state, "score": p.score,
                     "result": classify(p.state, p.at, set().union(*(truth_tags.get(x, set()) for x in selectors(t["paths"]))))})
    for h, p in zip(ann.get("hidden", []), hid_ps):
        spots = truth_hidden.get(h["content"], [])
        truth = {spots.pop(0)} if spots else set()
        a = h["after"]
        kind = "hidden-start" if a.get("start") else ("hidden-block" if "fingerprint" in a else "hidden-sentence")
        rows.append({**ctx, "kind": kind, "state": p.state, "score": p.score,
                     "result": classify(p.state, p.at, truth, same_script)})
    return rows


def summarize(rows, scope=None):
    rows = [r for r in rows if scope is None or r["scope"] == scope]
    out = [f"scope: {scope or 'all'}; markup instances on edited pages: {len(rows)}"]
    by_state = Counter((r["state"], r["result"]) for r in rows)
    for state in (EXACT, CLOSE, NEEDS_UPDATE):
        parts = ", ".join(f"{res} {n}" for (s, res), n in sorted(by_state.items()) if s == state)
        out.append(f"  {state:13} {parts}")
    edited = [r for r in rows if r["state"] != EXACT]
    kept = [r for r in edited if r["result"] in ("right", "wrong", "missed", "elsewhere, same script")]
    c = Counter(r["result"] for r in kept)
    out.append(f"  tested block or anchor changed, author kept the markup: {len(kept)}; "
               f"close right {c['right']}, close WRONG {c['wrong']}, "
               f"close elsewhere with the same script {c['elsewhere, same script']}, needs update {c['missed']}")
    miss = Counter(r["kind"] for r in kept if r["result"] == "missed")
    out.append(f"  needs update by kind: {dict(miss)}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("since")
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--scope", default="traffic-management")
    ap.add_argument("--rows")
    args = ap.parse_args()
    blobs = Blobs(args.repo)
    rows, pages = [], 0
    for parent, opath, commit, npath in page_edits(args.repo, args.since, args.ref):
        old_t = blobs.get(parent, opath)
        if not old_t or ("paths=" not in old_t and "doc-test" not in old_t):
            continue
        new_t = blobs.get(commit, npath)
        if new_t is None:
            continue
        pages += 1
        scope = args.scope if f"{args.scope}/" in npath else "other"
        rows += replay_pair(old_t, new_t, {"file": npath, "commit": commit[:8], "scope": scope})
    if args.rows:
        Path(args.rows).write_text(json.dumps(rows))
    print(f"page edits with markup replayed: {pages}")
    print(summarize(rows))
    print(summarize(rows, args.scope))


if __name__ == "__main__":
    main()
