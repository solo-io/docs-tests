#!/usr/bin/env python3
"""List markdown files in content/docs that have no test coverage.

A file is considered covered if its front matter contains a 'test:' key
(either a scenario dict or 'test: skip'), or if a docs-tests manifest
(products/*/*/tests.yaml) declares a scenario on it or lists it under `skip:`.
Everything else is written to the output file as a candidate for adding tests.

Only the version trees the runner tests (TESTED_VERSIONS: latest, main) are
listed. Frozen release trees never run, so a page there is not something to add
a test to.

Usage:
    python3 scripts/list_untested_docs.py \
        --docs-dir content/docs \
        --exclude content/docs/kubernetes/2.2.x \
        --output out/tests/generated/untested-docs.txt
"""

import argparse
import sys
from pathlib import Path

try:
    import yaml  # type: ignore[import-not-found]
except ModuleNotFoundError:
    print("PyYAML is required. Install it with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def parse_front_matter(path: Path) -> dict:
    """Return the YAML front matter dict from a markdown file, or {}."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        return yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="List markdown files without doc test coverage")
    parser.add_argument("--docs-dir", default="content/docs", help="Directory to scan")
    parser.add_argument("--repo-root", default=".", help="Content repo root that manifest page paths are relative to")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATH",
        help="Path prefix to exclude (may be repeated)",
    )
    parser.add_argument(
        "--output",
        default="out/tests/generated/untested-docs.txt",
        help="Output file path",
    )
    args = parser.parse_args()

    docs_dir = Path(args.docs_dir)
    if not docs_dir.is_dir():
        print(f"Directory not found: {docs_dir}", file=sys.stderr)
        return 1

    exclude_paths = [Path(e) for e in args.exclude]

    # Pages whose scenarios, or deliberate skip, live in a docs-tests manifest
    # rather than in their own front matter. Paths are relative to --repo-root.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from doc_test_run import TESTED_VERSIONS, _version_segment, build_test_cases_from_manifests  # noqa: E402

    repo_root = Path(args.repo_root).resolve()
    _, _, manifest_docs = build_test_cases_from_manifests(
        repo_root, Path(__file__).resolve().parent.parent, repo_root / "out"
    )
    covered = {(repo_root / d).resolve() for d in manifest_docs}

    untested: list[str] = []
    for md_file in sorted(docs_dir.rglob("*.md")):
        if any(md_file.is_relative_to(ex) for ex in exclude_paths):
            continue
        if _version_segment(md_file.as_posix()) not in TESTED_VERSIONS:
            continue
        fm = parse_front_matter(md_file)
        if "test" not in fm and md_file.resolve() not in covered:
            untested.append(md_file.as_posix())

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(untested) + ("\n" if untested else ""), encoding="utf-8")

    print(f"{len(untested)} file(s) without test coverage written to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
