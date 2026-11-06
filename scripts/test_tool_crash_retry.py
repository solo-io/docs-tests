#!/usr/bin/env python3
"""Tests for rerunning a test that failed on a tool crash.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'

Helm 4's `--wait` can panic with "send on closed channel" partway through an
install that would have succeeded (agentgateway/website run 42271149023). The
runner reruns the whole test once for that crash and for nothing else, so these
tests pin both halves: the crash is retried, and real failures are not.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from doc_test_run import TOOL_CRASH_ATTEMPTS, run_with_tool_crash_retry  # noqa: E402

HELM_PANIC = (
    "Release \"agentgateway\" does not exist. Installing it now.\n"
    "panic: send on closed channel\n\n"
    "goroutine 490 [running]:\n"
    "github.com/fluxcd/cli-utils/pkg/kstatus/watcher.(*ObjectStatusReporter).newStatusCheckTaskFunc.func1()\n"
)


class FakeRuns:
    """Hands back the given results in order and counts the calls."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return dict(self.results.pop(0))


class ToolCrashRetryTests(unittest.TestCase):
    def test_a_pass_runs_once(self):
        runs = FakeRuns({"status": "passed", "checks": []})
        result = run_with_tool_crash_retry(runs, "page.md::t")
        self.assertEqual(runs.calls, 1)
        self.assertEqual(result["status"], "passed")
        self.assertNotIn("retried", result)

    def test_a_real_failure_is_not_retried(self):
        runs = FakeRuns({"status": "failed", "error": "error: no matches for kind \"HTTPRoute\""})
        result = run_with_tool_crash_retry(runs, "page.md::t")
        self.assertEqual(runs.calls, 1)
        self.assertNotIn("retried", result)

    def test_some_other_panic_is_not_retried(self):
        """A crashing pod whose logs a test prints is a real failure."""
        runs = FakeRuns({"status": "failed", "error": "panic: runtime error: index out of range"})
        run_with_tool_crash_retry(runs, "page.md::t")
        self.assertEqual(runs.calls, 1)

    def test_helm_panic_then_pass_is_marked_retried(self):
        runs = FakeRuns(
            {"status": "failed", "error": HELM_PANIC},
            {"status": "passed", "checks": ["✓ ok"]},
        )
        result = run_with_tool_crash_retry(runs, "page.md::t")
        self.assertEqual(runs.calls, 2)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["retried"], 1)

    def test_helm_panic_every_time_stops_at_the_limit(self):
        runs = FakeRuns(*[{"status": "failed", "error": HELM_PANIC}] * TOOL_CRASH_ATTEMPTS)
        result = run_with_tool_crash_retry(runs, "page.md::t")
        self.assertEqual(runs.calls, TOOL_CRASH_ATTEMPTS)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["retried"], TOOL_CRASH_ATTEMPTS - 1)


if __name__ == "__main__":
    unittest.main()
