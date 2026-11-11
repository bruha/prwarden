"""Tests for what a review spent, reported per pull request and per run."""

import threading
import time
import unittest
from typing import Optional
from unittest.mock import Mock

from prwarden.cli import _MeasuredEngine, _ModelUsage, _usage_label
from prwarden.engine import report_invocation_credits, take_invocation_credits
from prwarden.metrics import ReviewMetrics
from prwarden.models import PullRequest, Repository, ReviewMaterial
from pathlib import Path
from tests.test_review_rules import without_review_rules


class ModelUsageTests(unittest.TestCase):
    def test_nothing_spent_says_nothing(self) -> None:
        """A run of pure skips must not report a cost it did not pay."""

        self.assertEqual(_ModelUsage().describe(), "")
        self.assertEqual(_ModelUsage().invocations, 0)

    def test_the_spend_names_each_level_it_paid_for(self) -> None:
        usage = _ModelUsage()

        usage.record("design", 61.0)
        usage.record("docs", 30.0)
        usage.record("code", 30.0)
        usage.record("code", 30.0)

        described = usage.describe()
        self.assertIn("code 2", described)
        self.assertIn("design", described)
        self.assertIn("docs", described)
        self.assertIn("4 invocations", described)
        self.assertEqual(usage.invocations, 4)
        self.assertEqual(usage.seconds, 151.0)

    def test_a_single_invocation_reads_as_one(self) -> None:
        usage = _ModelUsage()

        usage.record("code", 5.0)

        self.assertIn("1 invocation (", usage.describe())
        self.assertNotIn("invocations", usage.describe())

    def test_a_run_total_is_the_sum_of_its_pull_requests(self) -> None:
        run = _ModelUsage()
        for seconds in (10.0, 20.0):
            pull_request = _ModelUsage()
            pull_request.record("code", seconds)
            pull_request.record("code verification", 1.0)
            run.merge(pull_request)

        self.assertEqual(run.invocations, 4)
        self.assertEqual(run.seconds, 32.0)
        self.assertIn("code 2", run.describe())
        self.assertIn("code verification 2", run.describe())


class UsageLabelTests(unittest.TestCase):
    def material(self, **overrides) -> ReviewMaterial:
        return ReviewMaterial("diff", {}, **overrides)

    def test_each_level_and_its_verification_are_named_apart(self) -> None:
        cases = {
            "code": self.material(),
            "design": self.material(level="design"),
            "docs": self.material(level="docs"),
        }
        for label, material in cases.items():
            with self.subTest(label=label):
                self.assertEqual(_usage_label(material, "review"), label)

    def test_a_verification_pass_is_labelled_as_one(self) -> None:
        finding = Mock()
        self.assertEqual(
            _usage_label(self.material(verification_candidates=(finding,)), "verify"),
            "code verification",
        )
        self.assertEqual(
            _usage_label(
                self.material(level="design", design_candidates=(finding,)), "verify"
            ),
            "design verification",
        )
        self.assertEqual(
            _usage_label(
                self.material(level="docs", docs_candidates=(finding,)), "verify"
            ),
            "docs verification",
        )

    def test_the_fallback_is_named_as_the_fallback(self) -> None:
        """It is a second full review of the same change; that is worth seeing."""

        self.assertEqual(_usage_label(self.material(), "fallback"), "code fallback")

    def test_each_level_names_its_own_fallback(self) -> None:
        """Every level can hand over now, so "code fallback" alone would misname a
        design review the fallback did."""

        finding = Mock()
        for material, label in (
            (self.material(level="design"), "design fallback"),
            (self.material(level="docs"), "docs fallback"),
            (
                self.material(level="docs", docs_candidates=(finding,)),
                "docs fallback verification",
            ),
            (
                self.material(verification_candidates=(finding,)),
                "code fallback verification",
            ),
        ):
            with self.subTest(label=label):
                self.assertEqual(_usage_label(material, "fallback"), label)


class MeasuredEngineTests(unittest.TestCase):
    def engine_call(self, engine, material: ReviewMaterial):
        return engine.review(
            Repository("app", "owner/app", Path(".")),
            PullRequest(1, "Title", "", "author", "head", "url"),
            material,
        )

    def test_the_wrapper_returns_what_the_engine_returned(self) -> None:
        inner = Mock()
        inner.review.return_value = {"verdict": "approve"}
        usage = _ModelUsage()

        result = self.engine_call(
            _MeasuredEngine(inner, usage, "review"), ReviewMaterial("diff", {})
        )

        self.assertEqual(result, {"verdict": "approve"})
        self.assertEqual(usage.invocations, 1)

    def test_a_failed_invocation_is_still_counted(self) -> None:
        """A review that failed after four minutes still spent four minutes."""

        inner = Mock()
        inner.review.side_effect = RuntimeError("provider refused")
        usage = _ModelUsage()

        with self.assertRaises(RuntimeError):
            self.engine_call(
                _MeasuredEngine(inner, usage, "review"), ReviewMaterial("diff", {})
            )

        self.assertEqual(usage.invocations, 1)
        self.assertIn("code", usage.describe())

    def test_the_measured_duration_is_the_invocation(self) -> None:
        inner = Mock()
        inner.review.side_effect = lambda *args, **kwargs: time.sleep(0.05)
        usage = _ModelUsage()

        self.engine_call(
            _MeasuredEngine(inner, usage, "review"), ReviewMaterial("diff", {})
        )

        self.assertGreaterEqual(usage.seconds, 0.05)

    def test_keyword_arguments_reach_the_engine(self) -> None:
        """The verification pass passes its own timeout; wrapping must not eat it."""

        inner = Mock()
        usage = _ModelUsage()

        _MeasuredEngine(inner, usage, "verify").review(
            Repository("app", "owner/app", Path(".")),
            PullRequest(1, "Title", "", "author", "head", "url"),
            ReviewMaterial("diff", {}),
            timeout_seconds=42,
        )

        self.assertEqual(inner.review.call_args.kwargs, {"timeout_seconds": 42})

    def test_anything_else_the_adapter_exposes_stays_reachable(self) -> None:
        inner = Mock()
        inner.describe_engine.return_value = "antigravity"

        wrapped = _MeasuredEngine(inner, _ModelUsage(), "review")

        self.assertEqual(wrapped.describe_engine(), "antigravity")

    def test_what_a_call_cost_reaches_the_spend_and_the_measurement(self) -> None:
        """Kiro bills by credit, and the spend line is where an operator reads it."""

        inner = Mock()

        def costly_review(*_args, **_kwargs):
            report_invocation_credits(1.25)
            return {"verdict": "approve", "findings": []}

        inner.review.side_effect = costly_review
        usage = _ModelUsage()
        metrics = ReviewMetrics()

        self.engine_call(
            _MeasuredEngine(inner, usage, "review", metrics),
            ReviewMaterial("diff", {}),
        )

        self.assertEqual(metrics.invocations[0]["credits"], 1.25)
        self.assertEqual(metrics.summary()["credits"], 1.25)
        self.assertIn("of model time and 1.25 credits across 1", usage.describe())

    def test_a_cost_left_on_the_thread_is_not_charged_to_the_next_call(self) -> None:
        report_invocation_credits(9.0)
        inner = Mock()
        inner.review.return_value = {"verdict": "approve", "findings": []}
        usage = _ModelUsage()
        metrics = ReviewMetrics()

        self.engine_call(
            _MeasuredEngine(inner, usage, "review", metrics),
            ReviewMaterial("diff", {}),
        )

        self.assertNotIn("credits", metrics.invocations[0])
        self.assertIsNone(metrics.summary()["credits"])
        self.assertNotIn("credits", usage.describe())

    def test_each_thread_reports_only_its_own_cost(self) -> None:
        """One engine serves every parallel review, so the cost cannot live on it."""

        taken: "dict[float, Optional[float]]" = {}
        barrier = threading.Barrier(2)

        def call(credits: float) -> None:
            report_invocation_credits(credits)
            barrier.wait()
            taken[credits] = take_invocation_credits()

        threads = [threading.Thread(target=call, args=(value,)) for value in (1.0, 2.0)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(taken, {1.0: 1.0, 2.0: 2.0})

    def test_a_run_total_adds_up_the_costs_of_its_pull_requests(self) -> None:
        first = _ModelUsage()
        first.record("code", 60, 1.5)
        second = _ModelUsage()
        second.record("code", 60, 2.25)
        second.record("code verification", 30)
        total = _ModelUsage()

        total.merge(first)
        total.merge(second)

        self.assertEqual(total.credits, 3.75)
        self.assertIn("and 3.75 credits across 3", total.describe())
        self.assertIn("code 3.75 over 2 calls", total.describe())
        self.assertIn(
            "code verification with no credits reported", total.describe()
        )

    def test_a_priced_spend_says_what_each_stage_cost(self) -> None:
        """The total alone could not say which stage to cut."""

        usage = _ModelUsage()
        usage.record("code", 95, 4.12)
        usage.record("code verification", 84, 4.26)
        usage.record("design", 55, 3.1)

        self.assertEqual(
            usage.describe(),
            "3m54s of model time and 11.48 credits across 3 invocations "
            "(code 4.12, code verification 4.26, design 3.10)",
        )

    def test_a_priced_stage_counts_calls_so_no_count_reads_as_a_cost(self) -> None:
        usage = _ModelUsage()
        usage.record("code", 60, 1.5)
        usage.record("code", 60, 2.0)
        usage.record("code fallback", 60)
        usage.record("code fallback", 60)

        described = usage.describe()

        self.assertIn("(code 3.50 over 2 calls, ", described)
        self.assertIn("code fallback with no credits reported over 2 calls)", described)
        self.assertNotIn("code 2", described)


class UsageReportingTests(unittest.TestCase):
    """The two places an operator reads the spend: per pull request, and per run."""

    def setUp(self) -> None:
        without_review_rules(self)


    def test_a_reviewed_pull_request_reports_what_it_spent(self) -> None:
        import tempfile
        from io import StringIO
        from unittest.mock import patch

        from prwarden import cli
        from prwarden.config import Settings
        from prwarden.models import PullRequestSnapshot
        from prwarden.state import JsonStateStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            repository = Repository("app", "owner/app", root)
            settings = Settings(
                reviewer_username="reviewer",
                repositories=[repository],
                dry_run=True,
                slack_webhook_url=None,
                state_file=root / "state.json",
                review_engine="antigravity",
                review_engine_command=("agy",),
                review_engine_env_allowlist=(),
                review_model=None,
                review_timeout_seconds=300,
                max_diff_bytes=100000,
                max_findings=10,
                design_level=False,
                docs_level=False,
            )
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "The change is correct.",
                "findings": [],
            }
            run_usage = _ModelUsage()

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.list_pull_request_checks", return_value=()
            ), patch(
                "prwarden.cli._read_linked_issues", return_value=()
            ), patch(
                "prwarden.cli._read_file_history", return_value=()
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                return_value=PullRequestSnapshot(
                    head_sha="head",
                    state="open",
                    is_draft=False,
                    labels=frozenset(),
                    changed_files=1,
                    mergeable=True,
                ),
            ), patch(
                "prwarden.cli.review_workspace"
            ) as workspace, patch(
                "sys.stdout", new_callable=StringIO
            ) as output:
                workspace.return_value.__enter__.return_value = repository
                workspace.return_value.dependency_links = ()
                workspace.return_value.dependency_notes = ()
                with JsonStateStore(settings.state_file) as store:
                    cli._process_pull_request_with_head_restarts(
                        settings,
                        repository,
                        PullRequest(1, "Title", "", "author", "head", "url"),
                        store,
                        engine,
                        live=False,
                        force=False,
                        usage=run_usage,
                    )

            self.assertIn("-> Spent", output.getvalue())
            self.assertIn("of model time across 1 invocation (code)", output.getvalue())
            # The run total is fed by each pull request it reviewed.
            self.assertEqual(run_usage.invocations, 1)

    def test_a_skipped_pull_request_reports_no_spend(self) -> None:
        """A run of pure skips is the case this was built to make visible."""

        import tempfile
        from io import StringIO
        from unittest.mock import patch

        from prwarden import cli
        from prwarden.config import Settings
        from prwarden.state import JsonStateStore

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = Repository("app", "owner/app", root)
            settings = Settings(
                reviewer_username="reviewer",
                repositories=[repository],
                dry_run=True,
                slack_webhook_url=None,
                state_file=root / "state.json",
                review_engine="antigravity",
                review_engine_command=("agy",),
                review_engine_env_allowlist=(),
                review_model=None,
                review_timeout_seconds=300,
                max_diff_bytes=100000,
                max_findings=10,
                design_level=False,
                docs_level=False,
            )
            engine = Mock()
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")

            with patch("sys.stdout", new_callable=StringIO) as output:
                with JsonStateStore(settings.state_file) as store:
                    store.set_status(
                        store.key("owner/app", 1, "head", "github"), "delivered"
                    )
                    cli._process_pull_request_with_head_restarts(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=False,
                        force=False,
                    )

            engine.review.assert_not_called()
            self.assertNotIn("Spent", output.getvalue())


if __name__ == "__main__":
    unittest.main()
