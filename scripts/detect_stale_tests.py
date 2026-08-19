#!/usr/bin/env python3
"""Detect doc pages whose visible example changed recently, so the corresponding
docs-tests file referenced via `{{< doc-test file="..." >}}` might now be stale.

For each `assets/agw-docs/pages/**/*.md` file touched since a given git revision in a
checked-out agentgateway-oss-website repo, compares the raw text of every fenced code
block against its content as of that revision. A `paths="X"` value that carries both a
visible fenced block (the config a reader copies) and a hidden
`{{< doc-test paths="X" file="..." >}}` reference is flagged when that visible block's
text changed -- the doc-test's assertion was written against the OLD block, so it may
no longer match what the page now shows.

This is a heuristic, not a guarantee. It only catches drift that shows up as a change to
the block a reader actually copies. A behavior change with no corresponding config change
(a newly-documented status code against unchanged YAML, say) isn't detectable this way and
still needs a human to catch it -- same as before this existed. Over-flagging is the safer
failure mode here, not under-flagging: a Copilot-assigned issue that turns out to need no
real change just gets closed.

A page can name its docs-tests file(s) for a `paths=` value two ways, and this script
checks both: the inline `{{< doc-test paths="X" file="..." >}}` shortcode in the SAME
assets page (extract_doc_test_files), or a step's `assert:` list in a DIFFERENT file's
front matter -- the versioned content/docs/.../page.md that `{{< reuse >}}`s this assets
page (find_reusing_content_pages + assert_files_for_selector). A changed selector with
no reference found via either path has no known assertion content to flag as stale, so
it's silently skipped -- same fail-closed behavior as before either mechanism existed.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from doc_test_run import parse_front_matter, version_path_tokens

FENCE_OPEN_RE = re.compile(r"^\s*(`{3,})(.*)$")
DOC_TEST_RE = re.compile(r"\{\{<\s*doc-test\b([^>]*?)>\}\}(.*?)\{\{<\s*/doc-test\s*>\}\}", re.DOTALL)
PARAM_RE = re.compile(r'([\w-]+)="([^"]*)"')


def parse_shortcode_params(params: str) -> Dict[str, str]:
    return {m.group(1): m.group(2) for m in PARAM_RE.finditer(params)}


def extract_visible_blocks(text: str) -> Dict[str, List[str]]:
    """Map each `paths=` value to the raw content of every fenced block carrying it,
    in file order.

    A single `paths=` value commonly labels more than one block on the same page --
    e.g. the CR-creation block and an unrelated cleanup block both tagged
    `paths="rewrite"`. Collapsing to one block per path (last-write-wins) would
    silently drop the earlier one; a change to it would then go undetected. Mirrors
    doc_test_extract.py's _extract_code_blocks fence-matching, but only needs the
    paths->content mapping, not full CodeBlock objects.
    """
    blocks: Dict[str, List[str]] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        open_match = FENCE_OPEN_RE.match(lines[i])
        if not open_match:
            i += 1
            continue
        fence = open_match.group(1)
        info = (open_match.group(2) or "").strip()
        j = i + 1
        content_lines: List[str] = []
        close_pattern = re.compile(rf"^\s*`{{{len(fence)},}}\s*$")
        while j < len(lines) and not close_pattern.match(lines[j]):
            content_lines.append(lines[j])
            j += 1
        path_match = re.search(r'paths\s*=\s*"([^"]+)"', info)
        if path_match:
            content = "\n".join(content_lines)
            for p in (x.strip() for x in path_match.group(1).split(",")):
                if p:
                    blocks.setdefault(p, []).append(content)
        i = j + 1 if j < len(lines) else j
    return blocks


def extract_doc_test_files(text: str) -> Dict[str, str]:
    """Map each `paths=` value to the docs-tests `file=` it references (if any)."""
    refs: Dict[str, str] = {}
    for match in DOC_TEST_RE.finditer(text):
        params = parse_shortcode_params(match.group(1) or "")
        external_file = params.get("file")
        if not external_file:
            continue
        for p in (x.strip() for x in params.get("paths", "").split(",")):
            if p:
                refs[p] = external_file
    return refs


def find_reusing_content_pages(repo_root: Path, asset_rel_path: str) -> List[Path]:
    """Find versioned content pages that {{< reuse >}} this assets page.

    Convention: content/docs/<section>/<version>/<suffix> reuses
    assets/agw-docs/pages/<suffix> via {{< reuse "agw-docs/pages/<suffix>" >}}.
    Verifies the reuse shortcode is actually present in each candidate rather than
    trusting the path-mirroring convention alone.
    """
    prefix = "assets/agw-docs/pages/"
    if not asset_rel_path.startswith(prefix):
        return []
    suffix = asset_rel_path[len(prefix):]
    reuse_needle = f'"agw-docs/pages/{suffix}"'
    pages: List[Path] = []
    for section in ("kubernetes", "standalone"):
        for candidate in sorted(repo_root.glob(f"content/docs/{section}/*/{suffix}")):
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except OSError:
                continue
            if reuse_needle in text:
                pages.append(candidate)
    return pages


def assert_files_for_selector(repo_root: Path, content_page: Path, path_selector: str) -> List[str]:
    """Read a content page's `test:` front matter for a step matching path_selector
    whose `file:` resolves to this same page (the declaring page's own step, not a
    prerequisite that happens to reuse the same selector name), and return its
    `assert:` list if present. Mirrors doc_test_run.py's own step resolution
    (${versionRoot}/${version} token substitution) so this stays in sync with how
    the runner itself interprets the same front matter.
    """
    metadata = parse_front_matter(content_page)
    tests = metadata.get("test")
    if not isinstance(tests, dict):
        return []

    rel_doc = content_page.relative_to(repo_root).as_posix()
    tokens = version_path_tokens(rel_doc)
    found: List[str] = []
    for entries in tests.values():
        if isinstance(entries, dict):
            entries = entries.get("steps")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("path") != path_selector:
                continue
            source_file = entry.get("file") or rel_doc
            for token, value in tokens.items():
                source_file = source_file.replace(token, value)
            if source_file != rel_doc:
                continue  # a prerequisite step reusing this selector name by coincidence
            for f in entry.get("assert") or []:
                if f not in found:
                    found.append(f)
    return found


def git_show(repo_root: Path, rev: str, rel_path: str) -> Optional[str]:
    result = subprocess.run(
        ["git", "show", f"{rev}:{rel_path}"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None  # file didn't exist at that revision (new file) -- nothing to compare
    return result.stdout


def changed_files_since(repo_root: Path, since_rev: str, glob_prefix: str) -> List[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{since_rev}...HEAD", "--", glob_prefix],
        cwd=repo_root, capture_output=True, text=True, check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip().endswith(".md")]


def find_stale(repo_root: Path, since_rev: str) -> List[Dict[str, object]]:
    findings: List[Dict[str, object]] = []
    reuse_cache: Dict[str, List[Path]] = {}

    for rel_path in changed_files_since(repo_root, since_rev, "assets/agw-docs/pages"):
        new_text = (repo_root / rel_path).read_text(encoding="utf-8") if (repo_root / rel_path).exists() else None
        old_text = git_show(repo_root, since_rev, rel_path)
        if new_text is None or old_text is None:
            continue  # file added or removed -- not a "drifted" case this script handles

        old_blocks = extract_visible_blocks(old_text)
        new_blocks = extract_visible_blocks(new_text)
        inline_refs = extract_doc_test_files(new_text)

        for paths_value in sorted(set(old_blocks) | set(new_blocks)):
            old_block_list = old_blocks.get(paths_value)
            new_block_list = new_blocks.get(paths_value)
            if not old_block_list or not new_block_list:
                continue  # the visible block itself was added/removed/renamed -- needs a human, not Copilot
            if old_block_list == new_block_list:
                continue

            docs_tests_files: List[str] = []
            inline_ref = inline_refs.get(paths_value)
            if inline_ref:
                docs_tests_files.append(inline_ref)
            else:
                if rel_path not in reuse_cache:
                    reuse_cache[rel_path] = find_reusing_content_pages(repo_root, rel_path)
                for content_page in reuse_cache[rel_path]:
                    for f in assert_files_for_selector(repo_root, content_page, paths_value):
                        if f not in docs_tests_files:
                            docs_tests_files.append(f)

            if not docs_tests_files:
                continue  # no known assertion content for this selector -- nothing to flag as stale

            findings.append({
                "doc_file": rel_path,
                "paths": paths_value,
                "docs_tests_files": docs_tests_files,
                "old_blocks": old_block_list,
                "new_blocks": new_block_list,
            })
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, help="Path to the agentgateway-oss-website checkout")
    parser.add_argument("--since", required=True, help="Git revision/ref to diff against (e.g. a commit SHA or 'HEAD@{2 days ago}')")
    parser.add_argument("--output", default=None, help="Write findings as JSON to this path (default: stdout)")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    findings = find_stale(repo_root, args.since)

    output = json.dumps(findings, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        print(output)

    print(f"Found {len(findings)} potentially stale docs-tests reference(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
