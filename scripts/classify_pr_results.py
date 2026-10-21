#!/usr/bin/env python3
"""Turn a pull request's doc test results into one plain verdict per scenario.

WHY THIS EXISTS

A contributor who edits a page cannot tell from a red doc test whether they
broke something or a test no longer matches the docs, and should not have to.
Most failures on a content PR are not theirs: a check docs-tests added that is
out of date, a step on a page they did not touch, or a scenario that already
fails on main. Reporting those as failures makes a good PR look broken.

So there are only two outcomes a contributor sees besides a pass:

  failure   The failing step is a code block (`page`, not a docs-tests
            `check`) on a page this PR affects. The message names the step.
  skipped   Anything else. Titled "Test update required" and worded so the
            contributor knows they need to do nothing. The docs team sees it
            in the nightly results, so nothing else is notified.

The page/check split comes from `failed_step` in the results, which the runner
records from the step markers in the generated script.

KNOWN LIMIT

A change can break a page's behavior while every command on it still
succeeds, so only a docs-tests check fails. That is reported as "Test update
required", not as the contributor's failure. The nightly run on main catches
it after merge. Blaming every failed check on the PR instead would put the far
more common stale-test case on the contributor, which is what this replaces.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

FAILURE = "failure"
SKIPPED = "skipped"
SUCCESS = "success"

UPDATE_REQUIRED_TITLE = "Test update required"
UPDATE_REQUIRED_SUMMARY = (
    "This is not caused by your change, and you do not need to do anything.\n\n"
    "This doc test needs an update. The docs team sees it in the nightly test results."
)


def _read_lines(path: str | None) -> set[str] | None:
    if not path:
        return None
    return {line.strip() for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if line.strip()}


def _fence(text: str) -> str:
    # Longer than any backtick run in the text, so command output cannot close it.
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}\n{text}\n{ticks}"


def verdict(key: str, result: dict | None, affected: set[str] | None, known: set[str]) -> dict:
    """One scenario's conclusion, title, and summary.

    ``affected`` is the content pages the PR reaches, from reuse_consumers.py.
    None means the PR changed something that reaches every page (the site
    config, the workflow), so every page counts as affected.
    """
    if key in known:
        return {"conclusion": SKIPPED, "title": UPDATE_REQUIRED_TITLE, "summary": UPDATE_REQUIRED_SUMMARY}
    if result is None:
        # The job produced nothing to read: a runner fault, not the page.
        return {"conclusion": SKIPPED, "title": UPDATE_REQUIRED_TITLE, "summary": UPDATE_REQUIRED_SUMMARY}
    if result.get("status") == "passed":
        return {"conclusion": SUCCESS, "title": "Passed", "summary": "Every step in this guide worked."}

    step = result.get("failed_step") or {}
    on_affected_page = affected is None or step.get("file") in affected
    if step.get("kind") == "page" and on_affected_page:
        page = step["file"]
        # No line number: it counts lines in the page as rendered, after reuse
        # and rebase are expanded, which matches no file the contributor
        # edits. The command is what they can search for.
        parts = [
            f"This step in `{page}` failed. "
            "Find it on the page, fix it, then push the fix.",
        ]
        if step.get("command"):
            parts += ["", "**Step**", "", _fence(step["command"])]
        if step.get("output_tail"):
            parts += ["", "**Output**", "", _fence(step["output_tail"])]
        return {
            "conclusion": FAILURE,
            "title": f"Your change broke a step in {pathlib.PurePosixPath(page).name}",
            "summary": "\n".join(parts),
        }
    return {"conclusion": SKIPPED, "title": UPDATE_REQUIRED_TITLE, "summary": UPDATE_REQUIRED_SUMMARY}


def classify(expected: list[dict], results: dict, affected: set[str] | None, known: set[str]) -> list[dict]:
    """``expected`` is every scenario the PR selected, run or not: {file, test}."""
    names = [e["test"] for e in expected]
    out = []
    for entry in expected:
        key = f"{entry['file']}::{entry['test']}"
        name = entry["test"]
        if names.count(name) > 1:
            # The same scenario name on two pages; the check names must differ.
            name = f"{name} ({pathlib.PurePosixPath(entry['file']).stem})"
        out.append({"key": key, "name": name, **verdict(key, results.get(key), affected, known)})
    return out


def summary_markdown(verdicts: list[dict]) -> str:
    icon = {SUCCESS: "✅", FAILURE: "❌", SKIPPED: "⏭️"}
    counts = {c: sum(1 for v in verdicts if v["conclusion"] == c) for c in (SUCCESS, FAILURE, SKIPPED)}
    lines = [
        f"## Doc tests: {counts[SUCCESS]} passed, {counts[FAILURE]} broken by this change, "
        f"{counts[SKIPPED]} need a test update",
        "",
        "| Result | Scenario | |",
        "| :---: | --- | --- |",
    ]
    order = {FAILURE: 0, SKIPPED: 1, SUCCESS: 2}
    for v in sorted(verdicts, key=lambda v: (order[v["conclusion"]], v["name"])):
        lines.append(f"| {icon[v['conclusion']]} | `{v['name']}` | {v['title']} |")
    if counts[SKIPPED]:
        lines += ["", f"⏭️ {UPDATE_REQUIRED_SUMMARY}"]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", required=True, help="merged test-results.yaml from the PR run")
    parser.add_argument("--expected", required=True, help="JSON list of {file, test} the PR selected")
    parser.add_argument("--affected-pages", help="content pages the PR reaches, one per line; omit for all")
    parser.add_argument("--known-failing", help="result keys (file::test) failing on main, one per line")
    parser.add_argument("--output", required=True, help="where to write the verdicts as JSON")
    parser.add_argument("--summary", help="append a Markdown table here (e.g. $GITHUB_STEP_SUMMARY)")
    args = parser.parse_args()
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")

    results_path = pathlib.Path(args.results)
    report = (yaml.safe_load(results_path.read_text(encoding="utf-8")) or {}) if results_path.exists() else {}
    expected = json.loads(pathlib.Path(args.expected).read_text(encoding="utf-8"))
    verdicts = classify(
        expected, report.get("tests") or {},
        _read_lines(args.affected_pages), _read_lines(args.known_failing) or set(),
    )
    pathlib.Path(args.output).write_text(json.dumps(verdicts, indent=2), encoding="utf-8")
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(summary_markdown(verdicts))
    for v in verdicts:
        print(f"{v['conclusion']:8} {v['name']}: {v['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
