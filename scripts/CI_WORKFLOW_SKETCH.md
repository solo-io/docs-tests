# Sketch: consumer CI workflow change (Phase C) — superseded, kept for history

This sketch's original scope (checkout `docs-tests` for its `products/` *content*,
pass `DOCS_TESTS_ROOT`, keep invoking the consumer repo's own script copy) was applied
in Phase C and is now superseded by a further step: `agentgateway-oss-website` no
longer keeps its own copy of the scripts at all. Every job in
`.github/workflows/doc-tests.yaml` and every `Makefile` doc-test target now checks out
`docs-tests` and invokes its scripts directly —
`python3 docs-tests/scripts/doc_test_run.py --repo-root .`, not
`python3 scripts/doc_test_run.py`. The checkout step below is still exactly right; only
the invocation path changed:

```yaml
- name: Checkout docs-tests
  uses: actions/checkout@<pinned-sha>
  with:
    repository: solo-io/docs-tests
    path: docs-tests

- name: Run doc tests
  run: python3 docs-tests/scripts/doc_test_run.py --repo-root .
```

`--docs-tests-root`/`DOCS_TESTS_ROOT` are still relevant for the `products/` *content*
(`{{< doc-test file="..." >}}` and front-matter `assert:` resolution) — that's
independent of which repo's copy of the script is executing, and defaults to a sibling
`docs-tests` directory when unset, which the checkout step above already satisfies.

Every other consumer repo (Phase F hub workflow, Phase G rollout repos) needs the same
two things: a `docs-tests` checkout step, and its scripts invoked directly from that
checkout rather than a local copy.
