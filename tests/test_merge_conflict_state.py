"""Tests for not re-reviewing a merge conflict that cannot have changed."""

import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prwarden import cli
from prwarden.commands import CommandError
from prwarden.config import Settings
from prwarden.models import PullRequest, PullRequestSnapshot, Repository
from prwarden.state import JsonStateStore
from tests.test_review_rules import without_review_rules


CONFLICTED_SNAPSHOT = PullRequestSnapshot(
    head_sha="head",
    state="open",
    is_draft=False,
    labels=frozenset(),
    changed_files=1,
    mergeable=False,
    base_ref="develop",
)
MERGEABLE_SNAPSHOT = replace(CONFLICTED_SNAPSHOT, mergeable=True)
UNSETTLED_SNAPSHOT = replace(CONFLICTED_SNAPSHOT, mergeable=None)


class MergeConflictStateTests(unittest.TestCase):
    def setUp(self) -> None:
        without_review_rules(self)

    def settings(self, directory: str, repository: Repository) -> Settings:
        return Settings(
            reviewer_username="reviewer",
            repositories=[repository],
            dry_run=False,
            slack_webhook_url="https://example.invalid/webhook",
            state_file=Path(directory) / "state.json",
            review_engine="antigravity",
            review_engine_command=("agy",),
            review_engine_env_allowlist=(),
            review_model=None,
            review_timeout_seconds=300,
            max_diff_bytes=100000,
            max_findings=10,
            design_level=False,
        )

    def run_review(
        self,
        directory: str,
        engine: Mock,
        *,
        base_tip: str = "base-1",
        recorded: dict = None,
        force: bool = False,
        snapshots: tuple = (CONFLICTED_SNAPSHOT,),
        expect_error: bool = False,
    ):
        root = Path(directory)
        (root / "app.py").write_text("value = 1\n", encoding="utf-8")
        repository = Repository("app", "owner/app", root)
        pull_request = PullRequest(1, "Title", "", "author", "head", "url")
        settings = self.settings(directory, repository)
        store = JsonStateStore(settings.state_file)
        state_key = store.key("owner/app", 1, "head", "github")
        if recorded is not None:
            with store as opened:
                opened.set_status(state_key, "merge_conflict", **recorded)
        # Each read of the pull request takes the next snapshot, and the last one
        # holds, so a test names only the sequence that matters to it.
        remaining = list(snapshots)

        def read_snapshot(*_args):
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

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
            "prwarden.cli._read_file_history", return_value=()
        ), patch(
            "prwarden.cli.get_pull_request_snapshot", side_effect=read_snapshot
        ) as snapshot, patch(
            "prwarden.cli.time.sleep"
        ) as sleep, patch(
            "prwarden.cli.get_branch_head_sha"
        ) as branch_head, patch(
            "prwarden.cli.review_workspace"
        ) as workspace, patch(
            "prwarden.cli.submit_approval", return_value="approval-1"
        ) as approval, patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            if isinstance(base_tip, Exception):
                branch_head.side_effect = base_tip
            else:
                branch_head.return_value = base_tip
            workspace.return_value.__enter__.return_value = repository
            workspace.return_value.dependency_links = ()
            workspace.return_value.dependency_notes = ()
            error = None
            with JsonStateStore(settings.state_file) as opened:
                try:
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        opened,
                        engine,
                        live=True,
                        force=force,
                        verification=None,
                    )
                except cli.ReviewError as raised:
                    # Opt-in, so a test asserting that nothing was reviewed cannot
                    # pass on an error that stopped the review for another reason.
                    if not expect_error:
                        raise
                    error = raised
                record = opened.get(state_key) or {}
        return {
            "terminal": output.getvalue(),
            "approval": approval,
            "record": record,
            "branch_head": branch_head,
            "snapshot": snapshot,
            "sleep": sleep,
            "error": error,
        }

    def clean_review(self) -> Mock:
        engine = Mock()
        engine.review.return_value = {
            "verdict": "approve",
            "approval_reason": "The change is correct.",
            "findings": [],
        }
        return engine

    def test_conflict_records_the_base_it_was_measured_against(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(directory, engine, base_tip="base-1")

            self.assertEqual(result["record"]["status"], "merge_conflict")
            self.assertEqual(result["record"]["base_sha"], "base-1")
            self.assertIn("Recorded as merge_conflict against develop", result["terminal"])
            result["approval"].assert_not_called()

    def test_unmoved_base_is_not_reviewed_again(self) -> None:
        """An unchanged conflict once cost five full reviews of the same pair."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory,
                engine,
                base_tip="base-1",
                recorded={"base_sha": "base-1"},
            )

            engine.review.assert_not_called()
            self.assertIn("merge conflict already recorded", result["terminal"])
            self.assertIn("which has not moved", result["terminal"])
            # The skip must leave the conflict recorded, or the next run pays for it
            # again: the in-progress marker is written before this decision.
            self.assertEqual(result["record"]["status"], "merge_conflict")
            self.assertEqual(result["record"]["base_sha"], "base-1")

    def test_moved_base_that_still_conflicts_is_not_reviewed_again(self) -> None:
        """A busy base branch moving must not buy a review of unchanged code."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory,
                engine,
                base_tip="base-2",
                recorded={"base_sha": "base-1"},
            )

            engine.review.assert_not_called()
            result["approval"].assert_not_called()
            self.assertIn("still conflicts with develop", result["terminal"])
            # Recorded against the new tip, so the next run skips without asking
            # GitHub again until the base moves once more.
            self.assertEqual(result["record"]["status"], "merge_conflict")
            self.assertEqual(result["record"]["base_sha"], "base-2")

    def test_moved_base_that_cleared_the_conflict_is_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory,
                engine,
                base_tip="base-2",
                recorded={"base_sha": "base-1"},
                snapshots=(MERGEABLE_SNAPSHOT,),
            )

            engine.review.assert_called_once()
            self.assertIn("conflict is gone; reviewing again", result["terminal"])
            result["approval"].assert_called_once()
            self.assertEqual(result["record"]["status"], "delivered")

    def test_unsettled_mergeability_keeps_the_record_for_the_next_run(self) -> None:
        """Reviewing would end at the same unanswered question, a review later."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory,
                engine,
                base_tip="base-2",
                recorded={"base_sha": "base-1"},
                snapshots=(UNSETTLED_SNAPSHOT,),
            )

            engine.review.assert_not_called()
            self.assertEqual(
                [call.args[0] for call in result["sleep"].call_args_list],
                list(cli.MERGEABILITY_RECHECK_DELAYS_SECONDS),
            )
            self.assertIn("asking again next run", result["terminal"])
            # The old base stays, so the next run still sees it moved and asks.
            self.assertEqual(result["record"]["status"], "merge_conflict")
            self.assertEqual(result["record"]["base_sha"], "base-1")

    def test_mergeability_settling_during_the_wait_decides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory,
                engine,
                base_tip="base-2",
                recorded={"base_sha": "base-1"},
                snapshots=(UNSETTLED_SNAPSHOT, CONFLICTED_SNAPSHOT),
            )

            engine.review.assert_not_called()
            self.assertEqual(result["sleep"].call_count, 1)
            self.assertEqual(result["record"]["base_sha"], "base-2")

    def test_head_moving_during_the_wait_is_superseded_not_skipped(self) -> None:
        """The wait revalidates the head, so a push still restarts the review."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory,
                engine,
                base_tip="base-2",
                recorded={"base_sha": "base-1"},
                snapshots=(
                    UNSETTLED_SNAPSHOT,
                    replace(CONFLICTED_SNAPSHOT, head_sha="newer"),
                ),
                expect_error=True,
            )

            engine.review.assert_not_called()
            self.assertIsInstance(result["error"], cli.PullRequestHeadChangedError)
            self.assertEqual(result["record"]["status"], "superseded")
            self.assertNotIn("Skipped", result["terminal"])

    def test_unmoved_base_does_not_ask_github_about_mergeability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory,
                engine,
                base_tip="base-1",
                recorded={"base_sha": "base-1"},
                snapshots=(UNSETTLED_SNAPSHOT,),
            )

            engine.review.assert_not_called()
            result["sleep"].assert_not_called()
            self.assertEqual(result["snapshot"].call_count, 1)

    def test_record_without_a_base_is_reviewed_again(self) -> None:
        """State written before the base was tracked has nothing to compare."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory, engine, base_tip="base-1", recorded={}
            )

            engine.review.assert_called_once()
            self.assertNotIn("already recorded", result["terminal"])

    def test_force_reviews_a_settled_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory,
                engine,
                base_tip="base-1",
                recorded={"base_sha": "base-1"},
                force=True,
            )

            engine.review.assert_called_once()
            self.assertNotIn("already recorded", result["terminal"])

    def test_unreadable_base_reviews_rather_than_skipping(self) -> None:
        """An unreadable branch must not silently stop reviewing a pull request."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory,
                engine,
                base_tip=CommandError("gh exited with 1: Not Found"),
                recorded={"base_sha": "base-1"},
            )

            engine.review.assert_called_once()
            self.assertIn("Could not read the base branch tip", result["terminal"])
            self.assertEqual(result["record"]["status"], "merge_conflict")
            self.assertEqual(result["record"]["base_sha"], "")

    def test_first_conflict_reads_the_branch_once(self) -> None:
        """Without a recorded conflict there is nothing to compare, only to record."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.clean_review()

            result = self.run_review(
                directory, engine, base_tip="base-1", recorded=None
            )

            self.assertEqual(result["branch_head"].call_count, 1)
            self.assertEqual(result["record"]["base_sha"], "base-1")


if __name__ == "__main__":
    unittest.main()
