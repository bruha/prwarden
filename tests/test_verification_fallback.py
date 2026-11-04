"""Tests for the verifier's own fallback, used when its engine cannot answer."""

import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prwarden import cli
from prwarden.config import Settings
from prwarden.engine import DESIGN_LEVEL, EngineError, design_candidate_ref
from prwarden.models import DesignAssessment, PullRequest, Repository, ReviewMaterial
from prwarden.review import ReviewError
from tests.test_design_level import design_response


def settings(directory: str, **overrides) -> Settings:
    values = dict(
        reviewer_username="reviewer",
        repositories=[],
        dry_run=True,
        slack_webhook_url=None,
        state_file=Path(directory) / "state.json",
        review_engine="kiro",
        review_engine_command=("kiro-cli",),
        review_engine_env_allowlist=(),
        review_model="claude-opus-5.5",
        review_timeout_seconds=300,
        max_diff_bytes=100000,
        max_findings=10,
        verify_engine="kiro",
        verify_engine_command=("kiro-cli",),
        verify_model="gpt-5.6-terra",
        verify_fallback_engine="claude",
        verify_fallback_engine_command=("claude",),
        verify_fallback_model="claude-sonnet-5-5",
        verify_fallback_effort="xhigh",
    )
    values.update(overrides)
    return Settings(**values)


def paired(primary: Mock, fallback: Mock, base: Settings) -> cli.VerificationPass:
    """A verification pass with a fallback, wrapped as one pull request uses it."""

    verification = cli.VerificationPass(
        engine=primary,
        settings=cli.verification_engine_settings(base),
        fallback=cli.VerificationPass(
            engine=fallback, settings=cli.verify_fallback_engine_settings(base)
        ),
    )
    return cli._with_verification_fallback(verification)


class FallbackVerifierTests(unittest.TestCase):
    """Only an engine failure hands over, and the hand-over holds for the pull
    request, so a correction retry goes to the engine told how to correct."""

    def call(self, verification: cli.VerificationPass):
        return verification.engine.review(
            "repository", "pull request", "material", timeout_seconds=60
        )

    def test_a_working_verifier_is_never_bypassed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary, fallback = Mock(), Mock()
            primary.review.return_value = {"judgments": []}
            verification = paired(primary, fallback, settings(directory))

            self.assertEqual(self.call(verification), {"judgments": []})

        fallback.review.assert_not_called()
        self.assertFalse(verification.engine.handed_over)
        self.assertEqual(
            cli._verification_identity(verification, settings(directory)),
            ("kiro", "gpt-5.6-terra"),
        )

    def test_an_engine_failure_hands_the_same_call_over(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary, fallback = Mock(), Mock()
            primary.review.side_effect = EngineError("Kiro does not review this head")
            fallback.review.return_value = {"judgments": ["kept"]}
            verification = paired(primary, fallback, settings(directory))

            with patch("sys.stdout", new_callable=StringIO) as output:
                result = self.call(verification)
                self.call(verification)

        self.assertEqual(result, {"judgments": ["kept"]})
        # Sticky: the second call never asked the engine that had failed.
        primary.review.assert_called_once()
        self.assertEqual(fallback.review.call_count, 2)
        self.assertEqual(fallback.review.call_args.kwargs, {"timeout_seconds": 60})
        self.assertTrue(verification.engine.handed_over)
        self.assertEqual(
            cli._verification_identity(verification, settings(directory)),
            ("claude", "claude-sonnet-5-5"),
        )
        reported = output.getvalue()
        self.assertIn("kiro (gpt-5.6-terra) verification engine failed", reported)
        self.assertIn("Verifying with fallback claude (claude-sonnet-5-5)", reported)

    def test_anything_but_an_engine_failure_stays_with_the_verifier(self) -> None:
        """A malformed answer is the controller's to correct, not another engine's."""

        with tempfile.TemporaryDirectory() as directory:
            primary, fallback = Mock(), Mock()
            primary.review.side_effect = ReviewError("judged a candidate twice")
            verification = paired(primary, fallback, settings(directory))

            with self.assertRaises(ReviewError):
                self.call(verification)

        fallback.review.assert_not_called()

    def test_both_failures_are_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            primary, fallback = Mock(), Mock()
            primary.review.side_effect = EngineError("Kiro is down")
            fallback.review.side_effect = EngineError("usage limit reached")
            verification = paired(primary, fallback, settings(directory))

            with patch("sys.stdout", new_callable=StringIO):
                with self.assertRaisesRegex(
                    EngineError,
                    r"kiro \(gpt-5.6-terra\) verification failed: Kiro is down; "
                    r"claude \(claude-sonnet-5-5\) fallback also failed: usage limit",
                ):
                    self.call(verification)

    def test_without_a_fallback_the_pass_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory, verify_fallback_engine=None)
            verification = cli._create_verification_pass(base, Mock())

        self.assertIsNone(verification.fallback)
        self.assertIs(cli._with_verification_fallback(verification), verification)
        self.assertIsNone(cli._with_verification_fallback(None))


class CodeVerifierAttributionTests(unittest.TestCase):
    """An inline finding names the verifier that judged it."""

    def test_the_delivered_verifier_follows_the_hand_over(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory)
            primary, fallback = Mock(), Mock()
            primary.review.side_effect = EngineError("Kiro is down")
            verification = paired(primary, fallback, base)
            reviewer = replace(base, review_engine="antigravity", review_model="x")

            before = cli._delivered_verifier(verification, base, reviewer)
            with patch("sys.stdout", new_callable=StringIO):
                verification.engine.review("r", "p", "m")
            after = cli._delivered_verifier(verification, base, reviewer)
            # A verifier that is the reviewer itself is not named twice.
            itself = cli._delivered_verifier(
                verification,
                base,
                replace(base, review_engine="claude", review_model="claude-sonnet-5-5"),
            )

        self.assertEqual(before, ("kiro", "gpt-5.6-terra"))
        self.assertEqual(after, ("claude", "claude-sonnet-5-5"))
        self.assertEqual(itself, (None, None))


class AdvisoryVerifierAttributionTests(unittest.TestCase):
    """A design note names the verifier that judged it, never only the configured
    one, because delivered feedback must not credit an engine that did not work."""

    def test_a_design_note_names_the_fallback_that_verified_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            base = settings(directory)
            reviewer = Mock()
            reviewer.review.return_value = design_response()
            primary, fallback = Mock(), Mock()
            primary.review.side_effect = EngineError("Kiro does not review this head")
            fallback.review.return_value = {
                "judgments": [
                    {
                        "ref": design_candidate_ref(1),
                        "verdict": "confirmed",
                        "reason": "Read app.py; the duplicate selector is there.",
                    }
                ]
            }
            verification = paired(primary, fallback, base)

            with patch("sys.stdout", new_callable=StringIO):
                assessment = cli._design_level(
                    base,
                    reviewer,
                    verification,
                    Repository("app", "owner/app", root),
                    PullRequest(1, "Title", "", "author", "head", "url"),
                    ReviewMaterial("diff", {"app.py": frozenset({1})}),
                )

        self.assertEqual(len(assessment.findings), 1)
        self.assertEqual(
            (assessment.verifier_engine, assessment.verifier_model),
            ("claude", "claude-sonnet-5-5"),
        )
        self.assertEqual(
            cli._advisory_verifier_identity(base, assessment),
            ("claude", "claude-sonnet-5-5"),
        )

    def test_without_a_hand_over_the_configured_verifier_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory)
            assessment = DesignAssessment(understanding="u", verdict="flawed")

            self.assertEqual(
                cli._advisory_verifier_identity(base, assessment),
                ("kiro", "gpt-5.6-terra"),
            )


class VerificationFallbackUsageTests(unittest.TestCase):
    def test_each_pull_request_gets_its_own_measured_hand_over(self) -> None:
        """Wrapped per pull request: one pull request's hand-over must not send the
        next one's verification straight to the fallback."""

        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory)
            verification = cli.VerificationPass(
                engine=Mock(),
                settings=cli.verification_engine_settings(base),
                fallback=cli.VerificationPass(
                    engine=Mock(), settings=cli.verify_fallback_engine_settings(base)
                ),
            )
            received = []
            with patch(
                "prwarden.cli._review_with_head_restarts",
                side_effect=lambda *args, **kwargs: received.append(
                    kwargs["verification"]
                ),
            ):
                for _ in range(2):
                    cli._process_pull_request_with_head_restarts(
                        base,
                        Repository("app", "owner/app", Path(directory)),
                        PullRequest(1, "Title", "", "author", "head", "url"),
                        Mock(),
                        Mock(),
                        live=False,
                        force=False,
                        verification=verification,
                    )

        first, second = (item.engine for item in received)
        self.assertIsInstance(first, cli._FallbackVerifier)
        self.assertIsNot(first, second)
        self.assertEqual(first.fallback.engine._role, "verify fallback")
        self.assertEqual(first.engine._role, "verify")

    def test_a_fallback_verification_is_counted_apart(self) -> None:
        """A spend line that folded it into the verifier's would hide the hand-over."""

        material = ReviewMaterial(
            "diff", {}, level=DESIGN_LEVEL, design_candidates=(Mock(),)
        )

        self.assertEqual(
            cli._usage_label(material, "verify fallback"),
            "design verification fallback",
        )
        self.assertEqual(cli._usage_label(material, "verify"), "design verification")


if __name__ == "__main__":
    unittest.main()
