#!/usr/bin/env python3
"""Add a 'schema' doc test for every page that has no test yet and shows
something the schema check can validate.

A schema test needs no markup on the page. `path: all` selects every block, and
the schema check reads every visible block on the scenario's own page anyway,
so the whole test is one manifest entry:

    <name>-schema:
      type: schema
      page: <version-relative page>      # or source: <assets file>
      path: all
      scaffolded: <date>                 # marks a new page for the functional-test drafter

That makes it the one kind of test that can be written without a person:
there is nothing to decide about prerequisites, waits, or assertions. A
functional test still needs one, so a page scaffolded here still counts
toward the functional-test backlog in the coverage report. The `scaffolded:`
date is how list_functional_candidates.py tells a new page from one someone
already decided about; nothing else reads it.

A page is scaffolded when, in a tested version root (latest, main):
  - no manifest scenario or `skip:` entry covers it, and its front matter has
    no `test:` key (the same rule as list_untested_docs.py);
  - and at least one of its visible blocks holds a Kubernetes object
    (`apiVersion` and `kind`), or, on a standalone page with --config-schema,
    a whole standalone config file. Those are the only things the schema check
    validates; a page with neither would fail with "nothing was validated".

The entry is keyed the way the manifest already is: by `source:` when the
manifest uses `source:` and the page is a thin wrapper around one assets file
that no other page in a live root reuses, otherwise by `page:`. A page in both
latest and main gets one entry, which covers both.

New entries go into `scenarios:` in alphabetical order, as text, so the
manifest's comments and layout are untouched. Nothing is validated here: the
site's schema job runs the new scenarios like any other, and a failure there
is a real finding (or a page to add under `skip:`).

Usage, from a docs-tests checkout:

    python3 scripts/scaffold_schema_tests.py --repo ../website --product agentgateway \\
        --config-schema path/to/config.json --report out/scaffold.json --dry-run
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import yaml  # type: ignore[import-not-found]
except ModuleNotFoundError:
    print("PyYAML is required. Install it with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_test_schema_check as SC  # noqa: E402
from doc_test_run import build_test_cases_from_manifests, tested_versions, version_root  # noqa: E402
from reuse_consumers import build_reverse_index  # noqa: E402

DOCS_TESTS_ROOT = Path(__file__).resolve().parent.parent
SCENARIO_KEY = re.compile(r"^  ([A-Za-z0-9][\w.-]*):\s*$")
TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][\w-]*:")
REUSE = re.compile(r'\{\{[<%]\s*reuse\s+"([^"]+)"')
SHORTCODE = re.compile(r"\{\{[<%].*?[>%]\}\}", re.S)
REBASE = re.compile(r"\{\{[<%]\s*rebase\s+file=")


def front_matter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def body_of(text: str) -> str:
    parts = text.split("---", 2)
    return parts[2] if text.startswith("---") and len(parts) >= 3 else text


def checkable(repo: Path, rel: str, version: str, mode: str, config) -> int:
    """How many objects the schema check would validate on this page.

    Reads the page's blocks with the same extractor and options the schema
    check uses, so reuse, rebase shells, and conditional text resolve the same
    way, and classifies each YAML document the same way check_step does.
    """
    extractor = SC._extractor(repo, rel, [{"file": rel, "paths": ["all"]}], version, mode, DOCS_TESTS_ROOT)
    blocks = [b for b in extractor.select_blocks() if not b.hidden and b.language in SC.LANGS]
    # select_blocks keeps shell blocks only; check_step adds the page's other
    # visible blocks (the YAML ones) page-wide, so do the same.
    seen = {(b.file_path, b.start_line) for b in blocks}
    result = extractor.file_cache.get((repo / rel).resolve())
    for b in (result.code_blocks if result else []):
        if not b.hidden and b.language in SC.LANGS and (b.file_path, b.start_line) not in seen:
            seen.add((b.file_path, b.start_line))
            blocks.append(b)
    count = 0
    for block in blocks:
        for doc_text in SC.extract_yaml_documents(block.content):
            try:
                doc = yaml.safe_load(doc_text)
            except yaml.YAMLError:
                continue
            if mode == "standalone" and config is not None and SC.is_config_file(doc, config, doc_text):
                count += 1
            elif isinstance(doc, dict) and "kind" in doc and "apiVersion" in doc:
                count += 1
    return count


def wrapper_source(text: str, root_prefixes: List[str], reverse_index: Dict[str, set]) -> Optional[str]:
    """The assets file a thin wrapper page reuses, if it names the page uniquely
    in every live root (the rule generate_test_manifest.py uses)."""
    body = body_of(text)
    if len(SHORTCODE.sub("", body).strip()) >= 200:
        return None
    found = REUSE.findall(body)
    if len(found) != 1:
        return None
    key = f"assets/{found[0]}"
    for prefix in root_prefixes:
        if len([c for c in reverse_index.get(key, ()) if c.startswith(prefix)]) > 1:
            return None
    return found[0]


def scenario_name(page: str, taken: set) -> str:
    """`<file stem>-schema`, with parent folders added until it is unique.
    An `_index.md` is named after its folder."""
    parts = [p for p in Path(page).with_suffix("").parts if p != "_index"]
    for n in range(1, len(parts) + 1):
        name = "-".join(parts[-n:]) + "-schema"
        name = re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-").lower()
        if name not in taken:
            return name
    raise ValueError(f"no unique scenario name for {page}")


def insert_scenarios(text: str, entries: List[Tuple[str, str]]) -> str:
    """Insert `(name, yaml block)` pairs into the `scenarios:` mapping, each
    before the first existing key that sorts after it."""
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.rstrip() == "scenarios:")
    end = next((i for i in range(start + 1, len(lines)) if TOP_LEVEL_KEY.match(lines[i])), len(lines))
    for name, block in sorted(entries, reverse=True):
        keys = [(i, m.group(1)) for i in range(start + 1, end) for m in [SCENARIO_KEY.match(lines[i])] if m]
        at = next((i for i, k in keys if k > name), None)
        if at is None:   # after the last scenario: before any blank lines that end the section
            at = end
            while at > start + 1 and not lines[at - 1].strip():
                at -= 1
        new = block.rstrip("\n").split("\n")
        lines[at:at] = new
        end += len(new)
    return "\n".join(lines)


def scaffold_manifest(repo: Path, manifest: Path, covered: set, reverse_index: Dict[str, set],
                      config, only: Optional[set]) -> Tuple[List[dict], List[dict]]:
    """(entries to add, pages skipped) for one product/mode manifest."""
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    mode = manifest.parent.name
    content_root = str(data.get("content_root") or "content/docs").strip("/")
    scenarios = data.get("scenarios") or {}
    by_source = any(isinstance(s, dict) and "source" in s for s in scenarios.values())
    roots = [(v, version_root(content_root, mode, v) + "/") for v in tested_versions(data, repo)
             if (repo / version_root(content_root, mode, v)).is_dir()]
    taken = set(scenarios)
    added: Dict[Tuple[str, str], dict] = {}
    skipped: List[dict] = []

    for version, prefix in roots:
        for path in sorted((repo / prefix).rglob("*.md")):
            rel = path.relative_to(repo).as_posix()
            if only and rel not in only:
                continue
            if path.resolve() in covered:
                continue
            text = path.read_text(encoding="utf-8")
            fm = front_matter(text)
            if "test" in fm or fm.get("draft") is True:
                continue
            page = rel[len(prefix):]
            source = wrapper_source(text, [p for _, p in roots], reverse_index) if by_source else None
            key = ("source", source) if source else ("page", page)
            if key in added:
                continue
            try:
                n = checkable(repo, rel, version, mode, config)
            except Exception as exc:  # one unreadable page must not stop the rest
                skipped.append({"page": rel, "reason": f"could not be read: {exc}"})
                continue
            if not n:
                reason = "rebase shell; pass --upstream-root" if REBASE.search(text) and not SC._UPSTREAM.get("root") \
                    else "nothing the schema check validates"
                skipped.append({"page": rel, "reason": reason})
                continue
            name = scenario_name(page, taken)
            taken.add(name)
            added[key] = {"manifest": manifest.relative_to(DOCS_TESTS_ROOT).as_posix(), "name": name,
                          "page": rel, "objects": n}
            added[key]["source" if source else "page_key"] = key[1]
    return list(added.values()), skipped


def entry_yaml(e: dict, date: str) -> str:
    ref = f"    source: {e['source']}" if "source" in e else f"    page: {e['page_key']}"
    return f"  {e['name']}:\n    type: schema\n{ref}\n    path: all\n    scaffolded: \"{date}\"\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="content repo checkout")
    ap.add_argument("--product", required=True, help="products/<product> in this repo")
    ap.add_argument("--mode", action="append", default=[], help="only this mode's manifest (repeatable)")
    ap.add_argument("--page", action="append", default=[], help="only this repo-relative page (repeatable)")
    ap.add_argument("--config-schema", help="standalone config.json; without it, standalone pages count "
                                            "only their Kubernetes objects")
    ap.add_argument("--upstream-root", help="checkout a {{< rebase >}} shell expands from, as in the schema check")
    ap.add_argument("--upstream-versions", help="JSON version map, required with --upstream-root")
    ap.add_argument("--condition", action="append", default=[], help="conditional-text gate to match (repeatable)")
    ap.add_argument("--limit", type=int, default=0, help="add at most this many scenarios (0: no limit)")
    ap.add_argument("--report", help="write the added and skipped pages here as JSON")
    ap.add_argument("--dry-run", action="store_true", help="report only; leave the manifests unchanged")
    ap.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    help="the `scaffolded:` date to write (default: today, UTC)")
    ap.add_argument("--docs-tests-root", help="docs-tests checkout whose manifests to read and change "
                                              "(default: the one this script is in)")
    args = ap.parse_args(argv)

    global DOCS_TESTS_ROOT
    if args.docs_tests_root:
        DOCS_TESTS_ROOT = Path(args.docs_tests_root).resolve()
    repo = Path(args.repo).resolve()
    manifests = sorted((DOCS_TESTS_ROOT / "products" / args.product).glob("*/tests.yaml"))
    if args.mode:
        manifests = [m for m in manifests if m.parent.name in args.mode]
    if not manifests:
        print(f"no manifests under products/{args.product}", file=sys.stderr)
        return 1

    SC._UPSTREAM.clear()
    SC._UPSTREAM["conditions"] = args.condition
    if args.upstream_root:
        if not args.upstream_versions:
            print("--upstream-root needs --upstream-versions", file=sys.stderr)
            return 1
        SC._UPSTREAM["root"] = Path(args.upstream_root).resolve()
        SC._UPSTREAM["versions"] = json.loads(args.upstream_versions)
    config = SC.load_config_schema(Path(args.config_schema)) if args.config_schema else None

    _, _, tested = build_test_cases_from_manifests(repo, DOCS_TESTS_ROOT, repo / "out")
    covered = {(repo / d).resolve() for d in tested}
    reverse_index = build_reverse_index(repo)
    only = set(args.page) or None

    report = {"added": [], "skipped": []}
    for manifest in manifests:
        added, skipped = scaffold_manifest(repo, manifest, covered, reverse_index, config, only)
        if args.limit:
            added = added[: max(0, args.limit - len(report["added"]))]
        report["added"].extend(added)
        report["skipped"].extend(skipped)
        if added and not args.dry_run:
            text = manifest.read_text(encoding="utf-8")
            manifest.write_text(insert_scenarios(text, [(e["name"], entry_yaml(e, args.date)) for e in added]), encoding="utf-8")
        for e in added:
            print(f"{'would add' if args.dry_run else 'added'} {e['name']} ({e['objects']} object(s)): {e['page']}")

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{len(report['added'])} scenario(s) {'to add' if args.dry_run else 'added'}, "
          f"{len(report['skipped'])} untested page(s) with nothing to schema-check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
