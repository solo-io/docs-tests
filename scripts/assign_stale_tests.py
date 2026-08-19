#!/usr/bin/env python3
"""Turn detect_stale_tests.py findings into GitHub issues assigned to Copilot's coding
agent.

Issue creation only needs the default GITHUB_TOKEN (write access to this repo's own
issues). Assigning an issue to Copilot needs a PAT -- the default token cannot add the
copilot bot as an assignee, since Copilot billing is attributed to the PAT's user. A
missing/expired PAT only breaks assignment, not visibility: the issue is still created
and findable by a human, so the two are deliberately independent failure modes.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

REPO_URL_BASE = "https://github.com/agentgateway/website/blob/main"


def run(cmd: List[str], env: Optional[Dict[str, str]] = None, input_text: Optional[str] = None) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, input=input_text)
    if result.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def render_blocks(blocks: List[str]) -> str:
    parts = []
    for i, block in enumerate(blocks, 1):
        parts.append(f"Block {i}:\n\n```\n{block}\n```\n")
    return "\n".join(parts)


def build_issue_body(finding: dict) -> str:
    doc_file = finding["doc_file"]
    paths = finding["paths"]
    test_files = finding["docs_tests_files"]
    doc_url = f"{REPO_URL_BASE}/{doc_file}"
    test_files_list = "\n".join(f"- `{f}`" for f in test_files)
    test_files_inline = ", ".join(f"`{f}`" for f in test_files)

    return f"""The visible example in [`{doc_file}`]({doc_url}) tagged `paths="{paths}"` changed.
The test content below in this repo was written against the OLD version of that example
and may now be out of date:

{test_files_list}

## What changed

**Before:**

{render_blocks(finding["old_blocks"])}

**After:**

{render_blocks(finding["new_blocks"])}

## What to do

1. Open {test_files_inline}.
2. Compare each against the "After" example above. Update any field names, values, or
   assertions that no longer match (e.g. a renamed field, a changed status code, a
   different response value). If more than one file is listed, they usually run in
   sequence (e.g. wait / warmup / assert) -- check each against the same "After" example.
3. If the change doesn't actually affect what these files assert (for example, the
   example changed a comment or an unrelated shortcode reference), say so in the PR
   description and leave them as-is.

## Important

This repo has no CI that can run the test itself -- that only happens from
`agentgateway-oss-website`, which checks this repo out as `DOCS_TESTS_ROOT`. A human
reviewer needs to verify this change is correct (by running
`python3 scripts/doc_test_run.py` locally against a checkout of both repos, or by
waiting for agentgateway-oss-website's own scheduled doc-tests run) before merging.

---
*Opened automatically by the sync-stale-tests workflow.*
"""


def find_existing_issue(repo: str, title: str) -> Optional[int]:
    out = run([
        "gh", "issue", "list", "--repo", repo, "--label", "stale-test", "--state", "open",
        "--search", f'"{title}" in:title', "--json", "number", "--jq", ".[0].number",
    ])
    return int(out) if out else None


def create_issue(repo: str, title: str, body: str) -> int:
    url = run(["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body, "--label", "stale-test"])
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def resolve_issue_node_id(repo: str, issue_number: int) -> str:
    owner, name = repo.split("/", 1)
    out = run([
        "gh", "api", "graphql",
        "-f", "query=query($owner: String!, $repo: String!, $number: Int!) { "
              "repository(owner: $owner, name: $repo) { issue(number: $number) { id } } }",
        "-f", f"owner={owner}", "-f", f"repo={name}", "-F", f"number={issue_number}",
        "--jq", ".data.repository.issue.id",
    ])
    return out


def resolve_copilot_bot_id(repo: str, pat_env: Dict[str, str]) -> Optional[str]:
    owner, name = repo.split("/", 1)
    out = run([
        "gh", "api", "graphql",
        "-f", "query=query($owner: String!, $repo: String!) { "
              "repository(owner: $owner, name: $repo) { "
              "suggestedActors(capabilities: [CAN_BE_ASSIGNED], first: 100) { "
              "nodes { login ... on Bot { id } } } } }",
        "-f", f"owner={owner}", "-f", f"repo={name}",
        "--jq", '.data.repository.suggestedActors.nodes[] | select(.login == "copilot-swe-agent") | .id',
    ], env=pat_env)
    return out or None


def assign_to_copilot(issue_id: str, bot_id: str, pat_env: Dict[str, str]) -> None:
    run([
        "gh", "api", "graphql",
        "-f", "query=mutation($assignableId: ID!, $actorId: ID!) { "
              "replaceActorsForAssignable(input: {assignableId: $assignableId, actorIds: [$actorId]}) { "
              "assignable { ... on Issue { number } } } }",
        "-f", f"assignableId={issue_id}", "-f", f"actorId={bot_id}",
    ], env=pat_env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--findings", required=True, help="Path to detect_stale_tests.py's JSON output")
    parser.add_argument("--repo", required=True, help="owner/repo to create issues in, e.g. solo-io/docs-tests")
    args = parser.parse_args()

    import os
    findings = json.loads(Path(args.findings).read_text(encoding="utf-8"))
    if not findings:
        print("No findings; nothing to do.")
        return 0

    copilot_token = os.environ.get("COPILOT_ASSIGN_TOKEN")
    pat_env = {**os.environ, "GH_TOKEN": copilot_token} if copilot_token else None

    for finding in findings:
        files_label = ", ".join(finding["docs_tests_files"])
        title = f'Stale test: {finding["doc_file"]} (paths="{finding["paths"]}") changed, check {files_label}'

        existing = find_existing_issue(args.repo, title)
        if existing:
            print(f"Skipping (already tracked as #{existing}): {title}")
            continue

        body = build_issue_body(finding)
        issue_number = create_issue(args.repo, title, body)
        print(f"Created issue #{issue_number}: {title}")

        if pat_env is None:
            print(f"::warning::COPILOT_ASSIGN_TOKEN is not set -- issue #{issue_number} was created but not assigned to Copilot.")
            continue

        issue_id = resolve_issue_node_id(args.repo, issue_number)
        bot_id = resolve_copilot_bot_id(args.repo, pat_env)
        if not bot_id:
            print(f"::warning::Could not resolve copilot-swe-agent's bot ID for #{issue_number} -- is Copilot coding agent enabled for this repo? Issue created but not assigned.")
            continue

        assign_to_copilot(issue_id, bot_id, pat_env)
        print(f"Assigned #{issue_number} to copilot-swe-agent")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
