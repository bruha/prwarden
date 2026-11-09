"""A repository kept out of Slack: own pull requests on GitHub, no Slack at all."""

import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import prwarden.cli as cli
from prwarden.commands import CommandError
from prwarden.config import ConfigError, Settings, load_repositories
from prwarden.github import LGTM_COMMENT_MARKER, format_lgtm_comment
from prwarden.models import (
    DiffBudget,
    DeferredFile,
    PriorReviewComment,
    PullRequest,
    PullRequestSnapshot,
    Repository,
)
from prwarden.state import JsonStateStore
from tests.test_review_rules import without_review_rules


READY_SNAPSHOT = PullRequestSnapshot(
    head_sha="head",
    state="open",
    is_draft=False,
    labels=frozenset(),
    changed_files=1,
    mergeable=True,
)
APPROVING = {
    "verdict": "approve",
    "approval_reason": "No qualifying defects remain.",
    "findings": [],
}
FINDING = {
    "verdict": "do_not_approve",
    "approval_reason": "A qualifying defect remains.",
    "findings": [
        {
            "severity": "medium",
            "title": "Incorrect value",
            "path": "app.py",
            "line": 1,
            "problem": "This value causes a definite failure.",
            "suggestion": "Use the validated value instead.",
            "trigger": "Process the changed value.",
            "impact": "The operation fails.",
            "evidence": [
                {
                    "path": "app.py",
                    "line": 1,
                    "excerpt": "value = 1",
                    "relevance": "This line triggers the defect.",
                }
            ],
            "counterevidence": [],
            "counterevidence_reason": "No direct counterevidence found.",
        }
    ],
}
CONFIRMED = {
    "verdict": "do_not_approve",
    "approval_reason": "The candidate holds against the branch.",
    "judgments": [
        {
            "ref": "F1",
            "verdict": "confirmed",
            "reason": "Read the caller; nothing validates the value.",
        }
    ],
}


def settings_for(directory: str, repository: Repository, webhook=None) -> Settings:
    return Settings(
        reviewer_username="reviewer",
        repositories=[repository],
        dry_run=False,
        slack_webhook_url=webhook,
        state_file=Path(directory) / "state.json",
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


class RepositoryConfigurationTests(unittest.TestCase):
    def load(self, entry: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repositories.json"
            path.write_text(f"[{entry}]", encoding="utf-8")
            return load_repositories(path)

    def test_slack_defaults_to_on_so_existing_entries_keep_their_routing(self):
        (repository,) = self.load('{"slug": "owner/app"}')
        self.assertTrue(repository.slack)

    def test_a_repository_can_be_kept_out_of_slack(self):
        (repository,) = self.load('{"slug": "owner/app", "slack": false}')
        self.assertFalse(repository.slack)

    def test_slack_must_be_a_json_boolean(self):
        """A quoted "false" is truthy, and reading it as on would route the
        repository's own pull requests into the workspace it was meant to avoid."""

        for value in ('"false"', "0", "null"):
            with self.subTest(value):
                with self.assertRaisesRegex(
                    ConfigError, r"\.slack must be true or false"
                ):
                    self.load(f'{{"slug": "owner/app", "slack": {value}}}')


class OwnPullRequestOutsideSlackTests(unittest.TestCase):
    def setUp(self) -> None:
        without_review_rules(self)

    def review(
        self,
        responses,
        *,
        author: str = "reviewer",
        slack: bool = False,
        webhook=None,
        hide=None,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            repository = Repository("app", "owner/app", root, slack=slack)
            pull_request = PullRequest(1, "Title", "", author, "head", "url")
            settings = settings_for(directory, repository, webhook)
            engine = Mock()
            engine.review.side_effect = list(responses)
            # One recorder for posting and hiding, so a test can read their order.
            calls = Mock()
            calls.post.return_value = "comment-1"
            calls.review.return_value = "review-1"
            calls.hide.return_value = 0
            if hide is not None:
                calls.hide.side_effect = hide
            with patch(
                "prwarden.cli.has_current_review", return_value=False
            ) as current, patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch("prwarden.cli.list_pull_request_checks", return_value=()), patch(
                "prwarden.cli._read_linked_issues", return_value=()
            ), patch(
                "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_approval", return_value="approval-1"
            ) as approval, patch(
                "prwarden.cli.submit_review", calls.review
            ) as review, patch(
                "prwarden.cli.submit_lgtm_comment", calls.post
            ) as lgtm, patch(
                "prwarden.cli.hide_earlier_lgtm_comments", calls.hide
            ) as hidden, patch(
                "prwarden.cli.send_slack_message", return_value="http-200"
            ) as slack_message, patch("sys.stdout", new_callable=StringIO) as output:
                workspace.return_value.__enter__.return_value = repository
                workspace.return_value.dependency_links = ()
                workspace.return_value.dependency_notes = ()
                with JsonStateStore(settings.state_file) as store:
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )
                    records = dict(store.data["records"])
        return {
            "approval": approval,
            "review": review,
            "lgtm": lgtm,
            "hide": hidden,
            "calls": calls,
            "slack": slack_message,
            "current": current,
            "records": records,
            "terminal": output.getvalue(),
        }

    def test_a_clean_result_is_an_lgtm_comment_never_an_approval(self):
        """GitHub forbids approving your own pull request, so the clean result is
        said in a comment, and with no webhook configured at all."""

        result = self.review([APPROVING])

        result["approval"].assert_not_called()
        result["review"].assert_not_called()
        result["slack"].assert_not_called()
        result["lgtm"].assert_called_once()
        body = result["lgtm"].call_args.args[2]
        self.assertTrue(body.startswith("## LGTM\n"))
        self.assertIn("`head`", body)
        self.assertIn("Reviewed by", body)
        self.assertTrue(body.rstrip().endswith(LGTM_COMMENT_MARKER))
        self.assertEqual(
            result["records"]["owner/app#1#head#github-own"]["status"], "delivered"
        )
        self.assertEqual(
            result["records"]["owner/app#1#head#github-own"]["receipt_id"],
            "comment-1",
        )
        self.assertIn("-> GITHUB, own pull request", result["terminal"])
        self.assertIn("Posted LGTM successfully", result["terminal"])

    def test_the_lgtm_names_the_configured_verifier_that_had_nothing_to_check(self):
        with patch(
            "prwarden.cli._verifier_identity", return_value=("kiro", "gpt-5.6-terra")
        ):
            result = self.review([APPROVING])

        self.assertIn(
            "so GPT-5.6 Terra @ Kiro had nothing to verify",
            result["lgtm"].call_args.args[2],
        )

    def test_findings_are_a_comment_review_without_an_approval(self):
        result = self.review([FINDING, CONFIRMED])

        result["review"].assert_called_once()
        result["approval"].assert_not_called()
        result["lgtm"].assert_not_called()
        result["slack"].assert_not_called()
        self.assertEqual(
            result["records"]["owner/app#1#head#github-own"]["status"], "delivered"
        )

    def test_the_authors_own_replies_do_not_settle_a_head(self):
        """On a teammate's pull request a submitted review by this account means the
        head was already reviewed. On your own, replying to a thread submits one, so
        the check would skip heads nobody reviewed."""

        result = self.review([APPROVING])

        result["current"].assert_not_called()
        result["lgtm"].assert_called_once()

    def test_a_repository_in_slack_still_sends_your_own_results_there(self):
        result = self.review([APPROVING], slack=True, webhook="https://example.invalid")

        result["slack"].assert_called_once()
        result["lgtm"].assert_not_called()
        result["approval"].assert_not_called()
        self.assertIn("owner/app#1#head#slack", result["records"])

    def test_a_teammates_pull_request_is_still_approved(self):
        result = self.review([APPROVING], author="author")

        result["approval"].assert_called_once()
        result["lgtm"].assert_not_called()
        result["slack"].assert_not_called()
        self.assertIn("owner/app#1#head#github", result["records"])
        # An approval is tied to its commit; there is nothing earlier to hide.
        result["hide"].assert_not_called()

    def test_a_new_lgtm_hides_the_earlier_ones_after_it_is_posted(self):
        """An LGTM left visible from an earlier head speaks for code that is gone.
        Posting first means the pull request is never left without a verdict."""

        result = self.review([APPROVING], hide=lambda *args: 2)

        self.assertEqual(
            [name for name, _, _ in result["calls"].mock_calls],
            ["post", "hide"],
        )
        repository, pull_request, account, keep = result["hide"].call_args.args
        self.assertEqual(
            (pull_request.number, account, keep), (1, "reviewer", "comment-1")
        )
        self.assertIn("Marked 2 earlier LGTM comments as outdated", result["terminal"])

    def test_findings_hide_every_earlier_lgtm(self):
        """An earlier LGTM now contradicts what this head received."""

        result = self.review([FINDING, CONFIRMED])

        self.assertEqual(
            [name for name, _, _ in result["calls"].mock_calls],
            ["review", "hide"],
        )
        self.assertIsNone(result["hide"].call_args.args[3])

    def test_your_own_results_in_slack_hide_nothing_on_github(self):
        result = self.review([APPROVING], slack=True, webhook="https://example.invalid")

        result["hide"].assert_not_called()

    def test_failing_to_hide_leaves_the_delivered_result_alone(self):
        """Cosmetic: the current result is posted and recorded either way."""

        def fail(*args):
            raise CommandError("GraphQL refused")

        result = self.review([APPROVING], hide=fail)

        self.assertIn(
            "Earlier LGTM comments were left visible: GraphQL refused",
            result["terminal"],
        )
        self.assertEqual(
            result["records"]["owner/app#1#head#github-own"]["status"], "delivered"
        )
        result["hide"].assert_called_once()


class NoticesOutsideSlackTests(unittest.TestCase):
    def test_run_errors_about_a_repository_outside_slack_stay_out_of_it(self):
        repositories = [
            Repository("app", "owner/app"),
            Repository("private", "owner/private", slack=False),
        ]
        errors = [
            "owner/app#1: engine failed",
            "owner/private#2: engine failed",
            "owner/private: discovery failed",
            "owner/private-tools#3: a different repository",
        ]

        self.assertEqual(
            cli._errors_for_slack(errors, repositories),
            [
                "owner/app#1: engine failed",
                "owner/private-tools#3: a different repository",
            ],
        )

    def test_a_manual_review_request_outside_slack_is_settled_without_sending(self):
        """Nowhere to send it, and retrying every run would never change that."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory), slack=False)
            settings = settings_for(directory, repository, "https://example.invalid")
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            budget = DiffBudget(
                limit_bytes=10,
                total_bytes=30,
                reviewed_bytes=10,
                reviewed_files=1,
                deferred=(DeferredFile("big.py", 20),),
            )
            with patch("prwarden.cli.send_slack_message") as slack, patch(
                "sys.stdout", new_callable=StringIO
            ), JsonStateStore(settings.state_file) as store:
                settled = cli._notify_manual_review(
                    settings, repository, pull_request, store, "too large", budget
                )
                record = store.get(
                    store.pull_request_key("owner/app", 1, "oversized")
                )
                again = cli._notify_manual_review(
                    settings, repository, pull_request, store, "too large", budget
                )

        self.assertTrue(settled)
        self.assertTrue(again)
        self.assertEqual(record["status"], "oversized_not_notified")
        slack.assert_not_called()

    def test_a_stale_review_outside_slack_is_not_announced_there(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory), slack=False)
            settings = settings_for(directory, repository, "https://example.invalid")
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            objection = PriorReviewComment(
                "inline:9", "inline", "teammate", "Please wait.", "app.py", 1
            )
            with patch("prwarden.cli.send_slack_message") as slack, patch(
                "sys.stdout", new_callable=StringIO
            ), JsonStateStore(settings.state_file) as store:
                key = store.key("owner/app", 1, "head", "github")
                cli._record_stale_feedback_skip(
                    settings,
                    repository,
                    pull_request,
                    store,
                    key,
                    (objection,),
                    (),
                    (),
                    "antigravity",
                    None,
                )
                status = store.get(key)["status"]

        slack.assert_not_called()
        self.assertEqual(status, "feedback_changed")


class LgtmCommentTests(unittest.TestCase):
    def test_it_names_the_head_both_models_and_the_rejected_candidates(self):
        body = format_lgtm_comment(
            PullRequest(1, "Title", "", "reviewer", "0123456789abcdef", "url"),
            "kiro",
            "claude-opus-5.5",
            "kiro",
            "gpt-5.6-terra",
            rejected_candidates=2,
        )

        self.assertIn("`0123456789ab`", body)
        self.assertIn("Verification confirmed 0 of 2 candidate issues", body)
        self.assertIn(
            "_Reviewed by Claude Opus 5.5 @ Kiro, verified by GPT-5.6 Terra @ Kiro._",
            body,
        )

    def test_a_verifier_with_nothing_to_check_is_named_as_such(self):
        """The reader is told which verifier stood behind the review without being
        told it verified anything, because no candidate reached it."""

        body = format_lgtm_comment(
            PullRequest(1, "Title", "", "reviewer", "head", "url"),
            "kiro",
            "claude-opus-5.5",
            idle_verifier=("kiro", "gpt-5.6-terra"),
        )

        self.assertIn(
            "_Reviewed by Claude Opus 5.5 @ Kiro; it raised no candidate findings, "
            "so GPT-5.6 Terra @ Kiro had nothing to verify._",
            body,
        )
        self.assertNotIn("verified by", body)

    def test_a_verifier_that_judged_is_not_also_called_idle(self):
        body = format_lgtm_comment(
            PullRequest(1, "Title", "", "reviewer", "head", "url"),
            "kiro",
            "claude-opus-5.5",
            "kiro",
            "gpt-5.6-terra",
            rejected_candidates=1,
            idle_verifier=("kiro", "gpt-5.6-terra"),
        )

        self.assertIn("verified by GPT-5.6 Terra @ Kiro", body)
        self.assertNotIn("nothing to verify", body)

    def test_a_negative_candidate_count_is_refused(self):
        with self.assertRaises(ValueError):
            format_lgtm_comment(
                PullRequest(1, "Title", "", "reviewer", "head", "url"),
                "kiro",
                None,
                rejected_candidates=-1,
            )


if __name__ == "__main__":
    unittest.main()
