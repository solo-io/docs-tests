#!/usr/bin/env python3
"""The schema check's API server pass: CRD CEL rules (solo-io/docs-tests#19).

`jsonschema` ignores `x-kubernetes-validations`, so a manifest that breaks a
CEL rule passed the schema check. With --apiserver (the default), each custom resource that
passes its schema is also created as a dry run on a bare API server.

The end-to-end tests need envtest's binaries: set KUBEBUILDER_ASSETS, or run
the check with --apiserver once so they are in the cache.
"""
import importlib.util
import pathlib
import shutil
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import doc_test_apiserver as api  # noqa: E402
from doc_test_apiserver import field_path, pick_release, rejection  # noqa: E402

# A CEL rule `jsonschema` can't see: maxRetries needs retries set.
CEL_CRD = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: policies.example.dev
spec:
  group: example.dev
  scope: Namespaced
  names: {kind: Policy, plural: policies, singular: policy, listKind: PolicyList}
  versions:
  - name: v1
    served: true
    storage: true
    schema:
      openAPIV3Schema:
        type: object
        properties:
          apiVersion: {type: string}
          kind: {type: string}
          metadata: {type: object}
          spec:
            type: object
            x-kubernetes-validations:
            - rule: "!has(self.maxRetries) || has(self.retries)"
              message: maxRetries requires retries
            properties:
              timeout: {type: string}
              retries: {type: integer}
              maxRetries: {type: integer}
              host:
                type: string
                x-kubernetes-validations:
                - rule: "isURL(self)"
                  message: host must be a URL
"""


class PickReleaseTests(unittest.TestCase):
    RELEASES = {"v1.34.1": {}, "v1.35.0": {}, "v1.36.0": {}, "v1.36.2": {}}

    def test_newest_patch_of_the_minor(self):
        self.assertEqual(pick_release(self.RELEASES, "1.36.0"), "v1.36.2")
        self.assertEqual(pick_release(self.RELEASES, "1.35"), "v1.35.0")

    def test_newest_older_minor_when_the_minor_has_none(self):
        self.assertEqual(pick_release(self.RELEASES, "1.38"), "v1.36.2")

    def test_newest_without_a_version(self):
        self.assertEqual(pick_release(self.RELEASES, None), "v1.36.2")

    def test_nothing_old_enough(self):
        with self.assertRaises(RuntimeError):
            pick_release(self.RELEASES, "1.20")


class RejectionTests(unittest.TestCase):
    MANIFEST = {"kind": "Policy", "metadata": {"name": "${NAME}"},
                "spec": {"host": "${HOST}", "retries": 3, "rules": [{"path": "/a"}, {"path": "$P"}]}}

    def status(self, *fields):
        return {"message": "Policy is invalid", "details": {"causes": [
            {"reason": "FieldValueInvalid", "message": f"bad {f}", "field": f} for f in fields]}}

    def test_field_path(self):
        self.assertEqual(field_path("spec.rules[1].path"), ["spec", "rules", 1, "path"])
        self.assertEqual(field_path("metadata.labels[app.kubernetes.io/name]"),
                         ["metadata", "labels", "app.kubernetes.io/name"])
        self.assertEqual(field_path(""), [])

    def test_causes_on_shell_variables_are_dropped(self):
        self.assertIsNone(rejection(self.MANIFEST, self.status("metadata.name", "spec.host", "spec.rules[1].path")))

    def test_other_causes_are_kept(self):
        self.assertEqual(rejection(self.MANIFEST, self.status("spec.host", "spec.retries", "spec.rules[0].path")),
                         "at spec.retries: bad spec.retries; at spec.rules[0].path: bad spec.rules[0].path")

    def test_a_rule_on_an_object_holding_a_shell_variable_is_dropped(self):
        # spec's own rule might compare spec.host, which the shell fills in.
        self.assertIsNone(rejection(self.MANIFEST, self.status("spec")))

    def test_angle_bracket_placeholders_are_dropped(self):
        manifest = {"metadata": {"name": "<policy_name>", "namespace": "<namespace>"}, "spec": {}}
        self.assertIsNone(rejection(manifest, self.status("metadata.name", "metadata.namespace")))

    def test_follow_on_cause_alone_is_dropped(self):
        status = self.status("spec.host")
        status["details"]["causes"].append({"reason": "FieldValueInvalid", "field": "<nil>", "message":
                                            "Invalid value: null: " + api.FOLLOW_ON + "; correct the existing errors"})
        self.assertIsNone(rejection(self.MANIFEST, status))

    def test_status_without_causes_is_its_message(self):
        self.assertEqual(rejection(self.MANIFEST, {"message": "strict decoding error"}), "strict decoding error")


def _binaries():
    try:
        return api.ensure_binaries(None) if api.os.environ.get("KUBEBUILDER_ASSETS") else next(
            d for d in sorted(api.CACHE_DIR.glob("v*")) if all((d / b).is_file() for b in api.BINARIES))
    except (StopIteration, RuntimeError, OSError):
        return None


BINARIES = _binaries()


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
@unittest.skipUnless(BINARIES and shutil.which("openssl"), "envtest binaries or openssl not found")
class EndToEndTests(unittest.TestCase):
    """Each run starts its own API server, as a CI run does, so each takes a few seconds."""

    @classmethod
    def setUpClass(cls):
        import test_schema_check as t
        cls.t = t
        cls.base = t.Base()
        cls.base.setUp()
        cls.crds = pathlib.Path(cls.base.new)
        (cls.crds / "policy.yaml").write_text(CEL_CRD)

    def run_check(self, spec):
        key = self.base.write("main", self.t.page(self.t.policy_block(spec)))
        return key, self.base.run_check(str(self.crds), "--apiserver", f"--envtest-dir={BINARIES}")

    def test_manifest_that_breaks_a_cel_rule_fails(self):
        key, (code, tests) = self.run_check("maxRetries: 5")
        self.assertEqual(code, 1)
        self.assertIn("rejected by the Kubernetes API server", tests[key]["error"])
        self.assertIn("maxRetries requires retries", tests[key]["error"])

    def test_manifest_that_keeps_the_rules_passes(self):
        key, (code, tests) = self.run_check("retries: 1\n  maxRetries: 5\n  host: https://example.com")
        self.assertEqual(code, 0, tests)

    def test_kubernetes_cel_library_is_used(self):
        # isURL is a Kubernetes CEL extension, not plain CEL.
        key, (code, tests) = self.run_check("host: not a url")
        self.assertIn("host must be a URL", tests[key]["error"])

    def test_shell_variable_is_not_judged(self):
        key, (code, tests) = self.run_check("host: ${HOST}")
        self.assertEqual(code, 0, tests)

    def test_missing_name_and_shell_variable_namespace_are_filled_in(self):
        # A fragment without a name passes the static check; so must the dry run.
        key = self.base.write("main", self.t.page(self.t.policy_block("retries: 1").replace(
            "  name: p\n", "  namespace: ${NS}\n")))
        code, tests = self.base.run_check(str(self.crds), "--apiserver", f"--envtest-dir={BINARIES}")
        self.assertEqual(code, 0, tests)

    def test_schema_error_is_reported_once(self):
        # Fails the static check, so it never reaches the API server.
        key, (code, tests) = self.run_check("retries: three")
        self.assertNotIn("API server", tests[key]["error"])


if __name__ == "__main__":
    unittest.main()
