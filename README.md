# docs-tests

Automated tests for Solo product documentation, extracted out of the doc markdown so
that guides stay readable to casual contributors and test scripts are reusable across
each product's own CI workflow.

This repo currently holds two things:

- `scripts/` — the extractor/runner tooling (`doc_test_extract.py`, `doc_test_run.py`,
  `doc_test_schema_check.py`, `doc_test_inject_status.py`, `doc_test_fetch_artifacts.sh`,
  `merge_test_results.py`, `report_summary.py`, `list_untested_docs.py`). This is the
  canonical copy — `agentgateway-oss-website`'s CI and Makefile check this repo out and
  invoke these scripts directly (`docs-tests/scripts/doc_test_run.py --repo-root .`, etc.);
  it no longer keeps its own copies.
- `products/` — the actual test content, one directory per product and version,
  mirroring the doc's own path, e.g.
  `products/agentgateway/main/traffic-management/transformations/rewrite.sh`. Kept as
  a directory layout on `main` rather than a branch per product × version, and as
  committed, reviewable, diffable files rather than CI-generated artifacts.

## Dependencies

Install yamltest and cloud-provider-kind to run the tests:

```bash
npm i -y -g yamltest@latest
pip3 install PyYAML
go install sigs.k8s.io/cloud-provider-kind@latest
```

---

## How it works

1. A doc page declares a `test:` block in its YAML front matter, listing one or more named test scenarios.
2. Each scenario lists source files and path selectors — the pieces of shell/YAML to include.
3. `doc_test_run.py` reads this metadata, chains the sources together, and produces a standalone bash script.
4. The script is run inside a fresh `kind` cluster (with `cloud-provider-kind` for load balancer support).

---

## Test annotations

Content repos are moving to carry no test markup at all. The markup (`paths=`
tags on code blocks, hidden `{{< doc-test >}}` checks, and front matter `test:`)
moves into this repo as test annotations under `products/<product>/annotations/`,
one YAML file per page, and `scripts/annotations.py` attaches it to a copy of the
pages before a run.

An annotation finds its code block by fingerprint, a hash of the block's text, so
pages can move and prose can change freely. Each piece of markup ends a run in one
of three states:

| State | What happened |
|---|---|
| exact | The block's text is unchanged. |
| close | The block between the same unchanged neighbors was edited slightly, and it is the only nearly identical candidate: line similarity at least 0.6, and at least 0.1 ahead of the next candidate. |
| needs update | The block was rewritten or removed. The markup stays in the annotation file and is reported, never attached somewhere else. |

Hidden checks move only with the block or sentence they follow. A check that
follows a sentence is looked for between the same two code blocks, even when the
block before it was close-matched.

| Command | Use |
|---|---|
| `make annotate PAGE=<page>` then `make save PAGE=<page>` | Add or change the tests on one page. `annotate` writes the page with its markup to `out/annotate/`; edit the markup there; `save` writes only the markup back, and refuses if the copy changes the page itself. |
| `annotations.py attach` | Attach annotations to a checkout, in place or into `--out`. Removes any inline markup first. |
| `annotations.py refresh` | After content changes: re-attach, record the new fingerprints, and follow moved pages (git rename detection, then a search by fingerprint). Never drops markup; refuses to write if any would disappear. |
| `annotations.py export` / `strip` | Migration only: copy inline markup into annotation files, then remove it from the pages. |
| `annotations.py roundtrip` | Strip and re-attach every page in memory; every page must come back byte-identical. Runs daily in CI against agentgateway/website. |
| `annotation_replay.py` | Replays a repo's git history, whose inline markup is the answer key, to measure close matching. Re-run it when matching changes; `close WRONG` must not rise. |
| `doc_test_run.py --annotation-report <attach report>` | Skips, with a warning, any scenario that would select markup which did not attach, instead of running it with a step missing. Skipped scenarios go under `skipped_needs_update` in the results, not under `tests`. |

`.github/workflows/refresh-annotations.yml` runs `refresh` daily at 05:00 UTC
against agentgateway/website `main` and commits the result to this repo's
`main`. Every scheduled run posts to `#doctopus-tests` in the same layout as the
doc test results, listing every piece of markup that needs update, with a thread
of the day's close matches and moved pages; a failed refresh also posts to
`#doctopus-builds`. Posting needs a `DOCS_TESTS_SLACK_BOT_TOKEN` secret on this
repo; until it is set, the step skips with a warning.

For the sections moved so far (traffic-management, and llm under
`documentation/llm/`), scenario definitions live in the `tests.yaml`
manifests, so their annotations were exported with `--front-matter none`. A
manifest key must be unique, but a scenario name need not be: set `name:` when
two pages in one mode declare the same name. Pages
that deliberately have no test are listed under the manifest's `skip:` key, the
manifest form of front matter `test: skip`; the runner and
`list_untested_docs.py` count them as covered.

---

## Referencing external test content in this repo

The hidden `{{< doc-test >}}` shortcode (used for setup/assertions that must run
during tests but never appear in rendered HTML) accepts an optional `file="..."`
attribute. When present, the extractor reads the block's content from that path
resolved against a `docs-tests` checkout, instead of requiring an inline body between
the opening and closing shortcode tags:

```md
{{< doc-test paths="rewrite" file="products/agentgateway/main/traffic-management/transformations/rewrite.sh" >}}{{< /doc-test >}}
```

The referenced file (`products/agentgateway/main/traffic-management/transformations/rewrite.sh`
in this example) holds exactly the content that used to sit inline between the shortcode
tags — nothing else changes about how the block is selected or emitted; it flows through
the same `paths=` selection and script assembly as an inline block.

**Resolving the `docs-tests` checkout root:**

- `doc_test_extract.py` and `doc_test_run.py` both accept `--docs-tests-root <path>`,
  or the `DOCS_TESTS_ROOT` environment variable.
- If neither is set, it defaults to a sibling directory next to the consuming repo's
  own root (e.g. `agentgateway-oss-website/` and `docs-tests/` cloned side by side),
  so a plain local checkout of both repos works with no extra configuration.
- In CI, add a checkout step for `docs-tests` and pass its path through either
  mechanism — see `scripts/CI_WORKFLOW_SKETCH.md` for the concrete workflow change.
- A missing or misspelled `file=` path fails the extractor immediately with a
  `FileNotFoundError` naming the source doc, line number, and the resolved path that
  didn't exist — it never silently drops the block.

This is unrelated to the `<!-- doc-test-include file="..." -->` HTML comment some of
the extractor's code still supports — that mechanism runs an external `bun test <file>`
as a separate subprocess (built for a different kind of test) and is not used for this
external-content use case.

### Referencing external test content from front matter instead

The `file="..."` shortcode above still leaves one line of test scaffolding sitting in
the page body for every hidden block. A step in a page's `test:` front matter can name
the same external content directly instead, via an `assert:` list, so the page body
carries nothing but the real content and the `paths="X"` tag on the visible block:

```yaml
test:
  host-rewrite:
    type: functional
    steps:
    - file: ${versionRoot}/quickstart/install.md
      path: experimental
    - file: ${versionRoot}/traffic-management/rewrite/host.md
      path: host-rewrite
      assert:
      - products/agentgateway/main/traffic-management/rewrite/host-rewrite-wait.sh
      - products/agentgateway/main/traffic-management/rewrite/host-rewrite-warmup.sh
      - products/agentgateway/main/traffic-management/rewrite/host-rewrite-assert.sh
```

Each entry in `assert:` is a `docs-tests`-relative path, run in list order. This
replaces what would otherwise be three separate `{{< doc-test paths="host-rewrite"
file="..." >}}{{< /doc-test >}}` lines physically sitting in the page. Both mechanisms
are supported and can coexist across different pages — `assert:` is the newer, cleaner
form; `file="..."` on an inline shortcode still works for pages that haven't been
converted.

**Ordering**: `assert:` doesn't say *where* in the page these files logically belong,
so the extractor anchors each entry to the first block (visible or hidden) in that same
file whose `paths=` matches the step's `path:` — the same anchor a reader would expect
from where the inline shortcode used to sit — and runs the whole `assert:` list
immediately after it, before anything else in the file. This matters because a single
`paths=` value is often reused later in the same page for something unrelated (a
`Cleanup` section's `kubectl delete`, say); anchoring to the *first* matching block, not
just appending at the end of the file, keeps assertions running before cleanup rather
than after it.

Only scenarios that actually execute something need an `assert:` list — a `type: schema`
step never runs anything, so it has no equivalent.

---

## Path selectors

A **path** is a string label attached to a fenced code block or hidden command block. It controls which blocks are included in which test scenario.

### Tagging visible code blocks

Add `,{paths="<name>}"` to the fenced code language line:

````md
```sh,{paths="install-httpbin"}
kubectl apply -f https://raw.githubusercontent.com/.../httpbin.yaml
```
````

A block may belong to multiple paths:

````md
```sh,{paths="standard,experimental"}
helm upgrade -i --create-namespace ...
```
````

Only `sh`/`bash`/`shell`/`yaml`/`yml` blocks are extracted.

### Tagging hidden command blocks

Use the `{{< doc-test >}}` Hugo shortcode for commands that must run during tests but must **not** appear in the website HTML (waits, retries, cleanup). The shortcode template outputs nothing, so the content is completely absent from rendered pages:

```md
{{< doc-test paths="install-httpbin" >}}
YAMLTest -f - <<'EOF'
- name: wait for httpbin deployment
  wait:
    ...
EOF
{{< /doc-test >}}
```

The `paths=` attribute works identically to fenced blocks. As above, add `file="..."`
instead of an inline body to pull the content from this repo's `products/` tree.

---

## Front matter test metadata

On the page being tested, add a `test:` key to the YAML front matter. Each child key is a named test scenario. Each entry in the list is a `file`+`path` pair — a source file and the path selector to pull from it.

```yaml
---
title: CORS
test:
  cors-in-httproute:
  - file: content/docs/kubernetes/main/quickstart/install.md
    path: experimental
  - file: content/docs/kubernetes/main/setup/gateway.md
    path: all
  - file: content/docs/kubernetes/main/install/sample-app.md
    path: install-httpbin
  - file: content/docs/kubernetes/main/security/cors.md
    path: cors-in-httproute

  cors-in-agentgatewaypolicy:
  - file: content/docs/kubernetes/main/quickstart/install.md
    path: standard
  - file: content/docs/kubernetes/main/setup/gateway.md
    path: all
  - file: content/docs/kubernetes/main/install/sample-app.md
    path: install-httpbin
  - file: content/docs/kubernetes/main/security/cors.md
    path: cors-in-agentgatewaypolicy
---
```

Multiple scenarios on the same page each get their own kind cluster and generated script.

### Typing scenarios

A scenario can declare a `type:` alongside its `file`/`path` entries, renamed `steps:` under it:

```yaml
test:
  rewrite:
    type: functional
    steps:
    - file: content/docs/kubernetes/main/quickstart/install.md
      path: experimental
    - file: content/docs/kubernetes/main/traffic-management/transformations/rewrite.md
      path: rewrite
```

A bare list (no `type:`/`steps:` wrapper) is still accepted and treated as `functional` —
every scenario written before this typing existed already applies real config and
asserts on a real response, which is what `functional` means.

| Type | What it checks | Needs | Blocks the PR? |
|---|---|---|---|
| `schema` | The doc's own example custom resource validates against the real CRD's OpenAPI schema (renamed/removed/mistyped fields, wrong types) | Nothing — no cluster, no execution (`doc_test_schema_check.py`) | Yes, in its own job |
| `functional` | Real behavior in a real cluster (apply config, assert on a real response) | A `kind` cluster, no vendor credentials | Yes |
| `live` | A real external endpoint is reachable and returns the documented unauthenticated response | A real public endpoint, no credentials | Yes, alongside `functional` |
| `credentialed` | Full behavior against a real vendor with real credentials | Named secrets, provisioned out of band | No — scheduled, non-blocking, never on a PR |

`schema` needs no prerequisite chain — since nothing executes, only the step that shows
the custom resource itself matters:

```yaml
test:
  rewrite-schema:
    type: schema
    steps:
    - file: content/docs/kubernetes/main/traffic-management/transformations/rewrite.md
      path: rewrite
```

**Known gap, not a mechanism limitation:** some enterprise-only pages (Entra token
exchange, for one) can't reach `live` today because there's no shared dev tenant to test
against — registering a real Entra app/tenant is manual, one-time setup with no
vendor-provided sandbox. That test stays tagged at whatever type it can actually reach,
with the gap tracked, rather than silently passing at a narrower type than the page's own
content would suggest.

### Declaring more than one type on the same scenario

`type:` also accepts a list, so one `steps:` chain gets validated more than one way
without copy-pasting the whole scenario into a same-named `-schema` sibling just to add a
second type:

```yaml
test:
  rewrite:
    type: [schema, functional]
    steps:
    - file: content/docs/kubernetes/main/quickstart/install.md
      path: experimental
    - file: content/docs/kubernetes/main/traffic-management/transformations/rewrite.md
      path: rewrite
```

This produces two independent test cases, `rewrite::schema` and `rewrite::functional`,
sharing the same `steps:` — the same shape as hand-writing two separate scenarios, minus
the duplication. `--test rewrite` selects both; `--test rewrite::schema` selects only the
schema one. A single-type scenario is unaffected: its name, generated filenames, and
report key stay exactly as before — the `name::type` suffix only appears once a scenario
declares more than one type.

Add `schema` this way only when the scenario's final step is a recognized custom-resource
kind with a local CRD schema (currently `AgentgatewayPolicy`/`AgentgatewayBackend`) — a
plain Gateway API resource (`HTTPRoute`, `Gateway`) has nothing to validate against and
would only ever pass vacuously, so `schema` is opt-in per scenario, not automatic.

---

## Tracing prerequisites

Every guide has a **Before you begin** section that lists prerequisites. Follow the chain from the feature guide back to the install guide:

```
feature page (e.g. cors.md)
  └── sample-app.md           (httpbin installed + HTTPRoute ready)
        └── gateway.md        (Gateway created + LB address exported)
              └── helm.md     (CRDs + controller installed)
```

For each hop:

1. Open the file and find its `## Before you begin` section.
2. Follow the linked page.
3. Identify which code blocks are relevant and what path label they carry (or add one if missing).
4. Add that file+path as a source entry above the current one in the `test:` front matter.

The extractor follows `{{< reuse "..." >}}` and internal links automatically, so you don't need to inline snippet contents — just reference the top-level content file.

---

## Choosing the right path

- Use an **existing** path label if one already exists on the blocks you need.
- The path `all` is a conventional catch-all for blocks that are included in every scenario from that file.
- For tabbed content (Standard / Experimental installs), separate paths (`standard`, `experimental`) let you pick the right tab per scenario.
- A code block with **no** `paths=` is skipped by default (`skip_tabs_without_paths: true`).

### Adding a path to an existing block

If a block you need has no path, add one:

````md
```sh {paths="install-httpbin"}
kubectl apply -f ...
```
````

If the same block already belongs to another path and you need to add yours:

````md
```sh {paths="standard,my-new-path"}
...
```
````

---

## Waiting for resources with YAMLTest

Use `YAMLTest -f - <<'EOF' ... EOF` inside a `{{< doc-test >}}` shortcode block immediately after the `kubectl apply` it depends on. The `wait` test type polls a Kubernetes resource until a JSONPath condition is met.

### Wait for a Deployment to be ready

```md
{{< doc-test paths="all" >}}
YAMLTest -f - <<'EOF'
- name: wait for agentgateway-proxy deployment to be ready
  wait:
    target:
      kind: Deployment
      metadata:
        namespace: agentgateway-system
        name: agentgateway-proxy
    jsonPath: "$.status.availableReplicas"
    jsonPathExpectation:
      comparator: greaterThan
      value: 0
    polling:
      timeoutSeconds: 300
      intervalSeconds: 5
EOF
{{< /doc-test >}}
```

### Wait for a Service to get a load balancer address and export it

```md
{{< doc-test paths="all" >}}
YAMLTest -f - <<'EOF'
- name: wait for agentgateway-proxy service LB address
  wait:
    target:
      kind: Service
      metadata:
        namespace: agentgateway-system
        name: agentgateway-proxy
    jsonPath: "$.status.loadBalancer.ingress[0].ip"
    jsonPathExpectation:
      comparator: exists
    polling:
      timeoutSeconds: 300
      intervalSeconds: 5
  setVars:
    INGRESS_GW_ADDRESS:
      value: true
EOF
{{< /doc-test >}}
```

`setVars` exports the `jsonPath`-matched value as an environment variable for downstream steps. It is a sibling of `wait:` (not nested inside it).

### Wait for an HTTPRoute condition

```md
{{< doc-test paths="install-httpbin" >}}
YAMLTest -f - <<'EOF'
- name: wait for httpbin HTTPRoute to be accepted
  wait:
    target:
      kind: HTTPRoute
      metadata:
        namespace: httpbin
        name: httpbin
    jsonPath: "$.status.parents[0].conditions[?(@.type=='Accepted')].status"
    jsonPathExpectation:
      comparator: equals
      value: "True"
    polling:
      timeoutSeconds: 300
      intervalSeconds: 5
EOF
{{< /doc-test >}}
```

### Comparators

| Comparator | Meaning |
|---|---|
| `equals` | Exact string/number match |
| `greaterThan` | Numeric greater-than |
| `exists` | Field is present and non-empty |
| `contains` | String contains substring |

---

## Testing a feature with YAMLTest HTTP assertions

After all resources are ready and `INGRESS_GW_ADDRESS` is exported, add an HTTP test inside a hidden block on the feature page itself:

```md
{{< doc-test paths="cors-in-httproute,cors-in-agentgatewaypolicy" >}}
YAMLTest -f - <<'EOF'
- name: CORS preflight returns expected headers
  http:
    url: "http://${INGRESS_GW_ADDRESS}:80/get"
    method: OPTIONS
    headers:
      host: www.example.com
      Origin: https://example.com
  source:
    type: local
  expect:
    statusCode: 200
    headers:
      - name: access-control-allow-origin
        comparator: equals
        value: https://example.com
      - name: access-control-allow-methods
        comparator: contains
        value: GET
      - name: access-control-max-age
        comparator: equals
        value: "86400"
EOF
{{< /doc-test >}}
```

- `source.type: local` sends the request from the local machine (default).
- Use `source.type: pod` with a pod selector to send the request from inside the cluster.
- The `headers` list under `expect` checks response headers (case-insensitive name matching).
- Use `retries:` on a test entry to retry on transient failures (e.g. a race condition where a response code flips briefly). **Do not use `retries:` to work around a new-hostname ECONNRESET** — see the Troubleshooting section.

---

## Running the tests

### Generate scripts only (no cluster)

```sh
python3 scripts/doc_test_run.py --generate-only
```

Scripts are written to `out/tests/generated/`.

### Run all tests

Requires `kind` and `cloud-provider-kind` in PATH.

```sh
python3 scripts/doc_test_run.py
```

Each test scenario:
1. Creates a `kind` cluster named `doc-test-<scenario>`.
2. Starts `cloud-provider-kind` in the background (provides LoadBalancer IPs).
3. Runs the generated bash script.
4. Deletes the cluster.
5. Writes results to `out/tests/generated/test-results.yaml`.

### Run a single test scenario

Point directly to a file and (optionally) a named scenario. This generates the script, creates a `kind` cluster, starts `cloud-provider-kind`, runs the test, and cleans up — all in one command:

```sh
# Run one specific scenario
python3 scripts/doc_test_run.py \
  --file content/docs/kubernetes/main/security/cors.md \
  --test cors-in-httproute

# Run all scenarios defined in a single file
python3 scripts/doc_test_run.py \
  --file content/docs/kubernetes/main/security/cors.md
```

To only generate the script without running (useful for inspection):

```sh
python3 scripts/doc_test_run.py \
  --file content/docs/kubernetes/main/security/cors.md \
  --test cors-in-httproute \
  --generate-only
bash out/tests/generated/<script-name>.sh
```

### Key CLI options

| Flag | Default | Description |
|---|---|---|
| `--file` | — | Path to a single markdown file to generate/run tests for |
| `--test` | — | Name of a specific test scenario within `--file` |
| `--docs-glob` | `content/docs/**/*.md` | Glob to discover pages with `test:` metadata (ignored when `--file` is set) |
| `--product` | `kubernetes` | Context product used for `conditional-text` resolution |
| `--docs-tests-root` | sibling `docs-tests` dir | Checkout root for `{{< doc-test file="..." >}}` external content (or `DOCS_TESTS_ROOT` env var) |
| `--generated-dir` | `out/tests/generated` | Output directory for scripts and manifests |
| `--generate-only` | false | Skip cluster creation and execution |
| `--verbose` | true | Stream all command output |

The `version` context (used to resolve `{{< version include-if="..." >}}` blocks) is inferred automatically from the source file paths — e.g. a source under `kubernetes/latest/` resolves to version token `latest`, and `kubernetes/main/` to `main`.

---

## Extractor rules

`doc_test_extract.py` processes source files before emitting the script:

- **`{{< reuse "..." >}}`** — inlined recursively from `assets/`.
- **`{{< version include-if="..." >}}`** — resolved against the inferred version token; non-matching blocks are dropped.
- **`{{< conditional-text include-if="..." >}}`** — resolved against the `product` context; non-matching blocks are dropped.
- **Indentation is stripped** from fenced block content so heredocs work correctly in bash.
- **Duplicate blocks** (same content) are emitted only once.
- Blocks without a `paths=` attribute are skipped.
- **`{{< doc-test file="..." >}}`** — content is read from the `docs-tests` checkout instead of the shortcode body (see above).
- **A step's `assert:` list** — synthesizes the same kind of hidden block directly from front matter, anchored to the first block in that file sharing the step's `paths=` selector (see above).

---

## Checklist for adding a test to a new page

1. **Trace prerequisites** — follow "Before you begin" links back to `helm.md`.
2. **Verify path labels** on all prerequisite code blocks; add `paths="..."` where missing.
3. **Add wait blocks** after each `kubectl apply` that creates something tests depend on.
4. **Export `INGRESS_GW_ADDRESS`** — it flows from `gateway.md` via `setVars`.
5. **Add the feature assertion** as a `{{< doc-test >}}` shortcode block on the feature page, either inline or via `file="..."` pointing into this repo's `products/` tree.
6. **Write the `test:` front matter** on the feature page, listing sources in dependency order (install → setup → prereqs → feature).
7. **Regenerate** with `--generate-only` and inspect the script for unresolved shortcodes or missing commands.
8. **Run locally** with `bash out/tests/generated/<script>.sh` against an existing cluster to verify before committing.

---

## Keeping tests in sync with doc changes

Content in this repo is only ever correct as of when it was written against a doc's
example. When that example changes, the file here can silently go stale — nothing forces
an author editing `agentgateway-oss-website` to also update the corresponding file here.

The `sync-stale-tests` workflow (`.github/workflows/sync-stale-tests.yml`) catches this
automatically:

1. **Daily**, it diffs `assets/agw-docs/pages/**/*.md` in `agentgateway-oss-website`
   against its state ~26 hours earlier (`scripts/detect_stale_tests.py`).
2. For every `paths="X"` value that carries both a visible fenced block (the config a
   reader copies) and a hidden `{{< doc-test paths="X" file="..." >}}` reference, it
   checks whether that visible block's text changed.
3. For each one that did, it opens a GitHub issue here with the old/new content inline,
   and assigns it to GitHub Copilot's coding agent, which proposes a PR updating the
   test file.

This is a heuristic, not a guarantee. It only catches drift that shows up as a change to
the block a reader actually copies — a behavior change with no corresponding config
change (a newly-documented status code against unchanged YAML, say) isn't detectable this
way and still needs a human to notice, same as before this existed. Over-flagging is the
intended failure mode: a raw-text change that turns out to be cosmetic (a renamed
`{{< reuse >}}` snippet that resolves to the same value, for instance) still gets flagged,
and Copilot or a reviewer just closes it as a no-op.

**This workflow does not verify its own output.** There is no CI in this repo that can
run `doc_test_run.py` against a real page — that only happens from
`agentgateway-oss-website`, which checks this repo out as `DOCS_TESTS_ROOT`. Every PR
Copilot opens against this repo needs a human to actually run the test (or wait for
`agentgateway-oss-website`'s own scheduled doc-tests run) before merging.

**Setup required, not yet done:** assigning an issue to Copilot needs a PAT — the default
`GITHUB_TOKEN` can't add the Copilot bot as an assignee, since Copilot billing attributes
to the PAT's user. Add one as the `COPILOT_ASSIGN_TOKEN` repo secret (scoped to issues:
read/write) before this workflow's assignment step will do anything; until then it still
creates the issue, just unassigned, and logs a workflow warning saying so.

**Both mechanisms are detected.** `detect_stale_tests.py` finds the docs-tests file(s)
for a changed `paths=` value two ways: the inline `{{< doc-test paths="X" file="..." >}}`
shortcode in the same assets page (the original mechanism), or a step's `assert:` list in
a *different* file's front matter — the versioned `content/docs/...` page that
`{{< reuse >}}`s the changed assets page (see [Referencing external test content from
front matter instead](#referencing-external-test-content-from-front-matter-instead)).
For the latter, it resolves the changed assets page back to every versioned content page
that reuses it (`find_reusing_content_pages`), then reads each one's matching `assert:`
entry (`assert_files_for_selector`) the same way `doc_test_run.py` itself would. A
selector with no reference found via either path has no known assertion content, so it's
silently skipped — same fail-closed behavior either mechanism has always had. A finding's
`docs_tests_files` is a list (not a single file) since an `assert:` scenario commonly
names more than one, e.g. `wait`/`warmup`/`assert`.

---

## Displaying test status on doc pages

Doc pages with passing tests display a "Verified" badge below the page title.

### How it works

1. **Test results** are written to `out/tests/generated/test-results.yaml` after tests run.
2. **`doc_test_inject_status.py`** reads the results and adds a `test_status` field to each tested document's front matter:
   - `test_status: passed` — all tests for the page passed
   - `test_status: failed` — one or more tests failed (no badge displayed)
3. **Hugo templates** check for `test_status: passed` and render a green "Verified" badge.

### Makefile targets (in the consuming repo)

| Target | Description |
|---|---|
| `make deps` | Install Python dependencies (PyYAML) |
| `make test-generate` | Generate doc test scripts without running them |
| `make test-run` | Run all doc tests |
| `make test-artifacts-fetch` | Fetch test artifacts from the latest main branch workflow run |
| `make test-status` | Inject test status into markdown files |
| `make fetch-test-artifacts-build` | Fetch artifacts, inject status, and build Hugo site |
| `make fetch-test-artifacts-serve` | Fetch artifacts, inject status, and serve Hugo site locally |
| `make test-run-build` | Run tests, inject status, and build Hugo site |
| `make test-run-serve` | Run tests, inject status, and serve Hugo site locally |

### Running locally

To preview the "Verified" badges locally:

```sh
# Option 1: Fetch results from CI and serve
make fetch-test-artifacts-serve

# Option 2: Run tests locally and serve
make test-run-serve
```

To manually inject test status after running tests:

```sh
make test-status
```

This updates the markdown files in `content/docs/` with the test status. The badge will appear when you run Hugo.

### Fetching test artifacts

The `test-artifacts-fetch` target downloads test results from the most recent completed workflow run on the `main` branch. This requires a `GITHUB_TOKEN` environment variable with `actions:read` scope:

```sh
export GITHUB_TOKEN=<your-token>
make test-artifacts-fetch
```

### CLI options for inject script

| Flag | Default | Description |
|---|---|---|
| `--repo-root` | `.` | Repository root directory |
| `--results-file` | `out/tests/generated/test-results.yaml` | Path to test results file |
| `--dry-run` | false | Preview changes without modifying files |
| `--quiet` | false | Suppress verbose output |

---

## Troubleshooting

If you run into issues with installing yamltest, include the `--force` flag.

On macOS, you might need to run either the `python3 scripts/doc_test_run.py` command with `sudo`, or run `sudo cloud-provider-kind --gateway-channel=disabled` in a separate tab before running the tests. In macOS, the cloud-provider-kind tool to get a LoadBalancer IP requires elevated permissions.

### Common issues

**File paths differ between `latest` and `main`**

When copying a test chain from `main` to `latest` (or vice versa), update every `file:` path in the front matter. A `main` chain references files under `content/docs/kubernetes/main/`, while `latest` uses `content/docs/kubernetes/latest/`. Using the wrong version directory causes the extractor to pull blocks from a different version's content, or fail silently if the file doesn't exist.

**Wrong prerequisite file paths**

The install prerequisite should point to `content/docs/kubernetes/<version>/quickstart/install.md`, not `content/docs/kubernetes/<version>/install/helm.md` or similar. Check the "Before you begin" section of the guide you're testing and follow the links to confirm the exact paths rather than guessing from memory.

**Test fails immediately with "kubectl port-forward" error**

Tests that contain `kubectl port-forward` in the generated script are automatically failed without running. Port-forwarding requires a persistent background process that doesn't work in the automated test environment. Replace any port-forward-based verification with a `YAMLTest` HTTP assertion using `${INGRESS_GW_ADDRESS}` instead.

**Wait assertions pass but HTTP test hangs then fails with `read ECONNRESET`**

When a test creates a new HTTPRoute with a hostname that was not previously registered, agentgateway-proxy (Rust/hyper) goes through two phases before it can serve the new route. `Accepted=True` and `ResolvedRefs=True` on the HTTPRoute only reflect control plane state (~50ms) — they do not guarantee the data plane is ready.

- **Phase 1 (~120s)**: Proxy is unaware of the new hostname; every connection is immediately reset (< 1ms). `curl --max-time 5` iterations cost ~2s each (instant failure + 2s sleep).
- **Phase 2 (last few seconds)**: Proxy receives xDS update and holds connections while applying it (4–26s per connection), then resets them.

Adding `retries: 3` alone (without a warmup loop) multiplies the Phase 2 hang — observed total: 4 × 107s ≈ 429s.

Fix: use **both** a curl warmup loop (covers Phase 1) and `retries: 1` on the first HTTP test entry (covers Phase 2):

```
{{< doc-test paths="<scenario-name>" >}}
for i in $(seq 1 60); do
  curl -s --max-time 5 -o /dev/null "http://${INGRESS_GW_ADDRESS}:80/get" -H "host: <new-hostname>" && break
  sleep 2
done
{{< /doc-test >}}
```

Then on the first YAMLTest HTTP assertion entry, add `retries: 1`. Once curl gets any HTTP response (even 404), Phase 1 is over. `retries: 1` absorbs Phase 2. Max poll window: ~420 seconds.

This only applies when the feature page creates an HTTPRoute with a **new hostname** not in the prereq chain. Tests that update an existing prereq-chain HTTPRoute (same name/namespace) are not affected.

**`read ECONNRESET` persists beyond the warmup window (cloud-provider-kind LB failure)**

If the test already has the warmup loop + `retries: 1` pattern but the HTTP assertion still fails with `read ECONNRESET` after 200+ seconds (the warmup curl loop never breaks out), the issue is likely a cloud-provider-kind LoadBalancer networking failure — not a data-plane warmup problem.

**How to distinguish from the data-plane warmup issue:** Check the controller logs in the diagnostic artifacts at `out/tests/generated/context/<scenario>/pods/<controller-pod>-controller-logs.log`. Look for `XDS: Pushing` entries with `clients:1` and `RDS` push responses for your routes:

```
{"msg":"XDS: Pushing","component":"krtxds","clients":1,"version":"..."}
{"msg":"push response","component":"krtxds","type":"RDS","resources":1,...}
```

If the controller pushed routes to the proxy successfully, the proxy IS configured — the problem is at the network level between the LB IP and the pod.

**Root cause:** cloud-provider-kind assigns a LoadBalancer IP to the Service, but traffic from that IP is not properly forwarded to the pod through the Kind Docker network. The LB IP is reachable (TCP connect succeeds) but the proxy resets the connection because it never receives the forwarded packets.

**Typical diagnostic evidence:**
- All pods Running with 0 restarts
- LB IP assigned (e.g. `172.18.0.x`) and Service shows `80:<nodePort>/TCP`
- HTTPRoutes show `Accepted=True`
- Controller logs show successful xDS pushes with `clients:1`
- Proxy log shows `started bind bind="80/agentgateway-system/agentgateway-proxy"`
- curl gets `ECONNRESET` for the entire test duration (200+ seconds)

**Resolution:** This is a transient infrastructure issue. Re-running the test typically resolves it. On macOS, ensure `cloud-provider-kind` has proper permissions (`sudo`). If it recurs frequently, check Docker resource allocation (memory/CPU) for the Kind cluster.

**`/expect: unknown property "bodyJsonPath"` errors**

This error almost always means the `expect` block has bad indentation. `bodyJsonPath` must be a direct child of `expect:`, not nested under `statusCode` or `headers`. Double-check that all keys under `expect:` are at the same indentation level:

```yaml
  expect:
    statusCode: 200
    bodyJsonPath:
      - path: "$.choices[0].message.content"
        comparator: contains
        value: "hello"
```
