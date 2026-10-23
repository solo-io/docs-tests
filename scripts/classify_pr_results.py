#!/usr/bin/env python3
"""Turn a pull request's doc test results into one plain verdict per scenario.

WHY THIS EXISTS

A contributor who edits a page cannot tell from a red doc test whether they
broke something or a test no longer matches the docs, and should not have to.
Most failures on a content PR are not theirs: a check docs-tests added that is
out of date, a step on a page they did not touch, or a scenario that already
fails on main. Reporting those as failures makes a good PR look broken.

So there are only two outcomes a contributor sees besides a pass:

  failure   The failing step is the page's own on a page this PR affects:
            a code block a reader sees (`page`), or a hidden block that runs
            the page's commands in a form CI can (`setup`). The message names
            the step.
  skipped   Anything else, including a scenario that already fails in the
            nightly run on main AT THE SAME STEP. Titled "Test update
            required" and worded so the contributor knows they need to do
            nothing.

A scenario that fails on main still runs on the PR. If it passes, the PR fixed
it, and it says so. If it fails at a different step from the nightly, on a page
the PR affects, the PR broke that step, and it is the PR's like any other
failure: "fails on main" says only that something else on that scenario was
already broken. Without the nightly's steps (--known-failing alone), every
failure of a known-failing scenario is "Test update required". The docs team sees it
            in the nightly results, so nothing else is notified.

A failed `assert` (a check docs-tests added) is never the contributor's. The
split comes from `failed_step` in the results, which the runner records from
the step markers in the generated script.

On the pull request these become three checks, one per group, so the result
reads at a glance: "Doc tests: needs your fix", "Doc tests: test update
required", and "Doc tests: passed". See `group_checks`.

KNOWN LIMIT

A change can break a page's behavior while every command on it still
succeeds, so only a docs-tests assertion fails. That is reported as "Test update
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

# Step kinds whose failure is the page's. A `check` (results written before
# setup and assert were told apart) is treated as an assert.
PAGE_KINDS = ("page", "setup")

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


def _step_id(step: dict | None) -> tuple | None:
    """What makes two failures the same failure: the step, not its line, which moves as pages change."""
    if not step:
        return None
    return (step.get("kind"), step.get("file"), (step.get("command") or "").strip())


def verdict(key: str, result: dict | None, affected: set[str] | None, known: set[str],
            known_steps: dict[str, dict | None] | None = None) -> dict:
    """One scenario's conclusion, title, and summary.

    ``affected`` is the content pages the PR reaches, from reuse_consumers.py.
    None means the PR changed something that reaches every page (the site
    config, the workflow), so every page counts as affected.

    ``known`` is the scenarios failing in the nightly run on main;
    ``known_steps`` maps each to the step it failed at there (None when it
    failed before any step), when the nightly's results are available.
    """
    if key in known:
        # A PR that fixes the page or its test shows the pass.
        if result is not None and result.get("status") == "passed":
            return {"conclusion": SUCCESS, "title": "Passed (fails on main)",
                    "summary": "Every step in this guide worked. It fails in the nightly run on main, "
                               "so this change fixes it."}
        # The same failure as on main is not this PR's. A failure somewhere
        # else is judged like any other: on the day every nightly scenario
        # broke at install, a PR that broke a page step was otherwise blamed
        # for nothing.
        ours = (result or {}).get("failed_step")
        if known_steps is None or key not in known_steps or ours is None \
                or _step_id(ours) == _step_id(known_steps[key]):
            return {"conclusion": SKIPPED, "title": UPDATE_REQUIRED_TITLE, "summary": UPDATE_REQUIRED_SUMMARY}
    if result is None:
        # The job produced nothing to read: a runner fault, not the page.
        return {"conclusion": SKIPPED, "title": UPDATE_REQUIRED_TITLE, "summary": UPDATE_REQUIRED_SUMMARY}
    if result.get("status") == "passed":
        return {"conclusion": SUCCESS, "title": "Passed", "summary": "Every step in this guide worked."}

    step = result.get("failed_step") or {}
    on_affected_page = affected is None or step.get("file") in affected
    if step.get("kind") in PAGE_KINDS and on_affected_page:
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


def classify(expected: list[dict], results: dict, affected: set[str] | None, known: set[str],
             known_steps: dict[str, dict | None] | None = None) -> list[dict]:
    """``expected`` is every scenario the PR selected, run or not: {file, test}."""
    names = [e["test"] for e in expected]
    out = []
    for entry in expected:
        key = f"{entry['file']}::{entry['test']}"
        name = entry["test"]
        if names.count(name) > 1:
            # The same scenario name on two pages; the check names must differ.
            name = f"{name} ({pathlib.PurePosixPath(entry['file']).stem})"
        out.append({"key": key, "name": name, **verdict(key, results.get(key), affected, known, known_steps)})
    return out


# Fixed names, so a rerun on the same commit replaces each check rather than
# leaving an old group behind under a name with a different count in it.
GROUP_NEEDS_FIX = "Doc tests: needs your fix"
GROUP_UPDATE = "Doc tests: test update required"
GROUP_PASSED = "Doc tests: passed"

# GitHub's limit on a check's summary.
SUMMARY_LIMIT = 65535


def _plural(n: int) -> str:
    return f"{n} scenario" + ("" if n == 1 else "s")


def _cap(text: str) -> str:
    if len(text) <= SUMMARY_LIMIT:
        return text
    note = "\n\n(Cut short. The workflow run's summary has the rest.)"
    return text[: SUMMARY_LIMIT - len(note)] + note


def group_checks(verdicts: list[dict]) -> list[dict]:
    """The three checks posted on the PR, always all three.

    An empty group is posted as passed, so the list never keeps a stale red
    or skipped entry from an earlier run on the same commit.
    """
    by = {c: sorted((v for v in verdicts if v["conclusion"] == c), key=lambda v: v["name"])
          for c in (FAILURE, SKIPPED, SUCCESS)}
    broken, update, passed = by[FAILURE], by[SKIPPED], by[SUCCESS]

    fix_summary = "\n\n".join(f"### `{v['name']}`\n\n{v['summary']}" for v in broken)
    update_list = "\n".join(f"- `{v['name']}`" for v in update)
    passed_list = "\n".join(f"- `{v['name']}`" for v in passed)
    return [
        {
            "name": GROUP_NEEDS_FIX,
            "conclusion": FAILURE if broken else SUCCESS,
            "title": f"{_plural(len(broken))} broken by this change" if broken else "Nothing to fix",
            "summary": _cap(fix_summary) if broken else "No step in a guide this PR changes broke.",
        },
        {
            "name": GROUP_UPDATE,
            "conclusion": SKIPPED if update else SUCCESS,
            "title": f"{_plural(len(update))}: no action needed" if update else "None",
            "summary": _cap(f"{UPDATE_REQUIRED_SUMMARY}\n\n{update_list}") if update else "No doc test needs an update.",
        },
        {
            "name": GROUP_PASSED,
            "conclusion": SUCCESS,
            "title": f"{_plural(len(passed))} passed",
            "summary": _cap(passed_list) if passed else "No scenario passed.",
        },
    ]


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
    parser.add_argument("--known-results",
                        help="merged test-results.yaml of the nightly run on main; its failures count as known, "
                             "and a failure at a different step from the nightly's is judged like any other")
    parser.add_argument("--output", required=True, help="where to write the verdicts as JSON")
    parser.add_argument("--groups", help="where to write the three group checks as JSON")
    parser.add_argument("--summary", help="append a Markdown table here (e.g. $GITHUB_STEP_SUMMARY)")
    args = parser.parse_args()
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")

    results_path = pathlib.Path(args.results)
    report = (yaml.safe_load(results_path.read_text(encoding="utf-8")) or {}) if results_path.exists() else {}
    expected = json.loads(pathlib.Path(args.expected).read_text(encoding="utf-8"))
    known = _read_lines(args.known_failing) or set()
    known_steps = None
    if args.known_results and pathlib.Path(args.known_results).exists():
        nightly = (yaml.safe_load(pathlib.Path(args.known_results).read_text(encoding="utf-8")) or {}).get("tests") or {}
        known_steps = {k: v.get("failed_step") for k, v in nightly.items() if v.get("status") != "passed"}
        known |= set(known_steps)
    verdicts = classify(
        expected, report.get("tests") or {},
        _read_lines(args.affected_pages), known, known_steps,
    )
    pathlib.Path(args.output).write_text(json.dumps(verdicts, indent=2), encoding="utf-8")
    if args.groups:
        pathlib.Path(args.groups).write_text(json.dumps(group_checks(verdicts), indent=2), encoding="utf-8")
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as fh:
            fh.write(summary_markdown(verdicts))
    for v in verdicts:
        print(f"{v['conclusion']:8} {v['name']}: {v['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
