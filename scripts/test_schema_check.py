#!/usr/bin/env python3
"""The schema check counts only validated resources, and checks each version tree
against that version's CRDs (solo-io/docs-tests#19).

Before: a scenario of only Gateway API kinds passed with nothing validated, and
every tree was checked against one CRD set with gates resolved as `main`.
"""
import contextlib
import importlib.util
import io
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

HUGO = """params:
  versions:
    - version: "1.8.x"
      linkVersion: "main"
    - version: "1.7.x"
      linkVersion: "latest"
"""

# `retries` exists only in the newer CRD, the way a field added on main would.
CRD = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
spec:
  group: example.dev
  names: {{kind: Policy}}
  versions:
  - name: v1
    served: true
    schema:
      openAPIV3Schema:
        type: object
        properties:
          apiVersion: {{type: string}}
          kind: {{type: string}}
          metadata: {{type: object}}
          spec:
            type: object
            properties:
              timeout: {{type: string}}{extra}
"""
OLD_CRD = CRD.format(extra="")
NEW_CRD = CRD.format(extra="\n              retries: {type: integer}")


def page(body):
    return f"""---
title: Policy
test:
  policy:
    type: schema
    steps:
    - path: policy
---

{body}
"""


def policy_block(spec):
    return f"""```yaml {{paths="policy"}}
kubectl apply -f- <<EOF
apiVersion: example.dev/v1
kind: Policy
metadata:
  name: p
spec:
  {spec}
EOF
```"""


ROUTE_ONLY = page("""```yaml {paths="policy"}
kubectl apply -f- <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: r
spec: {}
EOF
```""")

# The gate shows each version the field its own release has.
GATED = page(
    '{{< version include-if="1.7.x" >}}\n' + policy_block("timeout: 10s") + "\n{{< /version >}}\n\n"
    '{{< version include-if="1.8.x" >}}\n' + policy_block("retries: 3") + "\n{{< /version >}}"
)


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args)


class Base(unittest.TestCase):
    def setUp(self):
        self.repo = pathlib.Path(tempfile.mkdtemp())
        (self.repo / "hugo.yaml").write_text(HUGO)
        self.old = pathlib.Path(tempfile.mkdtemp())
        (self.old / "policy.yaml").write_text(OLD_CRD)
        self.new = pathlib.Path(tempfile.mkdtemp())
        (self.new / "policy.yaml").write_text(NEW_CRD)

    def write(self, version, text):
        rel = f"content/docs/kubernetes/{version}/policy.md"
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / rel).write_text(text)
        return f"{rel}::policy"

    def run_check(self, *crd_dirs):
        """Each argument is a --crd-dir value, or a whole --flag=value."""
        import doc_test_schema_check
        argv = ["doc_test_schema_check.py", "--repo-root", str(self.repo),
                "--docs-tests-root", str(self.repo), "--report-file", str(self.repo / "out/schema.yaml")]
        for d in crd_dirs:
            argv += [d] if d.startswith("--") else ["--crd-dir", d]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": ""}):
            code = quiet(doc_test_schema_check.main)
        return code, yaml.safe_load((self.repo / "out/schema.yaml").read_text())["tests"]


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
class SchemaCheckTests(Base):
    def test_scenario_with_no_crd_kind_fails(self):
        key = self.write("main", ROUTE_ONLY)
        code, tests = self.run_check(str(self.new))
        self.assertEqual(code, 1)
        self.assertIn("found only HTTPRoute", tests[key]["error"])

    def test_each_tree_uses_its_own_crds_and_gates(self):
        latest = self.write("latest", GATED)
        main = self.write("main", GATED)
        code, tests = self.run_check(f"latest={self.old}", f"main={self.new}")
        self.assertEqual(code, 0, tests)
        self.assertEqual(tests[latest]["status"], "passed")
        self.assertEqual(tests[main]["status"], "passed")

    def test_latest_page_resolves_gates_as_latest(self):
        """Resolved as main, the latest page would show `retries` and fail the 1.7 CRD."""
        latest = self.write("latest", GATED)
        code, tests = self.run_check(str(self.old))
        self.assertEqual(code, 0, tests)
        self.assertEqual(tests[latest]["status"], "passed")

    def test_main_only_field_fails_older_crds(self):
        """The check is real: main's gated field is rejected by a CRD that lacks it."""
        main = self.write("main", GATED)
        code, tests = self.run_check(str(self.old))
        self.assertEqual(code, 1)
        self.assertIn("retries", tests[main]["error"])

    def test_untagged_manifest_on_the_page_is_checked(self):
        """The badge covers every resource on the page, not only the tagged ones."""
        untagged = policy_block("bogus: 1").replace(' {paths="policy"}', "")
        key = self.write("main", page(policy_block("timeout: 10s") + "\n\n" + untagged))
        code, tests = self.run_check(str(self.new))
        self.assertIn("Policy at spec", tests[key]["error"])
        self.assertIn("bogus", tests[key]["error"])

    def test_bare_dir_is_the_fallback(self):
        latest = self.write("latest", GATED)
        main = self.write("main", GATED)
        code, tests = self.run_check(f"latest={self.old}", str(self.new))
        self.assertEqual(code, 0, tests)

    def test_version_with_no_crds_fails(self):
        latest = self.write("latest", GATED)
        code, tests = self.run_check(f"main={self.new}")
        self.assertEqual(code, 1)
        self.assertIn("no CRD schemas for version 'latest'", tests[latest]["error"])


ROUTE_CRD = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
spec:
  group: gateway.networking.k8s.io
  names: {kind: HTTPRoute}
  versions:
  - name: v1
    served: true
    schema:
      openAPIV3Schema:
        type: object
        properties:
          apiVersion: {type: string}
          kind: {type: string}
          metadata: {type: object}
          spec:
            type: object
            properties:
              hostnames: {type: array, items: {type: string}}
  - name: v1alpha1
    served: false
    schema:
      openAPIV3Schema: {type: object}
"""

CONFIG_SCHEMA = """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "binds": {"type": "array", "items": {"type": "object", "additionalProperties": false,
              "properties": {"port": {"type": "integer"}}}},
    "llm": {"type": "object"}
  }
}"""


def route(api_version, spec):
    return page(f"""```yaml {{paths="policy"}}
kubectl apply -f- <<EOF
apiVersion: {api_version}
kind: HTTPRoute
metadata:
  name: r
spec:
  {spec}
EOF
```""")


def config_page(body):
    return page(f"""```sh {{paths="policy"}}
cat <<'EOF' > config.yaml
{body}
EOF
agentgateway -f config.yaml
```""")


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
class RebaseShellTests(Base):
    """A site whose pages are {{< rebase >}} shells has its blocks in another
    repo. Without --upstream-root the shell has nothing to check."""

    def setUp(self):
        super().setUp()
        self.upstream = pathlib.Path(tempfile.mkdtemp())
        up = self.upstream / "content/docs/kubernetes/main/policy.md"
        up.parent.mkdir(parents=True)
        up.write_text("---\ntitle: Policy\n---\n\n" + policy_block("bogus: 1"))
        self.key = self.write("latest", page('{{< rebase file="agw-docs/kubernetes/policy.md" >}}'))

    def test_without_upstream_the_shell_has_nothing_to_check(self):
        code, tests = self.run_check(str(self.new))
        self.assertEqual(code, 1)
        self.assertIn("found no recognizable", tests[self.key]["error"])

    def test_with_upstream_the_upstream_blocks_are_checked(self):
        code, tests = self.run_check(str(self.new), f"--upstream-root={self.upstream}",
                                     '--upstream-versions={"latest": "main"}')
        self.assertEqual(code, 1)
        self.assertIn("bogus", tests[self.key]["error"])

    def test_upstream_root_needs_a_version_map(self):
        import doc_test_schema_check
        argv = ["x", "--repo-root", str(self.repo), "--crd-dir", str(self.new),
                f"--upstream-root={self.upstream}"]
        with mock.patch.object(sys, "argv", argv):
            self.assertEqual(quiet(doc_test_schema_check.main), 1)


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
class MoreKindsTests(Base):
    """Gateway API kinds, core kinds, and standalone config files."""

    def setUp(self):
        super().setUp()
        self.gw = pathlib.Path(tempfile.mkdtemp()) / "experimental-install.yaml"
        self.gw.write_text(ROUTE_CRD)
        self.config = pathlib.Path(tempfile.mkdtemp()) / "config.json"
        self.config.write_text(CONFIG_SCHEMA)

    def test_gateway_api_kind_is_checked(self):
        key = self.write("main", route("gateway.networking.k8s.io/v1", "hostnames: [a.example.com]"))
        code, tests = self.run_check(str(self.new), str(self.gw))
        self.assertEqual(code, 0, tests)
        key = self.write("main", route("gateway.networking.k8s.io/v1", "hostname: a.example.com"))
        code, tests = self.run_check(str(self.new), str(self.gw))
        self.assertIn("hostname", tests[key]["error"])

    def test_unserved_api_version_fails(self):
        key = self.write("main", route("gateway.networking.k8s.io/v1alpha1", "hostnames: [a]"))
        code, tests = self.run_check(str(self.gw))
        self.assertIn("is not served by its CRD (serves gateway.networking.k8s.io/v1)", tests[key]["error"])

    def test_same_kind_in_another_group_is_not_matched(self):
        key = self.write("main", route("networking.istio.io/v1", "anything: 1"))
        code, tests = self.run_check(str(self.gw))
        self.assertIn("found only HTTPRoute", tests[key]["error"])

    def run_config(self, version, text, *extra):
        rel = f"content/docs/standalone/{version}/config.md"
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / rel).write_text(text)
        code, tests = self.run_check(str(self.new), *extra)
        return code, tests[f"{rel}::policy"]

    def test_standalone_config_file_is_checked(self):
        code, result = self.run_config("main", config_page("binds:\n- port: 3000"), f"--config-schema={self.config}")
        self.assertEqual(result["status"], "passed", result)
        code, result = self.run_config("main", config_page("binds:\n- prot: 3000"), f"--config-schema={self.config}")
        self.assertIn("config file at binds/0", result["error"])

    def test_untagged_visible_config_block_is_checked(self):
        """The reader's copy is untagged; the test runs a hidden copy of it."""
        body = config_page("binds:\n- port: 3000").replace("---\n\n```sh", "---\n\n```yaml\nbinds:\n- prot: 3000\n```\n\n```sh", 1)
        code, result = self.run_config("main", body, f"--config-schema={self.config}")
        self.assertIn("config file at binds/0", result["error"])

    def test_lookalikes_are_not_config_files(self):
        import doc_test_schema_check as S
        v = S.load_config_schema(self.config)
        v.schema["properties"]["services"] = {"type": "array"}
        v.schema["properties"]["config"] = {"type": "object"}
        self.assertTrue(S.is_config_file({"binds": [{"port": 1}]}, v))
        # Docker Compose's `services:` is a mapping; the config's is a list.
        self.assertFalse(S.is_config_file({"services": {"redis": {"image": "redis"}}}, v))
        # Helm values for the standalone chart nest the whole config.
        self.assertFalse(S.is_config_file({"config": {"binds": []}}, v))
        self.assertFalse(S.is_config_file({"binds": [{"port": 1}]}, v, "binds:\n- port: 1\n  # ...\n"))
        self.assertFalse(S.is_config_file({"binds": [{"port": 1}]}, v, "binds:\n- port: 1\n  # ... your listeners ...\n"))
        # A comment that only mentions an ellipsis later is not an elision.
        self.assertTrue(S.is_config_file({"binds": [{"port": 1}]}, v, "binds:\n- port: 1  # see ... below\n"))

    def test_shell_variable_is_not_judged(self):
        key = self.write("main", route("gateway.networking.k8s.io/v1", "hostnames: [\"${DOMAIN}\"]"))
        ROUTE = ROUTE_CRD.replace("items: {type: string}", "items: {type: string, pattern: '^[a-z.]+$'}")
        self.gw.write_text(ROUTE)
        code, tests = self.run_check(str(self.gw))
        self.assertEqual(code, 0, tests)
        self.write("main", route("gateway.networking.k8s.io/v1", "hostnames: [A_B]"))
        code, tests = self.run_check(str(self.gw))
        self.assertIn("does not match", tests[key]["error"])

    def test_config_schema_is_per_version(self):
        code, result = self.run_config("latest", config_page("binds:\n- port: 3000"), f"--config-schema=main={self.config}")
        self.assertIn("found no recognizable", result["error"])

    def test_fragment_and_kubernetes_pages_are_not_config_files(self):
        code, result = self.run_config("main", config_page("policies:\n  cors: {}"), f"--config-schema={self.config}")
        self.assertIn("found no recognizable", result["error"])
        key = self.write("main", config_page("binds:\n- prot: 3000"))
        code, tests = self.run_check(str(self.new), f"--config-schema={self.config}")
        self.assertIn("found no recognizable", tests[key]["error"])


KUBECONFORM = os.environ.get("KUBECONFORM") or shutil.which("kubeconform")


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
@unittest.skipUnless(KUBECONFORM, "kubeconform is not installed (set KUBECONFORM to its path)")
class KubeconformTests(Base):
    """Needs kubeconform and network access to its schema registry."""

    def service(self, port):
        return self.write("main", page(f"""```yaml {{paths="policy"}}
apiVersion: v1
kind: Service
metadata:
  name: s
spec:
  ports:
  - port: {port}
---
apiVersion: example.dev/v1
kind: Unknown
metadata:
  name: u
```"""))

    def test_core_kind_is_checked(self):
        key = self.service(8080)
        code, tests = self.run_check(str(self.new), f"--kubeconform={KUBECONFORM}", "--kubernetes-version=1.36.0")
        self.assertEqual(code, 0, tests)
        key = self.service('"http"')
        code, tests = self.run_check(str(self.new), f"--kubeconform={KUBECONFORM}", "--kubernetes-version=1.36.0")
        self.assertIn("Service at /spec/ports/0/port", tests[key]["error"])

    def test_without_kubeconform_core_kinds_are_unchecked(self):
        key = self.service(8080)
        code, tests = self.run_check(str(self.new))
        self.assertIn("found only Service, Unknown", tests[key]["error"])


class ParseVersionedTests(unittest.TestCase):
    def test_forms(self):
        import doc_test_schema_check as S
        paths = S.parse_versioned(["latest=a", "/tmp/x=y", "main=b", "c"])
        self.assertEqual(paths["latest"], [pathlib.Path("a").resolve()])
        self.assertEqual(paths[None], [pathlib.Path("/tmp/x=y").resolve(), pathlib.Path("c").resolve()])


if __name__ == "__main__":
    unittest.main()
