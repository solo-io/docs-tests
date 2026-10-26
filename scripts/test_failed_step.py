#!/usr/bin/env python3
"""Tests for the failed step a doc test run records.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'

classify_pr_results.py compares a PR's failed step with the nightly run's
`reached` list, so the runner and the classifier must give a step the same id.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import classify_pr_results as c  # noqa: E402
from doc_test_extract import step_marker  # noqa: E402
from doc_test_run import failed_step_from, step_hash  # noqa: E402

PAGE = "content/en/p/install.md"
TEST = "tests/check.sh"

SCRIPT = "\n".join([
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    "",
    f"# Source: {PAGE}:10 paths=all",
    step_marker("setup", PAGE, 10),
    "helm install agentgateway",
    "",
    f"# Source: {PAGE}:20 paths=all",
    step_marker("page", PAGE, 20),
    "kubectl apply -f route.yaml",
    "kubectl get route",
    "",
    f"# Test include: {PAGE}:30 paths=all file={TEST}",
    step_marker("assert", PAGE, 30),
    "curl -sf localhost",
    "",
])


class FailedStepTests(unittest.TestCase):
    def test_it_records_the_failing_step_and_the_steps_before_it(self):
        step = failed_step_from(f"page {PAGE}:20", SCRIPT, "error: route.yaml not found\n")
        self.assertEqual(step["kind"], "page")
        self.assertEqual(step["line"], 20)
        self.assertEqual(step["command"], "kubectl apply -f route.yaml\nkubectl get route")
        self.assertEqual(step["reached"], [step_hash("setup", PAGE, "helm install agentgateway")])

    def test_the_first_step_reached_nothing(self):
        self.assertEqual(failed_step_from(f"setup {PAGE}:10", SCRIPT, "")["reached"], [])

    def test_no_marker_means_no_step(self):
        self.assertIsNone(failed_step_from("", SCRIPT, "kind create failed"))

    def test_the_runner_and_the_classifier_agree_on_step_ids(self):
        """A PR failing at the page step, where main got past it and failed at the assert."""
        nightly = failed_step_from(f"assert {PAGE}:30", SCRIPT, "")
        ours = failed_step_from(f"page {PAGE}:20", SCRIPT, "")
        self.assertIn(c._step_hash(ours), nightly["reached"])

    def test_a_moved_step_keeps_its_id(self):
        moved = SCRIPT.replace(f"{PAGE}:20", f"{PAGE}:25")
        nightly = failed_step_from(f"assert {PAGE}:30", moved, "")
        ours = failed_step_from(f"page {PAGE}:20", SCRIPT, "")
        self.assertIn(c._step_hash(ours), nightly["reached"])


if __name__ == "__main__":
    unittest.main()
