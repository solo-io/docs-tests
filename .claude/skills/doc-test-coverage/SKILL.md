---
name: doc-test-coverage
description: Build the doc test coverage report for every product in docs-tests, as one HTML page. It shows which pages have schema, functional, or no tests, which are skipped on purpose, and the latest nightly result. Use when the user asks "what's our test coverage", "which pages have tests", "show me doc test progress", "regenerate the coverage report", or "which pages still need tests".
---

# Doc test coverage report

This skill builds one HTML page that compares every product's test manifests
(`products/<product>/<mode>/tests.yaml`) with the pages in that product's
content repo. For each page it shows:

- the test types the page has (schema + functional, functional only, schema
  only, or none), plus any `live` or `credentialed` scenarios
- whether the page is under `skip:` on purpose
- the result from the newest scheduled or manual run of the content repo's
  doc-test workflow
- how many hidden `doc-test` checks the page's annotations add

Coverage comes from the runner's own `build_test_cases_from_manifests()`, so a
page counts as tested here only when the runner would test it.

## 1. Find a checkout for every product

The script needs a local clone of each product's content repo. It reads the
clone's default branch from `origin` (after a `git fetch`), never the local
branch, so a stale or dirty clone is fine.

List the products with `ls products/`. For each one, find a clone whose
`git remote get-url origin` is that product's content repo:

- Open source products: the header comment of the product's `tests.yaml`
  names the repo.
- Enterprise products: the repo is not named in this public repo. Look for it
  in the user's workspace instructions (`CLAUDE.md`), or ask the user for the
  path. Confirm the clone has the manifest's `content_root` folder.

When more than one clone matches (a fork, a second working clone), use the
one whose `origin` is the real repo, not a personal fork.

## 2. Run the script

From the docs-tests root, pass one `--checkout` per product:

```sh
python3 .claude/skills/doc-test-coverage/coverage_report.py \
  --checkout agentgateway=<path> \
  --checkout agentgateway-enterprise=<path> \
  --checkout kgateway=<path>
```

The script stops and names any product that is missing a `--checkout`, so a
new product can't drop out of the report unnoticed. Other options:

- `--docs-tests <path>`: read test definitions from another docs-tests
  checkout, such as a branch worktree. Uncommitted changes count, and the
  page header says so. Use it when the user asks what coverage looks like
  with a branch's tests. By default the script reads the checkout it lives in.
  Pass `--out` to a scratchpad file too, so a branch preview doesn't replace
  the published `main` report.
- `--product <name>` (repeatable): report on fewer products.
- `--no-results`: skip the nightly results, for example when `gh` isn't signed
  in.
- `--out <file>`: the default is `out/coverage/doc-test-coverage.html`, which
  is gitignored.

It needs `gh`, signed in with read access to every content repo, and PyYAML.
It prints a per-tree summary table to the terminal.

## 3. Publish the page

Publish the HTML file as an artifact. If the user already has a coverage
report artifact, update that one by its URL so their link keeps working.
Look for it with the Artifact tool's `list` action; the title is
"Doc Test Coverage".

Never commit the HTML. It holds enterprise repo names and run links, and this
repo is public.

## 4. Report back

Give the user the link, then a short summary:

- the per-tree table from the script's output, trimmed to the trees they care
  about
- what stands out: trees with no schema tests, sections with the most
  untested pages, failed or held tests, and functional tests with no hidden
  checks
- the caveat that `skip:` pages count as covered in the runner's own
  coverage figure but not as tested here

## How the numbers work

| Column | Meaning |
|---|---|
| Schema + functional | At least one scenario on the page runs it (functional, live, or credentialed), and one compares its YAML against the schema. |
| Functional only | Scenarios run the page, but none checks the schema. |
| Schema only | Only schema scenarios. |
| No test yet | No scenario resolves to the page, and it isn't under `skip:`. |
| Skipped on purpose | The page is under `skip:` (a `page:`, `source:`, or `dir:` entry). |
| Nightly | `failed` if any of the page's scenarios failed. `not run yet` if a scenario, or a test type on one, has no result in the run (it's new since the run, or only on a branch). `held` if the run skipped the scenario because its markup needs an update. `passed` if every scenario passed. |
| Hidden checks | `{{< doc-test` blocks in the page's annotation file and in the annotation files of every snippet the page reuses. A dash means the page is a rebase shell, so its checks are in the upstream product's annotations. |

Only `latest` and `main` trees are counted (`TESTED_VERSIONS` in
`doc_test_run.py`), because frozen release trees never run.

The script reads results from the newest completed `schedule` or
`workflow_dispatch` run on the default branch. It uses the merged
`doc-test-results` artifact when there is one, and otherwise merges every
`test-result-*` shard. Set a product's workflow file in `WORKFLOWS` in
`coverage_report.py` when it isn't `doc-tests.yaml`.
