#!/usr/bin/env python3
"""Check an agent's fixes for failing doc tests before anything is re-run or published.

The failure fixer gives an agent the failures from collect_failures.py and the
checkouts they involve: the docs-tests checkout, the content repo the results
name, and, for a consumer whose pages rebase another site's, that upstream
repo. The agent decides for each failure whether the page, the test, or
neither is wrong, and edits files to fix the first two. This script checks the
edits against the files, never against what the agent says about them. The run
stops, and nothing is re-run or published, if any rule fails.

1. Every failure has exactly one row in the summary, with a kind and a reason.
   Only `content` and `test` rows may carry a fix; `environment`, `product`
   and `unknown` rows change no file.
2. Every changed file is in a repo's allowed paths, and is listed under the
   failure it fixes. A changed annotation file of a page with markup that needs
   update (--pending) is the other fixer's work and is left to
   annotation_fix_check.py.
3. An annotation edited for a failure belongs to a page that failure runs: the
   scenario's page, the page of the step that failed, the upstream pages they
   rebase, and the files those pages reuse.
4. No content page gains test markup (`paths=`, a `{{< doc-test >}}` block, a
   front matter `test:` key). Test markup lives in docs-tests only.
5. Nothing written to a public content repo names a private one. Every line
   added to the files of a repo marked `leak_check`, and the `public_reason` of
   every row that changes one, is checked for the terms in LEAK_TERMS. Such a
   row needs a `public_reason`: it is the only text of the row that reaches that
   repo's pull request. docs-tests is not marked: its enterprise annotations
   are public by decision, and annotation_fix_check.py --private keeps page
   prose out of them.
6. No added line, in any repo, reads a secret or reaches somewhere new. The
   verify job runs these lines on a cluster before anybody reviews them, with
   the enterprise license key in its environment, and the agent read pages that
   outside contributors can edit. So an added line may not name a secret-shaped
   variable, call `gh`, dump the environment, or name an http(s) host that the
   repo does not already name somewhere.

--repos is a JSON list, one entry per checkout:

    [{"name": "oss", "path": "consumer", "base": "<sha>", "leak_check": true,
      "allowed": ["content/docs/", "assets/"]},
     {"name": "hub", "path": ".", "base": "<sha>",
      "allowed": ["assets/agw-docs/"],
      "ignore": ["out/", "consumer/", "docs-tests/", "fix-summary.json", "codex-final.txt"]},
     {"name": "docs-tests", "path": "docs-tests", "base": "<sha>",
      "allowed": ["products/agentgateway/annotations/"], "annotations": true}]

`files` in the summary are workspace-relative: "<path>/<file>", or "<file>"
for the repo at ".". The failure rows of the agent's summary:

    {"failures": [{"key": ..., "kind": "content" | "test" | "environment" | "product" | "unknown",
                   "fix": true | false, "reason": ..., "public_reason": ..., "files": [...]}]}

--rerun-out lists the failures with a fix, for the verify job to re-run.
--exempt-out lists the annotation pages edited for a failure, for
annotation_fix_check.py's --exempt-pages: their checks may change more than a
re-anchor would, because the verify job re-runs them instead.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from typing import Dict, List, Set

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from reuse_consumers import inclusion_targets  # noqa: E402

KINDS = {"content", "test", "environment", "product", "unknown"}
FIXABLE = {"content", "test"}

# Words that name the private side in text bound for a public repo. Broad on
# purpose: a false hit costs a re-run, a missed one publishes internal detail.
LEAK_TERMS = re.compile(
    r"enterprise|solo[ ._-]?io|\bsolo\b|licen[cs]e[ _-]?key|content/en/|docs hub",
    re.IGNORECASE,
)
# Test markup that must never be added to a content page.
TEST_MARKUP = re.compile(r"""\{\{[<%]\s*/?\s*doc-test\b|\bpaths\s*=\s*"|^\s*test\s*:""")
ANNOTATION_SUFFIX = ".yaml"
# Rule 6. A fix never needs any of these.
SECRET_READ = re.compile(
    r"\$\{?\w*(?:TOKEN|SECRET|PASSWORD|LICENSE)\w*|\b(?:GITHUB|ACTIONS|RUNNER)_\w+"
    r"|(?:^|[\s;|&(`])gh\s|\bprintenv\b|(?:^|[\s;|&(`])env\s*(?:$|[|>;])|/proc/\S*environ|\bset\s*\|",
    re.IGNORECASE,
)
URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
# A provider key such as $OPENAI_API_KEY is how many guides are written, and the
# nightly sets it to a placeholder. One the repo already uses is allowed; a new
# one is not.
KEY_VAR = re.compile(r"\$\{?(\w*_KEY)\b")


def git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def ws(repo: dict, rel: str) -> str:
    """A repo-relative path as the workspace-relative path the summary uses."""
    return rel if repo["path"] in (".", "") else f"{repo['path'].rstrip('/')}/{rel}"


def repo_of(repos: List[dict], path: str) -> dict:
    """The repo a workspace-relative path is in: the longest matching checkout path, else the one at "."."""
    nested = [r for r in repos if r["path"] not in (".", "") and path.startswith(r["path"].rstrip("/") + "/")]
    if nested:
        return max(nested, key=lambda r: len(r["path"]))
    return next((r for r in repos if r["path"] in (".", "")), {})


def changed(repo: dict, workspace: pathlib.Path) -> List[str]:
    root = workspace / repo["path"]
    tracked = git(root, "diff", "--name-only", repo["base"], "--").split("\n")
    untracked = git(root, "ls-files", "--others", "--exclude-standard").split("\n")
    ignore = tuple(repo.get("ignore") or ())
    return sorted({f for f in tracked + untracked if f and not f.startswith(ignore)})


def added_lines(repo: dict, workspace: pathlib.Path, rel: str) -> List[str]:
    root = workspace / repo["path"]
    path = root / rel
    try:
        git(root, "cat-file", "-e", f"{repo['base']}:{rel}")
    except subprocess.CalledProcessError:
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    diff = git(root, "diff", "--no-color", "-U0", repo["base"], "--", rel)
    return [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]


_known_hosts: Dict[str, Set[str]] = {}


def known_hosts(root: pathlib.Path, base: str) -> Set[str]:
    """Every http(s) host the repo names at `base`: a fix may reuse one, never add one."""
    key = f"{root.resolve()}@{base}"
    if key not in _known_hosts:
        out = subprocess.run(["git", "-C", str(root), "grep", "-h", "-o", "-I", "-E", r"https?://[A-Za-z0-9.-]+", base],
                             capture_output=True, text=True).stdout
        _known_hosts[key] = {m.group(1).lower() for m in URL_HOST.finditer(out)}
    return _known_hosts[key]


def known_key_vars(root: pathlib.Path, base: str, names: Set[str]) -> Set[str]:
    """Which of `names` the repo already uses as a variable at `base`."""
    return {n for n in names if subprocess.run(["git", "-C", str(root), "grep", "-q", "-F", f"${n}", base],
                                               capture_output=True).returncode == 0
            or subprocess.run(["git", "-C", str(root), "grep", "-q", "-F", "${" + n, base],
                              capture_output=True).returncode == 0}


def unsafe(repo: dict, workspace: pathlib.Path, lines: List[str]) -> List[str]:
    """Rule 6 for one file's added lines: the reasons any of them is refused."""
    hosts = known_hosts(workspace / repo["path"], repo["base"])
    out = []
    for line in lines:
        if SECRET_READ.search(line):
            out.append(f"reads a secret or the environment: {line.strip()[:80]!r}")
        keys = set(KEY_VAR.findall(line))
        if keys - known_key_vars(workspace / repo["path"], repo["base"], keys):
            out.append(f"reads a key variable the repo does not use: {line.strip()[:80]!r}")
        out += [f"names a host the repo does not use ({h}): {line.strip()[:80]!r}"
                for h in {m.group(1).lower() for m in URL_HOST.finditer(line)} - hosts]
    return out


def pages_of(failure: dict) -> Set[str]:
    """The pages a failure's scenario reads, by repo-relative path, before following reuse."""
    step = failure.get("step") or {}
    return {p for p in (failure.get("page"), failure.get("upstream_page"),
                        step.get("file"), step.get("upstream_file")) if p}


def closure(pages: Set[str], roots: List[pathlib.Path]) -> Set[str]:
    """Pages plus every file they reuse, transitively, looked up in each root in turn."""
    seen, todo = set(), list(pages)
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        for root in roots:
            if (root / rel).exists():
                todo.extend(inclusion_targets(root / rel, root) - seen)
                break
    return seen


def check(workspace: pathlib.Path, repos: List[dict], failures: List[dict], summary: dict,
          pending: List[dict]) -> tuple[List[str], List[dict], List[str]]:
    errors: List[str] = []
    rows = summary.get("failures") or []
    by_key: Dict[str, dict] = {}
    for r in rows:
        key = r.get("key")
        if key in by_key:
            errors.append(f"summary lists {key} twice")
        by_key[key] = r
    keys = {f["key"] for f in failures}
    errors += [f"summary row for a failure that is not in the list: {k}" for k in sorted(set(by_key) - keys)]

    for f in failures:
        r = by_key.get(f["key"])
        if r is None:
            errors.append(f"{f['key']}: no row in the summary")
            continue
        if r.get("kind") not in KINDS:
            errors.append(f"{f['key']}: kind must be one of {sorted(KINDS)}, not {r.get('kind')!r}")
        if not (r.get("reason") or "").strip():
            errors.append(f"{f['key']}: needs a reason")
        if r.get("fix") and r.get("kind") not in FIXABLE:
            errors.append(f"{f['key']}: a {r.get('kind')} failure is reported, not fixed")
        if r.get("fix") and not r.get("files"):
            errors.append(f"{f['key']}: marked fixed but lists no files")
        if not r.get("fix") and r.get("files"):
            errors.append(f"{f['key']}: lists files but is not marked fixed")

    # One file can fix several failures: three scenarios that share a broken
    # setup page all list it.
    claimed: Dict[str, List[str]] = {}
    for r in rows:
        for path in r.get("files") or []:
            claimed.setdefault(path, []).append(r.get("key", ""))

    roots = [workspace / r["path"] for r in repos if not r.get("annotations")]
    pending_files = set()
    for repo in repos:
        if repo.get("annotations"):
            for adir in repo["allowed"]:
                pending_files |= {ws(repo, f"{adir.rstrip('/')}/{p['page']}{ANNOTATION_SUFFIX}") for p in pending}

    exempt: Set[str] = set()
    for repo in repos:
        for rel in changed(repo, workspace):
            path = ws(repo, rel)
            if not rel.startswith(tuple(repo["allowed"])):
                errors.append(f"{path}: outside the paths a fix may change in {repo['name']}")
                continue
            errors += [f"{path}: {why}" for why in unsafe(repo, workspace, added_lines(repo, workspace, rel))]
            owners = [f for f in failures if f["key"] in claimed.get(path, [])]
            if not owners:
                if path not in pending_files:
                    errors.append(f"{path}: changed, but no failure lists it")
                continue
            if repo.get("annotations"):
                adir = next(a for a in repo["allowed"] if rel.startswith(a))
                page = rel[len(adir.rstrip("/")) + 1:]
                page = page[: -len(ANNOTATION_SUFFIX)] if page.endswith(ANNOTATION_SUFFIX) else page
                if any(page in closure(pages_of(f), roots) for f in owners):
                    exempt.add(page)
                else:
                    errors.append(f"{path}: annotates {page}, which no failure that lists it runs")
                continue
            lines = added_lines(repo, workspace, rel)
            errors += [f"{path}: adds test markup to a content page: {l.strip()[:80]!r}"
                       for l in lines if TEST_MARKUP.search(l)]
            if repo.get("leak_check"):
                errors += [f"{path}: an added line names the private side: {l.strip()[:80]!r}"
                           for l in lines if LEAK_TERMS.search(l)]

    all_changed = {ws(repo, rel) for repo in repos for rel in changed(repo, workspace)}
    for r in rows:
        files = r.get("files") or []
        for path in files:
            if path not in all_changed:
                errors.append(f"{r.get('key')}: lists {path}, which did not change")
        if any(repo_of(repos, p).get("leak_check") for p in files):
            text = (r.get("public_reason") or "").strip()
            if not text:
                errors.append(f"{r.get('key')}: changes a public repo, so it needs a public_reason")
            elif LEAK_TERMS.search(text):
                errors.append(f"{r.get('key')}: public_reason names the private side: {text[:80]!r}")

    rerun = [f for f in failures if (by_key.get(f["key"]) or {}).get("fix")]
    return errors, rerun, sorted(exempt)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default=".", help="directory the repo paths are relative to")
    ap.add_argument("--repos", required=True, help="JSON file: the checkouts, see above")
    ap.add_argument("--failures", required=True, help="collect_failures.py output the agent worked from")
    ap.add_argument("--summary", required=True, help="the agent's JSON summary")
    ap.add_argument("--pending", help="`annotations.py pending --json` output, for the other fixer's files")
    ap.add_argument("--rerun-out", help="write the failures to re-run here, as JSON")
    ap.add_argument("--exempt-out", help="write the annotation pages edited for a failure here, one per line")
    args = ap.parse_args(argv)

    workspace = pathlib.Path(args.workspace)
    repos = json.loads(pathlib.Path(args.repos).read_text(encoding="utf-8"))
    failures = json.loads(pathlib.Path(args.failures).read_text(encoding="utf-8"))
    pending = json.loads(pathlib.Path(args.pending).read_text(encoding="utf-8")) if args.pending else []
    try:
        summary = json.loads(pathlib.Path(args.summary).read_text(encoding="utf-8"))
        errors, rerun, exempt = check(workspace, repos, failures, summary, pending)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        errors, rerun, exempt = [f"summary: {e}"], [], []
    for e in errors:
        print(f"::error::{e}")
    if errors:
        print(f"{len(errors)} problem(s); nothing may be re-run or published")
        return 1
    if args.rerun_out:
        pathlib.Path(args.rerun_out).write_text(json.dumps(rerun, indent=2) + "\n", encoding="utf-8")
    if args.exempt_out:
        pathlib.Path(args.exempt_out).write_text("".join(f"{p}\n" for p in exempt), encoding="utf-8")
    rows = summary.get("failures") or []
    counts = {k: sum(1 for r in rows if r.get("kind") == k) for k in sorted(KINDS)}
    print(f"ok: {len(rerun)} to re-run; " + ", ".join(f"{v} {k}" for k, v in counts.items() if v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
