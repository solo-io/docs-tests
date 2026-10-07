#!/usr/bin/env python3
"""Tests for kind cluster naming.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'

These exist because the failure they guard against is expensive and does not
name itself. When the control-plane node name overflows 63 characters, kind
still creates the container; kubeadm then fails ~4 minutes in with
"[-]poststarthook/rbac/bootstrap-roles failed: reason withheld" and a livez
timeout, which says nothing about names or lengths. It took four failing CI
shards to trace back to a scenario name that had grown by the 11 characters of
a "::functional" suffix.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from doc_test_run import (  # noqa: E402
    KIND_NODE_SUFFIX,
    MAX_CLUSTER_NAME,
    MAX_DNS_LABEL,
    make_cluster_name,
    sanitize_name,
)


class ClusterNameTests(unittest.TestCase):
    def node_name(self, scenario: str, prefix: str = "doc-test") -> str:
        return make_cluster_name(prefix, sanitize_name(scenario)) + KIND_NODE_SUFFIX

    def test_short_names_are_left_alone(self):
        """No churn for the common case: existing cluster names must not move."""
        self.assertEqual(self.node_name("csrf"), "doc-test-csrf-control-plane")

    def test_the_names_that_actually_broke_ci(self):
        for scenario in (
            "per-try-timeout-in-gatewaylistener::functional",
            "per-try-timeout-in-agentgateway::functional",
        ):
            with self.subTest(scenario=scenario):
                self.assertLessEqual(len(self.node_name(scenario)), MAX_DNS_LABEL)

    def test_no_length_of_scenario_name_can_overflow(self):
        for n in (1, 40, 49, 50, 63, 64, 200):
            with self.subTest(length=n):
                self.assertLessEqual(len(self.node_name("x" * n)), MAX_DNS_LABEL)

    def test_a_multi_type_suffix_cannot_push_a_name_over(self):
        """The regression itself: `name` fits, `name::type` must fit too."""
        base = "per-try-timeout-in-gatewaylistener"
        self.assertLessEqual(len(self.node_name(base)), MAX_DNS_LABEL)
        for suffix in ("schema", "functional", "credentialed"):
            with self.subTest(type=suffix):
                self.assertLessEqual(len(self.node_name(f"{base}::{suffix}")), MAX_DNS_LABEL)

    def test_truncated_names_stay_distinct(self):
        """Two long scenarios sharing a prefix must not land on one cluster."""
        stem = "a-very-long-scenario-name-that-will-certainly-be-truncated"
        a = make_cluster_name("doc-test", sanitize_name(f"{stem}-one::functional"))
        b = make_cluster_name("doc-test", sanitize_name(f"{stem}-two::functional"))
        self.assertNotEqual(a, b)

    def test_names_remain_valid_dns_labels(self):
        import re
        for scenario in ("csrf", "x" * 90, "per-try-timeout-in-gatewaylistener::functional"):
            with self.subTest(scenario=scenario):
                name = make_cluster_name("doc-test", sanitize_name(scenario))
                self.assertRegex(name, r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
                self.assertLessEqual(len(name), MAX_CLUSTER_NAME)


if __name__ == "__main__":
    unittest.main()
