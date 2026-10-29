#!/usr/bin/env python3
"""
Inject test status metadata into markdown front matter based on test-results.yaml.

This script reads the test results file and updates the front matter of each
tested document with a `test_status` field indicating whether all tests passed.
A page that passed also gets `test_types`, the kinds of test that ran on it
(schema, functional), which the Verified badge's tooltip
lists. The kinds come from the results, never from tests.yaml: a scenario that
declares `[schema, functional]` but ran only `functional` claims only that.

Usage:
    python3 scripts/inject_test_status.py [--results-file PATH] [--only PATH ...] [--dry-run]

`--only` limits injection to the named documents (repeatable). Use it to roll
the Verified badge out one page at a time: every other tested document is left
untouched, whatever its result.
"""

import argparse
import re
from pathlib import Path
from typing import Dict, List, Optional

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


def load_test_results(results_path: Path) -> Dict:
    """Load test results from YAML file."""
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")
    
    if not results_path.exists():
        print(f"Warning: Test results file not found: {results_path}")
        return {"tested_documents": [], "tests": {}}
    
    return yaml.safe_load(results_path.read_text(encoding="utf-8")) or {}


def compute_document_status(doc_path: str, tests: Dict, skipped: Optional[Dict] = None) -> Optional[str]:
    """
    Compute the overall test status for a document.
    
    Returns:
        "failed" if any test failed
        "partial" if none failed but a scenario was skipped because its test
            annotations did not attach (`skipped_needs_update` in the results)
        "passed" if all tests for this document passed and none were skipped
        None if no tests exist for this document
    
    Only "passed" shows the Verified badge, so a page with an untested example
    loses the badge until its annotations are fixed.
    """
    prefix = f"{doc_path}::"
    doc_tests = {k: v for k, v in tests.items() if k.startswith(prefix)}
    has_skipped = any(k.startswith(prefix) for k in (skipped or {}))
    
    if not doc_tests and not has_skipped:
        return None
    
    if any(t.get("status") != "passed" for t in doc_tests.values()):
        return "failed"
    return "partial" if has_skipped else "passed"


# Badge order for the known kinds, cheapest check first. An unknown kind sorts
# after these, alphabetically; the badge partial skips any kind it has no
# wording for, so a new kind never shows its raw name to readers.
TYPE_ORDER = ["schema", "functional"]


def compute_document_types(doc_path: str, tests: Dict) -> List[str]:
    """
    The kinds of test that ran for a document, in TYPE_ORDER.

    A result with no `type` is "functional", the runner's default for a
    scenario that declares none (results written before the field existed).
    """
    prefix = f"{doc_path}::"
    kinds = {
        (v or {}).get("type") or "functional"
        for k, v in tests.items()
        if k.startswith(prefix)
    }
    known = [t for t in TYPE_ORDER if t in kinds]
    return known + sorted(kinds - set(TYPE_ORDER))


def parse_front_matter(content: str) -> tuple[Optional[str], Optional[Dict], str]:
    """
    Parse YAML front matter from markdown content.
    
    Returns:
        Tuple of (raw_front_matter, parsed_dict, body_content)
    """
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")
    
    match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not match:
        return None, None, content
    
    raw_fm = match.group(1)
    body = content[match.end():]
    
    try:
        parsed = yaml.safe_load(raw_fm)
        if not isinstance(parsed, dict):
            parsed = {}
    except yaml.YAMLError:
        parsed = {}
    
    return raw_fm, parsed, body


def update_front_matter(
    content: str, test_status: Optional[str], test_types: Optional[List[str]] = None
) -> str:
    """
    Update the front matter with test_status field.

    If test_status is None, removes any existing test_status field.
    `test_types` is written only alongside `passed`, and removed otherwise, so
    a page that stops passing can't keep a list of checks it no longer passes.
    """
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install it with: pip install pyyaml")
    
    raw_fm, parsed_fm, body = parse_front_matter(content)
    
    if raw_fm is None or parsed_fm is None:
        return content
    
    if test_status is None:
        if "test_status" in parsed_fm:
            del parsed_fm["test_status"]
    else:
        parsed_fm["test_status"] = test_status

    if test_status == "passed" and test_types:
        parsed_fm["test_types"] = list(test_types)
    else:
        parsed_fm.pop("test_types", None)

    new_fm = yaml.safe_dump(parsed_fm, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000)
    new_fm = new_fm.rstrip("\n")
    
    return f"---\n{new_fm}\n---\n{body}"


def process_documents(
    repo_root: Path,
    results: Dict,
    dry_run: bool = False,
    verbose: bool = True,
    only: Optional[List[str]] = None,
) -> Dict[str, str]:
    """
    Process all tested documents and update their front matter.

    If `only` is given, documents not in it are skipped entirely, and each
    entry of `only` that has no results is reported, so a typo in a path can't
    silently produce no badge.
    
    Returns:
        Dict mapping document paths to their test status
    """
    tested_docs = results.get("tested_documents", [])
    tests = results.get("tests", {})
    skipped = results.get("skipped_needs_update") or {}
    
    status_map: Dict[str, str] = {}

    if only is not None:
        wanted = set(only)
        for missing in sorted(wanted - set(tested_docs)):
            print(f"Warning: --only {missing}: no test results for this document")
        tested_docs = [d for d in tested_docs if d in wanted]
    
    for doc_path in tested_docs:
        full_path = repo_root / doc_path
        
        if not full_path.exists():
            if verbose:
                print(f"Warning: Document not found: {doc_path}")
            continue
        
        status = compute_document_status(doc_path, tests, skipped)
        
        if status is None:
            if verbose:
                print(f"Skipping {doc_path}: no test results")
            continue
        
        status_map[doc_path] = status
        
        if verbose:
            icon = {"passed": "✓", "partial": "~"}.get(status, "✗")
            print(f"{icon} {doc_path}: {status}")
        
        if dry_run:
            continue
        
        content = full_path.read_text(encoding="utf-8")
        types = compute_document_types(doc_path, tests) if status == "passed" else None
        updated = update_front_matter(content, status, types)
        
        if content != updated:
            full_path.write_text(updated, encoding="utf-8")
    
    return status_map


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inject test status metadata into markdown front matter."
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repository root directory (default: current directory)"
    )
    parser.add_argument(
        "--results-file",
        default="out/tests/generated/test-results.yaml",
        help="Path to test results YAML file"
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="PATH",
        help="Inject only into this document, relative to --repo-root (repeatable)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress verbose output"
    )
    args = parser.parse_args()
    
    if yaml is None:
        print("Error: PyYAML is required. Install it with: pip install pyyaml")
        return 1
    
    repo_root = Path(args.repo_root).resolve()
    results_path = repo_root / args.results_file
    
    if not results_path.exists():
        print(f"Results file not found: {results_path}")
        print("Skipping test status injection.")
        return 0
    
    if args.dry_run:
        print("=== DRY RUN MODE ===\n")
    
    results = load_test_results(results_path)
    status_map = process_documents(
        repo_root,
        results,
        dry_run=args.dry_run,
        verbose=not args.quiet,
        only=args.only,
    )
    
    passed = sum(1 for s in status_map.values() if s == "passed")
    failed = sum(1 for s in status_map.values() if s == "failed")
    
    print(f"\nSummary: {len(status_map)} documents processed, {passed} passed, {failed} failed")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
