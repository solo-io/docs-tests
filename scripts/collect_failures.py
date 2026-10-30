#!/usr/bin/env python3
"""List a nightly run's failed doc tests as work for the failure fixer.

The fixer (see "Fixing a failing doc test" in the README) reads this list, not
the raw results, so that every agent run starts from the same short record of
each failure: the scenario, the step that broke, and the pages to read.

    python3 scripts/collect_failures.py --results merged.yaml --suite agentgateway \
        --run-url https://github.com/... --out failures.json

For a consumer whose pages are rebase shells of an upstream page (the docs
hub's agentgateway pages), pass --consumer-root, --upstream-root and
--upstream-versions, the same values doc_test_run.py takes. Each page is then
also resolved to the upstream page it rebases, under `upstream_page`, because
that page, not the shell, is what a content fix edits.

Only `functional` and `live` failures are listed. A `schema` failure has no
cluster step to re-run, and the fixer only publishes a fix it has re-run.

Each entry:

    {"suite": "agentgateway", "key": "<page>::<test>[::<type>]",
     "page": ..., "test": ..., "type": "functional",
     "upstream_page": ... or absent,
     "step": {"kind": ..., "file": ..., "line": ..., "command": ...,
              "upstream_file": ... or absent} or null,
     "output": "<the last OUTPUT_CHARS characters of the step's output or error>",
     "run_url": ...}
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from reuse_consumers import rebase_targets, resolve_rebase_target  # noqa: E402

# Enough to see the error and the commands just before it, small enough that a
# dozen failures fit in an agent's first read.
OUTPUT_CHARS = 4000
FIXABLE_TYPES = {"functional"}


def split_key(key: str) -> tuple[str, str, str]:
    """`<page>::<test>[::<type>]` -> (page, test, type or "")."""
    parts = key.split("::")
    page, test = parts[0], parts[1] if len(parts) > 1 else ""
    return page, test, parts[2] if len(parts) > 2 else ""


def upstream_of(page: str, consumer_root: pathlib.Path | None, versions: dict[str, str],
                upstream_root: pathlib.Path | None) -> str | None:
    """The upstream page a consumer rebase shell pulls in, if it is one and the page exists."""
    if consumer_root is None:
        return None
    targets = rebase_targets(consumer_root / page)
    for value in sorted(targets):
        up = resolve_rebase_target(value, page, versions)
        if up and (upstream_root is None or (upstream_root / up).exists()):
            return up
    return None


def collect(results: dict, suite: str, run_url: str = "", consumer_root: pathlib.Path | None = None,
            upstream_root: pathlib.Path | None = None, versions: dict[str, str] | None = None) -> list[dict]:
    versions = versions or {}
    out = []
    for key, r in sorted((results.get("tests") or {}).items()):
        if r.get("status") == "passed":
            continue
        page, test, _ = split_key(key)
        kind = r.get("type") or "functional"
        if kind not in FIXABLE_TYPES:
            continue
        entry: dict = {"suite": suite, "key": key, "page": page, "test": test, "type": kind,
                       "status": r.get("status"), "run_url": run_url}
        up = upstream_of(page, consumer_root, versions, upstream_root)
        if up:
            entry["upstream_page"] = up
        fs = r.get("failed_step")
        if fs:
            step = {k: fs.get(k) for k in ("kind", "file", "line", "command")}
            if fs.get("file") and fs["file"] != page:
                up_step = upstream_of(fs["file"], consumer_root, versions, upstream_root)
                if up_step:
                    step["upstream_file"] = up_step
            elif up:
                step["upstream_file"] = up
            entry["step"] = step
            output = fs.get("output_tail") or r.get("error") or ""
        else:
            entry["step"] = None
            output = r.get("error") or ""
        entry["output"] = str(output)[-OUTPUT_CHARS:]
        out.append(entry)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", required=True, help="merged test-results.yaml")
    ap.add_argument("--suite", required=True, help="product the results are for, e.g. agentgateway")
    ap.add_argument("--run-url", default="", help="the run the results came from")
    ap.add_argument("--consumer-root", help="checkout of the repo whose pages the results name")
    ap.add_argument("--upstream-root", help="checkout of the repo its rebase shells pull from")
    ap.add_argument("--upstream-versions", default="{}", help="JSON map of consumer version -> upstream version dir")
    ap.add_argument("--max", type=int, default=0, help="list at most this many (0 = all)")
    ap.add_argument("--out", required=True, help="write the JSON list here")
    args = ap.parse_args(argv)

    results = yaml.safe_load(pathlib.Path(args.results).read_text(encoding="utf-8")) or {}
    failures = collect(
        results, args.suite, args.run_url,
        pathlib.Path(args.consumer_root) if args.consumer_root else None,
        pathlib.Path(args.upstream_root) if args.upstream_root else None,
        json.loads(args.upstream_versions),
    )
    if args.max and len(failures) > args.max:
        print(f"{len(failures)} failures; listing the first {args.max}, the rest wait for a later run")
        failures = failures[: args.max]
    pathlib.Path(args.out).write_text(json.dumps(failures, indent=2) + "\n", encoding="utf-8")
    for f in failures:
        step = f.get("step") or {}
        print(f"{f['key']}: {step.get('kind', '?')} step at {step.get('file', '?')}:{step.get('line', '?')}")
    print(f"{len(failures)} failure(s) to look at")
    return 0


if __name__ == "__main__":
    sys.exit(main())
