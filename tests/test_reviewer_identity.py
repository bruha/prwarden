import unittest

from prwarden.reviewer_identity import (
    format_model_display_name,
    format_review_attribution,
    format_review_plan,
    format_reviewer_identity,
)


class SharedProviderWarningTests(unittest.TestCase):
    def test_one_provider_in_two_roles_is_named_at_startup(self):
        """Two safety nets on one account are one safety net, and the 12:00 run of
        2026-09-22 lost four pull requests to exactly that."""

        lines = format_review_plan(
            "antigravity",
            "gemini-3.1-pro-high",
            verify_engine="kiro",
            verify_model="gpt-5.6-terra",
            fallback_engine="kiro",
            fallback_model="claude-opus-5",
        )

        self.assertTrue(
            any("serves both fallback and verification" in line for line in lines)
        )

    def test_separate_providers_say_nothing(self):
        lines = format_review_plan(
            "antigravity",
            "gemini-3.1-pro-high",
            verify_engine="kiro",
            verify_model="gpt-5.6-terra",
            fallback_engine="claude",
            fallback_model="opus",
        )

        self.assertFalse(any("serves both" in line for line in lines))


class ReviewerIdentityTests(unittest.TestCase):
    def test_formats_configured_antigravity_model(self) -> None:
        self.assertEqual(
            format_review_attribution("antigravity", "gemini-3.6-flash-high"),
            "Reviewed by Gemini 3.6 Flash (High) @ Antigravity",
        )

    def test_preserves_unknown_model_identifier(self) -> None:
        self.assertEqual(
            format_reviewer_identity("command", "vendor/model-v2"),
            "vendor/model-v2 @ Command",
        )

    def test_formats_known_provider_model_families(self) -> None:
        for model, expected in (
            ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
            ("gpt-5.6-terra", "GPT-5.6 Terra"),
            ("gpt-5", "GPT-5"),
            ("claude-opus-5", "Claude Opus 5"),
            ("claude-sonnet-4.5", "Claude Sonnet 4.5"),
            ("minimax-m2.5", "minimax-m2.5"),
            ("auto", "auto"),
            (None, "provider default (model ID not configured)"),
        ):
            with self.subTest(model=model):
                self.assertEqual(format_model_display_name(model), expected)

    def test_attribution_names_a_separate_verifier(self) -> None:
        self.assertEqual(
            format_review_attribution(
                "antigravity",
                "gemini-3.1-pro-high",
                "kiro",
                "gpt-5.6-terra",
            ),
            "Reviewed by Gemini 3.1 Pro (High) @ Antigravity, "
            "verified by GPT-5.6 Terra @ Kiro",
        )

    def test_attribution_omits_the_verifier_when_it_is_the_reviewer(self) -> None:
        self.assertEqual(
            format_review_attribution("kiro", "claude-opus-5", None, None),
            "Reviewed by Claude Opus 5 @ Kiro",
        )


    def test_plan_states_every_configured_engine_in_order(self) -> None:
        self.assertEqual(
            format_review_plan(
                "antigravity",
                "gemini-3.1-pro-high",
                "kiro",
                "gpt-5.6-terra",
                "claude",
                "claude-opus-5",
            ),
            (
                "Review will be done by Gemini 3.1 Pro (High) @ Antigravity.",
                "Falls back to Claude Opus 5 @ Claude on engine failure.",
                "Verification by GPT-5.6 Terra @ Kiro.",
            ),
        )

    def test_plan_omits_unconfigured_engines(self) -> None:
        self.assertEqual(
            format_review_plan("codex", "gpt-5.1-codex"),
            ("Review will be done by GPT-5.1 Codex @ Codex.",),
        )

    def test_plan_describes_a_provider_default_without_a_model_id(self) -> None:
        self.assertEqual(
            format_review_plan("claude", None, fallback_engine="kiro"),
            (
                "Review will be done by provider default "
                "(model ID not configured) @ Claude.",
                "Falls back to provider default "
                "(model ID not configured) @ Kiro on engine failure.",
            ),
        )

    def test_plan_states_each_role_effort(self) -> None:
        self.assertEqual(
            format_review_plan(
                "kiro",
                "claude-opus-5.5",
                "kiro",
                "gpt-5.6-terra",
                "antigravity",
                "claude-opus-4-6-thinking",
                review_effort="xhigh",
                code_effort="max",
                verify_effort="xhigh",
            )[:3],
            (
                "Review will be done by Claude Opus 5.5 @ Kiro at xhigh effort, "
                "max for the code review.",
                "Falls back to Claude Opus 4.6 (Thinking) @ Antigravity on engine "
                "failure.",
                "Verification by GPT-5.6 Terra @ Kiro at xhigh effort.",
            ),
        )

    def test_plan_names_a_code_override_over_the_model_default(self) -> None:
        self.assertEqual(
            format_review_plan("kiro", "claude-opus-5.5", code_effort="max"),
            (
                "Review will be done by Claude Opus 5.5 @ Kiro at the model's "
                "default effort, max for the code review.",
            ),
        )

    def test_one_effort_for_every_level_is_stated_once(self) -> None:
        self.assertEqual(
            format_review_plan(
                "kiro", "claude-opus-5.5", review_effort="max", code_effort="max"
            ),
            ("Review will be done by Claude Opus 5.5 @ Kiro at max effort.",),
        )

    def test_antigravity_claude_ids_are_named_like_the_others(self) -> None:
        """Its IDs spell the version with dashes and carry the reasoning variant."""

        for model, expected in (
            ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6"),
            # From agy 1.4.6 an ID names the effort tier instead of a variant.
            ("claude-opus-5-5-high", "Claude Opus 5.5 (High)"),
            ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)"),
            ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
        ):
            with self.subTest(model=model):
                self.assertEqual(format_model_display_name(model), expected)


class SharedReviewAndVerifierProviderTests(unittest.TestCase):
    def test_a_reviewer_that_also_verifies_is_named_at_startup(self) -> None:
        """A fallback on another provider still reviews while the reviewer's is down,
        but nothing it finds can be verified, so it reads as more cover than it is."""

        lines = format_review_plan(
            "kiro",
            "claude-opus-5.5",
            verify_engine="kiro",
            verify_model="gpt-5.6-terra",
            fallback_engine="antigravity",
            fallback_model="claude-opus-4-6-thinking",
        )

        self.assertEqual(
            lines[-1],
            "Note: kiro serves both review and verification, so while it is down "
            "the fallback's findings cannot be verified or delivered.",
        )

    def test_no_fallback_needs_no_note(self) -> None:
        lines = format_review_plan(
            "kiro",
            "claude-opus-5.5",
            verify_engine="kiro",
            verify_model="gpt-5.6-terra",
        )

        self.assertFalse(any(line.startswith("Note:") for line in lines))

    def test_a_fallback_on_the_same_provider_needs_no_note(self) -> None:
        """Then the fallback cannot outlast the provider either, so the note would
        promise a difference that is not there."""

        lines = format_review_plan(
            "kiro",
            "claude-opus-5.5",
            verify_engine="kiro",
            verify_model="gpt-5.6-terra",
            fallback_engine="kiro",
            fallback_model="claude-opus-4.8",
        )

        self.assertFalse(any(line.startswith("Note:") for line in lines))

    def test_a_verification_fallback_elsewhere_lifts_the_note(self) -> None:
        """With a verifier on another provider the fallback's findings are verified
        and delivered while the reviewer's provider is down."""

        lines = format_review_plan(
            "kiro",
            "claude-opus-5.5",
            verify_engine="kiro",
            verify_model="gpt-5.6-terra",
            fallback_engine="antigravity",
            fallback_model="claude-opus-5-5-high",
            verify_effort="xhigh",
            verify_fallback_engine="claude",
            verify_fallback_model="claude-sonnet-5-5",
            verify_fallback_effort="xhigh",
        )

        self.assertIn(
            "Verification falls back to Claude Sonnet 5.5 @ Claude at xhigh "
            "effort on engine failure.",
            lines,
        )
        self.assertFalse(any(line.startswith("Note:") for line in lines))

    def test_a_verification_fallback_on_the_reviewer_s_provider_keeps_it(self) -> None:
        lines = format_review_plan(
            "kiro",
            "claude-opus-5.5",
            verify_engine="kiro",
            verify_model="gpt-5.6-terra",
            fallback_engine="antigravity",
            fallback_model="claude-opus-5-5-high",
            verify_fallback_engine="kiro",
            verify_fallback_model="claude-sonnet-5.5",
        )

        self.assertTrue(lines[-1].startswith("Note: kiro serves both review"))


if __name__ == "__main__":
    unittest.main()
