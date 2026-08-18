# Sketch: consumer CI workflow change (Phase C)

Not applied yet — this documents what `agentgateway-oss-website/.github/workflows/doc-tests.yaml`
will need once the extractor change proven here in Phase A lands for real.

Add a checkout step for `docs-tests` before the existing script-generation/test-run steps:

```yaml
- name: Checkout docs-tests
  uses: actions/checkout@<pinned-sha>
  with:
    repository: solo-io/docs-tests
    path: docs-tests
```

Then pass its path to `doc_test_run.py`, either flag or env var (both are supported):

```yaml
- name: Run doc tests
  env:
    DOCS_TESTS_ROOT: ${{ github.workspace }}/docs-tests
  run: python3 scripts/doc_test_run.py
```

No change needed to the `kind`/`cloud-provider-kind` steps — this only affects how
`doc_test_extract.py` resolves `{{< doc-test file="..." >}}` content, not cluster
setup or teardown.

Every other consumer repo (Phase F hub workflow, Phase G rollout repos) needs the
equivalent two additions: a `docs-tests` checkout step, and `DOCS_TESTS_ROOT` (or
`--docs-tests-root`) passed to whatever invokes the extractor.
