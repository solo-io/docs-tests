"""Tests for classify_pr_results.

Run: python3 -m unittest discover -s scripts -p 'test_*.py'
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import classify_pr_results as c  # noqa: E402

PAGE = "content/en/p/kubernetes/latest/documentation/traffic-management/locality.md"
OTHER = "content/en/p/kubernetes/latest/documentation/quickstart/install.md"
KEY = f"{PAGE}::locality"


def failed(kind, file, line=70):
    return {"status": "failed", "failed_step": {
        "kind": kind, "file": file, "line": line,
        "command": "kubectl rollout restart deployment/agentgateway",
        "output_tail": 'deployments.apps "agentgateway" not found',
    }}


class VerdictTests(unittest.TestCase):
    def test_a_broken_page_step_on_a_changed_page_fails(self):
        v = c.verdict(KEY, failed("page", PAGE), {PAGE}, set())
        self.assertEqual(v["conclusion"], c.FAILURE)
        self.assertIn("locality.md", v["title"])
        self.assertNotIn("line 70", v["summary"])
        self.assertIn("kubectl rollout restart", v["summary"])

    def test_a_failed_docs_tests_assertion_is_a_test_update(self):
        """The stale-test case: the page ran, only the added check disagreed."""
        v = c.verdict(KEY, failed("assert", PAGE), {PAGE}, set())
        self.assertEqual(v["conclusion"], c.SKIPPED)
        self.assertEqual(v["title"], c.UPDATE_REQUIRED_TITLE)

    def test_a_failed_setup_step_is_the_pages_failure(self):
        """Locality: a hidden block running the page's own commands broke."""
        v = c.verdict(KEY, failed("setup", PAGE), {PAGE}, set())
        self.assertEqual(v["conclusion"], c.FAILURE)

    def test_a_legacy_check_kind_counts_as_an_assertion(self):
        self.assertEqual(c.verdict(KEY, failed("check", PAGE), {PAGE}, set())["conclusion"], c.SKIPPED)

    def test_a_page_step_on_a_page_the_pr_did_not_reach_is_a_test_update(self):
        v = c.verdict(KEY, failed("page", OTHER), {PAGE}, set())
        self.assertEqual(v["conclusion"], c.SKIPPED)

    def test_a_scenario_failing_on_main_that_passes_here_shows_the_fix(self):
        v = c.verdict(KEY, {"status": "passed"}, {PAGE}, {KEY})
        self.assertEqual(v["conclusion"], c.SUCCESS)
        self.assertIn("fails on main", v["title"])

    def test_a_scenario_failing_on_main_is_never_the_prs_failure(self):
        """Even a broken page step on a page the PR changed: it was broken before the PR."""
        v = c.verdict(KEY, failed("page", PAGE), {PAGE}, {KEY})
        self.assertEqual(v["conclusion"], c.SKIPPED)
        self.assertEqual(c.verdict(KEY, None, {PAGE}, {KEY})["conclusion"], c.SKIPPED)

    def test_a_known_failure_at_a_step_main_got_past_on_a_changed_page_is_the_prs(self):
        """Main ran this page step and failed later; the PR broke the page step."""
        ours = failed("page", PAGE)
        nightly = {KEY: {"kind": "assert", "file": PAGE, "command": "curl -sf localhost",
                         "reached": [c._step_hash(ours["failed_step"])]}}
        self.assertEqual(c.verdict(KEY, ours, {PAGE}, {KEY}, nightly)["conclusion"], c.FAILURE)

    def test_a_known_failure_at_a_step_main_never_reached_is_not_the_prs(self):
        """The nightly broke at install, so it never showed the PR's failing step working."""
        nightly = {KEY: {"kind": "setup", "file": OTHER, "command": "kubectl apply -f install.yaml",
                         "reached": []}}
        v = c.verdict(KEY, failed("page", PAGE), {PAGE}, {KEY}, nightly)
        self.assertEqual(v["conclusion"], c.SKIPPED)

    def test_a_known_failure_at_the_same_step_is_not_the_prs(self):
        ours = failed("page", PAGE, line=90)   # same step; the page moved it down
        nightly = {KEY: {**ours["failed_step"], "line": 70, "reached": []}}
        self.assertEqual(c.verdict(KEY, ours, {PAGE}, {KEY}, nightly)["conclusion"], c.SKIPPED)

    def test_a_nightly_that_failed_before_any_step_does_not_blame_the_pr(self):
        v = c.verdict(KEY, failed("page", PAGE), {PAGE}, {KEY}, {KEY: None})
        self.assertEqual(v["conclusion"], c.SKIPPED)

    def test_nightly_results_without_reached_steps_do_not_blame_the_pr(self):
        """Results from a runner older than `reached`: nothing says main got past the step."""
        nightly = {KEY: {"kind": "setup", "file": OTHER, "command": "kubectl apply -f install.yaml"}}
        self.assertEqual(c.verdict(KEY, failed("page", PAGE), {PAGE}, {KEY}, nightly)["conclusion"], c.SKIPPED)

    def test_a_known_failure_at_a_step_main_got_past_on_an_untouched_page_is_not_the_prs(self):
        ours = failed("page", OTHER)
        nightly = {KEY: {"kind": "setup", "file": PAGE, "command": "helm install",
                         "reached": [c._step_hash(ours["failed_step"])]}}
        self.assertEqual(c.verdict(KEY, ours, {PAGE}, {KEY}, nightly)["conclusion"], c.SKIPPED)

    def test_no_affected_set_means_every_page_counts(self):
        """A site-config or workflow change reaches every page."""
        v = c.verdict(KEY, failed("page", OTHER), None, set())
        self.assertEqual(v["conclusion"], c.FAILURE)

    def test_a_failure_with_no_step_recorded_is_not_blamed_on_the_pr(self):
        v = c.verdict(KEY, {"status": "failed", "error": "kind create failed"}, {PAGE}, set())
        self.assertEqual(v["conclusion"], c.SKIPPED)

    def test_a_missing_result_is_not_blamed_on_the_pr(self):
        self.assertEqual(c.verdict(KEY, None, {PAGE}, set())["conclusion"], c.SKIPPED)

    def test_a_pass_passes(self):
        self.assertEqual(c.verdict(KEY, {"status": "passed"}, {PAGE}, set())["conclusion"], c.SUCCESS)


class ClassifyTests(unittest.TestCase):
    def test_a_name_on_two_pages_gets_distinct_check_names(self):
        expected = [{"file": PAGE, "test": "dup"}, {"file": OTHER, "test": "dup"}]
        names = [v["name"] for v in c.classify(expected, {}, None, set())]
        self.assertEqual(len(set(names)), 2)

    def test_output_in_a_step_cannot_close_the_fence(self):
        fenced = c._fence("a ``` b")
        self.assertTrue(fenced.startswith("````\n"))

    def test_the_summary_says_no_action_is_needed_for_test_updates(self):
        verdicts = c.classify([{"file": PAGE, "test": "locality"}], {KEY: failed("assert", PAGE)}, {PAGE}, set())
        self.assertIn("do not need to do anything", c.summary_markdown(verdicts))



class GroupCheckTests(unittest.TestCase):
    def verdicts(self):
        return c.classify(
            [{"file": PAGE, "test": "locality"}, {"file": PAGE, "test": "llm"}, {"file": OTHER, "test": "ok"}],
            {KEY: failed("setup", PAGE), f"{PAGE}::llm": failed("assert", PAGE), f"{OTHER}::ok": {"status": "passed"}},
            {PAGE}, set(),
        )

    def test_three_groups_always_posted_under_fixed_names(self):
        names = [g["name"] for g in c.group_checks(self.verdicts())]
        self.assertEqual(names, [c.GROUP_NEEDS_FIX, c.GROUP_UPDATE, c.GROUP_PASSED])
        self.assertEqual([g["name"] for g in c.group_checks([])], names)

    def test_each_group_gets_its_conclusion(self):
        groups = {g["name"]: g for g in c.group_checks(self.verdicts())}
        self.assertEqual(groups[c.GROUP_NEEDS_FIX]["conclusion"], c.FAILURE)
        self.assertIn("locality", groups[c.GROUP_NEEDS_FIX]["summary"])
        self.assertEqual(groups[c.GROUP_UPDATE]["conclusion"], c.SKIPPED)
        self.assertIn("llm", groups[c.GROUP_UPDATE]["summary"])
        self.assertIn("ok", groups[c.GROUP_PASSED]["summary"])

    def test_an_empty_group_passes(self):
        groups = {g["name"]: g for g in c.group_checks([])}
        self.assertEqual(groups[c.GROUP_NEEDS_FIX]["conclusion"], c.SUCCESS)
        self.assertEqual(groups[c.GROUP_UPDATE]["conclusion"], c.SUCCESS)

    def test_a_long_summary_is_cut_to_githubs_limit(self):
        self.assertLessEqual(len(c._cap("x" * 70000)), c.SUMMARY_LIMIT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
