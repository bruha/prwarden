import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from prwarden import cli
from prwarden.commands import CommandError
from prwarden.config import Settings
from prwarden.delivery import (
    DeliveryError,
    DeliveryUncertainError,
    format_waiting_slack_message,
)
from prwarden.github import (
    MAX_WAITING_ITEMS,
    WAITING_COMMENT_MARKER,
    code_level_feedback,
    format_waiting_comment,
)
from prwarden.models import (
    WAITING_FOR_A_PERSON,
    WAITING_FOR_CHECKS,
    PriorReviewComment,
    PullRequest,
    Repository,
    WaitingReason,
)
from prwarden.state import JsonStateStore
from tests.test_cli import READY_SNAPSHOT

PULL_REQUEST = PullRequest(
    7,
    "Add export",
    "",
    "author",
    "0123456789abcdef",
    "https://github.com/owner/app/pull/7",
)
CHECKS = WaitingReason(WAITING_FOR_CHECKS, ("Lint", "Build"))
POLICY = WaitingReason(WAITING_FOR_A_PERSON, (".github/workflows/test.yml",))


class WaitingCommentTests(unittest.TestCase):
    def test_a_checks_note_names_the_head_the_checks_and_where_they_are(self):
        body = format_waiting_comment(PULL_REQUEST, CHECKS)

        self.assertTrue(body.startswith("## Waiting for checks\n"))
        self.assertIn("reviewed `0123456789ab` and found nothing to report", body)
        self.assertIn("holds an approval while these checks fail", body)
        self.assertIn("- `Lint`\n- `Build`", body)
        self.assertIn("[checks](https://github.com/owner/app/pull/7/checks)", body)
        self.assertTrue(body.rstrip().endswith(WAITING_COMMENT_MARKER))

    def test_the_own_pull_request_waits_with_its_lgtm(self):
        """GitHub lets nobody approve their own pull request, so it gets an LGTM."""

        checks = format_waiting_comment(PULL_REQUEST, CHECKS, own=True)
        policy = format_waiting_comment(PULL_REQUEST, POLICY, own=True)

        self.assertIn("holds its LGTM while", checks)
        self.assertIn("never gives its LGTM to a change to review policy", policy)

    def test_a_policy_note_asks_for_a_person(self):
        body = format_waiting_comment(PULL_REQUEST, POLICY)

        self.assertTrue(body.startswith("## Waiting for a person\n"))
        self.assertIn("- `.github/workflows/test.yml`", body)
        self.assertIn("A person needs to review and approve it.", body)
        self.assertNotIn("checks)", body)

    def test_names_from_the_head_cannot_shape_the_comment(self):
        """A check is named by the reviewed head's workflows, so its name is
        flattened, cut, fenced as code, and a long list is counted, not printed."""

        names = ("`x` @team [link](https://example.invalid)\n## Heading",) + tuple(
            f"check {number}" for number in range(MAX_WAITING_ITEMS + 4)
        )
        body = format_waiting_comment(
            PULL_REQUEST, WaitingReason(WAITING_FOR_CHECKS, names)
        )

        self.assertIn(
            "- `` `x` @team [link](https://example.invalid) ## Heading ``", body
        )
        self.assertNotIn("\n## Heading", body)
        self.assertEqual(body.count("\n- `"), MAX_WAITING_ITEMS)
        self.assertIn("- and 5 more", body)
        long_name = format_waiting_comment(
            PULL_REQUEST, WaitingReason(WAITING_FOR_CHECKS, ("x" * 500,))
        )
        self.assertNotIn("x" * 200, long_name)

    def test_a_pull_request_without_a_web_address_gets_no_link(self):
        body = format_waiting_comment(replace(PULL_REQUEST, url="url"), CHECKS)

        self.assertNotIn("[checks]", body)

    def test_a_slack_note_names_the_checks(self):
        repository = Repository("app", "owner/app", None)
        message = format_waiting_slack_message(
            repository, PULL_REQUEST, CHECKS, ["Li`nt", "Build"], more=2
        )

        self.assertIn("*PR #7 Waiting for checks (app)*", message)
        self.assertIn("`Lint`, `Build` and 2 more", message)
        self.assertIn("holds its LGTM", message)

    def test_another_installations_note_is_no_feedback_for_the_code_level(self):
        """Another developer's PR Warden posts it under their name; like an LGTM,
        it must never read as an objection an approval has to answer."""

        note = PriorReviewComment(
            "conversation:1",
            "conversation",
            "colleague",
            format_waiting_comment(PULL_REQUEST, CHECKS),
        )
        human = PriorReviewComment(
            "conversation:2", "conversation", "colleague", "Why does Lint fail?"
        )

        self.assertEqual(code_level_feedback((note, human)), (human,))


class DeliverWaitingNoteTests(unittest.TestCase):
    def settings(self, directory: str, **overrides) -> Settings:
        repository = Repository("app", "owner/app", Path(directory))
        values = dict(
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
        )
        values.update(overrides)
        return Settings(**values)

    def deliver(
        self,
        directory: str,
        destination: str = "github",
        *,
        snapshot=None,
        comment=None,
        slack=None,
        settings=None,
        times: int = 1,
    ) -> dict:
        settings = settings or self.settings(directory)
        repository = settings.repositories[0]
        pull_request = replace(PULL_REQUEST, head_sha="head")
        snapshot = snapshot or READY_SNAPSHOT
        with patch(
            "prwarden.cli.get_pull_request_snapshot",
            **(
                {"side_effect": snapshot}
                if isinstance(snapshot, Exception)
                else {"return_value": snapshot}
            ),
        ), patch(
            "prwarden.cli.submit_waiting_comment",
            **(
                {"side_effect": comment}
                if isinstance(comment, Exception)
                else {"return_value": comment or "comment-1"}
            ),
        ) as submit, patch(
            "prwarden.cli.send_slack_message",
            **(
                {"side_effect": slack}
                if isinstance(slack, Exception)
                else {"return_value": "http-200"}
            ),
        ) as send, patch(
            "prwarden.cli.hide_earlier_comments", return_value=0
        ) as hide, patch("sys.stdout", new_callable=StringIO) as output:
            with JsonStateStore(settings.state_file) as store:
                for _ in range(times):
                    cli._deliver_waiting_note(
                        settings, repository, pull_request, store, destination, CHECKS
                    )
                record = store.get(
                    store.key("owner/app", 7, "head", destination + ":waiting")
                )
        return {
            "record": record,
            "submit": submit,
            "send": send,
            "hide": hide,
            "terminal": output.getvalue(),
        }

    def test_a_note_is_posted_once_for_a_head(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.deliver(directory, times=2)

        result["submit"].assert_called_once()
        self.assertEqual(result["record"]["status"], "delivered")
        self.assertEqual(result["record"]["waiting_for"], "checks")
        self.assertEqual(result["record"]["items"], ["Lint", "Build"])

    def test_a_note_that_may_have_been_posted_is_never_posted_again(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.deliver(
                directory, comment=CommandError("gh timed out"), times=2
            )

        result["submit"].assert_called_once()
        self.assertEqual(result["record"]["status"], "delivery_uncertain")
        self.assertIn("may not have been delivered", result["terminal"])

    def test_a_note_that_definitely_failed_is_tried_again(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            first = self.deliver(
                directory,
                "slack",
                slack=DeliveryError("Slack refused the message"),
                settings=settings,
            )
            second = self.deliver(directory, "slack", settings=settings)

        self.assertEqual(first["record"]["status"], "failed")
        second["send"].assert_called_once()
        self.assertEqual(second["record"]["status"], "delivered")

    def test_an_uncertain_slack_note_is_not_tried_again(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.deliver(
                directory,
                "slack",
                slack=DeliveryUncertainError("connection reset"),
                times=2,
            )

        result["send"].assert_called_once()
        self.assertEqual(result["record"]["status"], "delivery_uncertain")

    def test_the_own_pull_request_on_slack_gets_a_message_not_a_comment(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.deliver(directory, "slack")

        result["submit"].assert_not_called()
        result["send"].assert_called_once()
        self.assertIn("Waiting for checks", result["send"].call_args.args[1])

    def test_the_own_pull_request_on_github_waits_with_its_lgtm(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.deliver(directory, cli.OWN_GITHUB_DESTINATION)

        self.assertIn("holds its LGTM", result["submit"].call_args.args[2])

    def test_a_pull_request_that_closed_or_moved_gets_no_note(self):
        for label, snapshot in (
            ("closed", replace(READY_SNAPSHOT, state="closed")),
            ("moved", replace(READY_SNAPSHOT, head_sha="newer")),
            ("unreadable", CommandError("gh exited with 1: HTTP 502")),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as directory:
                result = self.deliver(directory, snapshot=snapshot)

                result["submit"].assert_not_called()
                self.assertIsNone(result["record"])

    def test_slack_without_a_webhook_records_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory, slack_webhook_url=None)
            result = self.deliver(directory, "slack", settings=settings)

        result["send"].assert_not_called()
        self.assertIsNone(result["record"])

    def test_a_new_note_collapses_the_earlier_heads_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            with JsonStateStore(settings.state_file) as store:
                store.set_status(
                    store.key("owner/app", 7, "older", "github:waiting"),
                    "delivered",
                    receipt_id="comment-0",
                )
            result = self.deliver(directory, settings=settings)
            with JsonStateStore(settings.state_file) as store:
                older = store.get(store.key("owner/app", 7, "older", "github:waiting"))

        result["hide"].assert_called_once()
        self.assertEqual(result["hide"].call_args.args[3], WAITING_COMMENT_MARKER)
        self.assertEqual(result["hide"].call_args.args[4], "comment-1")
        self.assertTrue(older["hidden"])
        self.assertEqual(older["receipt_id"], "comment-0")


if __name__ == "__main__":
    unittest.main()
