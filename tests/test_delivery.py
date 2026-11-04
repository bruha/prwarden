import json
import unittest
from unittest.mock import patch

from src.delivery import (
    format_manual_review_message,
    format_slack_lgtm_message,
    format_slack_message,
    redact_local_paths,
    send_slack_message,
)
from src.models import Finding, FindingEvidence, PullRequest, Repository


class DeliveryFormattingTests(unittest.TestCase):
    def lgtm(self, **kwargs) -> str:
        return format_slack_lgtm_message(
            Repository(name="app", slug="owner/app"),
            PullRequest(
                number=12,
                title="Ready change",
                body="",
                author="reviewer",
                head_sha="abc",
                url="https://github.com/owner/app/pull/12",
            ),
            "antigravity",
            None,
            **kwargs,
        )

    def test_lgtm_message_reports_candidates_verification_rejected(self):
        """A clean result after rejecting candidates is not a quiet review."""

        message = self.lgtm(rejected_candidates=2)

        self.assertIn(
            "> Verification confirmed 0 of 2 candidate issues the review raised.",
            message,
        )
        self.assertLess(
            message.index("Verification confirmed"), message.index("_Reviewed")
        )

    def test_lgtm_message_counts_a_single_rejected_candidate_in_singular(self):
        self.assertIn(
            "0 of 1 candidate issue the review raised",
            self.lgtm(rejected_candidates=1),
        )

    def test_lgtm_message_stays_silent_when_no_candidate_was_raised(self):
        self.assertNotIn("Verification confirmed", self.lgtm())

    def test_lgtm_message_rejects_an_impossible_candidate_count(self):
        with self.assertRaises(ValueError):
            self.lgtm(rejected_candidates=-1)

    def test_lgtm_message_reports_merge_readiness(self):
        message = format_slack_lgtm_message(
            Repository(name="app", slug="owner/app"),
            PullRequest(
                number=12,
                title="Ready change",
                body="",
                author="reviewer",
                head_sha="abc",
                url="https://github.com/owner/app/pull/12",
            ),
            "antigravity",
            None,
        )

        self.assertEqual(
            message,
            "\n".join(
                [
                    "*PR Warden: LGTM*",
                    "<https://github.com/owner/app/pull/12|owner/app PR #12>",
                    "*Ready change*",
                    "> LGTM — AI review found no qualifying issues and GitHub "
                    "reports no merge conflicts. This PR can be merged once "
                    "required checks and branch protections pass.",
                    "",
                    "_Reviewed by provider default (model ID not configured) "
                    "@ Antigravity_",
                ]
            ),
        )
        self.assertNotIn("Title:", message)
        self.assertEqual(message.count("https://github.com/owner/app/pull/12"), 1)

    def test_manual_review_message_identifies_oversized_pull_request(self):
        message = format_manual_review_message(
            Repository(name="app", slug="owner/app"),
            PullRequest(
                number=12,
                title="Large change",
                body="",
                author="author",
                head_sha="abc",
                url="https://github.com/owner/app/pull/12",
            ),
            "Pull-request diff exceeds MAX_DIFF_BYTES (100000)",
            "antigravity",
            None,
        )

        self.assertIn("*MANUAL REVIEW NEEDED*", message)
        self.assertIn(
            "<https://github.com/owner/app/pull/12|owner/app PR #12>",
            message,
        )
        self.assertIn("MAX_DIFF_BYTES", message)
        self.assertIn("*Large change*", message)
        self.assertNotIn("Title:", message)
        self.assertIn(
            "> Automated review: not run\n> Reason: Pull-request diff exceeds",
            message,
        )
        self.assertTrue(
            message.endswith(
                "_Configured reviewer: provider default (model ID not configured) "
                "@ Antigravity_"
            )
        )
        self.assertEqual(message.count("https://github.com/owner/app/pull/12"), 1)

    def test_slack_message_is_single_copyable_block(self):
        message = format_slack_message(
            Repository(name="app", slug="owner/app"),
            PullRequest(
                number=12,
                title="Fix behavior",
                body="",
                author="reviewer",
                head_sha="abc",
                url="https://github.com/owner/app/pull/12",
            ),
            [
                Finding(
                    severity="high",
                    title="Data loss",
                    path="src/app.py",
                    line=42,
                    problem="The new branch drops persisted data.",
                    suggestion="Preserve the existing data before replacing it.",
                    trigger="A replacement is saved after the old value is cleared.",
                    impact="Previously persisted data is permanently lost.",
                    evidence=(
                        FindingEvidence(
                            "src/app.py",
                            42,
                            "replace(data)",
                            "The call replaces data without preserving it.",
                        ),
                    ),
                    counterevidence_reason="No rollback or recovery path exists.",
                )
            ],
            "codex",
            "gpt-5.1-codex",
        )
        self.assertIn("*[HIGH] Data loss*", message)
        self.assertIn("`src/app.py` (Line 42)", message)
        self.assertIn("• Trigger: A replacement", message)
        self.assertIn("• Evidence: `src/app.py:42`", message)
        self.assertIn("• Counterevidence considered:", message)
        self.assertIn(
            "<https://github.com/owner/app/pull/12|owner/app PR #12>",
            message,
        )
        self.assertIn("*Fix behavior*", message)
        self.assertNotIn("Title:", message)
        self.assertTrue(message.endswith("_Reviewed by GPT-5.1 Codex @ Codex_"))
        self.assertEqual(message.count("https://github.com/owner/app/pull/12"), 1)
        self.assertNotIn("```", message)

    def test_slack_messages_name_a_separate_verification_engine(self):
        repository = Repository(name="app", slug="owner/app")
        pull_request = PullRequest(
            number=12,
            title="Fix behavior",
            body="",
            author="reviewer",
            head_sha="abc",
            url="https://github.com/owner/app/pull/12",
        )
        finding = Finding(
            severity="high",
            title="Data loss",
            path="src/app.py",
            line=42,
            problem="The new branch drops persisted data.",
            suggestion="Preserve the existing data before replacing it.",
            trigger="A replacement is saved after the old value is cleared.",
            impact="Previously persisted data is permanently lost.",
            evidence=(
                FindingEvidence(
                    "src/app.py",
                    42,
                    "replace(data)",
                    "The call replaces data without preserving it.",
                ),
            ),
            counterevidence_reason="No rollback or recovery path exists.",
        )
        expected = (
            "_Reviewed by Gemini 3.1 Pro (High) @ Antigravity, "
            "verified by GPT-5.6 Terra @ Kiro_"
        )

        findings_message = format_slack_message(
            repository,
            pull_request,
            [finding],
            "antigravity",
            "gemini-3.1-pro-high",
            "kiro",
            "gpt-5.6-terra",
        )
        lgtm_message = format_slack_lgtm_message(
            repository,
            pull_request,
            "antigravity",
            "gemini-3.1-pro-high",
            "kiro",
            "gpt-5.6-terra",
        )

        self.assertTrue(findings_message.endswith(expected))
        self.assertTrue(lgtm_message.endswith(expected))


class PathRedactionTests(unittest.TestCase):
    def test_absolute_local_paths_are_reduced_to_their_final_component(self) -> None:
        self.assertEqual(
            redact_local_paths(
                "run: Another reviewer process holds "
                "/Users/name/projects/prwarden/.state/review-state.json.lock"
            ),
            "run: Another reviewer process holds review-state.json.lock",
        )
        self.assertEqual(
            redact_local_paths("/opt/homebrew/bin/claude exited with 1"),
            "claude exited with 1",
        )

    def test_urls_and_relative_paths_are_preserved(self) -> None:
        for unchanged in (
            "<https://github.com/owner/app/pull/31|owner/app PR #31>",
            "https://hooks.slack.com/services/T000/B000/secret",
            "src/example/useExample.ts:12",
            "owner/app#34",
        ):
            with self.subTest(text=unchanged):
                self.assertEqual(redact_local_paths(unchanged), unchanged)

    def test_slack_delivery_redacts_before_sending(self) -> None:
        captured = {}

        class Response:
            status = 200

            def read(self):
                return b"ok"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def urlopen(request, timeout=None):
            captured["body"] = json.loads(request.data.decode("utf-8"))["text"]
            return Response()

        with patch("urllib.request.urlopen", urlopen):
            send_slack_message(
                "https://example.invalid/webhook",
                "Failure at /Users/name/projects/app/.state/review-state.json.lock",
            )

        self.assertEqual(
            captured["body"], "Failure at review-state.json.lock"
        )


if __name__ == "__main__":
    unittest.main()
