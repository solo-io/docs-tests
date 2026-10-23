#!/usr/bin/env python3
"""Check an agent's fix of needs-update markup before anyone publishes it.

The fixer workflow lists the markup marked needs update (`annotations.py
pending --json`), lets an agent rewrite the annotation files, and then runs
this. It trusts nothing the agent says about its own work: every rule below is
checked against the files. The run fails, and no pull request opens, if any of
them does not hold.

1. Only annotation files under --annotations changed.
2. Every changed annotation file loads.
3. On every page that had pending markup, everything attaches, apart from the
   pieces the agent lists under `unresolved`, each with a reason. Those stay
   in the file, still marked needs update.
4. No markup disappears unless the agent lists it under `removed`, with a
   reason, for example a test whose whole section was deleted from the page.
5. With --private (a product whose pages are not public), every changed file
   keeps `previews: false` and holds no `preview:` line, and no line the agent
   added to a hidden check repeats a sentence of the page's prose. docs-tests
   is public; test commands are expected there, page text is not.

The agent's summary is JSON:

    {"fixed":      [{"page": ..., "label": ...}],
     "unresolved": [{"page": ..., "label": ..., "reason": ...}],
     "removed":    [{"page": ..., "label": ..., "reason": ...}]}

Run from a docs-tests checkout; --base is the commit the agent started from.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import annotations as A  # noqa: E402

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
# A prose line shorter than this is too generic to call copied ("Example output:").
MIN_PROSE_WORDS = 6


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def changed_files(root: Path, base: str) -> List[str]:
    tracked = git(root, "diff", "--name-only", base, "--").split()
    untracked = git(root, "ls-files", "--others", "--exclude-standard").split()
    return sorted(set(tracked) | set(untracked))


def old_annotation(root: Path, base: str, rel: str) -> dict:
    try:
        return yaml.safe_load(git(root, "show", f"{base}:{rel}")) or {}
    except subprocess.CalledProcessError:
        return {}


def prose_lines(text: str) -> Set[str]:
    """The page's sentences: lines outside code fences, normalized, long enough to mean something."""
    out, fence = set(), None
    for line in text.splitlines():
        m = FENCE.match(line)
        if m:
            mark = m.group(1)[0]
            if fence is None:
                fence = mark
            elif fence == mark:
                fence = None
            continue
        if fence is None:
            norm = _norm(line)
            if len(norm.split()) >= MIN_PROSE_WORDS:
                out.add(norm)
    return out


def _norm(line: str) -> str:
    line = re.sub(r"^[\s>#*\-|0-9.]+", "", line)
    return " ".join(line.split()).lower()


def hidden_lines(ann: dict) -> Set[str]:
    return {_norm(l) for h in ann.get("hidden", []) for l in h.get("content", "").splitlines()}


def labels(ann: dict) -> Set[str]:
    out = {t["paths"] for t in ann.get("tags", [])}
    for h in ann.get("hidden", []):
        m = A.PATHS_VALUE.search(h.get("content", "").split("\n", 1)[0])
        out.add(m.group(1) if m else "")
    return out


def listed(summary: dict, key: str, errors: List[str]) -> List[Tuple[str, str]]:
    """The (page, label) rows under one summary key; a list, so two removals of one label count twice."""
    rows = summary.get(key) or []
    for r in rows:
        if not (r.get("page") and r.get("label") and (key == "fixed" or r.get("reason"))):
            errors.append(f"summary `{key}` entry needs page, label{'' if key == 'fixed' else ' and reason'}: {r}")
    return [(r.get("page"), r.get("label")) for r in rows]


def check(root: Path, repo: Path, adir_rel: str, base: str, pending: List[dict], summary: dict,
          private: bool) -> List[str]:
    errors: List[str] = []
    listed(summary, "fixed", errors)
    unresolved = set(listed(summary, "unresolved", errors))
    removed_rows = listed(summary, "removed", errors)
    removed = set(removed_rows)

    changed = changed_files(root, base)
    outside = [f for f in changed if not f.startswith(adir_rel.rstrip("/") + "/")]
    errors += [f"changed outside {adir_rel}: {f}" for f in outside]

    pending_pages = {r["page"] for r in pending}
    for rel in changed:
        if rel in outside or not rel.endswith(".md.yaml"):
            continue
        path = root / rel
        old = old_annotation(root, base, rel)
        if not path.exists():
            gone = {(old.get("source", ""), label) for label in labels(old)}
            if not gone <= removed:
                errors.append(f"{rel}: deleted, but not every piece is listed under `removed`")
            continue
        try:
            ann = A.load_annotation(path)
        except Exception as e:  # noqa: BLE001 -- any load failure is a failed check
            errors.append(f"{rel}: does not load: {e}")
            continue
        page = ann["source"]

        before, after = A.count_markup(old) if old else 0, A.count_markup(ann)
        dropped = before - after
        n_removed = sum(1 for p, _ in removed_rows if p == page)
        if dropped > n_removed:
            errors.append(f"{page}: {dropped} piece(s) of markup disappeared, {n_removed} listed under `removed`")

        text_path = repo / page
        if not text_path.exists():
            errors.append(f"{page}: not in the content checkout")
            continue
        text = text_path.read_text(encoding="utf-8")
        _, placements, _ = A.attach(text, ann)
        for p in placements:
            if p.state == A.NEEDS_UPDATE and (page, p.label) not in unresolved:
                errors.append(f"{page}: {p.kind} {p.label} still does not attach and is not listed under `unresolved`")

        if private:
            if ann.get("previews", True) is not False:
                errors.append(f"{page}: previews must stay off (previews: false) for a private product")
            if re.search(r"^\s*preview:", path.read_text(encoding="utf-8"), re.M):
                errors.append(f"{page}: holds a `preview:` line; a private page's text must not reach docs-tests")
            copied = (hidden_lines(ann) - hidden_lines(old)) & prose_lines(text)
            errors += [f"{page}: a hidden check repeats a sentence of the page: {c[:60]!r}..." for c in sorted(copied)]

    untouched = pending_pages - {A.load_annotation(root / r)["source"] for r in changed
                                 if r.endswith(".md.yaml") and (root / r).exists()}
    for page in sorted(untouched):
        labels = {r["label"] for r in pending if r["page"] == page}
        if not all((page, l) in unresolved for l in labels):
            errors.append(f"{page}: had markup that needs update, was not changed, and is not listed under `unresolved`")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="content repo checkout the annotations attach to")
    ap.add_argument("--annotations", required=True, help="products/<product>/annotations, relative to --root")
    ap.add_argument("--root", default=".", help="the docs-tests checkout")
    ap.add_argument("--base", default="HEAD", help="docs-tests commit the agent started from")
    ap.add_argument("--pending", required=True, help="`annotations.py pending --json` output from before the agent ran")
    ap.add_argument("--summary", required=True, help="the agent's JSON summary")
    ap.add_argument("--private", action="store_true", help="the product's pages are not public")
    args = ap.parse_args(argv)

    pending = json.loads(Path(args.pending).read_text(encoding="utf-8"))
    try:
        summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
        errors = check(Path(args.root), Path(args.repo), args.annotations, args.base, pending, summary, args.private)
    except (OSError, ValueError) as e:
        errors = [f"summary: {e}"]
    for e in errors:
        print(f"::error::{e}")
    if errors:
        print(f"{len(errors)} problem(s); nothing may be published")
        return 1
    print(f"ok: {len(summary.get('fixed') or [])} fixed, {len(summary.get('unresolved') or [])} unresolved, "
          f"{len(summary.get('removed') or [])} removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
