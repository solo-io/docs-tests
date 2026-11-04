#!/usr/bin/env python3
"""List new pages that could get a functional doc test, for the fixer agent.

A candidate is a scenario that:
  - scaffold_schema_tests.py added (it has a `scaffolded:` date), so the page
    is new and nobody has decided about a functional test for it yet;
  - is still `type: schema` only;
  - has no `functional:` key, the reason someone (or the agent) recorded for
    keeping it schema only;
  - and is reported `could-be-functional` by doc_test_type_check.py: the page
    has commands to run, in every version root it is in.

Older pages are left out on purpose. A schema-only page from before the
scaffolder may be schema only by a decision nobody wrote down, and the agent
would reconsider it every night. Give such a page a `functional:` reason, or
a `scaffolded:` date to put it in the queue.

The output has the shape of collect_failures.py's, so the fixer workflow adds
the rows to its failure list and every check that holds the agent to a
failure holds it to a candidate too:

    [{"suite": "<product>", "key": "<page>::<scenario>::functional",
      "page": "<content page>", "upstream_page": "<page a rebase shell pulls in, if any>",
      "test": "<scenario>", "type": "functional",
      "candidate": {"manifest": "products/<product>/<mode>/tests.yaml",
                    "scenario": "<scenario>", "scaffolded": "<date>",
                    "entry": {<the scenario's manifest entry, as it is now>}}}]

Oldest `scaffolded:` first, at most --max per product.

Run it like the type check: from a docs-tests checkout, against the content
repo with the product's annotations attached.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_test_schema_check  # noqa: E402
import doc_test_type_check as TC  # noqa: E402
from doc_test_run import TESTED_VERSIONS, _resolve_manifest_ref, load_test_manifests  # noqa: E402
from doc_test_extract import resolve_docs_tests_root  # noqa: E402
from reuse_consumers import build_reverse_index  # noqa: E402
from collect_failures import upstream_of  # noqa: E402


def candidates(repo_root: Path, docs_tests_root: Path, product: str, reverse_index, config_validator=None):
    rows = []
    for mpath, mode, data in load_test_manifests(docs_tests_root):
        if mpath.parent.parent.name != product:
            continue
        scenarios = data.get("scenarios") or {}
        queued = {k for k, e in scenarios.items()
                  if isinstance(e, dict) and e.get("scaffolded") and "functional" not in e
                  and TC._types(e) == ["schema"]}
        if not queued:
            continue
        content_root = str(data.get("content_root") or "content/docs").strip("/")
        # The type check judges every scenario; only the queued ones matter here.
        only = dict(data, scenarios={k: scenarios[k] for k in queued}, skip=[])
        for f in TC.check_manifest(repo_root, mpath, mode, only, reverse_index, docs_tests_root, config_validator):
            if f.kind != "could-be-functional" or f.scenario not in queued:
                continue
            page = None
            for version in TESTED_VERSIONS:
                if version not in f.versions:
                    continue
                page = _resolve_manifest_ref(f.ref, mode, version, reverse_index, mpath, f.scenario, content_root)
                if page:
                    break
            if not page:
                continue
            row = {"suite": product, "key": f"{page}::{f.scenario}::functional", "page": page}
            up = doc_test_schema_check._UPSTREAM
            if up.get("root"):
                upstream = upstream_of(page, repo_root, up.get("versions") or {}, up["root"])
                if upstream:
                    row["upstream_page"] = upstream
            rows.append({**row, "test": f.scenario, "type": "functional",
                         "candidate": {"manifest": f.manifest, "scenario": f.scenario,
                                       "scaffolded": str(scenarios[f.scenario]["scaffolded"]),
                                       # The entry as it is now: every attempt's edit starts from it.
                                       "entry": {k: (str(v) if k == "scaffolded" else v)
                                                 for k, v in scenarios[f.scenario].items()}}})
    return sorted(rows, key=lambda r: (r["candidate"]["scaffolded"], r["key"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-root", default=".", help="content repo, with annotations attached")
    ap.add_argument("--docs-tests-root", default=None)
    ap.add_argument("--product", required=True, help="products/<product>")
    ap.add_argument("--upstream-root", help="where {{< rebase >}} shells' content comes from")
    ap.add_argument("--upstream-versions", help="JSON version map, required with --upstream-root")
    ap.add_argument("--condition", action="append", default=[], metavar="NAME")
    ap.add_argument("--config-schema", help="standalone config.json, as in the type check")
    ap.add_argument("--max", type=int, default=3, help="at most this many candidates (default 3)")
    ap.add_argument("--out", required=True, help="write the candidates here as JSON")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s", stream=sys.stderr)

    repo_root = Path(args.repo_root).resolve()
    docs_tests_root = resolve_docs_tests_root(args.docs_tests_root)
    doc_test_schema_check._UPSTREAM.clear()
    doc_test_schema_check._UPSTREAM["conditions"] = args.condition
    if args.upstream_root:
        if not args.upstream_versions:
            ap.error("--upstream-root needs --upstream-versions")
        doc_test_schema_check._UPSTREAM["root"] = Path(args.upstream_root).resolve()
        doc_test_schema_check._UPSTREAM["versions"] = json.loads(args.upstream_versions)
    config = doc_test_schema_check.load_config_schema(Path(args.config_schema)) if args.config_schema else None

    rows = candidates(repo_root, docs_tests_root, args.product, build_reverse_index(repo_root), config)
    picked = rows[: max(0, args.max)]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(picked, indent=2) + "\n", encoding="utf-8")
    for r in picked:
        print(f"{r['key']} (scaffolded {r['candidate']['scaffolded']})")
    print(f"{len(picked)} candidate(s) of {len(rows)} for {args.product}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
