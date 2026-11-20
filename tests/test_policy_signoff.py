import json
import unittest
from unittest.mock import patch

from prwarden.commands import CommandError
from prwarden.github import (
    WAITING_COMMENT_MARKER,
    code_level_feedback,
    has_current_review,
    is_signoff,
    policy_signoff,
    submit_waiting_review,
)
from prwarden.models import PriorReviewComment, PullRequest, Repository

REPOSITORY = Repository("app", "owner/app")
PULL_REQUEST = PullRequest(7, "Change CI", "", "author", "head", "url")
SINCE = "2026-11-20T10:00:00+00:00"


def user(login: str) -> dict:
    return {"login": login}


class SignoffWordingTests(unittest.TestCase):
    def test_what_reads_as_a_sign_off(self):
        for body in (
            "LGTM",
            "lgtm, thanks!",
            "Looks good to me",
            "Ship it",
            "Approved.",
            "\U0001f44d",
            ":+1: nice",
        ):
            with self.subTest(body=body):
                self.assertTrue(is_signoff(body))

    def test_what_does_not(self):
        for body in (
            "LGTM?",
            "not LGTM",
            "Looks good, but one question?",
            "LGTMs are cheap",
            "Please fix the workflow first. LGTM after that",
            "",
        ):
            with self.subTest(body=body):
                self.assertFalse(is_signoff(body))


class PolicySignoffTests(unittest.TestCase):
    """A sign-off comes from a person with write access other than the author, for
    this head: never from the author, a bot, this tool, or before the review."""

    def signoff(
        self,
        *,
        reviews=(),
        comments=(),
        review_comments=(),
        permissions=None,
        since=SINCE,
    ):
        permissions = permissions or {}
        asked = []

        def run(arguments, **kwargs):
            endpoint = next(part for part in arguments if part.startswith("repos/"))
            if "/reviews" in endpoint:
                return json.dumps([list(reviews)])
            if "/issues/" in endpoint:
                return json.dumps([list(comments)])
            if "/pulls/7/comments" in endpoint:
                return json.dumps([list(review_comments)])
            if endpoint.endswith("/permission"):
                login = endpoint.split("/")[-2]
                asked.append(login)
                if permissions.get(login) == "error":
                    raise CommandError("gh exited with 1: HTTP 403")
                return json.dumps({"permission": permissions.get(login, "read")})
            raise AssertionError(endpoint)

        with patch("prwarden.github.run_cmd", side_effect=run):
            return policy_signoff(REPOSITORY, PULL_REQUEST, since), asked

    def test_an_approval_of_this_head_by_a_writer_signs_off(self):
        found, _ = self.signoff(
            reviews=[
                {"user": user("maintainer"), "state": "APPROVED", "commit_id": "head"}
            ],
            permissions={"maintainer": "write"},
            since=None,
        )

        self.assertEqual(found, "maintainer")

    def test_an_approval_of_an_earlier_head_does_not(self):
        found, _ = self.signoff(
            reviews=[
                {"user": user("maintainer"), "state": "APPROVED", "commit_id": "old"}
            ],
            permissions={"maintainer": "write"},
        )

        self.assertIsNone(found)

    def test_an_lgtm_after_the_review_in_any_place_signs_off(self):
        later = "2026-11-20T11:00:00Z"
        for place in ("comments", "review_comments", "reviews"):
            with self.subTest(place=place):
                record = {"user": user("maintainer"), "body": "LGTM"}
                record["submitted_at" if place == "reviews" else "created_at"] = later
                if place == "reviews":
                    record.update(state="COMMENTED", commit_id="head")
                found, _ = self.signoff(
                    **{place: [record]}, permissions={"maintainer": "admin"}
                )
                self.assertEqual(found, "maintainer")

    def test_what_never_signs_off(self):
        later = "2026-11-20T11:00:00Z"
        earlier = "2026-11-20T09:00:00Z"
        for label, comment, permissions in (
            (
                "the author",
                {"user": user("author"), "body": "LGTM"},
                {"author": "admin"},
            ),
            ("a bot", {"user": user("ci[bot]"), "body": "LGTM"}, {"ci[bot]": "write"}),
            ("a reader", {"user": user("guest"), "body": "LGTM"}, {"guest": "read"}),
            (
                "this tool",
                {
                    "user": user("colleague"),
                    "body": "LGTM\n<!-- prwarden:lgtm -->",
                },
                {"colleague": "write"},
            ),
            (
                "before the review",
                {"user": user("maintainer"), "body": "LGTM", "created_at": earlier},
                {"maintainer": "write"},
            ),
            (
                "an unreadable permission",
                {"user": user("maintainer"), "body": "LGTM"},
                {"maintainer": "error"},
            ),
        ):
            with self.subTest(label):
                comment.setdefault("created_at", later)
                found, _ = self.signoff(comments=[comment], permissions=permissions)
                self.assertIsNone(found)

    def test_an_lgtm_counts_only_with_a_time_to_compare(self):
        found, asked = self.signoff(
            comments=[
                {
                    "user": user("maintainer"),
                    "body": "LGTM",
                    "created_at": "2026-11-20T11:00:00Z",
                }
            ],
            permissions={"maintainer": "write"},
            since=None,
        )

        self.assertIsNone(found)
        self.assertEqual(asked, [])


class WaitingReviewTests(unittest.TestCase):
    def test_the_note_is_one_comment_review_on_the_first_changed_line(self):
        with patch("prwarden.github.run_cmd", return_value='{"id": 91}') as run:
            receipt = submit_waiting_review(
                REPOSITORY, PULL_REQUEST, ".github/workflows/test.yml", 4, "note"
            )

        self.assertEqual(receipt, "91")
        self.assertEqual(run.call_count, 1)
        payload = json.loads(run.call_args.kwargs["input_text"])
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["commit_id"], "head")
        self.assertIn(WAITING_COMMENT_MARKER, payload["body"])
        self.assertEqual(
            payload["comments"],
            [
                {
                    "path": ".github/workflows/test.yml",
                    "line": 4,
                    "side": "RIGHT",
                    "body": "note",
                }
            ],
        )

    def test_the_note_is_not_this_heads_review(self):
        """Otherwise the run after a sign-off would find a review on the head, skip
        it as delivered, and never approve."""

        reviews = [
            {
                "user": user("reviewer"),
                "state": "COMMENTED",
                "commit_id": "head",
                "submitted_at": "2026-11-20T10:00:00Z",
                "body": f"Waiting.\n{WAITING_COMMENT_MARKER}",
            }
        ]
        with patch("prwarden.github.run_cmd", return_value=json.dumps([reviews])):
            self.assertFalse(has_current_review(REPOSITORY, PULL_REQUEST, "reviewer"))

    def test_another_installations_inline_note_objects_to_nothing(self):
        note = PriorReviewComment(
            "inline:3",
            "inline",
            "colleague",
            f"## Waiting for a person\n{WAITING_COMMENT_MARKER}",
            ".github/workflows/test.yml",
            1,
            False,
        )
        reply = PriorReviewComment(
            "inline:4", "inline", "colleague", "Why is this needed?", "app.py", 1, False
        )

        self.assertEqual(code_level_feedback((note, reply)), (reply,))


if __name__ == "__main__":
    unittest.main()
