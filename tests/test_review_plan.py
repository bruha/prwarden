import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prwarden import cli
from prwarden.config import ConfigError, Settings
from prwarden.engine import (
    CODE_LEVEL,
    DESIGN_LEVEL,
    DOCS_LEVEL,
    effort_for,
    fallback_engine_settings,
    verification_engine_settings,
    verify_fallback_engine_settings,
)
from prwarden.models import PullRequest, Repository, ReviewMaterial
from prwarden.review_plan import ReviewPlan, ReviewType, review_plan
from prwarden.state import JsonStateStore
from tests.test_cli import READY_SNAPSHOT
from tests.test_review_rules import without_review_rules


def settings(directory: str, **overrides) -> Settings:
    """Settings naming every role, at a different effort for each."""

    repository = Repository("app", "owner/app", Path(directory))
    values = dict(
        reviewer_username="reviewer",
        repositories=[repository],
        dry_run=False,
        slack_webhook_url="https://example.invalid/webhook",
        state_file=Path(directory) / "state.json",
        review_engine="kiro",
        review_engine_command=("kiro-cli",),
        review_engine_env_allowlist=(),
        review_model="claude-sonnet-5.5",
        review_timeout_seconds=300,
        max_diff_bytes=100000,
        max_findings=10,
        review_effort="high",
        review_code_effort="xhigh",
        verify_engine="kiro",
        verify_engine_command=("kiro-cli",),
        verify_model="gpt-5.6-luna",
        verify_effort="max",
        fallback_engine="claude",
        fallback_engine_command=("claude",),
        fallback_model="claude-opus-5-5",
        fallback_effort="xhigh",
        verify_fallback_engine="codex",
        verify_fallback_engine_command=("codex",),
        verify_fallback_model="gpt-6.1-terra",
        verify_fallback_effort="xhigh",
    )
    values.update(overrides)
    return Settings(**values)


def material(level: str, verifying: bool = False) -> ReviewMaterial:
    candidates = (Mock(),) if verifying else ()
    return ReviewMaterial(
        "diff",
        {},
        level=level,
        verification_candidates=candidates if level == CODE_LEVEL else (),
        design_candidates=candidates if level == DESIGN_LEVEL else (),
        docs_candidates=candidates if level == DOCS_LEVEL else (),
    )


class ReviewPlanTests(unittest.TestCase):
    def test_every_type_runs_with_the_code_review_last(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = review_plan(settings(directory))

        self.assertEqual(plan.names, (DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL))

    def test_a_disabled_advisory_type_is_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for design, docs, expected in (
                (False, True, (DOCS_LEVEL, CODE_LEVEL)),
                (True, False, (DESIGN_LEVEL, CODE_LEVEL)),
                (False, False, (CODE_LEVEL,)),
            ):
                with self.subTest(design=design, docs=docs):
                    plan = review_plan(
                        settings(directory, design_level=design, docs_level=docs)
                    )
                    self.assertEqual(plan.names, expected)

    def test_each_call_runs_at_the_effort_it_always_did(self) -> None:
        """The code review's effort is now its reviewer's own, so every call, its
        verification on the same engine included, must land where it did."""

        with tempfile.TemporaryDirectory() as directory:
            for code_effort in ("xhigh", None):
                base = settings(directory, review_code_effort=code_effort)
                plan = review_plan(base)
                for review_type in plan.types:
                    for verifying in (False, True):
                        with self.subTest(
                            code_effort=code_effort,
                            type=review_type.name,
                            verifying=verifying,
                        ):
                            call = material(review_type.name, verifying)
                            self.assertEqual(
                                effort_for(review_type.settings, call),
                                effort_for(base, call),
                            )

    def test_the_other_roles_are_derived_exactly_as_before(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory)
            for review_type in review_plan(base).types:
                for derive in (
                    verification_engine_settings,
                    verify_fallback_engine_settings,
                    fallback_engine_settings,
                ):
                    with self.subTest(type=review_type.name, role=derive.__name__):
                        self.assertEqual(derive(review_type.settings), derive(base))

    def test_a_plan_refuses_what_no_run_can_do(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory)
            for names, message in (
                ((CODE_LEVEL, DOCS_LEVEL), "must run last"),
                ((DOCS_LEVEL, DOCS_LEVEL, CODE_LEVEL), "listed twice"),
                (("security", CODE_LEVEL), "Unknown review type 'security'"),
            ):
                with self.subTest(names=names):
                    with self.assertRaisesRegex(ConfigError, message):
                        ReviewPlan(tuple(ReviewType(name, base) for name in names))

    def test_a_plan_finds_its_types_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plan = review_plan(settings(directory, design_level=False))

        self.assertEqual(plan.get(DOCS_LEVEL).name, DOCS_LEVEL)
        self.assertIsNone(plan.get(DESIGN_LEVEL))

    def test_the_checks_type_reads_on_its_own_model_with_no_second_engine(self):
        """Its reading is checked by the controller, and failing costs only it."""

        with tempfile.TemporaryDirectory() as directory:
            base = settings(
                directory,
                checks_level=True,
                checks_model="claude-haiku-5.5",
                checks_effort="medium",
            )
            plan = review_plan(base)
            inherited = review_plan(settings(directory, checks_level=True))

        self.assertEqual(plan.names, (DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL, "checks"))
        checks = plan.get("checks").settings
        self.assertEqual(
            (checks.review_engine, checks.review_model, checks.review_effort),
            ("kiro", "claude-haiku-5.5", "medium"),
        )
        self.assertIsNone(verification_engine_settings(checks))
        self.assertIsNone(fallback_engine_settings(checks))
        own = inherited.get("checks").settings
        self.assertEqual(
            (own.review_model, own.review_effort), ("claude-sonnet-5.5", "high")
        )

    def test_the_checks_type_is_not_ordered_with_the_others(self) -> None:
        """It runs when a waiting review is due, so the code review stays last."""

        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory)
            for names in (
                ("checks", DOCS_LEVEL, CODE_LEVEL),
                (DOCS_LEVEL, CODE_LEVEL, "checks"),
            ):
                with self.subTest(names=names):
                    ReviewPlan(tuple(ReviewType(name, base) for name in names))


class ReviewStagesTests(unittest.TestCase):
    def create(self, base: Settings) -> "dict[str, cli.ReviewStages]":
        made = []

        def engine_for(engine_settings):
            engine = Mock(name=f"engine {len(made)}")
            made.append(engine_settings)
            return engine

        with patch("prwarden.cli.create_review_engine", side_effect=engine_for):
            return cli._create_review_stages(review_plan(base))

    def test_types_configured_alike_share_their_engines(self) -> None:
        """A verifier that handed over is down for every type of the pull request,
        so the types must share it, as they shared the one verifier before."""

        with tempfile.TemporaryDirectory() as directory:
            stages = self.create(settings(directory))

        self.assertEqual(list(stages), [DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL])
        design, docs, code = stages.values()
        self.assertIs(design.engine, docs.engine)
        # The code review runs at its own effort, so on an engine of its own.
        self.assertIsNot(code.engine, design.engine)
        self.assertEqual(code.settings.review_effort, "xhigh")
        self.assertEqual(design.settings.review_effort, "high")
        self.assertIsNotNone(design.verification)
        self.assertIsNotNone(design.verification.fallback)
        self.assertIs(design.verification, docs.verification)
        self.assertIs(design.verification, code.verification)
        self.assertIsNotNone(design.fallback)
        self.assertIs(design.fallback, code.fallback)

    def test_one_effort_for_every_type_is_one_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stages = self.create(settings(directory, review_code_effort=None))

        engines = {id(stage.engine) for stage in stages.values()}
        self.assertEqual(len(engines), 1)

    def test_each_pull_request_measures_a_shared_engine_once(self) -> None:
        """Wrapped per engine, not per type, so a hand-over one type makes holds for
        the pull request's other types too."""

        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory)
            stages = self.create(base)
            received = []
            with patch(
                "prwarden.cli._review_with_head_restarts",
                side_effect=lambda *args, **kwargs: received.append(kwargs["stages"]),
            ):
                cli._process_pull_request_with_head_restarts(
                    base,
                    base.repositories[0],
                    PullRequest(1, "Title", "", "author", "head", "url"),
                    Mock(),
                    live=False,
                    force=False,
                    stages=stages,
                )

        (measured,) = received
        design, docs, code = (measured[name] for name in stages)
        self.assertIs(design.engine, docs.engine)
        self.assertIsNot(design.engine, code.engine)
        self.assertEqual(code.engine._role, "review")
        self.assertIs(design.verification, code.verification)
        self.assertIsInstance(code.verification.engine, cli._FallbackVerifier)
        self.assertIs(design.fallback, code.fallback)
        self.assertEqual(code.fallback.engine._role, "fallback")

    def test_a_type_falls_back_through_the_chains_it_names(self) -> None:
        """A type may name more than one fallback and more than one verifier, and
        every engine of the chain is made and measured once."""

        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory)
            second = replace(base, review_engine="codex", review_model="gpt-6.1")
            third = replace(base, review_engine="claude", review_model="sonnet")
            code = ReviewType(
                CODE_LEVEL, base, fallbacks=(second, third), verifiers=(third, second)
            )
            with patch(
                "prwarden.cli.create_review_engine",
                side_effect=lambda made: Mock(name=made.review_engine),
            ):
                stages = cli._create_review_stages(ReviewPlan((code,)))
            measured = cli._measured_stages(
                stages, lambda engine, role: Mock(name=role, wrapped=engine)
            )

        stage = stages[CODE_LEVEL]
        self.assertEqual(
            [reviewer.settings for reviewer in stage.fallback.chain()], [second, third]
        )
        self.assertEqual(stage.verification.settings, third)
        self.assertEqual(stage.verification.fallback.settings, second)
        self.assertIsNone(stage.verification.fallback.fallback)
        wrapped = measured[CODE_LEVEL]
        self.assertEqual(
            [reviewer.engine._mock_name for reviewer in wrapped.fallback.chain()],
            ["fallback", "fallback"],
        )
        self.assertIsInstance(wrapped.verification.engine, cli._FallbackVerifier)
        self.assertEqual(
            wrapped.verification.engine.fallback.engine._mock_name, "verify fallback"
        )


class ReviewTypeRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        without_review_rules(self)
        for target, value in (
            ("prwarden.cli.list_pull_request_checks", ()),
            ("prwarden.cli._read_linked_issues", ()),
            ("prwarden.cli._read_file_history", ()),
            ("prwarden.cli.pin_github_account", None),
        ):
            stub = patch(target, return_value=value)
            stub.start()
            self.addCleanup(stub.stop)

    def test_each_type_reviews_on_its_own_engines_in_the_plans_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory)
            repository = base.repositories[0]
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            order = []
            stages = {}
            for name in (DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL):
                engine = Mock(name=f"{name} engine")
                stages[name] = cli.ReviewStages(
                    settings=replace(base, review_model=f"{name}-model"),
                    engine=engine,
                    verification=Mock(name=f"{name} verification"),
                    fallback=Mock(name=f"{name} fallback"),
                )
            stages[CODE_LEVEL].engine.review.side_effect = lambda *args, **kwargs: (
                order.append(CODE_LEVEL)
                or {
                    "verdict": "approve",
                    "approval_reason": "No qualifying defects remain.",
                    "findings": [],
                }
            )

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli._run_design_level",
                side_effect=lambda *args, **kwargs: order.append(DESIGN_LEVEL),
            ) as design, patch(
                "prwarden.cli._run_docs_level",
                side_effect=lambda *args, **kwargs: order.append(DOCS_LEVEL),
            ) as docs, patch(
                "prwarden.cli.submit_approval", return_value="approval-9"
            ) as submit_approval, patch("sys.stdout", new_callable=StringIO):
                (Path(directory) / "app.py").write_text("value = 1\n")
                workspace.return_value.__enter__.return_value = repository
                with JsonStateStore(base.state_file) as store:
                    cli._process_pull_request(
                        base,
                        repository,
                        pull_request,
                        store,
                        live=True,
                        force=False,
                        stages=stages,
                    )

        self.assertEqual(order, [DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL])
        for name, level in ((DESIGN_LEVEL, design), (DOCS_LEVEL, docs)):
            with self.subTest(type=name):
                stage = stages[name]
                self.assertIs(level.call_args.args[0], stage.settings)
                self.assertIs(level.call_args.args[5], stage.engine)
                self.assertIs(level.call_args.args[6], stage.verification)
                self.assertIs(level.call_args.kwargs["fallback"], stage.fallback)
                stage.engine.review.assert_not_called()
        submit_approval.assert_called_once()

    def test_a_run_gives_each_type_the_engines_its_plan_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = settings(directory)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            received = []
            args = Namespace(
                env_file=Path(directory) / ".env",
                repositories_file=None,
                state_file=None,
                repo=[],
                pr=None,
                live=False,
                force=False,
                engine=None,
                concurrency=1,
            )
            with patch("prwarden.cli.load_settings", return_value=base), patch(
                "prwarden.cli.create_review_engine",
                side_effect=lambda engine_settings: Mock(),
            ), patch(
                "prwarden.cli.list_open_prs", return_value=[pull_request]
            ), patch(
                "prwarden.cli._process_pull_request_with_head_restarts",
                side_effect=lambda *args, **kwargs: received.append(kwargs["stages"]),
            ), patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(cli.run(args), 0)

        (stages,) = received
        self.assertEqual(list(stages), [DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL])
        self.assertEqual(stages[CODE_LEVEL].settings.review_effort, "xhigh")
        self.assertIsNone(stages[CODE_LEVEL].settings.review_code_effort)
        self.assertEqual(stages[DESIGN_LEVEL].settings.review_effort, "high")

    def test_the_run_says_who_reads_failed_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = settings(
                directory,
                checks_level=True,
                checks_model="claude-haiku-5.5",
                checks_effort="medium",
            )
            args = Namespace(
                env_file=Path(directory) / ".env",
                repositories_file=None,
                state_file=None,
                repo=[],
                pr=None,
                live=False,
                force=False,
                engine=None,
                concurrency=1,
            )
            with patch("prwarden.cli.load_settings", return_value=base), patch(
                "prwarden.cli.create_review_engine",
                side_effect=lambda engine_settings: Mock(),
            ), patch("prwarden.cli.list_open_prs", return_value=[]), patch(
                "sys.stdout", new_callable=StringIO
            ) as output:
                self.assertEqual(cli.run(args), 0)

        self.assertIn(
            "Failed checks of a review that waits for them are read by Claude Haiku "
            "5.5 @ Kiro at medium effort.",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
