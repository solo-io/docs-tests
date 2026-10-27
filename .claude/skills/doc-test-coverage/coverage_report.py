#!/usr/bin/env python3
"""Doc test coverage for every product in products/, as one HTML page.

For each product, takes a snapshot of its content repo's default branch, works
out which pages the product's tests.yaml manifests test (and with which test
types), which they skip on purpose, and which have no test yet. It then adds
the newest scheduled or manual run's results from the content repo's doc-test
workflow, and writes everything into template.html.

Coverage is computed with the runner's own build_test_cases_from_manifests(),
so a page counts as tested here exactly when the runner would test it.

Usage:
    python3 .claude/skills/doc-test-coverage/coverage_report.py \\
        --checkout agentgateway=../agentgateway-oss-website \\
        --checkout agentgateway-enterprise=<path to the enterprise docs checkout> \\
        --checkout kgateway=../kgateway-oss

Every product under products/ needs a --checkout, so a new product can't drop
out of the report unnoticed. Pass --product to report on fewer.

Pass --docs-tests <worktree> to count test definitions on a branch, including
uncommitted ones. Nightly runs only use main, so a scenario that has no result
in the run is marked "not run yet".
"""

from __future__ import annotations

import argparse
import collections
import datetime
import importlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

# Set by use_docs_tests(): the docs-tests checkout whose manifests,
# annotations, and runner scripts the report uses.
DOCS_TESTS: Path
runner: Any
rc: Any


def use_docs_tests(path: Path) -> None:
    global DOCS_TESTS, runner, rc
    DOCS_TESTS = path
    sys.path.insert(0, str(path / "scripts"))
    runner = importlib.import_module("doc_test_run")
    rc = importlib.import_module("reuse_consumers")

# The doc-test workflow in each product's content repo, where it isn't the
# default. The repo itself comes from the checkout's `origin` remote.
DEFAULT_WORKFLOW = "doc-tests.yaml"
WORKFLOWS = {"agentgateway-enterprise": "agentgateway-doc-tests.yml"}

TESTED_TYPES = ("both", "func", "schema")
SOURCE_RE = re.compile(r"^\s*(?:-\s*)?source:", re.MULTILINE)


def run(cmd: list[str], **kw: Any) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw).stdout.strip()


def products() -> list[str]:
    return sorted({p.parent.parent.name for p in DOCS_TESTS.glob("products/*/*/tests.yaml")})


def manifests_for(product: str) -> list[tuple[Path, str, dict]]:
    base = DOCS_TESTS / "products" / product
    return [m for m in runner.load_test_manifests(DOCS_TESTS) if base in m[0].parents]


def repo_slug(checkout: Path) -> str:
    url = run(["git", "-C", str(checkout), "remote", "get-url", "origin"])
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    if not m:
        raise SystemExit(f"{checkout}: origin {url} is not a GitHub remote.")
    return m.group(1)


def snapshot(checkout: Path, manifests: list[tuple[Path, str, dict]], dest: Path) -> dict:
    """Extract the default branch's content (and assets/, when a manifest keys
    on `source:`) into dest. The local branch may be stale or mid-edit, so it is
    never read."""
    run(["git", "-C", str(checkout), "fetch", "-q", "origin"])
    head = run(["git", "-C", str(checkout), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    paths = sorted({str(data.get("content_root") or "content/docs").strip("/") for _, _, data in manifests})
    if any(SOURCE_RE.search(m.read_text(encoding="utf-8")) for m, _, _ in manifests):
        paths.append("assets")
    archive = subprocess.run(
        ["git", "-C", str(checkout), "archive", head, "--", *paths], check=True, capture_output=True
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    return {"ref": head, "sha": run(["git", "-C", str(checkout), "rev-parse", "--short", head])}


def fetch_results(slug: str, workflow: str, branch: str, tmp: Path) -> tuple[dict | None, dict | None]:
    """The newest completed scheduled or manual run on the default branch.

    Some workflows upload one merged doc-test-results artifact; others only
    upload a test-results.yaml per shard. Shards are merged when there is no
    merged artifact."""
    try:
        runs = json.loads(run([
            "gh", "run", "list", "-R", slug, "--workflow", workflow, "--branch", branch, "-L", "30",
            "--json", "databaseId,event,status,conclusion,createdAt,url",
        ]))
    except subprocess.CalledProcessError as exc:
        print(f"  {slug}: could not list {workflow} runs: {exc.stderr.strip()}", file=sys.stderr)
        return None, None
    picked = next(
        (r for r in runs if r["status"] == "completed" and r["event"] in ("schedule", "workflow_dispatch")),
        None,
    )
    if not picked:
        return None, None
    out = tmp / f"results-{picked['databaseId']}"
    rid = str(picked["databaseId"])
    try:
        run(["gh", "run", "download", rid, "-R", slug, "-n", "doc-test-results", "-D", str(out)])
    except subprocess.CalledProcessError:
        try:
            run(["gh", "run", "download", rid, "-R", slug, "-p", "test-result-*", "-D", str(out)])
        except subprocess.CalledProcessError as exc:
            print(f"  {slug} run {rid}: no result artifacts: {exc.stderr.strip()}", file=sys.stderr)
            return None, picked
    merged: dict = {"tests": {}, "skipped_needs_update": {}}
    for f in sorted(out.rglob("test-results.yaml")):
        data = runner.yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        merged["tests"].update(data.get("tests") or {})
        merged["skipped_needs_update"].update(data.get("skipped_needs_update") or {})
    return merged, picked


def section_of(page: str) -> str:
    parts = page.split("/")
    if parts[0] == "documentation" and len(parts) > 2:
        parts = parts[1:]
    return parts[0] if len(parts) > 1 else "(top level)"


def coverage(product: str, root: Path, manifests: list[tuple[Path, str, dict]], results: dict | None) -> list[list]:
    original = runner.load_test_manifests
    runner.load_test_manifests = lambda _root: manifests
    try:
        cases, _claimed, covered = runner.build_test_cases_from_manifests(root, DOCS_TESTS, root / "_gen")
    finally:
        runner.load_test_manifests = original
    covered = set(covered)

    by_page: dict[str, dict[str, set[str]]] = collections.defaultdict(lambda: collections.defaultdict(set))
    for tc in cases:
        by_page[tc.document.relative_to(root).as_posix()][tc.name.split("::")[0]].add(tc.type)

    sources_of: dict[str, set[str]] = collections.defaultdict(set)
    for source, pages in rc.build_reverse_index(root).items():
        for page in pages:
            sources_of[page].add(source)

    ann = DOCS_TESTS / "products" / product / "annotations"
    hidden = {
        f.relative_to(ann).as_posix()[: -len(".yaml")]: f.read_text(encoding="utf-8").count("{{< doc-test")
        for f in ann.rglob("*.md.yaml")
    }

    # page -> (scenario name, test type) -> status. Matching on the type too
    # means a type added to an existing scenario shows as not run yet.
    by_result: dict[str, dict[tuple[str, str], str]] = collections.defaultdict(dict)
    held: dict[str, set[str]] = collections.defaultdict(set)
    if results:
        for key, value in results["tests"].items():
            page_key, name = key.split("::")[:2]
            by_result[page_key][(name, value.get("type", "functional"))] = value.get("status", "")
        for key in results["skipped_needs_update"]:
            page_key, name = key.split("::")[:2]
            held[page_key].add(name)
    # Types the workflow runs at all. Some workflows never run a type (one
    # skips schema), and a type it never runs is not "not run yet".
    run_types = {t for found in by_result.values() for _n, t in found}

    rows = []
    for _mpath, mode, data in manifests:
        content_root = str(data.get("content_root") or "content/docs").strip("/")
        for version in runner.TESTED_VERSIONS:
            base = root / content_root / mode / version
            if not base.is_dir():
                continue
            for path in sorted(base.rglob("*.md")):
                rel = path.relative_to(root).as_posix()
                page = path.relative_to(base).as_posix()
                scenarios = by_page.get(rel, {})
                types = set().union(*scenarios.values()) if scenarios else set()
                if scenarios:
                    # live and credentialed run the page too, so they count
                    # with functional; the Tests column names them.
                    runs = bool(types - {"schema"})
                    status = "both" if runs and "schema" in types else ("func" if runs else "schema")
                else:
                    status = "skip" if rel in covered else "untested"
                ran = by_result.get(rel, {})
                statuses = list(ran.values())
                # A scenario or type the run has no result for was added after
                # the run, or only exists on a docs-tests branch.
                not_run = [(n, t) for n, kinds in scenarios.items() for t in kinds
                           if t in run_types and (n, t) not in ran and n not in held.get(rel, ())]
                if any(s != "passed" for s in statuses):
                    nightly = "fail"
                elif results and not_run:
                    nightly = "notrun"
                elif held.get(rel):
                    nightly = "held"
                else:
                    nightly = "pass" if statuses else ""
                # A rebase shell's blocks, and their checks, live on the
                # upstream page, under the upstream product's annotations.
                rebased = bool(rc.rebase_targets(path))
                checks = None if rebased else hidden.get(rel, 0) + sum(hidden.get(s, 0) for s in sources_of.get(rel, ()))
                rows.append([mode, version, section_of(page), page.removeprefix("documentation/"),
                             status, nightly, checks,
                             [name + "".join(f" ({t})" for t in sorted(kinds - {"schema", "functional"}))
                              for name, kinds in sorted(scenarios.items())]])
    return rows


def summarize(name: str, rows: list[list]) -> None:
    counts = collections.Counter((r[0], r[1], r[4]) for r in rows)
    print(f"\n{name}")
    print(f"  {'tree':24} {'schema+func':>11} {'func':>5} {'schema':>6} {'none':>5} {'skip':>5} "
          f"{'failed':>6} {'not run':>7}")
    for mode, version in sorted({(r[0], r[1]) for r in rows}):
        c = lambda s: counts[(mode, version, s)]  # noqa: E731
        night = collections.Counter(r[5] for r in rows if r[0] == mode and r[1] == version)
        print(f"  {mode + '/' + version:24} {c('both'):>11} {c('func'):>5} {c('schema'):>6} "
              f"{c('untested'):>5} {c('skip'):>5} {night['fail']:>6} {night['notrun']:>7}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--checkout", action="append", default=[], metavar="PRODUCT=PATH",
                        help="Local clone of a product's content repo. Repeat once per product.")
    parser.add_argument("--product", action="append", default=[],
                        help="Report on only these products. Default: every product under products/.")
    parser.add_argument("--docs-tests", default=str(HERE.parents[2]), metavar="PATH",
                        help="docs-tests checkout to read test definitions from, such as a branch worktree. "
                        "Uncommitted changes count. Default: the checkout this script is in.")
    parser.add_argument("--no-results", action="store_true", help="Skip fetching CI results.")
    parser.add_argument("--out", default=str(HERE.parents[2] / "out/coverage/doc-test-coverage.html"))
    args = parser.parse_args()
    use_docs_tests(Path(args.docs_tests).expanduser().resolve())

    checkouts = {}
    for item in args.checkout:
        key, _, path = item.partition("=")
        checkouts[key] = Path(path).expanduser().resolve()
    wanted = args.product or products()
    unknown = sorted(set(wanted) - set(products()))
    if unknown:
        raise SystemExit(f"Not a product under products/: {', '.join(unknown)}")
    missing = [p for p in wanted if p not in checkouts]
    if missing:
        raise SystemExit("Pass --checkout PRODUCT=PATH for: " + ", ".join(missing))

    git = ["git", "-C", str(DOCS_TESTS)]
    dirty = bool(run([*git, "status", "--porcelain", "--", "products"]))
    report: dict[str, Any] = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "docs_tests": {
            "branch": run([*git, "rev-parse", "--abbrev-ref", "HEAD"]),
            "sha": run([*git, "rev-parse", "--short", "HEAD"]),
            "dirty": dirty,
        },
        "products": [],
    }
    print(f"Test definitions: {DOCS_TESTS} ({report['docs_tests']['branch']}"
          f"{', with uncommitted changes' if dirty else ''})")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for product in wanted:
            manifests = manifests_for(product)
            root = tmp / product
            root.mkdir()
            snap = snapshot(checkouts[product], manifests, root)
            slug = repo_slug(checkouts[product])
            results, picked = (None, None)
            if not args.no_results:
                results, picked = fetch_results(
                    slug, WORKFLOWS.get(product, DEFAULT_WORKFLOW), snap["ref"].split("/", 1)[1], tmp)
            rows = coverage(product, root, manifests, results)
            summarize(product, rows)
            report["products"].append({
                "key": product, "repo": slug, "ref": snap["ref"], "sha": snap["sha"],
                "run": picked and {k: picked[k] for k in ("createdAt", "event", "conclusion", "url")},
                "rows": rows,
            })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    template = (HERE / "template.html").read_text(encoding="utf-8")
    payload = json.dumps(report, separators=(",", ":")).replace("</", "<\\/")
    out.write_text(template.replace("__DATA__", payload), encoding="utf-8")
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
