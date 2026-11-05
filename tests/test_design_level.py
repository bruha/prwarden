"""Tests for the advisory solution-design review level."""

import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prwarden import cli
from prwarden.config import Settings
from prwarden.delivery import format_design_slack_message
from prwarden.engine import (
    DESIGN_FIELD_LIMITS,
    DESIGN_LEVEL,
    build_design_verification_schema,
    build_prompt,
    build_schema,
    design_candidate_refs,
)
from prwarden.engine import EngineError, response_discriminator_keys
from prwarden.engine_kiro import KiroEngine, parse_kiro_output
from prwarden.github import DESIGN_COMMENT_MARKER, format_design_comment
from prwarden.models import (
    DesignAssessment,
    DesignFinding,
    FindingEvidence,
    PriorReviewComment,
    PullRequest,
    PullRequestSnapshot,
    RejectedDesignCandidate,
    Repository,
    ReviewMaterial,
)
from prwarden.review import (
    ReviewError,
    validate_design_findings,
    validate_design_judgments,
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


def design_response(**overrides):
    finding = {
        "title": "Reimplements the shared date picker",
        "severity": "MEDIUM",
        "category": "duplicates_existing_capability",
        "problem": "The new picker hand-rolls behaviour the workspace provides.",
        "consequence": "The settings page now has two pickers to maintain.",
        "why_not_preference": "The second implementation lacks the bounds check.",
        "alternative": "Wrap the existing picker instead.",
        "citations": [
            {
                "path": "app.py",
                "line": 1,
                "excerpt": "value = 1",
                "relevance": "The line introducing the second implementation.",
            }
        ],
    }
    finding.update(overrides.pop("finding", {}))
    payload = {
        "understanding": "It adds a date picker reached from the settings page.",
        "verdict": "flawed",
        "findings": [finding],
    }
    payload.update(overrides)
    return payload


def design_candidate(title: str = "One") -> DesignFinding:
    return DesignFinding(
        severity="MEDIUM",
        title=title,
        category="contract_mismatch",
        problem="p",
        consequence="c",
        why_not_preference="w",
        alternative="a",
        citations=(FindingEvidence("app.py", 1, "value = 1", "r"),),
    )


def fallback_for(settings, engine):
    """Return a fallback reviewer on another provider, or None without an engine."""

    if engine is None:
        return None
    return cli.FallbackReviewer(
        engine=engine,
        settings=Settings(
            **{
                **settings.__dict__,
                "review_engine": "kiro",
                "review_model": "claude-opus-5.5",
            }
        ),
    )


class DesignValidationTests(unittest.TestCase):
    def material(self) -> ReviewMaterial:
        return ReviewMaterial("diff", {"app.py": frozenset({1})})

    def test_validated_design_finding_keeps_its_citation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")

            understanding, verdict, findings = validate_design_findings(
                design_response(), self.material(), root, 4
            )

            self.assertIn("date picker", understanding)
            self.assertEqual(verdict, "flawed")
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].category, "duplicates_existing_capability")
            self.assertEqual(findings[0].citations[0].line, 1)

    def test_design_citation_drifted_by_a_line_is_relocated(self) -> None:
        """The excerpt is unique in the file, so the line number is recoverable."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("import os\nvalue = 1\n", encoding="utf-8")

            _, _, findings = validate_design_findings(
                design_response(
                    finding={
                        "citations": [
                            {
                                "path": "app.py",
                                "line": 1,
                                "excerpt": "value = 1",
                                "relevance": "r",
                            }
                        ]
                    }
                ),
                ReviewMaterial("diff", {"app.py": frozenset({2})}),
                root,
                4,
            )

            self.assertEqual(findings[0].citations[0].line, 2)

    def test_design_response_is_rejected_when_it_cannot_be_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            material = self.material()

            cases = {
                "sound with findings": design_response(verdict="sound"),
                "unknown category": design_response(finding={"category": "taste"}),
                "unknown severity": design_response(finding={"severity": "COSMETIC"}),
                "excerpt does not match": design_response(
                    finding={
                        "citations": [
                            {
                                "path": "app.py",
                                "line": 1,
                                "excerpt": "value = 2",
                                "relevance": "r",
                            }
                        ]
                    }
                ),
                "path outside the workspace": design_response(
                    finding={
                        "citations": [
                            {
                                "path": "../outside.py",
                                "line": 1,
                                "excerpt": "value = 1",
                                "relevance": "r",
                            }
                        ]
                    }
                ),
                "no citation at all": design_response(finding={"citations": []}),
            }
            for label, payload in cases.items():
                with self.subTest(label=label):
                    with self.assertRaises(ReviewError):
                        validate_design_findings(payload, material, root, 4)

    def test_design_response_may_not_exceed_the_configured_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            payload = design_response()
            payload["findings"] = payload["findings"] * 3

            with self.assertRaises(ReviewError) as raised:
                validate_design_findings(payload, self.material(), root, 2)

            self.assertIn("exceeding the configured maximum of 2", str(raised.exception))

    def test_sound_design_carries_no_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, verdict, findings = validate_design_findings(
                {
                    "understanding": "It renames a helper and updates its callers.",
                    "verdict": "sound",
                    "findings": [],
                },
                self.material(),
                Path(directory),
                4,
            )

            self.assertEqual(verdict, "sound")
            self.assertEqual(findings, [])


class DesignJudgmentTests(unittest.TestCase):
    def test_verifier_decides_each_candidate_by_reference(self) -> None:
        candidates = (design_candidate("One"), design_candidate("Two"))

        confirmed, rejected = validate_design_judgments(
            {
                "judgments": [
                    {"ref": "d1", "verdict": "confirmed", "reason": "Read the caller."},
                    {
                        "ref": "D2",
                        "verdict": "rejected",
                        "reason": "Only Storybook renders it.",
                    },
                ]
            },
            candidates,
        )

        self.assertEqual([finding.title for finding in confirmed], ["One"])
        self.assertEqual(rejected[0].finding.title, "Two")
        self.assertEqual(rejected[0].reason, "Only Storybook renders it.")

    def test_confirmed_candidate_is_the_controller_object(self) -> None:
        """The verifier judges references, so it cannot edit what it approves."""

        candidates = (design_candidate("One"),)

        confirmed, _ = validate_design_judgments(
            {
                "judgments": [
                    {"ref": "D1", "verdict": "confirmed", "reason": "Checked."}
                ]
            },
            candidates,
        )

        self.assertIs(confirmed[0], candidates[0])

    def test_judgments_must_cover_every_candidate_exactly_once(self) -> None:
        candidates = (design_candidate("One"), design_candidate("Two"))
        cases = {
            "unknown reference": [
                {"ref": "D9", "verdict": "confirmed", "reason": "x"}
            ],
            "left unjudged": [{"ref": "D1", "verdict": "confirmed", "reason": "x"}],
            "judged twice": [
                {"ref": "D1", "verdict": "confirmed", "reason": "x"},
                {"ref": "D1", "verdict": "rejected", "reason": "y"},
                {"ref": "D2", "verdict": "confirmed", "reason": "z"},
            ],
            "unknown verdict": [
                {"ref": "D1", "verdict": "maybe", "reason": "x"},
                {"ref": "D2", "verdict": "confirmed", "reason": "z"},
            ],
            "missing reason": [
                {"ref": "D1", "verdict": "confirmed", "reason": " "},
                {"ref": "D2", "verdict": "confirmed", "reason": "z"},
            ],
        }
        for label, judgments in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ReviewError):
                    validate_design_judgments({"judgments": judgments}, candidates)


class DesignPromptTests(unittest.TestCase):
    def prompt(self, material: ReviewMaterial) -> str:
        return build_prompt(
            Repository("app", "owner/app", Path("/tmp/review")),
            PullRequest(1, "Title", "Body", "author", "a" * 40, "url"),
            material,
            10,
        )

    def test_verification_prompt_states_the_reason_budget(self) -> None:
        """A limit the verifier is never told is one it can only break by luck."""

        material = ReviewMaterial(
            "diff",
            {"app.py": frozenset({1})},
            level=DESIGN_LEVEL,
            design_candidates=(design_candidate("One"),),
        )

        prompt = self.prompt(material)

        self.assertIn(
            f"`reason` accepts at most {DESIGN_FIELD_LIMITS['reason']} characters",
            prompt,
        )
        self.assertIn("rejected in full", prompt)
        self.assertNotIn("PREVIOUS JUDGMENT WAS REJECTED", prompt)

    def test_a_rejected_judgment_is_quoted_back_without_dropping_candidates(
        self,
    ) -> None:
        """The reviewer's correction says to drop what it cannot establish.

        A verifier that dropped a candidate would fail for leaving it unjudged, so
        this pass needs its own wording.
        """

        material = ReviewMaterial(
            "diff",
            {"app.py": frozenset({1})},
            level=DESIGN_LEVEL,
            design_candidates=(design_candidate("One"),),
            validation_error="Design judgment field 'reason' exceeds 700 characters",
        )

        prompt = self.prompt(material)

        self.assertIn("PREVIOUS JUDGMENT WAS REJECTED", prompt)
        self.assertIn("exceeds 700 characters", prompt)
        self.assertIn("Judge every supplied ref again", prompt)
        self.assertNotIn("drop the\none you cannot", prompt)

    def test_design_prompt_excludes_taste_and_line_level_defects(self) -> None:
        prompt = self.prompt(
            ReviewMaterial("diff", {"app.py": frozenset({1})}, level=DESIGN_LEVEL)
        )

        self.assertIn("SOLUTION DESIGN", prompt)
        self.assertIn("NOT reviewing visual or graphic design", prompt)
        self.assertIn("A separate level already", prompt)
        self.assertIn("Report at most 4,", prompt)
        self.assertIn("must be reachable in this workspace", prompt)
        self.assertIn(
            "If the only\n  cost you can name is that the result would be cleaner",
            prompt,
        )

    def test_design_prompt_carries_the_configured_finding_cap(self) -> None:
        prompt = self.prompt(
            ReviewMaterial(
                "diff",
                {"app.py": frozenset({1})},
                level=DESIGN_LEVEL,
                max_design_findings=2,
            )
        )

        self.assertIn("Report at most 2,", prompt)

    def test_design_prompt_ties_every_finding_to_a_flawed_verdict(self) -> None:
        """Opus 5.5 reported a flaw under a 'sound' verdict on both of its first
        design reviews, and each refusal cost a second design invocation."""

        prompt = self.prompt(
            ReviewMaterial("diff", {"app.py": frozenset({1})}, level=DESIGN_LEVEL)
        )

        self.assertIn('any finding requires "flawed"', prompt)
        self.assertIn('"sound" always comes with an empty `findings` array', prompt)

    def test_design_verification_prompt_supplies_candidates_by_reference(self) -> None:
        material = ReviewMaterial(
            "diff",
            {"app.py": frozenset({1})},
            level=DESIGN_LEVEL,
            design_candidates=(design_candidate("One"), design_candidate("Two")),
        )

        prompt = self.prompt(material)

        self.assertIn("adversarial verifier", prompt)
        self.assertIn('"ref": "D1"', prompt)
        self.assertIn('"ref": "D2"', prompt)
        self.assertIn("A candidate you could not check is rejected", prompt)
        self.assertIn("the cost named in `why_not_preference` is taste", prompt)
        self.assertEqual(
            sorted(design_candidate_refs(material.design_candidates)), ["D1", "D2"]
        )

    def test_schema_follows_the_level_of_the_material(self) -> None:
        design = ReviewMaterial("diff", {}, level=DESIGN_LEVEL)
        verification = ReviewMaterial(
            "diff", {}, level=DESIGN_LEVEL, design_candidates=(design_candidate(),)
        )

        self.assertEqual(
            sorted(build_schema(10, design)["properties"]),
            ["findings", "understanding", "verdict"],
        )
        self.assertEqual(
            sorted(build_schema(10, verification)["properties"]), ["judgments"]
        )
        self.assertEqual(
            sorted(build_schema(10, ReviewMaterial("diff", {}))["properties"]),
            sorted(build_schema(10)["properties"]),
        )

    def test_verification_schema_requires_one_judgment_per_candidate(self) -> None:
        judgments = build_design_verification_schema(3)["properties"]["judgments"]

        self.assertEqual(judgments["minItems"], 3)
        self.assertEqual(judgments["maxItems"], 3)


class DesignAdapterContractTests(unittest.TestCase):
    """What identifies a response differs per level, and adapters must follow it."""

    def test_response_keys_follow_the_level(self) -> None:
        design = ReviewMaterial("diff", {}, level=DESIGN_LEVEL)
        verification = ReviewMaterial(
            "diff", {}, level=DESIGN_LEVEL, design_candidates=(design_candidate(),)
        )

        self.assertEqual(
            response_discriminator_keys(design),
            frozenset({"understanding", "verdict", "findings"}),
        )
        self.assertEqual(
            response_discriminator_keys(verification), frozenset({"judgments"})
        )
        self.assertEqual(
            response_discriminator_keys(ReviewMaterial("diff", {})),
            frozenset({"verdict", "findings"}),
        )
        self.assertEqual(
            response_discriminator_keys(), frozenset({"verdict", "findings"})
        )

    def test_design_judgment_survives_a_narrated_transcript(self) -> None:
        """A real Kiro run narrates its tool use before answering.

        The salvage path recognised only the code level's keys, so a valid design
        judgment was reported as invalid JSON and the whole level failed.
        """

        transcript = (
            "Reading file: /tmp/app/src/engine.py, from line 1 to 5 (using tool: read)\n"
            " \u2713 Successfully read 126 bytes\n - Completed in 0.0s\n\n"
            '> {"judgments":[{"ref":"D1","verdict":"rejected",'
            '"reason":"Only stories reference it."}]}\n'
        )
        verification = ReviewMaterial(
            "diff", {}, level=DESIGN_LEVEL, design_candidates=(design_candidate(),)
        )

        parsed = parse_kiro_output(
            transcript, response_discriminator_keys(verification)
        )
        self.assertEqual(parsed["judgments"][0]["ref"], "D1")

        with self.assertRaises(EngineError):
            parse_kiro_output(transcript)

    def test_kiro_adapter_parses_a_design_judgment_end_to_end(self) -> None:
        """The adapter, not only the parser, has to follow the level.

        This is the wiring the first live dry run broke: prompt and schema selection
        followed the level while the transcript parser still looked for the code
        level's keys, so a valid judgment became "Kiro returned invalid JSON".
        """

        transcript = (
            "Reading file: /tmp/app/app.py, from line 1 to 5 (using tool: read)\n"
            " \u2713 Successfully read 126 bytes\n - Completed in 0.0s\n\n"
            '> {"judgments":[{"ref":"D1","verdict":"confirmed",'
            '"reason":"Read the settings route."}]}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = Settings(
                reviewer_username="reviewer",
                repositories=[repository],
                dry_run=True,
                slack_webhook_url=None,
                state_file=Path(directory) / "state.json",
                review_engine="kiro",
                review_engine_command=("kiro-cli",),
                review_engine_env_allowlist=(),
                review_model=None,
                review_timeout_seconds=300,
                max_diff_bytes=100000,
                max_findings=10,
            )
            material = ReviewMaterial(
                "diff",
                {"app.py": frozenset({1})},
                level=DESIGN_LEVEL,
                design_candidates=(design_candidate(),),
            )

            with patch(
                "prwarden.engine_kiro.run_cmd_streams", return_value=(transcript, "")
            ):
                parsed = KiroEngine(settings).review(
                    repository,
                    PullRequest(1, "Title", "", "author", "head", "url"),
                    material,
                )

            self.assertEqual(parsed["judgments"][0]["verdict"], "confirmed")

    def test_design_review_survives_a_narrated_transcript(self) -> None:
        transcript = (
            "Searching for: picker (using tool: grep)\n - Completed in 0.1s\n\n"
            '> {"understanding":"u","verdict":"sound","findings":[]}\n'
        )
        design = ReviewMaterial("diff", {}, level=DESIGN_LEVEL)

        parsed = parse_kiro_output(transcript, response_discriminator_keys(design))

        self.assertEqual(parsed["verdict"], "sound")


class DesignDeliveryFormatTests(unittest.TestCase):
    def test_the_transcript_names_each_candidate_s_category(self) -> None:
        """A measurement of one category cannot be read off a log that omits it."""

        kept = design_candidate("Kept")
        assessment = DesignAssessment(
            understanding="u",
            verdict="concerns",
            findings=(kept,),
            rejected=(RejectedDesignCandidate(design_candidate("Dropped"), "no"),),
        )

        with patch("sys.stdout", new_callable=StringIO) as output:
            cli._report_design_assessment(assessment, 2)

        reported = output.getvalue()
        self.assertIn(
            f"Rejected design candidate [MEDIUM] ({kept.category}) Dropped", reported
        )
        self.assertIn(f"-> [MEDIUM] ({kept.category}) Kept", reported)

    def test_github_comment_is_titled_and_attributes_both_models(self) -> None:
        body = format_design_comment(
            [design_candidate("Splits the source of truth")],
            "antigravity",
            "gemini-3.1-pro-high",
            "kiro",
            "gpt-5.6-terra",
        )

        # The notes stay advisory in what they can do, which the controller
        # enforces; the heading does not say so.
        self.assertTrue(body.startswith("## Solution design notes\n\n"))
        self.assertNotIn("(advisory)", body)
        self.assertIn("[MEDIUM] Splits the source of truth", body)
        self.assertIn("contract mismatch", body)
        self.assertIn("`app.py:1`", body)
        self.assertIn("verified by GPT-5.6 Terra @ Kiro", body)
        self.assertTrue(body.rstrip().endswith(DESIGN_COMMENT_MARKER))
        # No paragraph explains what the notes are, and the footer names the models
        # rather than the commit.
        self.assertNotIn("These notes are advisory", body)
        self.assertNotIn("Design level", body)
        self.assertNotIn("reviewed at", body)

    def test_slack_design_message_redacts_local_paths(self) -> None:
        finding = DesignFinding(
            severity="HIGH",
            title="Wrong layer",
            category="responsibility_in_wrong_place",
            problem="Reads the store from a component.",
            consequence="Every caller repeats the fallback.",
            why_not_preference="Two sources of truth disagree at runtime.",
            alternative="Read it in the container instead.",
            citations=(
                FindingEvidence(
                    "src/app.tsx", 4, "const theme = useAppSelector(", "r"
                ),
            ),
        )

        text = format_design_slack_message(
            Repository("app", "owner/app", Path("/Users/someone/checkouts/app")),
            PullRequest(7, "Title", "", "author", "head", "url"),
            [finding],
            "antigravity",
            None,
            "kiro",
            None,
        )

        self.assertIn("Solution Design Notes", text)
        self.assertIn("These describe the approach, not individual lines.", text)
        self.assertNotIn("dvisory", text)
        self.assertIn("[HIGH] Wrong layer", text)
        self.assertIn("responsibility in wrong place", text)
        self.assertNotIn("/Users/someone", text)


class DesignOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        without_review_rules(self)

    def settings(
        self,
        directory: str,
        repository: Repository,
        design_level: bool = True,
    ) -> Settings:
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
            design_level=design_level,
        )

    def approving_code_review(self) -> dict:
        return {
            "verdict": "approve",
            "approval_reason": "The change is correct.",
            "findings": [],
        }

    def run_review(
        self,
        directory: str,
        engine: Mock,
        *,
        design_level: bool = True,
        pull_request: "PullRequest" = None,
        max_diff_bytes: int = 100000,
        extra_files: list = None,
        author: str = "author",
        live: bool = True,
        verification_engine=None,
        store=None,
        force: bool = False,
        receipts=("design-comment-1", "approval-1"),
        fallback_engine=None,
        slack: bool = True,
        own_notes=(),
    ):
        root = Path(directory)
        (root / "app.py").write_text("value = 1\n", encoding="utf-8")
        repository = Repository("app", "owner/app", root, slack=slack)
        if pull_request is None:
            pull_request = PullRequest(1, "Title", "", author, "head", "url")
        settings = self.settings(directory, repository, design_level=design_level)
        settings = Settings(
            **{**settings.__dict__, "max_diff_bytes": max_diff_bytes}
        )
        verification = None
        if verification_engine is not None:
            verification = cli.VerificationPass(
                engine=verification_engine, settings=settings
            )
        lookup = (
            {"side_effect": own_notes}
            if isinstance(own_notes, Exception)
            else {"return_value": tuple(own_notes)}
        )

        with patch("prwarden.cli.has_current_review", return_value=False), patch(
            "prwarden.cli.list_prior_review_comments", return_value=()
        ), patch(
            "prwarden.cli.list_own_advisory_notes", **lookup
        ) as own_lookup, patch(
            "prwarden.cli.list_changed_files",
            return_value=[
                {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
            ]
            + list(extra_files or ()),
        ), patch(
            "prwarden.cli.list_pull_request_checks", return_value=()
        ), patch(
            "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
        ), patch(
            "prwarden.cli.review_workspace"
        ) as workspace, patch(
            "prwarden.cli.submit_design_notes", return_value=receipts[0]
        ) as design_notes, patch(
            "prwarden.cli.submit_approval", return_value=receipts[1]
        ) as approval, patch(
            "prwarden.cli.submit_review", return_value="review-1"
        ) as review, patch(
            "prwarden.cli.send_slack_message", return_value="http-200"
        ) as slack, patch(
            "prwarden.cli.submit_lgtm_comment", return_value="lgtm-comment-1"
        ) as lgtm, patch(
            "prwarden.cli.hide_earlier_lgtm_comments", return_value=0
        ), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            workspace.return_value.__enter__.return_value = repository
            workspace.return_value.dependency_links = ()
            workspace.return_value.dependency_notes = ()
            owned_store = store
            if owned_store is None:
                owned_store = JsonStateStore(settings.state_file)
            with owned_store as opened:
                cli._process_pull_request(
                    settings,
                    repository,
                    pull_request,
                    opened,
                    engine,
                    live=live,
                    force=force,
                    verification=verification,
                    fallback=fallback_for(settings, fallback_engine),
                )
        return {
            "terminal": output.getvalue(),
            "design_notes": design_notes,
            "approval": approval,
            "review": review,
            "slack": slack,
            "lgtm": lgtm,
            "settings": settings,
            "own_lookup": own_lookup,
        }

    def test_an_unusable_design_judgment_is_corrected_once(self) -> None:
        """The design review retries once; its verification needs the same.

        An over-long reason discarded a design review and its verification pass on
        one pull request [pr:m7o3], minutes of work lost to a formatting slip.
        """

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            confirmed = {
                "judgments": [
                    {
                        "ref": "D1",
                        "verdict": "confirmed",
                        "reason": "Read the existing picker and the settings route.",
                    }
                ]
            }
            verifier.review.side_effect = [
                # A reason past the 700-character budget, exactly as Kiro answered.
                {
                    "judgments": [
                        {"ref": "D1", "verdict": "confirmed", "reason": "x" * 701}
                    ]
                },
                confirmed,
            ]

            result = self.run_review(
                directory, engine, verification_engine=verifier
            )
            terminal = result["terminal"]

            self.assertEqual(verifier.review.call_count, 2)
            self.assertIn("Design verification output rejected", terminal)
            self.assertIn(
                "Retrying the design verification once with controller", terminal
            )
            # The corrected pass still delivered, so nothing was lost.
            result["design_notes"].assert_called_once()
            self.assertNotIn("Design level failed", terminal)

            # The retry has to say what was refused, or it repeats the same answer.
            retried = verifier.review.call_args_list[1].args[2]
            self.assertIn("exceeds 700 characters", retried.validation_error)
            self.assertIn("Design judgment field", retried.validation_error)

    def confirming_verifier(self) -> Mock:
        verifier = Mock()
        verifier.review.return_value = {
            "judgments": [
                {
                    "ref": "D1",
                    "verdict": "confirmed",
                    "reason": "Read the existing picker and the settings route.",
                }
            ]
        }
        return verifier

    def test_an_engine_failure_hands_the_design_review_to_the_fallback(self) -> None:
        """From 2026-10-13 a withdrawn model failed 48 design reviews in a week
        while the code level carried on through a fallback this level lacked."""

        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                EngineError("model gemini-3.1-pro-high is not recognized"),
                self.approving_code_review(),
            ]
            fallback = Mock()
            fallback.review.return_value = design_response()

            result = self.run_review(
                directory,
                engine,
                verification_engine=self.confirming_verifier(),
                fallback_engine=fallback,
            )
            terminal = result["terminal"]

            fallback.review.assert_called_once()
            self.assertEqual(fallback.review.call_args.args[2].level, DESIGN_LEVEL)
            self.assertIn(
                "antigravity design review engine failed: model gemini-3.1-pro-high",
                terminal,
            )
            self.assertIn(
                "Running fallback design review with kiro (claude-opus-5.5)", terminal
            )
            self.assertNotIn("Design level failed", terminal)
            # Credited to the engine that wrote it, never to the one that failed.
            body = result["design_notes"].call_args.args[2]
            self.assertIn("Reviewed by Claude Opus 5.5 @ Kiro", body)
            self.assertNotIn("Antigravity", body)
            # A transient refusal says nothing about the quota.
            self.assertEqual(cli._PRIMARY_ENGINE.exhausted_reason, "")

    def test_without_a_verifier_the_fallback_judges_its_own_design_notes(self) -> None:
        """The primary is the engine that just failed, so it cannot be the judge."""

        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                EngineError("UNAVAILABLE: try again"),
                self.approving_code_review(),
            ]
            fallback = Mock()
            fallback.review.side_effect = [
                design_response(),
                self.confirming_verifier().review.return_value,
            ]

            result = self.run_review(directory, engine, fallback_engine=fallback)

            self.assertEqual(fallback.review.call_count, 2)
            judged = fallback.review.call_args_list[1].args[2]
            self.assertTrue(judged.design_candidates)
            result["design_notes"].assert_called_once()

    def test_a_rejected_design_answer_does_not_hand_over(self) -> None:
        """The engine told how to fix its answer is the one that gave it."""

        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                {"understanding": "u", "verdict": "not-a-verdict", "findings": []},
                {"understanding": "u", "verdict": "not-a-verdict", "findings": []},
                self.approving_code_review(),
            ]
            fallback = Mock()

            result = self.run_review(directory, engine, fallback_engine=fallback)

            fallback.review.assert_not_called()
            self.assertIn("Design level failed", result["terminal"])
            result["approval"].assert_called_once()

    def test_a_failed_fallback_names_both_and_blames_neither_quota(self) -> None:
        """A spent fallback must not mark the primary spent: the code review would
        then skip a primary that can still answer and go to the spent fallback."""

        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                EngineError("UNAVAILABLE: try again"),
                self.approving_code_review(),
            ]
            fallback = Mock()
            fallback.review.side_effect = EngineError(
                "Individual quota reached. Resets in 9h12m."
            )

            result = self.run_review(directory, engine, fallback_engine=fallback)
            terminal = result["terminal"]

            self.assertIn(
                "Design level failed: antigravity design review failed: UNAVAILABLE: "
                "try again; kiro (claude-opus-5.5) fallback also failed: Individual "
                "quota reached",
                terminal,
            )
            self.assertEqual(cli._PRIMARY_ENGINE.exhausted_reason, "")
            # The code review still ran on the primary and approved.
            self.assertEqual(engine.review.call_count, 2)
            result["approval"].assert_called_once()

    def test_a_spent_primary_sends_the_design_review_straight_to_the_fallback(
        self,
    ) -> None:
        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)
        cli._PRIMARY_ENGINE.record_exhausted("Individual quota reached")
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            fallback = Mock()
            fallback.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]

            result = self.run_review(
                directory,
                engine,
                verification_engine=self.confirming_verifier(),
                fallback_engine=fallback,
            )

            engine.review.assert_not_called()
            self.assertIn(
                "running the design review with kiro (claude-opus-5.5) directly",
                result["terminal"],
            )
            result["design_notes"].assert_called_once()

    def test_a_design_judgment_correction_is_not_retried_forever(self) -> None:
        """One correction, then the level fails; it must never loop."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {
                "judgments": [
                    {"ref": "D1", "verdict": "confirmed", "reason": "x" * 701}
                ]
            }

            result = self.run_review(
                directory, engine, verification_engine=verifier
            )
            terminal = result["terminal"]

            self.assertEqual(verifier.review.call_count, 2)
            self.assertIn("Design verification correction retry rejected", terminal)
            self.assertIn("Design level failed", terminal)
            result["design_notes"].assert_not_called()
            # The advisory level failed, so the code level still approved.
            result["approval"].assert_called_once()

    def test_design_level_reviews_first_and_posts_advisory_notes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {
                "judgments": [
                    {
                        "ref": "D1",
                        "verdict": "confirmed",
                        "reason": "Read the existing picker and the settings route.",
                    }
                ]
            }

            result = self.run_review(
                directory, engine, verification_engine=verifier
            )
            terminal = result["terminal"]

            # The design level ran before the code level.
            self.assertLess(
                terminal.index("Reviewing solution design"),
                terminal.index("Running antigravity review"),
            )
            design_material = engine.review.call_args_list[0].args[2]
            code_material = engine.review.call_args_list[1].args[2]
            self.assertEqual(design_material.level, DESIGN_LEVEL)
            self.assertEqual(code_material.level, "code")
            self.assertEqual(design_material.max_design_findings, 4)

            result["design_notes"].assert_called_once()
            body = result["design_notes"].call_args.args[2]
            self.assertIn("Solution design notes", body)
            self.assertIn("Reimplements the shared date picker", body)
            self.assertIn("Posted advisory design notes (receipt: design-comment", terminal)
            # Advisory notes never enter the review channel, and the code level
            # still approved on its own evidence.
            result["review"].assert_not_called()
            result["approval"].assert_called_once()

    def test_design_verification_budget_grows_with_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            payload = design_response()
            payload["findings"] = [
                dict(payload["findings"][0], title=f"Finding {index}")
                for index in range(2)
            ]
            engine.review.side_effect = [payload, self.approving_code_review()]
            verifier.review.return_value = {
                "judgments": [
                    {"ref": "D1", "verdict": "rejected", "reason": "Not reachable."},
                    {"ref": "D2", "verdict": "rejected", "reason": "Taste only."},
                ]
            }

            result = self.run_review(
                directory, engine, verification_engine=verifier
            )
            settings = result["settings"]

            self.assertEqual(
                verifier.review.call_args.kwargs["timeout_seconds"],
                settings.review_verify_timeout_seconds
                + 2 * settings.review_verify_timeout_per_candidate_seconds,
            )

    def test_rejected_design_candidates_stay_out_of_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {
                "judgments": [
                    {
                        "ref": "D1",
                        "verdict": "rejected",
                        "reason": "Only stories reference the component.",
                    }
                ]
            }

            result = self.run_review(
                directory, engine, verification_engine=verifier
            )
            terminal = result["terminal"]

            self.assertIn("Confirmed 0 of 1 design candidate finding", terminal)
            self.assertIn("Only stories reference the component.", terminal)
            self.assertIn("No confirmed solution-design findings", terminal)
            result["design_notes"].assert_not_called()
            result["approval"].assert_called_once()

    def test_design_level_failure_leaves_the_code_review_alone(self) -> None:
        """An advisory level informs; it may not decide the fate of the review."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            malformed = {
                "understanding": "u",
                "verdict": "flawed",
                "findings": "not a list",
            }
            engine.review.side_effect = [
                malformed,
                malformed,
                self.approving_code_review(),
            ]

            result = self.run_review(directory, engine)
            terminal = result["terminal"]

            self.assertIn("Design output rejected:", terminal)
            self.assertIn("Retrying the design review once", terminal)
            self.assertIn("Design level failed:", terminal)
            self.assertIn("Approved successfully", terminal)
            result["design_notes"].assert_not_called()
            result["approval"].assert_called_once()

    def test_a_defect_in_the_design_level_stays_in_that_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [self.approving_code_review()]

            result = self.run_review(
                directory,
                engine,
                force=True,
                own_notes=KeyError("missing"),
            )
            terminal = result["terminal"]

            self.assertIn(
                "Design level failed unexpectedly: KeyError: 'missing'", terminal
            )
            self.assertIn("Approved successfully", terminal)
            result["design_notes"].assert_not_called()
            result["approval"].assert_called_once()

    def test_rejected_design_response_is_retried_with_the_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(
                    finding={
                        "citations": [
                            {
                                "path": "app.py",
                                "line": 1,
                                "excerpt": "value = 2",
                                "relevance": "r",
                            }
                        ]
                    }
                ),
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {
                "judgments": [
                    {"ref": "D1", "verdict": "confirmed", "reason": "Checked."}
                ]
            }

            result = self.run_review(
                directory, engine, verification_engine=verifier
            )

            retry_material = engine.review.call_args_list[1].args[2]
            self.assertIn(
                "excerpt does not match", retry_material.validation_error or ""
            )
            self.assertEqual(retry_material.level, DESIGN_LEVEL)
            result["design_notes"].assert_called_once()

    def test_unverified_design_findings_are_never_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {"judgments": []}

            result = self.run_review(
                directory, engine, verification_engine=verifier
            )

            self.assertIn("Design level failed:", result["terminal"])
            self.assertIn("unjudged", result["terminal"])
            result["design_notes"].assert_not_called()
            result["approval"].assert_called_once()

    def test_disabled_design_level_costs_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = self.approving_code_review()

            result = self.run_review(directory, engine, design_level=False)

            engine.review.assert_called_once()
            self.assertNotIn("solution design", result["terminal"])
            result["design_notes"].assert_not_called()

    def test_own_pull_request_receives_design_notes_in_slack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {
                "judgments": [
                    {"ref": "D1", "verdict": "confirmed", "reason": "Checked."}
                ]
            }

            result = self.run_review(
                directory,
                engine,
                verification_engine=verifier,
                author="reviewer",
            )

            result["design_notes"].assert_not_called()
            self.assertTrue(
                any(
                    "Solution Design Notes" in call.args[1]
                    for call in result["slack"].call_args_list
                )
            )

    def test_your_own_pull_request_outside_slack_receives_the_notes_on_github(
        self,
    ) -> None:
        """A repository kept out of Slack gets the comment any other author gets,
        and nothing of the pull request reaches the webhook."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {
                "judgments": [
                    {"ref": "D1", "verdict": "confirmed", "reason": "Checked."}
                ]
            }

            result = self.run_review(
                directory,
                engine,
                verification_engine=verifier,
                author="reviewer",
                slack=False,
            )

            result["design_notes"].assert_called_once()
            self.assertIn(
                "## Solution design notes", result["design_notes"].call_args.args[2]
            )
            result["slack"].assert_not_called()
            result["approval"].assert_not_called()
            result["lgtm"].assert_called_once()

    def test_dry_run_posts_no_design_notes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {
                "judgments": [
                    {"ref": "D1", "verdict": "confirmed", "reason": "Checked."}
                ]
            }

            result = self.run_review(
                directory, engine, verification_engine=verifier, live=False
            )

            self.assertIn("Would post 1 advisory design note (dry run)", result["terminal"])
            result["design_notes"].assert_not_called()

    def settled_design_state(self, state_file: Path, key_of) -> JsonStateStore:
        store = JsonStateStore(state_file)
        with store as opened:
            opened.set_status(
                key_of(opened),
                "delivered",
                receipt_id="design-comment-1",
            )
        return JsonStateStore(state_file)

    def test_only_whole_branch_names_count_as_a_promotion(self) -> None:
        """A topic branch whose name contains "develop" is not a promotion.

        Matching loosely would silently switch the design level off for feature
        work, which is the only place it earns its cost.
        """

        settings = None
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
        cases = {
            "develop": True,
            "DEVELOP": True,
            "release/2026.09": True,
            "feat/developer-docs": False,
            "developer-tools": False,
            "hotfix/crash": False,
            "": False,
        }
        for head_ref, expected in cases.items():
            with self.subTest(head_ref=head_ref):
                pull_request = PullRequest(
                    1, "Title", "", "author", "head", "url", head_ref, "main"
                )
                self.assertEqual(
                    cli._is_release_promotion(settings, pull_request),
                    expected,
                )

    def test_an_oversized_pull_request_still_has_its_design_reviewed(self) -> None:
        """The level most worth having on a large change must not die with the size.

        Design notes describe the shape of a change, which is exactly what a
        reviewer cannot read the whole of, so this level runs on what fitted.
        """

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {
                "judgments": [
                    {"ref": "D1", "verdict": "confirmed", "reason": "Checked."}
                ]
            }

            result = self.run_review(
                directory,
                engine,
                verification_engine=verifier,
                # app.py needs 80 bytes and fits; the second file does not.
                max_diff_bytes=100,
                extra_files=[
                    {"filename": "extra.py", "patch": "@@ -0,0 +1 @@\n+other = 2"}
                ],
            )

            self.assertIn("need a human", result["terminal"])
            result["design_notes"].assert_called_once()

    def test_a_release_promotion_never_reviews_a_design(self) -> None:
        """develop to main carries no new design, whatever its size.

        Every change in it already had its design reviewed on the feature pull
        request that put it on develop, so a promotion spends a full design pass
        and a verification pass to restate that.
        """

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = self.approving_code_review()

            result = self.run_review(
                directory,
                engine,
                pull_request=PullRequest(
                    1, "Release", "", "author", "head", "url", "develop", "main"
                ),
            )

            self.assertIn("Design level skipped: develop -> main", result["terminal"])
            # Only the code level ran.
            engine.review.assert_called_once()
            result["design_notes"].assert_not_called()

    def test_a_feature_branch_still_has_its_design_reviewed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {
                "judgments": [
                    {"ref": "D1", "verdict": "confirmed", "reason": "Checked."}
                ]
            }

            result = self.run_review(
                directory,
                engine,
                verification_engine=verifier,
                pull_request=PullRequest(
                    1, "Feature", "", "author", "head", "url", "feat/sample", "develop"
                ),
            )

            self.assertNotIn("Design level skipped", result["terminal"])
            result["design_notes"].assert_called_once()

    def test_a_settled_design_is_not_reviewed_again_on_a_later_head(self) -> None:
        """One advisory comment per pull request, not one per push.

        One pull request [pr:m7o3] collected two design comments saying much the
        same thing, because each push earned another full design review and another
        comment.
        """

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            store = self.settled_design_state(
                state_file,
                lambda opened: opened.pull_request_key("owner/app", 1, "design"),
            )
            engine = Mock()
            engine.review.return_value = self.approving_code_review()

            result = self.run_review(directory, engine, store=store)

            self.assertIn(
                "Design level already settled for this pull request",
                result["terminal"],
            )
            # Only the code level ran, so the design level cost nothing.
            engine.review.assert_called_once()
            result["design_notes"].assert_not_called()

    def test_a_design_settled_under_a_head_key_still_counts(self) -> None:
        """State written before per-pull-request keying must not deliver twice."""

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            store = self.settled_design_state(
                state_file,
                lambda opened: opened.key("owner/app", 1, "an-older-head", "design"),
            )
            engine = Mock()
            engine.review.return_value = self.approving_code_review()

            result = self.run_review(directory, engine, store=store)

            self.assertIn("settled at an earlier head", result["terminal"])
            engine.review.assert_called_once()
            result["design_notes"].assert_not_called()

    def test_force_reviews_a_settled_design_again(self) -> None:
        """The escape hatch stays: --force reconsiders one named pull request."""

        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            store = self.settled_design_state(
                state_file,
                lambda opened: opened.pull_request_key("owner/app", 1, "design"),
            )
            engine = Mock()
            verifier = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier.review.return_value = {
                "judgments": [
                    {"ref": "D1", "verdict": "confirmed", "reason": "Checked."}
                ]
            }

            result = self.run_review(
                directory,
                engine,
                store=store,
                verification_engine=verifier,
                force=True,
            )

            self.assertNotIn("already settled", result["terminal"])
            result["design_notes"].assert_called_once()

    def confirming_verifier(self) -> Mock:
        verifier = Mock()
        verifier.review.return_value = {
            "judgments": [{"ref": "D1", "verdict": "confirmed", "reason": "Checked."}]
        }
        return verifier

    def test_a_design_reviewed_again_sees_its_own_earlier_notes(self) -> None:
        """Prior feedback leaves out the reviewer's own comments, so under --force
        the design level would not know what it already posted. Both the review
        and the verifier read them; the code level never does, because it could
        cite one as a reason to withhold an approval."""

        note = PriorReviewComment(
            "conversation:12",
            "conversation",
            "reviewer",
            "## Solution design notes\n\nWrong layer.\n" + DESIGN_COMMENT_MARKER,
        )
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]
            verifier = self.confirming_verifier()

            result = self.run_review(
                directory,
                engine,
                verification_engine=verifier,
                force=True,
                own_notes=(note,),
            )

            self.assertEqual(
                result["own_lookup"].call_args.args[2:],
                ("reviewer", DESIGN_COMMENT_MARKER),
            )
            design, code = [call.args[2] for call in engine.review.call_args_list]
            self.assertIn(note, design.prior_comments)
            self.assertIn(note, verifier.review.call_args.args[2].prior_comments)
            self.assertEqual(code.prior_comments, ())
            self.assertIn(
                "Considering 1 earlier design note of this reviewer",
                result["terminal"],
            )

    def test_a_first_design_review_spends_no_lookup_on_its_notes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                design_response(),
                self.approving_code_review(),
            ]

            result = self.run_review(
                directory, engine, verification_engine=self.confirming_verifier()
            )

            result["own_lookup"].assert_not_called()
            result["design_notes"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
