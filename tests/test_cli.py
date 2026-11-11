import os
import re
import stat
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from io import StringIO
from argparse import Namespace
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch

import prwarden.cli as cli
import prwarden.progress as progress_module
from prwarden.commands import CommandError
from prwarden.config import ConfigError, Settings
from prwarden.engine import (
    EngineError,
    fallback_engine_settings,
    verification_engine_settings,
)
from prwarden.models import (
    PullRequestCheck,
    DocumentationFile,
    DependencyLink,
    Finding,
    PriorReviewComment,
    PullRequest,
    PullRequestSnapshot,
    Repository,
    ReviewMaterial,
)
from prwarden.review import CandidateRejection, ReviewError
from prwarden.state import JsonStateStore, StateError, StateLockedError
from tests.test_review_rules import without_review_rules


READY_SNAPSHOT = PullRequestSnapshot(
    head_sha="head",
    state="open",
    is_draft=False,
    labels=frozenset(),
    changed_files=1,
    mergeable=True,
)


def _plain(text: str) -> str:
    """Strip the escape sequences a shimmering row writes inside its text."""
    return re.sub(r"\033\[[0-9;]*[A-Za-z]", "", text)


def finding_response(path: str, line: int) -> dict:
    """Return one reviewable finding anchored where the caller wants it."""

    return {
        "verdict": "do_not_approve",
        "approval_reason": "A qualifying defect remains.",
        "findings": [
            {
                "severity": "medium",
                "title": "Incorrect value",
                "path": path,
                "line": line,
                "problem": "This value causes a definite failure.",
                "suggestion": "Use the validated value instead.",
                "trigger": "Process the changed value.",
                "impact": "The operation fails.",
                "evidence": [
                    {
                        "path": path,
                        "line": line,
                        "excerpt": "value = 1",
                        "relevance": "This line triggers the defect.",
                    }
                ],
                "counterevidence": [],
                "counterevidence_reason": "No direct counterevidence found.",
            }
        ],
    }


CONFIRMED_JUDGMENT = {
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


class OrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        without_review_rules(self)
        checks = patch("prwarden.cli.list_pull_request_checks", return_value=())
        checks.start()
        self.addCleanup(checks.stop)
        # The base branch's history is read from GitHub and Git, which no test has.
        history = patch("prwarden.cli._read_file_history", return_value=())
        history.start()
        self.addCleanup(history.stop)
        # Pinning asks the real GitHub CLI for a stored login, which no test has.
        pinning = patch("prwarden.cli.pin_github_account")
        pinning.start()
        self.addCleanup(pinning.stop)

    def test_a_recorded_review_leaves_the_live_row_to_the_board(self) -> None:
        """Two renderers on one row overwrite each other.

        A review running in parallel has its report captured and printed later,
        while the board owns the live row. Judging interactivity on the captured
        stream is what keeps the review's own in-place line silent.
        """

        class FakeTerminal(StringIO):
            def isatty(self) -> bool:
                return True

        terminal = FakeTerminal()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.stdout", terminal), patch("sys.stderr", StringIO()):
                with progress_module.transcript(
                    Path(directory) / "run-logs" / "run-1.log"
                ):
                    with progress_module.routed_output():
                        # Outside a recording the phase owns the row and animates.
                        with cli._ElapsedProgressLine("Review", 0.0) as sequential:
                            self.assertTrue(sequential.stream.isatty())
                            self.assertIsNotNone(sequential._thread)

                        with progress_module.recording():
                            with cli._ElapsedProgressLine("Review", 0.0) as parallel:
                                self.assertFalse(parallel.stream.isatty())
                                self.assertIsNone(parallel._thread)

                        board = cli._ReviewBoard([], stream=None)
                        self.assertTrue(board.stream.isatty())

        self.assertNotIn("Review in progress", _plain(terminal.getvalue()))

    def test_progress_frames_reach_the_terminal_behind_a_transcript(self) -> None:
        """Writes must pass the transcript, even though isatty does not."""

        class FakeTerminal(StringIO):
            def isatty(self) -> bool:
                return True

        terminal = FakeTerminal()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-logs" / "run-1.log"
            with patch("sys.stdout", terminal), patch("sys.stderr", StringIO()):
                with progress_module.transcript(path):
                    line = cli._ElapsedProgressLine("Review", 0.0)
                    self.assertTrue(line.stream.isatty())
                    line._display_frame(0)

            self.assertIn("Review in progress", _plain(terminal.getvalue()))
            self.assertNotIn(
                "Review in progress", _plain(path.read_text(encoding="utf-8"))
            )

    def test_rejected_candidates_are_reported_for_later_analysis(self) -> None:
        """A count cannot tell a strict verifier from a weak review."""

        def candidate(line: int, title: str) -> Finding:
            return Finding(
                severity="medium",
                title=title,
                path="app.py",
                line=line,
                problem=f"Claim about line {line}.",
                suggestion="Change it.",
                trigger=f"Trigger for line {line}.",
                impact="It fails.",
            )

        kept = candidate(1, "Real defect")
        thrown_out = candidate(2, "False positive")
        stream = StringIO()
        with patch("sys.stdout", stream):
            cli._print_rejected_candidates(
                (kept, thrown_out),
                [kept],
                {
                    "F2": CandidateRejection(
                        "The installed version has no such default.", "not_a_defect"
                    )
                },
            )
        written = stream.getvalue()

        self.assertIn(
            "Rejected candidate [MEDIUM] app.py:2 False positive", written
        )
        self.assertIn("Claimed: Claim about line 2.", written)
        self.assertIn("Trigger: Trigger for line 2.", written)
        self.assertIn(
            "Rejected [not_a_defect] because: The installed version has no such "
            "default.",
            written,
        )
        self.assertNotIn("Real defect", written)

    def test_two_candidates_on_one_line_are_both_reported(self) -> None:
        """Comparing positions reported the rejected one as confirmed, so it
        vanished from the transcript with the verifier's reason for it."""

        shared = [
            Finding(
                severity="medium",
                title=title,
                path="app.py",
                line=4,
                problem="Claim.",
                suggestion="Fix.",
                trigger="Trigger.",
                impact="It fails.",
            )
            for title in ("Held up", "Thrown out")
        ]
        stream = StringIO()
        with patch("sys.stdout", stream):
            cli._print_rejected_candidates(
                tuple(shared),
                [shared[0]],
                {"F2": CandidateRejection("Not a defect.", "not_a_defect")},
            )
        written = stream.getvalue()

        self.assertIn("Rejected candidate [MEDIUM] app.py:4 Thrown out", written)
        self.assertNotIn("Held up", written)
        self.assertIn("Rejected [not_a_defect] because: Not a defect.", written)

    def test_no_fallback_refuses_to_also_name_one(self) -> None:
        """Asking for no fallback and naming one is a contradiction, and guessing
        which was meant would silently review with an engine nobody chose."""

        parser = cli.build_parser()
        arguments = parser.parse_args(
            ["--no-fallback", "--fallback-engine", "kiro"]
        )

        with self.assertRaisesRegex(ConfigError, "--no-fallback cannot be combined"):
            cli.run(arguments)

    def test_a_spent_quota_hands_straight_to_the_fallback_next_time(self) -> None:
        """Learning a quota is gone costs a 2m30s refusal; paying it per pull request
        delayed every fallback by 8m34s on 2026-09-22."""

        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)
        settings = Mock(
            review_engine="antigravity",
            review_model="gemini-3.1-pro-high",
            review_timeout_seconds=900,
        )
        fallback = Mock()
        fallback.settings = Mock(review_engine="kiro", review_model="claude-opus-5")
        exhausted = EngineError(
            "RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
            "Resets in 17h59m45s."
        )
        stream = StringIO()
        with patch("sys.stdout", stream), patch(
            "prwarden.cli._review_with_validation_retry"
        ) as review:
            review.side_effect = [exhausted, ({}, [], True, "", ())]
            cli._review_with_engine_fallback(
                settings, Mock(), fallback, Mock(), Mock(), Mock()
            )
            first_calls = review.call_count

            review.side_effect = [({}, [], True, "", ())]
            cli._review_with_engine_fallback(
                settings, Mock(), fallback, Mock(), Mock(), Mock()
            )

        written = stream.getvalue()
        self.assertEqual(first_calls, 2)
        self.assertIn("is out of quota for this run", written)
        self.assertIn("reviewing with kiro (claude-opus-5) directly", written)
        # The second pull request never attempted the primary at all: its one call
        # was the fallback's.
        self.assertEqual(review.call_count - first_calls, 1)

    def test_a_transient_refusal_does_not_trip_the_run(self) -> None:
        """A momentary 429 is what retrying exists for, so it must not latch."""

        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)
        settings = Mock(
            review_engine="antigravity",
            review_model="gemini-3.1-pro-high",
            review_timeout_seconds=900,
        )
        fallback = Mock()
        fallback.settings = Mock(review_engine="kiro", review_model="claude-opus-5")
        with patch("sys.stdout", new_callable=StringIO), patch(
            "prwarden.cli._review_with_validation_retry"
        ) as review:
            review.side_effect = [
                EngineError("UNAVAILABLE: service temporarily unavailable"),
                ({}, [], True, "", ()),
            ]
            cli._review_with_engine_fallback(
                settings, Mock(), fallback, Mock(), Mock(), Mock()
            )

        self.assertEqual(cli._PRIMARY_ENGINE.exhausted_reason, "")

    def test_an_advisory_level_records_the_quota_it_met_first(self) -> None:
        """The advisory levels run before the code review and share its engine, so on
        2026-09-23 all three paid a refusal: 08:02:57, 08:05:22, 08:07:58."""

        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)

        cli._note_exhausted_quota(
            EngineError(
                "RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
                "Resets in 17h59m45s."
            )
        )

        self.assertIn("Individual quota reached", cli._PRIMARY_ENGINE.exhausted_reason)

    def test_a_failing_advisory_level_records_for_the_levels_after_it(self) -> None:
        """Design runs first, so what it learns must reach docs and the code review."""

        settings = Mock(
            review_engine="antigravity",
            review_model="gemini-3.1-pro-high",
            max_design_findings=4,
            max_docs_findings=3,
            review_timeout_seconds=900,
        )
        exhausted = EngineError(
            "RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
            "Resets in 17h59m45s."
        )

        for level in (cli._design_level, cli._docs_level):
            cli._PRIMARY_ENGINE.reset()
            self.addCleanup(cli._PRIMARY_ENGINE.reset)
            material = ReviewMaterial("diff", {"app.py": frozenset({1})})
            engine = Mock()
            engine.review.side_effect = exhausted
            with patch("sys.stdout", new_callable=StringIO), patch(
                "prwarden.cli.discover_documentation",
                return_value=(DocumentationFile("README.md", 1200, "human"),),
            ):
                with self.assertRaises(EngineError):
                    level(settings, engine, Mock(), Mock(), Mock(), material)

            with self.subTest(level=level.__name__):
                self.assertIn(
                    "Individual quota reached", cli._PRIMARY_ENGINE.exhausted_reason
                )

    def test_a_transient_advisory_failure_records_nothing(self) -> None:
        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)

        cli._note_exhausted_quota(EngineError("UNAVAILABLE: try again"))

        self.assertEqual(cli._PRIMARY_ENGINE.exhausted_reason, "")

    def test_an_advisory_level_does_not_pay_to_learn_the_quota_is_gone(self) -> None:
        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)
        cli._PRIMARY_ENGINE.record_exhausted("Individual quota reached")
        settings = Mock(
            review_engine="antigravity",
            review_model="gemini-3.1-pro-high",
            max_design_findings=4,
            max_docs_findings=3,
            review_timeout_seconds=900,
        )
        with patch("prwarden.cli._review_with_validation_retry") as review:
            for level in (cli._design_level, cli._docs_level):
                with self.assertRaisesRegex(EngineError, "out of quota for this run"):
                    level(settings, Mock(), Mock(), Mock(), Mock(), Mock())

        self.assertFalse(review.called)

    def test_every_rejection_line_carries_one_countable_class(self) -> None:
        """A week of transcripts must tally with a single grep."""

        rejected = tuple(
            Finding(
                severity="medium",
                title=f"Candidate {index}",
                path="app.py",
                line=index,
                problem="Claim.",
                suggestion="Fix.",
                trigger="Trigger.",
                impact="It fails.",
            )
            for index in range(1, 4)
        )
        stream = StringIO()
        with patch("sys.stdout", stream):
            cli._print_rejected_candidates(
                rejected,
                [],
                {
                    "F1": CandidateRejection("Real, wrong line.", "wrong_anchor"),
                    "F2": CandidateRejection("Not a defect.", "not_a_defect"),
                },
            )

        tally = re.findall(r"Rejected \[([a-z_]+)\] because:", stream.getvalue())
        self.assertEqual(
            sorted(tally), ["not_a_defect", "unclassified", "wrong_anchor"]
        )

    def test_a_rejection_without_a_reason_says_so(self) -> None:
        """Silence would read as though the candidate was never judged."""

        candidate = Finding(
            severity="low",
            title="Unexplained",
            path="app.py",
            line=3,
            problem="Claim.",
            suggestion="Change.",
        )
        stream = StringIO()
        with patch("sys.stdout", stream):
            cli._print_rejected_candidates((candidate,), [], {})

        self.assertIn(
            "Rejected [unclassified] because: the verifier gave no reason for this "
            "candidate",
            stream.getvalue(),
        )

    def test_nothing_is_reported_when_every_candidate_survived(self) -> None:
        finding = Finding(
            severity="low",
            title="Confirmed",
            path="app.py",
            line=1,
            problem="Problem.",
            suggestion="Suggestion.",
        )
        stream = StringIO()
        with patch("sys.stdout", stream):
            cli._print_rejected_candidates((finding,), [finding], {})

        self.assertEqual(stream.getvalue(), "")

    def test_formats_review_duration(self) -> None:
        self.assertEqual(cli._format_duration(5.4), "5s")
        self.assertEqual(cli._format_duration(86.2), "1m26s")
        self.assertEqual(cli._format_duration(300), "5m")
        self.assertEqual(cli._format_duration(3661), "1h01m01s")

    def test_progress_line_dims_the_text_and_emboldens_a_window(self) -> None:
        stream = StringIO()
        progress = cli._ElapsedProgressLine("Review", 10.0, stream, highlight_width=4)

        with patch("prwarden.cli.time.monotonic", return_value=96.2):
            progress._display_frame(5)
        progress._clear()

        self.assertEqual(
            stream.getvalue(),
            "\r\033[2K      "
            "\033[22;2mRe\033[22;1mview\033[22;2m in progress \u2014 1m26s elapsed"
            "\033[0m\r\033[2K",
        )

    def test_progress_line_never_names_a_palette_colour(self) -> None:
        """A theme may draw any palette colour as its own background.

        Solarized Dark draws bright black that way, and the dim part of the line
        vanished. Only the terminal's own text colour is safe in every theme.
        """

        progress = cli._ElapsedProgressLine("Review", 10.0, StringIO())
        with patch("prwarden.cli.time.monotonic", return_value=96.2):
            for step in range(0, 60):
                progress.stream = StringIO()
                progress._display_frame(step)
                rendered = progress.stream.getvalue()
                for codes in re.findall(r"\033\[([0-9;]*)m", rendered):
                    for code in filter(None, codes.split(";")):
                        self.assertIn(int(code), {0, 1, 2, 22}, codes)

    def test_progress_line_window_moves_left_to_right(self) -> None:
        progress = cli._ElapsedProgressLine("Review", 10.0, StringIO(), highlight_width=6)
        line = "Review in progress \u2014 1m26s elapsed"
        starts = []

        with patch("prwarden.cli.time.monotonic", return_value=96.2):
            for step in range(5, 12):
                progress.stream = StringIO()
                progress._display_frame(step)
                rendered = progress.stream.getvalue()
                bright = rendered.split(cli.PROGRESS_HIGHLIGHT_STYLE)[1].split(
                    cli.PROGRESS_DIM_STYLE
                )[0]
                starts.append(line.index(bright))

        self.assertEqual(starts, sorted(starts))
        self.assertLess(starts[0], starts[-1])

    def test_progress_line_always_shows_a_highlighted_window(self) -> None:
        progress = cli._ElapsedProgressLine("Review", 10.0, StringIO())
        widths = set()

        with patch("prwarden.cli.time.monotonic", return_value=96.2):
            for step in range(0, 120):
                progress.stream = StringIO()
                progress._display_frame(step)
                rendered = progress.stream.getvalue()
                self.assertIn(cli.PROGRESS_HIGHLIGHT_STYLE, rendered)
                bright = rendered.split(cli.PROGRESS_HIGHLIGHT_STYLE)[1].split(
                    cli.PROGRESS_DIM_STYLE
                )[0]
                widths.add(len(bright))

        self.assertNotIn(0, widths)
        self.assertEqual(max(widths), cli.PROGRESS_HIGHLIGHT_WIDTH)

    def test_progress_line_renders_no_loader_glyph(self) -> None:
        stream = StringIO()
        progress = cli._ElapsedProgressLine("Review", 10.0, stream)

        with patch("prwarden.cli.time.monotonic", return_value=96.2):
            progress._display_frame(6)

        visible = re.sub(r"\033\[[0-9;]*[A-Za-z]", "", stream.getvalue()).strip("\r")
        self.assertEqual(visible, "      Review in progress \u2014 1m26s elapsed")

    def test_progress_line_stays_disabled_for_redirected_output(self) -> None:
        progress = cli._ElapsedProgressLine("Review", 10.0, StringIO())

        with patch("prwarden.cli.threading.Thread") as thread:
            with progress:
                pass

        thread.assert_not_called()

    def test_progress_line_waits_five_seconds_before_the_first_frame(self) -> None:
        stream = StringIO()
        progress = cli._ElapsedProgressLine("Review", 10.0, stream)
        progress._stop = Mock()
        progress._stop.wait.side_effect = [False, True]
        progress._stop.is_set.return_value = False

        with patch("prwarden.cli.time.monotonic", return_value=15.0):
            progress._animate()

        self.assertEqual(progress._stop.wait.call_args_list[0].args, (5.0,))
        self.assertEqual(
            progress._stop.wait.call_args_list[1].args,
            (cli.PROGRESS_INTERVAL_SECONDS,),
        )
        self.assertIn("5s elapsed", stream.getvalue())

    def test_progress_line_is_erased_before_an_interrupting_message(self) -> None:
        """A retry notice must not be appended to the elapsed line."""

        out = StringIO()
        err = StringIO()
        line = cli._ElapsedProgressLine("Review", 10.0, out)
        progress_module.set_active(line)
        self.addCleanup(progress_module.release_active, line)

        with patch("prwarden.cli.time.monotonic", return_value=96.2):
            line._display_frame(6)
        progress_module.notify("Transient failure; retrying in 15s", stream=err)

        self.assertTrue(out.getvalue().endswith("\r\033[2K"))
        self.assertEqual(err.getvalue(), "Transient failure; retrying in 15s\n")
        visible = re.sub(r"\033\[[0-9;]*[A-Za-z]", "", out.getvalue())
        self.assertNotIn("elapsedTransient", visible)

    def test_progress_line_redraws_after_an_interruption(self) -> None:
        line = cli._ElapsedProgressLine("Review", 10.0, StringIO())
        progress_module.set_active(line)
        self.addCleanup(progress_module.release_active, line)

        with patch("prwarden.cli.time.monotonic", return_value=96.2):
            line._display_frame(6)
            progress_module.notify("interrupted", stream=StringIO())
            self.assertFalse(line._displayed)
            line.stream = StringIO()
            line._display_frame(7)

        self.assertIn("1m26s elapsed", line.stream.getvalue())
        self.assertTrue(line._displayed)

    def test_finished_progress_line_no_longer_receives_interruptions(self) -> None:
        out = StringIO()
        line = cli._ElapsedProgressLine("Review", 10.0, out)

        with line:
            pass
        before = out.getvalue()
        progress_module.notify("later message", stream=StringIO())

        self.assertEqual(out.getvalue(), before)

    def test_notify_reports_plainly_without_a_progress_line(self) -> None:
        err = StringIO()

        progress_module.notify("standalone", stream=err)

        self.assertEqual(err.getvalue(), "standalone\n")

    def test_progress_line_rejects_a_non_positive_highlight_width(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            cli._ElapsedProgressLine("Review", 10.0, StringIO(), highlight_width=0)

    def settings(
        self,
        directory: str,
        repository: Repository,
        design_level: bool = False,
    ) -> Settings:
        """Return settings for one review.

        The design level is off unless a test asks for it: these tests are about
        code-level behaviour, and an advisory level running first would answer the
        engine's first invocation and hide what they assert.
        """

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

    def test_github_incident_notice_reports_once_for_github_failures(self) -> None:
        notice = cli._GithubIncidentNotice()
        failure = CommandError("gh exited with 1: HTTP 503", command="gh")

        with patch(
            "prwarden.cli.describe_github_incident",
            return_value="GitHub status: Partial System Outage",
        ) as describe, patch("sys.stderr", new_callable=StringIO) as errors:
            notice.report(failure)
            notice.report(failure)

        describe.assert_called_once_with()
        self.assertEqual(notice.note, "GitHub status: Partial System Outage")
        self.assertEqual(
            errors.getvalue(),
            "      -> GitHub status: Partial System Outage\n",
        )

    def test_github_incident_notice_ignores_non_github_failures(self) -> None:
        notice = cli._GithubIncidentNotice()

        with patch("prwarden.cli.describe_github_incident") as describe:
            notice.report(CommandError("git exited with 1: no such ref", command="git"))
            notice.report(CommandError("Invalid GitHub repository slug: bad"))
            notice.report(ReviewError("Pull request contains no reviewable files"))

        describe.assert_not_called()
        self.assertIsNone(notice.note)

    def test_github_incident_notice_stays_quiet_when_github_is_healthy(self) -> None:
        notice = cli._GithubIncidentNotice()

        with patch(
            "prwarden.cli.describe_github_incident", return_value=None
        ) as describe, patch("sys.stderr", new_callable=StringIO) as errors:
            notice.report(CommandError("gh exited with 1: HTTP 503", command="gh"))

        describe.assert_called_once_with()
        self.assertIsNone(notice.note)
        self.assertEqual(errors.getvalue(), "")

    def _verifying_settings(self, directory: str, repository: Repository) -> Settings:
        return replace(
            self.settings(directory, repository),
            review_model="gemini-3.1-pro-high",
            verify_engine="kiro",
            verify_engine_command=("kiro-cli",),
            verify_model="gpt-5.6-terra",
        )

    def _live_args(self, directory: str) -> Namespace:
        return Namespace(
            env_file=Path(directory) / ".env",
            repositories_file=None,
            state_file=None,
            repo=[],
            pr=None,
            live=True,
            force=False,
            engine=None,
        )

    def _run_with_one_failing_pr(self, directory: str, process) -> str:
        """Run one PR that fails and return the Slack notification body."""

        repository = Repository("app", "owner/app", Path(directory))
        settings = self._verifying_settings(directory, repository)
        pull_request = PullRequest(
            7, "feat: add a feature", "", "author", "head", "https://example.invalid/7"
        )

        with patch("prwarden.cli.load_settings", return_value=settings), patch(
            "prwarden.cli.create_review_engine"
        ), patch("prwarden.cli.create_verification_engine"), patch(
            "prwarden.cli.list_open_prs", return_value=[pull_request]
        ), patch(
            "prwarden.cli._process_pull_request_with_head_restarts", side_effect=process
        ), patch(
            "prwarden.cli.send_slack_message", return_value="http-200"
        ) as send, patch(
            "sys.stdout", new_callable=StringIO
        ), patch(
            "sys.stderr", new_callable=StringIO
        ):
            self.assertEqual(cli.run(self._live_args(directory)), 1)

        send.assert_called_once()
        return send.call_args.args[1]

    def test_failure_notification_omits_a_verifier_that_never_ran(self) -> None:
        def fail_before_verification(*args, **kwargs):
            raise ReviewError(
                "FAILED_PRECONDITION (code 400): "
                "User location is not supported for the API use."
            )

        with tempfile.TemporaryDirectory() as directory:
            message = self._run_with_one_failing_pr(directory, fail_before_verification)

        self.assertIn("Reviewed by Gemini 3.1 Pro (High) @ Antigravity", message)
        self.assertNotIn("verified by", message)

    def test_failure_notification_names_a_verifier_that_ran(self) -> None:
        def fail_after_verification(*args, **kwargs):
            kwargs["verification_activity"].record()
            raise ReviewError("Delivery failed after verification")

        with tempfile.TemporaryDirectory() as directory:
            message = self._run_with_one_failing_pr(directory, fail_after_verification)

        self.assertIn(
            "Reviewed by Gemini 3.1 Pro (High) @ Antigravity, "
            "verified by GPT-5.6 Terra @ Kiro",
            message,
        )

    def test_verification_activity_starts_unperformed(self) -> None:
        activity = cli._VerificationActivity()

        self.assertFalse(activity.performed)
        activity.record()
        self.assertTrue(activity.performed)

    def _run_and_capture_header(self, directory: str, settings: Settings) -> str:
        args = Namespace(
            env_file=Path(directory) / ".env",
            repositories_file=None,
            state_file=None,
            repo=[],
            pr=None,
            live=False,
            force=False,
            engine=None,
        )

        with patch("prwarden.cli.load_settings", return_value=settings), patch(
            "prwarden.cli.create_review_engine"
        ), patch("prwarden.cli.create_verification_engine"), patch(
            "prwarden.cli.create_fallback_engine"
        ), patch("prwarden.cli.list_open_prs", return_value=[]), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            self.assertEqual(cli.run(args), 0)
        return output.getvalue()

    def test_startup_states_the_planned_engines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            header = self._run_and_capture_header(
                directory, self._fallback_settings(directory, repository)
            )

        self.assertIn(
            "Review will be done by Gemini 3.1 Pro (High) @ Antigravity.\n"
            "Falls back to Claude Opus 5 @ Claude on engine failure.\n"
            "Verification by GPT-5.6 Terra @ Kiro.\n",
            header,
        )
        self.assertNotIn("Reviewed by", header)

    def test_startup_names_only_the_configured_engines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            header = self._run_and_capture_header(
                directory, self.settings(directory, repository)
            )

        self.assertIn(
            "Review will be done by provider default "
            "(model ID not configured) @ Antigravity.",
            header,
        )
        self.assertNotIn("Falls back to", header)
        self.assertNotIn("Verification by", header)

    def test_failure_notification_names_the_reviewing_engines(self) -> None:
        message = cli._format_failure_notification(
            ["owner/app#1: gh exited with 1: HTTP 503"],
            None,
            "Reviewed by Gemini 3.1 Pro (High) @ Antigravity, "
            "verified by GPT-5.6 Terra @ Kiro",
        )

        self.assertTrue(
            message.endswith(
                "\n\n_Reviewed by Gemini 3.1 Pro (High) @ Antigravity, "
                "verified by GPT-5.6 Terra @ Kiro_"
            )
        )

    def test_verifier_identity_reports_only_a_separate_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)

            self.assertEqual(cli._verifier_identity(settings), (None, None))
            self.assertEqual(
                cli._verifier_identity(
                    replace(
                        settings,
                        verify_engine="kiro",
                        verify_engine_command=("kiro-cli",),
                        verify_model="gpt-5.6-terra",
                    )
                ),
                ("kiro", "gpt-5.6-terra"),
            )

    def test_failure_notification_includes_incident_note_before_errors(self) -> None:
        message = cli._format_failure_notification(
            ["owner/app#1: gh exited with 1: HTTP 503"],
            "GitHub status: Partial System Outage",
        )

        lines = message.splitlines()
        self.assertEqual(lines[2], "_GitHub status: Partial System Outage_")
        self.assertTrue(lines[3].startswith("• owner/app#1: "))

    def test_failure_notification_omits_incident_line_without_a_note(self) -> None:
        message = cli._format_failure_notification(["owner/app#1: boom"])

        self.assertEqual(
            message.splitlines(),
            [
                "*PR Warden live run completed with errors*",
                "1 review operation could not be completed:",
                "• owner/app#1: boom",
            ],
        )

    def test_failure_notification_shows_one_error_whole(self) -> None:
        """A citation mismatch is only readable when both texts are shown, and the
        old 500-character cut landed in the middle of exactly that comparison."""

        detail = (
            "owner/app#1: Finding counterevidence excerpt does not match "
            "tests/app.test.ts:1: cited " + "x" * 900 + " but that line is ''"
        )

        message = cli._format_failure_notification([detail])

        self.assertIn("x" * 900, message)
        self.assertNotIn("…", message)

    def test_failure_notification_drops_whole_errors_to_fit_slack(self) -> None:
        """Slack drops a message over its limit, so the notification must bound
        itself — by leaving errors out and saying so, never by mangling one."""

        errors = [f"owner/app#{index}: " + str(index) * 9000 for index in range(1, 6)]

        message = cli._format_failure_notification(errors)

        self.assertLessEqual(len(message), cli.MAX_FAILURE_NOTIFICATION_CHARACTERS)
        self.assertIn("more; inspect the local run log", message)
        # Every error that is shown is shown whole.
        for index, error in enumerate(errors, start=1):
            if f"owner/app#{index}: " in message:
                self.assertIn(str(index) * 9000, message)

    def test_failure_notification_keeps_the_reviewer_line_in_bounds(self) -> None:
        errors = [f"owner/app#{index}: " + str(index) * 9000 for index in range(1, 6)]

        message = cli._format_failure_notification(
            errors, reviewer="Reviewed by Gemini 3.1 Pro (High) @ Antigravity"
        )

        self.assertLessEqual(len(message), cli.MAX_FAILURE_NOTIFICATION_CHARACTERS)
        self.assertTrue(
            message.endswith("_Reviewed by Gemini 3.1 Pro (High) @ Antigravity_")
        )

    def test_repository_header_opens_a_divided_section(self) -> None:
        header = cli._repository_header("Checking app (2 selected open PRs)...")

        self.assertEqual(
            header,
            f"\n{cli.REPOSITORY_DIVIDER}\nChecking app (2 selected open PRs)...",
        )
        self.assertEqual(set(cli.REPOSITORY_DIVIDER), {"-"})
        self.assertGreaterEqual(len(cli.REPOSITORY_DIVIDER), 20)

    def test_pull_request_header_opens_without_a_leading_blank_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(7, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            with JsonStateStore(settings.state_file) as store:
                key = store.key(repository.slug, 7, "head", "github")
                store.set_status(key, "delivered", receipt_id="42")
                with patch(
                    "prwarden.cli.delivered_review_findings_resolved",
                    return_value=False,
                ), patch("sys.stdout", new_callable=StringIO) as output:
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        Mock(),
                        live=True,
                        force=False,
                    )

        self.assertTrue(output.getvalue().startswith("    - PR #7 "))

    def test_unchanged_withheld_feedback_skips_with_recorded_reason(self) -> None:
        comment = PriorReviewComment(
            "inline:5",
            "inline",
            "other-reviewer",
            "A qualifying defect remains.",
            "app.py",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            with JsonStateStore(settings.state_file) as store:
                key = store.key(repository.slug, 1, "head", "github")
                store.set_status(
                    key,
                    "approval_withheld",
                    reason="The existing defect remains.",
                    blocking_comment_ids=["inline:5"],
                    feedback_fingerprint=cli._feedback_fingerprint((comment,)),
                )
                with patch(
                    "prwarden.cli.list_prior_review_comments", return_value=(comment,)
                ):
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )

            engine.review.assert_not_called()

    def test_changed_feedback_reconsiders_withheld_approval(self) -> None:
        old_comment = PriorReviewComment(
            "inline:5",
            "inline",
            "other-reviewer",
            "A qualifying defect remains.",
            "app.py",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            with JsonStateStore(settings.state_file) as store:
                key = store.key(repository.slug, 1, "head", "github")
                store.set_status(
                    key,
                    "approval_withheld",
                    reason="The existing defect remains.",
                    blocking_comment_ids=["inline:5"],
                    feedback_fingerprint=cli._feedback_fingerprint((old_comment,)),
                )
                with patch(
                    "prwarden.cli.list_prior_review_comments",
                    side_effect=[(), ()],
                ), patch("prwarden.cli.has_current_review", return_value=False), patch(
                    "prwarden.cli.list_changed_files",
                    return_value=[
                        {
                            "filename": "app.py",
                            "patch": "@@ -0,0 +1 @@\n+value = 1",
                        }
                    ],
                ), patch(
                    "prwarden.cli.get_pull_request_snapshot",
                    side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
                ), patch(
                    "prwarden.cli.review_workspace"
                ) as workspace, patch(
                    "prwarden.cli.submit_approval", return_value="approval-10"
                ):
                    workspace.return_value.__enter__.return_value = repository
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )
                self.assertEqual(store.get(key)["status"], "delivered")

            engine.review.assert_called_once()

    def test_legacy_withheld_record_is_reconsidered_without_feedback_change(self) -> None:
        comment = PriorReviewComment(
            "inline:5",
            "inline",
            "other-reviewer",
            "A qualifying defect remains.",
            "app.py",
            1,
            False,
        )
        self._assert_stale_withheld_record_is_reconsidered(comment, None)

    def test_resolved_recorded_blocker_is_reconsidered(self) -> None:
        comment = PriorReviewComment(
            "inline:5",
            "inline",
            "other-reviewer",
            "This defect was fixed.",
            "app.py",
            1,
            True,
        )
        self._assert_stale_withheld_record_is_reconsidered(
            comment,
            ["inline:5"],
        )

    def _assert_stale_withheld_record_is_reconsidered(
        self,
        comment: PriorReviewComment,
        blocking_comment_ids: object,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "blocking_comment_ids": [],
                "findings": [],
            }
            with JsonStateStore(settings.state_file) as store:
                key = store.key(repository.slug, 1, "head", "github")
                state_fields = {
                    "reason": "The existing defect remains.",
                    "feedback_fingerprint": cli._feedback_fingerprint((comment,)),
                }
                if blocking_comment_ids is not None:
                    state_fields["blocking_comment_ids"] = blocking_comment_ids
                store.set_status(key, "approval_withheld", **state_fields)
                with patch(
                    "prwarden.cli.list_prior_review_comments",
                    side_effect=[(comment,), (comment,)],
                ), patch("prwarden.cli.has_current_review", return_value=False), patch(
                    "prwarden.cli.list_changed_files",
                    return_value=[
                        {
                            "filename": "app.py",
                            "patch": "@@ -0,0 +1 @@\n+value = 1",
                        }
                    ],
                ), patch(
                    "prwarden.cli.get_pull_request_snapshot",
                    side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
                ), patch("prwarden.cli.review_workspace") as workspace, patch(
                    "prwarden.cli.submit_approval", return_value="approval-recovered"
                ):
                    workspace.return_value.__enter__.return_value = repository
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )

                self.assertEqual(store.get(key)["status"], "delivered")

            engine.review.assert_called_once()

    def test_resolved_blocker_enters_controller_correction_retry(self) -> None:
        resolved = PriorReviewComment(
            "inline:5",
            "inline",
            "other-reviewer",
            "This defect was fixed.",
            "app.py",
            1,
            True,
        )
        material = ReviewMaterial(
            "diff",
            {"app.py": frozenset({1})},
            prior_comments=(resolved,),
        )
        engine = Mock()
        engine.review.side_effect = [
            {
                "verdict": "do_not_approve",
                "approval_reason": "Feedback on app.py:5 remains unresolved.",
                "blocking_comment_ids": ["C1"],
                "findings": [],
            },
            {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "blocking_comment_ids": [],
                "findings": [],
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            result = cli._review_with_validation_retry(
                settings,
                engine,
                repository,
                PullRequest(1, "Title", "", "author", "head", "url"),
                material,
                material,
                phase="review",
            )

        self.assertTrue(result[2])
        self.assertEqual(engine.review.call_count, 2)
        retry_material = engine.review.call_args_list[1].args[2]
        self.assertIn("already resolved: C1", retry_material.validation_error)

    def test_withheld_approval_persists_validated_blocker_ids(self) -> None:
        unresolved = PriorReviewComment(
            "inline:5",
            "inline",
            "other-reviewer",
            "A qualifying defect remains.",
            "app.py",
            1,
            False,
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "do_not_approve",
                "approval_reason": "Feedback on app.py:5 remains unresolved.",
                "blocking_comment_ids": ["C1"],
                "findings": [],
            }
            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=(unresolved,)
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
            ), patch("prwarden.cli.review_workspace") as workspace:
                workspace.return_value.__enter__.return_value = repository
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
                    key = store.key(repository.slug, 1, "head", "github")
                    record = store.get(key)

            self.assertEqual(record["status"], "approval_withheld")
            self.assertEqual(record["blocking_comment_ids"], ["inline:5"])

    def test_resolved_delivered_findings_reconsider_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            with JsonStateStore(settings.state_file) as store:
                key = store.key(repository.slug, 1, "head", "github")
                store.set_status(key, "delivered", receipt_id="99")
                with patch(
                    "prwarden.cli.delivered_review_findings_resolved",
                    return_value=True,
                ), patch(
                    "prwarden.cli.has_current_review"
                ) as has_current_review, patch(
                    "prwarden.cli.list_prior_review_comments",
                    side_effect=[(), ()],
                ), patch(
                    "prwarden.cli.list_changed_files",
                    return_value=[
                        {
                            "filename": "app.py",
                            "patch": "@@ -0,0 +1 @@\n+value = 1",
                        }
                    ],
                ), patch(
                    "prwarden.cli.get_pull_request_snapshot",
                    side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
                ), patch(
                    "prwarden.cli.review_workspace"
                ) as workspace, patch(
                    "prwarden.cli.submit_approval", return_value="approval-11"
                ):
                    workspace.return_value.__enter__.return_value = repository
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )

                self.assertEqual(store.get(key)["status"], "delivered")
                self.assertEqual(store.get(key)["receipt_id"], "approval-11")
                has_current_review.assert_not_called()

            engine.review.assert_called_once()

    def test_a_failed_head_is_reviewed_again_on_the_next_run(self) -> None:
        """`failed` is the status a later run is meant to reconsider.

        Nothing is carried over: the record holds no findings, so the next run
        repeats the review at full cost rather than resending an earlier result.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            repository = Repository("app", "owner/app", root)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            with JsonStateStore(settings.state_file) as store:
                key = store.key(repository.slug, 1, "head", "github")
                store.set_status(key, "failed", error="verification was rejected")
                self.assertFalse(store.is_terminal(key))
                with patch(
                    "prwarden.cli.has_current_review", return_value=False
                ), patch(
                    "prwarden.cli.list_prior_review_comments", return_value=()
                ), patch(
                    "prwarden.cli.list_changed_files",
                    return_value=[
                        {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                    ],
                ), patch(
                    "prwarden.cli.get_pull_request_snapshot",
                    return_value=READY_SNAPSHOT,
                ), patch("prwarden.cli.review_workspace") as workspace, patch(
                    "prwarden.cli.submit_approval", return_value="approval-1"
                ) as submit_approval, patch("sys.stdout", new_callable=StringIO):
                    workspace.return_value.__enter__.return_value = repository
                    workspace.return_value.dependency_links = ()
                    workspace.return_value.dependency_notes = ()
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )

                engine.review.assert_called_once()
                submit_approval.assert_called_once()
                self.assertEqual(store.get(key)["status"], "delivered")

    def test_an_uncertain_delivery_is_not_retried_without_force(self) -> None:
        """A lost write may have landed, so no later run may repeat it by itself."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()

            with JsonStateStore(settings.state_file) as store:
                key = store.key(repository.slug, 1, "head", "github")
                store.set_status(key, "delivery_uncertain", error="response was lost")
                stream = StringIO()
                with patch("sys.stdout", stream):
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )

                # No model call, no write, and the record is left as it was.
                engine.review.assert_not_called()
                self.assertIn("Skipped: state is delivery_uncertain", stream.getvalue())
                self.assertEqual(store.get(key)["status"], "delivery_uncertain")

    def test_unresolved_delivered_findings_remain_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            with JsonStateStore(settings.state_file) as store:
                key = store.key(repository.slug, 1, "head", "github")
                store.set_status(key, "delivered", receipt_id="99")
                with patch(
                    "prwarden.cli.delivered_review_findings_resolved",
                    return_value=False,
                ):
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )

            engine.review.assert_not_called()

    def test_verification_pass_runs_on_the_configured_second_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            installed = root / "node_modules"
            installed.mkdir()
            repository = Repository("app", "owner/app", root)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = replace(
                self.settings(directory, repository),
                verify_engine="kiro",
                verify_engine_command=("kiro-cli",),
                verify_model="claude-opus-5",
            )
            candidate = {
                "verdict": "do_not_approve",
                "approval_reason": "A candidate defect was found.",
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
            verified = {
                "verdict": "approve",
                "approval_reason": "The candidate is not supported by the branch.",
                "judgments": [
                    {
                        "ref": "F1",
                        "verdict": "rejected",
                        "reason": "The branch guards this value above the change.",
                    }
                ],
            }
            engine = Mock()
            engine.review.return_value = candidate
            verification_engine = Mock()
            verification_engine.review.return_value = verified
            verification = cli.VerificationPass(
                engine=verification_engine,
                settings=verification_engine_settings(settings),
            )

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", side_effect=[(), ()]
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_approval", return_value="approval-13"
            ), patch(
                "sys.stdout", new_callable=StringIO
            ) as output:
                workspace.return_value.__enter__.return_value = repository
                workspace.return_value.dependency_links = (
                    DependencyLink("node_modules", str(installed), ("bun.lock",)),
                )
                with JsonStateStore(settings.state_file) as store:
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                        verification=verification,
                    )

            engine.review.assert_called_once()
            verification_engine.review.assert_called_once()
            verification_material = verification_engine.review.call_args.args[2]
            self.assertEqual(len(verification_material.verification_candidates), 1)
            # Verifying one candidate is budgeted as the base plus one allowance,
            # while the review pass keeps the configured per-invocation budget.
            self.assertEqual(
                verification_engine.review.call_args.kwargs["timeout_seconds"],
                settings.review_verify_timeout_seconds
                + settings.review_verify_timeout_per_candidate_seconds,
            )
            self.assertIsNone(engine.review.call_args.kwargs["timeout_seconds"])
            self.assertEqual(
                verification_material.dependency_links,
                (DependencyLink("node_modules", str(installed), ("bun.lock",)),),
            )
            terminal = output.getvalue()
            self.assertIn("Linked installed node_modules for bun.lock", terminal)
            self.assertIn(
                "verifying 1 candidate finding with kiro (claude-opus-5)",
                terminal,
            )

    def test_verification_budget_grows_with_the_candidate_count(self) -> None:
        """Passes carrying three and four candidates exhausted a flat ten minutes."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)

            self.assertEqual(
                cli._verification_timeout_seconds(settings, 1),
                settings.review_verify_timeout_seconds
                + settings.review_verify_timeout_per_candidate_seconds,
            )
            self.assertEqual(
                cli._verification_timeout_seconds(settings, 4),
                settings.review_verify_timeout_seconds
                + 4 * settings.review_verify_timeout_per_candidate_seconds,
            )
            # A pass with nothing to check never runs, so it never asks for more
            # than the base, and no count can subtract from it.
            self.assertEqual(
                cli._verification_timeout_seconds(settings, 0),
                settings.review_verify_timeout_seconds,
            )
            self.assertEqual(
                cli._verification_timeout_seconds(settings, -3),
                settings.review_verify_timeout_seconds,
            )

    def test_run_creates_no_verification_pass_without_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            primary = Mock()

            self.assertIsNone(cli._create_verification_pass(settings, primary))

            configured = replace(
                settings,
                verify_engine="kiro",
                verify_engine_command=("kiro-cli",),
                verify_model="claude-opus-5",
            )
            with patch("prwarden.cli.create_verification_engine") as create:
                verification = cli._create_verification_pass(configured, primary)

            self.assertIsNotNone(verification)
            self.assertIs(verification.engine, create.return_value)
            self.assertEqual(verification.settings.review_engine, "kiro")
            self.assertEqual(verification.settings.review_model, "claude-opus-5")

    def test_adversarial_verifier_can_reject_false_positive_and_approve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            repository = Repository("app", "owner/app", root)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            candidate = {
                "verdict": "do_not_approve",
                "approval_reason": "A candidate defect was found.",
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
            verified = {
                "verdict": "approve",
                "approval_reason": "The candidate is not supported by the branch.",
                "judgments": [
                    {
                        "ref": "F1",
                        "verdict": "rejected",
                        "reason": "The branch guards this value above the change.",
                    }
                ],
            }
            engine = Mock()
            engine.review.side_effect = [candidate, verified]

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", side_effect=[(), ()]
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_approval", return_value="approval-12"
            ) as submit_approval:
                workspace.return_value.__enter__.return_value = repository
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

            self.assertEqual(engine.review.call_count, 2)
            verification_material = engine.review.call_args_list[1].args[2]
            self.assertEqual(len(verification_material.verification_candidates), 1)
            submit_approval.assert_called_once()

    def test_invalid_evidence_gets_one_controller_feedback_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            repository = Repository("app", "owner/app", root)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            invalid = {
                "verdict": "do_not_approve",
                "approval_reason": "A candidate defect was found.",
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
                                "excerpt": "value = 2",
                                "relevance": "This line triggers the defect.",
                            }
                        ],
                        "counterevidence": [],
                        "counterevidence_reason": (
                            "No direct counterevidence was found."
                        ),
                    }
                ],
            }
            corrected = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            engine = Mock()
            engine.review.side_effect = [invalid, corrected]

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", side_effect=[(), ()]
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_approval", return_value="approval-13"
            ) as submit_approval:
                workspace.return_value.__enter__.return_value = repository
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

            self.assertEqual(engine.review.call_count, 2)
            retry_material = engine.review.call_args_list[1].args[2]
            self.assertIn("excerpt does not match", retry_material.validation_error)
            submit_approval.assert_called_once()

    def test_verifier_validation_gets_one_controller_feedback_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            repository = Repository("app", "owner/app", root)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            candidate_finding = {
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
                "counterevidence_reason": "No direct counterevidence was found.",
            }
            candidate = {
                "verdict": "do_not_approve",
                "approval_reason": "A candidate defect was found.",
                "findings": [candidate_finding],
            }
            invalid_verification = {
                "verdict": "do_not_approve",
                "approval_reason": "The candidate was confirmed.",
                "judgments": [
                    {
                        "ref": "F1",
                        "verdict": "confirmed",
                        "reason": "Read the caller.",
                    },
                    {
                        "ref": "F1",
                        "verdict": "rejected",
                        "reason": "Answered twice by mistake.",
                    },
                ],
            }
            corrected_verification = {
                "verdict": "approve",
                "approval_reason": "The candidate is not supported by the branch.",
                "judgments": [
                    {
                        "ref": "F1",
                        "verdict": "rejected",
                        "reason": "The branch guards this value above the change.",
                    }
                ],
            }
            engine = Mock()
            engine.review.side_effect = [
                candidate,
                invalid_verification,
                corrected_verification,
            ]

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", side_effect=[(), ()]
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_approval", return_value="approval-14"
            ) as submit_approval:
                workspace.return_value.__enter__.return_value = repository
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

            self.assertEqual(engine.review.call_count, 3)
            retry_material = engine.review.call_args_list[2].args[2]
            self.assertIn("judged twice", retry_material.validation_error)
            self.assertEqual(len(retry_material.verification_candidates), 1)
            submit_approval.assert_called_once()

    @patch("prwarden.cli.submit_approval")
    @patch("prwarden.cli.send_slack_message")
    @patch("prwarden.cli.submit_review")
    @patch("prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT)
    @patch("prwarden.cli.list_prior_review_comments", return_value=())
    @patch("prwarden.cli.list_changed_files")
    def test_no_findings_on_own_pr_sends_lgtm_to_slack(
        self,
        changed_files: Mock,
        prior_comments: Mock,
        snapshot: Mock,
        submit_review: Mock,
        send_slack: Mock,
        submit_approval: Mock,
    ) -> None:
        changed_files.return_value = [
            {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
        ]
        send_slack.return_value = "http-200"
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "reviewer", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            exact_repository = Repository(
                "app", "owner/app", Path(directory) / "worktree"
            )
            with patch("prwarden.cli.review_workspace") as workspace:
                workspace.return_value.__enter__.return_value = exact_repository
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
                    key = store.key(repository.slug, 1, "head", "slack")
                    self.assertEqual(store.get(key)["status"], "delivered")
            self.assertIs(engine.review.call_args.args[0], exact_repository)

        send_slack.assert_called_once()
        self.assertIn("PR Warden: LGTM", send_slack.call_args.args[1])
        self.assertIn("This PR can be merged", send_slack.call_args.args[1])
        submit_review.assert_not_called()
        submit_approval.assert_not_called()

    @patch("prwarden.cli.submit_approval")
    @patch("prwarden.cli.send_slack_message")
    @patch("prwarden.cli.submit_review")
    @patch("prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT)
    @patch("prwarden.cli.list_prior_review_comments", return_value=())
    @patch("prwarden.cli.list_changed_files")
    def test_lgtm_without_candidates_omits_the_unused_verifier(
        self,
        changed_files: Mock,
        prior_comments: Mock,
        snapshot: Mock,
        submit_review: Mock,
        send_slack: Mock,
        submit_approval: Mock,
    ) -> None:
        changed_files.return_value = [
            {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
        ]
        send_slack.return_value = "http-200"
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "reviewer", "head", "url")
            settings = self._verifying_settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            verification = cli.VerificationPass(
                engine=Mock(), settings=verification_engine_settings(settings)
            )
            activity = cli._VerificationActivity()

            exact_repository = Repository(
                "app", "owner/app", Path(directory) / "worktree"
            )
            with patch("prwarden.cli.review_workspace") as workspace:
                workspace.return_value.__enter__.return_value = exact_repository
                with JsonStateStore(settings.state_file) as store:
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                        verification=verification,
                        verification_activity=activity,
                    )

        message = send_slack.call_args.args[1]
        self.assertIn("Reviewed by Gemini 3.1 Pro (High) @ Antigravity", message)
        self.assertNotIn("verified by", message)
        self.assertFalse(activity.performed)
        verification.engine.review.assert_not_called()

    @patch("prwarden.cli.submit_approval")
    @patch("prwarden.cli.send_slack_message")
    @patch("prwarden.cli.submit_review")
    @patch("prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT)
    @patch("prwarden.cli.list_prior_review_comments", return_value=())
    @patch("prwarden.cli.list_changed_files")
    def test_lgtm_reports_the_candidates_verification_rejected(
        self,
        changed_files: Mock,
        prior_comments: Mock,
        snapshot: Mock,
        submit_review: Mock,
        send_slack: Mock,
        submit_approval: Mock,
    ) -> None:
        """An LGTM hides the work when every candidate was thrown out."""

        changed_files.return_value = [
            {
                "filename": "app.py",
                "patch": "@@ -0,0 +1,2 @@\n+value = 1\n+other = 2",
            }
        ]
        send_slack.return_value = "http-200"
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            worktree = Path(directory) / "worktree"
            worktree.mkdir()
            (worktree / "app.py").write_text("value = 1\nother = 2\n", encoding="utf-8")
            pull_request = PullRequest(1, "Title", "", "reviewer", "head", "url")
            settings = self._verifying_settings(directory, repository)

            def candidate(line: int, excerpt: str) -> dict:
                return {
                    "severity": "medium",
                    "title": f"Defect on line {line}",
                    "path": "app.py",
                    "line": line,
                    "problem": "This value causes a definite failure.",
                    "suggestion": "Use the validated value instead.",
                    "trigger": "Process the changed value.",
                    "impact": "The operation fails.",
                    "evidence": [
                        {
                            "path": "app.py",
                            "line": line,
                            "excerpt": excerpt,
                            "relevance": "This line triggers the defect.",
                        }
                    ],
                    "counterevidence": [],
                    "counterevidence_reason": "No direct counterevidence found.",
                }

            engine = Mock()
            engine.review.return_value = {
                "verdict": "do_not_approve",
                "approval_reason": "Two candidate defects were found.",
                "findings": [candidate(1, "value = 1"), candidate(2, "other = 2")],
            }
            verifier = Mock()
            verifier.review.return_value = {
                "verdict": "approve",
                "approval_reason": "Neither candidate is supported by the branch.",
                "judgments": [
                    {
                        "ref": "F1",
                        "verdict": "rejected",
                        "reason": "The caller already validates this value.",
                        "rejection_class": "not_a_defect",
                    },
                    {
                        "ref": "F2",
                        "verdict": "rejected",
                        "reason": "The installed version returns a string here.",
                        "rejection_class": "not_a_defect",
                    },
                ],
            }
            verification = cli.VerificationPass(
                engine=verifier, settings=verification_engine_settings(settings)
            )

            exact_repository = Repository("app", "owner/app", worktree)
            reported = StringIO()
            with patch("prwarden.cli.review_workspace") as workspace, patch(
                "sys.stdout", reported
            ):
                workspace.return_value.__enter__.return_value = exact_repository
                with JsonStateStore(settings.state_file) as store:
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                        verification=verification,
                        verification_activity=cli._VerificationActivity(),
                    )

        message = send_slack.call_args.args[1]
        self.assertIn("*PR Warden: LGTM*", message)
        self.assertIn(
            "Verification confirmed 0 of 2 candidate issues the review raised.",
            message,
        )
        submit_review.assert_not_called()

        written = reported.getvalue()
        self.assertIn("Confirmed 0 of 2 candidate findings", written)
        self.assertIn("Rejected candidate [MEDIUM] app.py:1 Defect on line 1", written)
        self.assertIn("Rejected candidate [MEDIUM] app.py:2 Defect on line 2", written)
        self.assertIn(
            "Rejected [not_a_defect] because: The caller already validates this value.", written
        )
        self.assertIn(
            "Rejected [not_a_defect] because: The installed version returns a string here.", written
        )
        self.assertNotIn("Rejected candidate", message)
        self.assertNotIn("Rejected because", message)

    @patch("prwarden.cli.submit_approval")
    @patch("prwarden.cli.send_slack_message")
    @patch("prwarden.cli.submit_review")
    @patch("prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT)
    @patch("prwarden.cli.list_prior_review_comments", return_value=())
    @patch("prwarden.cli.list_changed_files")
    def test_verified_findings_name_the_verifier_in_delivery(
        self,
        changed_files: Mock,
        prior_comments: Mock,
        snapshot: Mock,
        submit_review: Mock,
        send_slack: Mock,
        submit_approval: Mock,
    ) -> None:
        changed_files.return_value = [
            {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
        ]
        send_slack.return_value = "http-200"
        candidate = {
            "verdict": "do_not_approve",
            "approval_reason": "A candidate defect was found.",
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
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "reviewer", "head", "url")
            settings = self._verifying_settings(directory, repository)
            engine = Mock()
            engine.review.return_value = candidate
            verifier_engine = Mock()
            verifier_engine.review.return_value = {
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
            verification = cli.VerificationPass(
                engine=verifier_engine,
                settings=verification_engine_settings(settings),
            )
            activity = cli._VerificationActivity()
            (Path(directory) / "app.py").write_text("value = 1\n", encoding="utf-8")

            with patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.has_current_review", return_value=False
            ):
                workspace.return_value.__enter__.return_value = repository
                workspace.return_value.dependency_links = ()
                with JsonStateStore(settings.state_file) as store:
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                        verification=verification,
                        verification_activity=activity,
                    )

        verifier_engine.review.assert_called_once()
        self.assertTrue(activity.performed)
        self.assertIn(
            "Reviewed by Gemini 3.1 Pro (High) @ Antigravity, "
            "verified by GPT-5.6 Terra @ Kiro",
            send_slack.call_args.args[1],
        )

    def _fallback_settings(self, directory: str, repository: Repository) -> Settings:
        return replace(
            self._verifying_settings(directory, repository),
            fallback_engine="claude",
            fallback_engine_command=("claude",),
            fallback_model="claude-opus-5",
        )

    def _fallback_material(self, directory: str) -> ReviewMaterial:
        (Path(directory) / "app.py").write_text("value = 1\n", encoding="utf-8")
        return ReviewMaterial(
            diff="@@ -0,0 +1 @@\n+value = 1",
            added_lines={"app.py": frozenset({1})},
        )

    def test_engine_failure_hands_the_review_to_the_fallback_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self._fallback_settings(directory, repository)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            engine = Mock()
            engine.review.side_effect = EngineError(
                "FAILED_PRECONDITION (code 400): "
                "User location is not supported for the API use."
            )
            fallback_engine = Mock()
            fallback_engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            fallback = cli.FallbackReviewer(
                engine=fallback_engine,
                settings=fallback_engine_settings(settings),
            )

            with patch("sys.stdout", new_callable=StringIO) as output:
                result = cli._review_with_engine_fallback(
                    settings,
                    engine,
                    fallback,
                    repository,
                    pull_request,
                    self._fallback_material(directory),
                )

        engine.review.assert_called_once()
        fallback_engine.review.assert_called_once()
        self.assertEqual(result[5].review_engine, "claude")
        self.assertEqual(result[5].review_model, "claude-opus-5")
        self.assertIs(result[6], fallback_engine)
        terminal = output.getvalue()
        self.assertIn("antigravity (gemini-3.1-pro-high) review engine failed", terminal)
        self.assertIn(
            "Running fallback review with claude (claude-opus-5)", terminal
        )

    def test_validation_failure_does_not_hand_over_to_the_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self._fallback_settings(directory, repository)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            engine = Mock()
            engine.review.return_value = {"verdict": "nonsense"}
            fallback_engine = Mock()
            fallback = cli.FallbackReviewer(
                engine=fallback_engine,
                settings=fallback_engine_settings(settings),
            )

            with patch("sys.stdout", new_callable=StringIO):
                with self.assertRaises(ReviewError):
                    cli._review_with_engine_fallback(
                        settings,
                        engine,
                        fallback,
                        repository,
                        pull_request,
                        self._fallback_material(directory),
                    )

        fallback_engine.review.assert_not_called()

    def test_both_engines_failing_reports_both_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self._fallback_settings(directory, repository)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            engine = Mock()
            engine.review.side_effect = EngineError("User location is not supported")
            fallback_engine = Mock()
            fallback_engine.review.side_effect = EngineError("claude exited with 1")
            fallback = cli.FallbackReviewer(
                engine=fallback_engine,
                settings=fallback_engine_settings(settings),
            )

            with patch("sys.stdout", new_callable=StringIO):
                with self.assertRaises(EngineError) as raised:
                    cli._review_with_engine_fallback(
                        settings,
                        engine,
                        fallback,
                        repository,
                        pull_request,
                        self._fallback_material(directory),
                    )

        detail = str(raised.exception)
        self.assertIn("User location is not supported", detail)
        self.assertIn("claude (claude-opus-5) fallback also failed", detail)
        self.assertIn("claude exited with 1", detail)

    def test_engine_failure_without_a_fallback_is_reported_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self._verifying_settings(directory, repository)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            engine = Mock()
            engine.review.side_effect = EngineError("User location is not supported")

            with patch("sys.stdout", new_callable=StringIO):
                with self.assertRaisesRegex(
                    EngineError, "^User location is not supported$"
                ):
                    cli._review_with_engine_fallback(
                        settings,
                        engine,
                        None,
                        repository,
                        pull_request,
                        self._fallback_material(directory),
                    )

    @patch("prwarden.cli.submit_approval")
    @patch("prwarden.cli.send_slack_message")
    @patch("prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT)
    @patch("prwarden.cli.list_prior_review_comments", return_value=())
    @patch("prwarden.cli.list_changed_files")
    def test_fallback_review_is_attributed_to_the_engine_that_reviewed(
        self,
        changed_files: Mock,
        prior_comments: Mock,
        snapshot: Mock,
        send_slack: Mock,
        submit_approval: Mock,
    ) -> None:
        changed_files.return_value = [
            {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
        ]
        send_slack.return_value = "http-200"
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "reviewer", "head", "url")
            settings = self._fallback_settings(directory, repository)
            engine = Mock()
            engine.review.side_effect = EngineError("User location is not supported")
            fallback_engine = Mock()
            fallback_engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            fallback = cli.FallbackReviewer(
                engine=fallback_engine,
                settings=fallback_engine_settings(settings),
            )

            with patch("prwarden.cli.review_workspace") as workspace, patch(
                "sys.stdout", new_callable=StringIO
            ):
                workspace.return_value.__enter__.return_value = repository
                workspace.return_value.dependency_links = ()
                with JsonStateStore(settings.state_file) as store:
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                        fallback=fallback,
                    )

        message = send_slack.call_args.args[1]
        self.assertIn("Reviewed by Claude Opus 5 @ Claude", message)
        self.assertNotIn("Antigravity", message)

    def test_no_findings_on_own_conflicting_pr_does_not_send_lgtm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "reviewer", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            conflicting = PullRequestSnapshot(
                "head", "open", False, frozenset(), 1, False
            )

            with patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[READY_SNAPSHOT, conflicting],
            ), patch(
                "prwarden.cli.review_workspace"
            ) as workspace, patch(
                "prwarden.cli.send_slack_message"
            ) as send_slack:
                workspace.return_value.__enter__.return_value = repository
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
                    key = store.key(repository.slug, 1, "head", "slack")
                    self.assertEqual(store.get(key)["status"], "merge_conflict")

            send_slack.assert_not_called()

    @patch("prwarden.cli.send_slack_message")
    @patch(
        "prwarden.cli.get_pull_request_snapshot",
        return_value=PullRequestSnapshot(
            "new-head", "open", False, frozenset(), 1, True
        ),
    )
    @patch("prwarden.cli.list_prior_review_comments", return_value=())
    @patch("prwarden.cli.list_changed_files")
    def test_stale_head_stops_before_delivery(
        self,
        changed_files: Mock,
        prior_comments: Mock,
        snapshot: Mock,
        send_slack: Mock,
    ) -> None:
        changed_files.return_value = [
            {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
        ]
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "reviewer", "old-head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            with patch("prwarden.cli.review_workspace") as workspace:
                workspace.return_value.__enter__.return_value = repository
                with JsonStateStore(settings.state_file) as store:
                    with self.assertRaisesRegex(ReviewError, "head changed"):
                        cli._process_pull_request(
                            settings,
                            repository,
                            pull_request,
                            store,
                            engine,
                            live=True,
                            force=False,
                        )
                    key = store.key(repository.slug, 1, "old-head", "slack")
                    self.assertEqual(store.get(key)["status"], "superseded")
                    self.assertEqual(
                        store.get(key)["replacement_head_sha"],
                        "new-head",
                    )

        send_slack.assert_not_called()

    def test_head_change_restarts_review_with_latest_sha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(
                1, "Title", "", "author", "head-0", "url"
            )
            settings = self.settings(directory, repository)
            store = Mock()
            engine = Mock()

            with patch("prwarden.cli._process_pull_request") as process:
                process.side_effect = [
                    cli.PullRequestHeadChangedError("head-0", "head-1"),
                    None,
                ]

                cli._process_pull_request_with_head_restarts(
                    settings,
                    repository,
                    pull_request,
                    store,
                    engine,
                    live=True,
                    force=False,
                )

            self.assertEqual(process.call_count, 2)
            self.assertEqual(process.call_args_list[0].args[2].head_sha, "head-0")
            self.assertEqual(process.call_args_list[1].args[2].head_sha, "head-1")

    def test_head_change_restarts_at_most_five_times(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(
                1, "Title", "", "author", "head-0", "url"
            )
            settings = self.settings(directory, repository)
            store = Mock()
            engine = Mock()

            def change_head(*args: object, **kwargs: object) -> None:
                current = args[2]
                assert isinstance(current, PullRequest)
                index = int(current.head_sha.removeprefix("head-"))
                raise cli.PullRequestHeadChangedError(
                    current.head_sha,
                    f"head-{index + 1}",
                )

            with patch(
                "prwarden.cli._process_pull_request", side_effect=change_head
            ) as process:
                with self.assertRaisesRegex(
                    ReviewError,
                    "changed after 5 review restarts",
                ):
                    cli._process_pull_request_with_head_restarts(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )

            self.assertEqual(process.call_count, 6)
            self.assertEqual(process.call_args_list[-1].args[2].head_sha, "head-5")

    @patch("prwarden.cli.submit_review")
    @patch("prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT)
    @patch("prwarden.cli.has_current_review", return_value=False)
    @patch("prwarden.cli.list_changed_files")
    def test_new_feedback_on_a_finding_line_stops_delivery(
        self,
        changed_files: Mock,
        current_review: Mock,
        snapshot: Mock,
        submit_review: Mock,
    ) -> None:
        changed_files.return_value = [
            {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
        ]
        new_comment = PriorReviewComment(
            "inline:5",
            "inline",
            "other-reviewer",
            "This issue was raised while analysis was running.",
            "app.py",
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            review_response = {
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
            engine.review.side_effect = [review_response, CONFIRMED_JUDGMENT]

            with patch(
                "prwarden.cli.list_prior_review_comments",
                side_effect=[(), (new_comment,)],
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.send_slack_message", return_value="http-200"
            ) as notify:
                (Path(directory) / "app.py").write_text("value = 1\n")
                workspace.return_value.__enter__.return_value = repository
                with JsonStateStore(settings.state_file) as store:
                    # Somebody reviewing the same line is not a fault of this run,
                    # so it reports rather than raising and leaves the exit code
                    # to genuine failures.
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )
                    key = store.key(repository.slug, 1, "head", "github")
                    record = store.get(key)
                    self.assertEqual(record["status"], "feedback_changed")
                    self.assertIn("shares the lines", record["reason"])

            # Silence is the one thing the operator cannot act on.
            self.assertEqual(notify.call_count, 1)
            message = notify.call_args.args[1]
            self.assertIn("review not delivered", message)
            self.assertIn("app.py:1", message)
            self.assertIn("Run the reviewer again", message)
            # The comment body is the author's text and never leaves the machine.
            self.assertNotIn("while analysis was running", message)

        submit_review.assert_not_called()

    def test_only_unread_feedback_holds_a_finished_review(self) -> None:
        """Classify each way feedback can move while a review is running.

        Only a change that could carry something this review never read may
        discard its result. Everything else either removes an objection or is the
        same comment it already judged.
        """

        comment = PriorReviewComment(
            "inline:1",
            "inline",
            "developer",
            "Please rename this.",
            "app.py",
            5,
            False,
        )
        general = PriorReviewComment(
            "conversation:2",
            "conversation",
            "reviewer",
            "Holding off on this one.",
            None,
            None,
            None,
        )

        self.assertEqual(cli._feedback_objections((comment,), (comment,)), ())
        # Removing an objection cannot invalidate a result reached without it.
        self.assertEqual(cli._feedback_objections((comment, general), (general,)), ())
        self.assertEqual(
            cli._feedback_objections((comment,), (replace(comment, resolved=True),)),
            (),
        )
        # Anything that can carry unread words counts.
        self.assertEqual(
            cli._feedback_objections((replace(comment, resolved=True),), (comment,)),
            (comment,),
        )
        edited = replace(comment, body="Actually this is wrong.")
        self.assertEqual(cli._feedback_objections((comment,), (edited,)), (edited,))
        self.assertEqual(
            cli._feedback_objections((comment,), (comment, general)),
            (general,),
        )
        # A pathless comment cannot share a finding's anchor, so findings stand.
        finding = Finding(
            severity="medium",
            title="Incorrect value",
            path="app.py",
            line=5,
            problem="p",
            suggestion="s",
            trigger="t",
            impact="i",
        )
        self.assertEqual(cli._objections_touching_findings((general,), (finding,)), ())
        self.assertEqual(
            cli._objections_touching_findings((comment,), (finding,)),
            (comment,),
        )

    def test_new_feedback_elsewhere_still_delivers_findings(self) -> None:
        """A comment on another file cannot have answered this finding.

        Discarding every finding over any change to the feedback threw away
        complete reviews for comments that had nothing to do with them.
        """

        new_comment = PriorReviewComment(
            "inline:9",
            "inline",
            "other-reviewer",
            "A remark about a different file entirely.",
            "other.py",
            4,
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.side_effect = [
                finding_response("app.py", 1),
                CONFIRMED_JUDGMENT,
            ]

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments",
                side_effect=[(), (new_comment,)],
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_review", return_value="review-7"
            ) as submit_review:
                (Path(directory) / "app.py").write_text("value = 1\n")
                workspace.return_value.__enter__.return_value = repository
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
                    key = store.key(repository.slug, 1, "head", "github")
                    self.assertEqual(store.get(key)["status"], "delivered")

            submit_review.assert_called_once()

    def test_a_resolved_thread_no_longer_discards_an_approval(self) -> None:
        """Resolving a thread removes an objection; it cannot create one.

        The approval was reached without needing that thread, so a change that
        only settles it leaves the result sound.
        """

        before = PriorReviewComment(
            "inline:5",
            "inline",
            "other-reviewer",
            "Please rename this.",
            "app.py",
            1,
            False,
        )
        after = replace(before, resolved=True)
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments",
                side_effect=[(before,), (after,)],
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_approval", return_value="approval-9"
            ) as submit_approval:
                (Path(directory) / "app.py").write_text("value = 1\n")
                workspace.return_value.__enter__.return_value = repository
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
                    key = store.key(repository.slug, 1, "head", "github")
                    self.assertEqual(store.get(key)["status"], "delivered")

            submit_approval.assert_called_once()

    def test_a_reopened_thread_still_withholds_an_approval(self) -> None:
        """A thread reopened during analysis is an objection this review never read."""

        before = PriorReviewComment(
            "inline:5",
            "inline",
            "other-reviewer",
            "Please rename this.",
            "app.py",
            1,
            True,
        )
        after = replace(before, resolved=False)
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments",
                side_effect=[(before,), (after,)],
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[READY_SNAPSHOT, READY_SNAPSHOT],
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.send_slack_message", return_value="http-200"
            ), patch("prwarden.cli.submit_approval") as submit_approval:
                (Path(directory) / "app.py").write_text("value = 1\n")
                workspace.return_value.__enter__.return_value = repository
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
                    key = store.key(repository.slug, 1, "head", "github")
                    record = store.get(key)
                    self.assertEqual(record["status"], "feedback_changed")
                    self.assertIn("newer than this approval", record["reason"])

            submit_approval.assert_not_called()

    def test_lgtm_approves_team_pull_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            exact_repository = Repository(
                "app", "owner/app", Path(directory) / "worktree"
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
            ), patch(
                "prwarden.cli.review_workspace"
            ) as workspace, patch(
                "prwarden.cli.submit_approval", return_value="approval-9"
            ) as submit_approval, patch(
                "prwarden.cli.submit_review"
            ) as submit_review:
                workspace.return_value.__enter__.return_value = exact_repository
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
                    key = store.key(repository.slug, 1, "head", "github")
                    self.assertEqual(store.get(key)["status"], "delivered")

            submit_approval.assert_called_once_with(repository, pull_request)
            submit_review.assert_not_called()

    def test_merge_conflict_withholds_approval_and_remains_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[
                    READY_SNAPSHOT,
                    PullRequestSnapshot("head", "open", False, frozenset(), 1, False),
                ],
            ), patch(
                "prwarden.cli.review_workspace"
            ) as workspace, patch(
                "prwarden.cli._print_findings"
            ) as print_findings, patch(
                "prwarden.cli.submit_approval"
            ) as submit_approval:
                workspace.return_value.__enter__.return_value = repository
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
                    key = store.key(repository.slug, 1, "head", "github")
                    self.assertEqual(store.get(key)["status"], "merge_conflict")
                    self.assertFalse(store.is_terminal(key))

            submit_approval.assert_not_called()
            print_findings.assert_called_once_with([], False)

    @staticmethod
    def _finding_payload(excerpt: str) -> dict:
        return {
            "verdict": "do_not_approve",
            "approval_reason": "A candidate defect was found.",
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
                            "excerpt": excerpt,
                            "relevance": "This line triggers the defect.",
                        }
                    ],
                    "counterevidence": [],
                    "counterevidence_reason": "No direct counterevidence was found.",
                }
            ],
        }

    def test_a_confirmed_candidate_is_delivered_exactly_as_reviewed(self) -> None:
        """The verifier cannot mangle a citation the controller already validated.

        Seven verification responses were rejected for reformatting an excerpt they
        only had to copy, and two of them cost a further review cycle.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            repository = Repository("app", "owner/app", root)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.side_effect = [
                self._finding_payload("value = 1"),
                {
                    "verdict": "do_not_approve",
                    "approval_reason": "The candidate holds against the branch.",
                    "judgments": [
                        {
                            "ref": "F1",
                            "verdict": "confirmed",
                            "reason": "Read the caller; nothing validates it.",
                            "severity": "high",
                        }
                    ],
                },
            ]

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_review", return_value="review-9"
            ) as submit_review, patch(
                "sys.stdout", new_callable=StringIO
            ):
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

            submit_review.assert_called_once()
            delivered = submit_review.call_args.args[2]
            self.assertEqual(len(delivered), 1)
            # The citation is the review's own, and severity is the one correction
            # the verifier is allowed to make.
            self.assertEqual(delivered[0].evidence[0].excerpt, "value = 1")
            self.assertEqual(delivered[0].path, "app.py")
            self.assertEqual(delivered[0].line, 1)
            self.assertEqual(delivered[0].severity, "high")
            # The verifier was handed references, not asked for findings.
            verification_material = engine.review.call_args_list[1].args[2]
            self.assertEqual(len(verification_material.verification_candidates), 1)

    def test_a_verification_response_the_controller_cannot_use_fails_once(self) -> None:
        """No extra review cycle: a judgment cannot be right in substance and wrong
        in evidence, because the verifier never restates the evidence."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            repository = Repository("app", "owner/app", root)
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            unusable = {
                "verdict": "do_not_approve",
                "approval_reason": "The candidate was confirmed.",
                "judgments": [
                    {"ref": "F7", "verdict": "confirmed", "reason": "Not supplied."}
                ],
            }
            engine = Mock()
            engine.review.side_effect = [
                self._finding_payload("value = 1"),
                unusable,
                unusable,
            ]

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_approval"
            ) as submit_approval, patch(
                "prwarden.cli.submit_review"
            ) as submit_review, patch(
                "sys.stdout", new_callable=StringIO
            ) as output:
                workspace.return_value.__enter__.return_value = repository
                workspace.return_value.dependency_links = ()
                workspace.return_value.dependency_notes = ()
                with JsonStateStore(settings.state_file) as store:
                    with self.assertRaises(ReviewError):
                        cli._process_pull_request(
                            settings,
                            repository,
                            pull_request,
                            store,
                            engine,
                            live=True,
                            force=False,
                        )
                    key = store.key("owner/app", 1, "head", "github")
                    self.assertEqual(store.get(key)["status"], "failed")

            # One review, then the verification attempt and its single retry.
            self.assertEqual(engine.review.call_count, 3)
            self.assertIn("was not supplied", output.getvalue())
            submit_approval.assert_not_called()
            submit_review.assert_not_called()

    def test_pending_mergeability_is_rechecked_before_failing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            pending = PullRequestSnapshot("head", "open", False, frozenset(), 1, None)

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=[READY_SNAPSHOT, pending, pending, READY_SNAPSHOT],
            ) as snapshot, patch(
                "prwarden.cli.time.sleep"
            ) as sleep, patch(
                "prwarden.cli.review_workspace"
            ) as workspace, patch(
                "prwarden.cli._print_findings"
            ), patch(
                "prwarden.cli.submit_approval", return_value="review-1"
            ) as submit_approval:
                workspace.return_value.__enter__.return_value = repository
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
                    key = store.key(repository.slug, 1, "head", "github")
                    self.assertEqual(store.get(key)["status"], "delivered")

            submit_approval.assert_called_once_with(repository, pull_request)
            self.assertEqual(snapshot.call_count, 4)
            self.assertEqual(
                [call.args[0] for call in sleep.call_args_list],
                list(cli.MERGEABILITY_RECHECK_DELAYS_SECONDS[:2]),
            )

    def test_unresolved_mergeability_still_fails_after_the_rechecks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            pending = PullRequestSnapshot("head", "open", False, frozenset(), 1, None)

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot", return_value=pending
            ), patch(
                "prwarden.cli.time.sleep"
            ), patch(
                "prwarden.cli.review_workspace"
            ) as workspace, patch(
                "prwarden.cli.submit_approval"
            ) as submit_approval:
                workspace.return_value.__enter__.return_value = repository
                with JsonStateStore(settings.state_file) as store:
                    with self.assertRaises(ReviewError) as raised:
                        cli._process_pull_request(
                            settings,
                            repository,
                            pull_request,
                            store,
                            engine,
                            live=True,
                            force=False,
                        )
                    key = store.key(repository.slug, 1, "head", "github")
                    self.assertEqual(store.get(key)["status"], "failed")

            self.assertIn("has not finished calculating", str(raised.exception))
            submit_approval.assert_not_called()

    def partially_reviewable_files(self) -> list:
        """Return changed files whose complete diff needs 631 bytes."""

        return [
            {
                "filename": "app.py",
                "patch": "@@ -0,0 +1,40 @@\n"
                + "\n".join(
                    "+value = {0}".format(number) for number in range(1, 41)
                ),
            },
            {"filename": "other.py", "patch": "@@ -0,0 +1 @@\n+other = 1"},
        ]

    def run_partial_review(
        self,
        directory: str,
        engine: Mock,
        head: str = "head",
        max_diff_bytes: int = 600,
        force: bool = False,
        changed_files=None,
        ignored_missing_patch_patterns: tuple = (),
        max_review_bundles: int = 1,
        generated_marks: "Optional[dict]" = None,
        marks_unreadable: bool = False,
        checks=(),
    ) -> dict:
        repository = Repository("app", "owner/app", Path(directory))
        pull_request = PullRequest(
            1, "Title", "", "author", head, "url", base_ref="main"
        )
        marks = None if marks_unreadable else dict(generated_marks or {})
        settings = self.settings(directory, repository)
        settings = Settings(
            **{
                **settings.__dict__,
                "max_diff_bytes": max_diff_bytes,
                "ignored_missing_patch_patterns": ignored_missing_patch_patterns,
                "max_review_bundles": max_review_bundles,
            }
        )
        (Path(directory) / "app.py").write_text(
            "".join("value = {0}\n".format(number) for number in range(1, 41)),
            encoding="utf-8",
        )

        with patch("prwarden.cli.has_current_review", return_value=False), patch(
            "prwarden.cli.list_prior_review_comments", return_value=()
        ), patch(
            "prwarden.cli._read_generated_marks", return_value=marks
        ) as read_marks, patch(
            "prwarden.cli.list_changed_files",
            return_value=(
                self.partially_reviewable_files()
                if changed_files is None
                else changed_files
            ),
        ), patch(
            "prwarden.cli.list_pull_request_checks",
            **(
                {"side_effect": checks}
                if isinstance(checks, list)
                else {"return_value": checks}
            ),
        ) as list_checks, patch(
            "prwarden.cli._read_file_history", return_value=()
        ), patch(
            "prwarden.cli.get_pull_request_snapshot",
            return_value=replace(READY_SNAPSHOT, head_sha=head),
        ), patch(
            "prwarden.cli.review_workspace"
        ) as workspace, patch(
            "prwarden.cli.submit_review", return_value="review-1"
        ) as submit_review, patch(
            "prwarden.cli.submit_approval", return_value="approval-1"
        ) as submit_approval, patch(
            "prwarden.cli.send_slack_message", return_value="http-200"
        ) as send_slack, patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
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
                    force=force,
                )
                record = store.get(store.key(repository.slug, 1, head, "github"))
        return {
            "record": record or {},
            "terminal": output.getvalue(),
            "submit_review": submit_review,
            "submit_approval": submit_approval,
            "send_slack": send_slack,
            "read_marks": read_marks,
            "list_checks": list_checks,
        }

    def test_a_bundled_review_reads_every_file_and_may_approve(self) -> None:
        """The point of bundling: a change too large for one review is still read.

        Measured across 394 pull requests, this happens to about one percent of them,
        which is why only the oversized path bundles and everything else is untouched.
        """

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            result = self.run_partial_review(
                directory, engine, max_diff_bytes=600, max_review_bundles=4
            )

        self.assertIn("Reviewing in 2 bundles", result["terminal"])
        self.assertEqual(engine.review.call_count, 2)
        self.assertTrue(result["submit_approval"].called)
        self.assertNotIn("were not reviewed", result["terminal"])

    def test_one_bundle_keeps_the_partial_review_it_always_did(self) -> None:
        """The regression guard: with bundling off, nothing about this path moves."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            result = self.run_partial_review(
                directory, engine, max_diff_bytes=600, max_review_bundles=1
            )

        self.assertNotIn("Reviewing in", result["terminal"])
        self.assertEqual(engine.review.call_count, 1)
        self.assertFalse(result["submit_approval"].called)
        self.assertIn("not reviewed", result["terminal"])

    def test_a_bundle_may_take_an_approval_away_but_never_grant_one(self) -> None:
        """An approval speaks for the whole pull request, a bundle for its own files."""

        with tempfile.TemporaryDirectory() as directory:
            # A citation's excerpt must be that line's exact text in the reviewed
            # worktree, and the second bundle holds the file the first had no room
            # for.
            (Path(directory) / "other.py").write_text(
                "value = 1\n", encoding="utf-8"
            )
            engine = Mock()
            engine.review.side_effect = [
                {
                    "verdict": "approve",
                    "approval_reason": "No qualifying defects remain.",
                    "findings": [],
                },
                finding_response("other.py", 1),
                CONFIRMED_JUDGMENT,
            ]

            result = self.run_partial_review(
                directory, engine, max_diff_bytes=600, max_review_bundles=4
            )

        # Two reviews and one verification of the second bundle's candidate.
        self.assertEqual(engine.review.call_count, 3)
        self.assertFalse(result["submit_approval"].called)
        self.assertTrue(result["submit_review"].called)
        # The finding the second bundle found is the one that gets delivered, so a
        # bundle that raises nothing cannot silently drop what another one found.
        comments = result["submit_review"].call_args.args[2]
        self.assertEqual([comment.path for comment in comments], ["other.py"])

    def test_an_undiffable_file_is_reviewed_around_rather_than_fatal(self) -> None:
        """A generated file GitHub will not diff no longer blocks a pull request.

        One pull request [pr:xmf2] failed on every run for a committed data file
        whose patch GitHub omits, so nothing in it was reviewed. The rest of the
        change is now reviewed, the file is named, and the approval goes with it.
        """

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            (Path(directory) / "app.py").write_text("value = 1\n")

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"},
                    {
                        "filename": "scripts/generated/records.json",
                        "status": "modified",
                        "additions": 9000,
                        "deletions": 8000,
                        "changes": 17000,
                    },
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_review", return_value="review-1"
            ), patch(
                "prwarden.cli.submit_approval"
            ) as submit_approval, patch(
                "prwarden.cli.send_slack_message", return_value="http-200"
            ) as send_slack, patch(
                "sys.stdout", new_callable=StringIO
            ) as output:
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
                    record = store.get(store.key(repository.slug, 1, "head", "github"))

            # The readable part was reviewed rather than the run failing.
            engine.review.assert_called()
            submit_approval.assert_not_called()
            self.assertEqual(record["status"], "approval_withheld")
            terminal = output.getvalue()
            self.assertIn(
                "Not reviewed: scripts/generated/records.json "
                "(GitHub omitted its patch)",
                terminal,
            )
            # The byte budget was never the problem, so no ratio is invented.
            self.assertNotIn("x the", terminal)
            message = send_slack.call_args.args[1]
            self.assertIn("GitHub omitted the patch of 1 of 2 changed files", message)

    def test_a_partial_review_reports_its_coverage(self) -> None:
        """The size of the change is a fact the run states, not one to reconstruct."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            result = self.run_partial_review(directory, engine)

            self.assertIn("Diff is 631 B across 2 files, 1.05x", result["terminal"])
            self.assertIn("Reviewing 1 files that fit", result["terminal"])
            self.assertIn("1 need a human", result["terminal"])

    def test_a_partial_review_is_never_approved(self) -> None:
        """A review that did not read every line cannot certify the whole change."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            result = self.run_partial_review(directory, engine)

            result["submit_approval"].assert_not_called()
            self.assertEqual(result["record"]["status"], "approval_withheld")
            self.assertIn("were not reviewed", result["record"]["reason"])

    def test_a_partial_review_says_so_where_the_author_reads_it(self) -> None:
        """Inline comments alone would imply the whole change was read."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                self.defect_review_response(),
                CONFIRMED_JUDGMENT,
            ]

            result = self.run_partial_review(directory, engine)

            result["submit_review"].assert_called_once()
            body = result["submit_review"].call_args.kwargs["body"]
            self.assertIn("read 1 of 2 changed files", body)
            self.assertIn("no approval was given", body)

    def test_oversized_diff_notifies_slack_and_is_terminal_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "max_diff_bytes": 10})
            engine = Mock()

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {
                        "filename": "app.py",
                        "patch": "@@ -0,0 +1 @@\n+value = 'too large'",
                    }
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
            ), patch(
                "prwarden.cli.send_slack_message", return_value="http-200"
            ) as send_slack:
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
                    key = store.key(repository.slug, 1, "head", "github")
                    self.assertEqual(store.get(key)["status"], "skipped_oversized")
                    self.assertTrue(store.is_terminal(key))
                    self.assertTrue(
                        store.is_settled_for_pull_request(
                            repository.slug, 1, "oversized"
                        )
                    )

            engine.review.assert_not_called()
            send_slack.assert_called_once()
            self.assertIn("*MANUAL REVIEW NEEDED*", send_slack.call_args.args[1])
            self.assertIn("Automated review: not run", send_slack.call_args.args[1])

    def test_a_later_head_of_a_big_pull_request_is_not_reported_again(self) -> None:
        """One manual-review message per pull request, not one per push."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "max_diff_bytes": 10})
            engine = Mock()

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {
                        "filename": "app.py",
                        "patch": "@@ -0,0 +1 @@\n+value = 'too large'",
                    }
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot",
                side_effect=lambda repo, pull: PullRequestSnapshot(
                    pull.head_sha, "open", False, frozenset(), 1, True
                ),
            ), patch(
                "prwarden.cli.send_slack_message", return_value="http-200"
            ) as send_slack:
                with JsonStateStore(settings.state_file) as store:
                    for head in ("head-one", "head-two"):
                        cli._process_pull_request(
                            settings,
                            repository,
                            PullRequest(1, "Title", "", "author", head, "url"),
                            store,
                            engine,
                            live=True,
                            force=False,
                        )

            send_slack.assert_called_once()

    def test_a_partial_review_asks_a_human_for_the_rest_once(self) -> None:
        """The message says what was read, so the human knows what is left."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            result = self.run_partial_review(directory, engine)

            result["send_slack"].assert_called_once()
            message = result["send_slack"].call_args.args[1]
            self.assertIn("Automated review: partial", message)
            self.assertIn("read 1 of 2 changed files", message)
            self.assertIn("Not reviewed: (repository root) (88 B)", message)

            # A later push re-reviews and must not ask again, because the request
            # is made once per pull request rather than once per head.
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            again = self.run_partial_review(directory, engine, head="head2")
            self.assertIn("already requested", again["terminal"])
            self.assertEqual(result["send_slack"].call_count, 1)

    def test_an_unread_file_is_not_re_reviewed_while_nothing_can_change(
        self,
    ) -> None:
        """A withheld approval the controller forced itself is not revisited.

        One pull request [pr:xmf2] was re-reviewed on ten consecutive runs against
        one unchanged head, each spending a full model invocation to record the
        identical answer, because the reconsider rule recognized only a withholding
        backed by validated blocking comment IDs and this one can never carry any.
        """

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            first = self.run_partial_review(directory, engine)
            self.assertEqual(first["record"]["status"], "approval_withheld")
            self.assertEqual(first["record"]["unread_paths"], ["other.py"])
            reviews_spent = engine.review.call_count
            self.assertEqual(reviews_spent, 1)

            again = self.run_partial_review(directory, engine)

            self.assertIn(
                "Skipped: approval stays withheld while other.py cannot be "
                "read",
                again["terminal"],
            )
            # The whole point of the skip: no second model invocation.
            self.assertEqual(engine.review.call_count, reviews_spent)
            self.assertEqual(again["send_slack"].call_count, 0)

    def test_naming_a_generated_file_reopens_it_and_allows_the_approval(
        self,
    ) -> None:
        """The documented remedy has to reach the approval it promises.

        Listing the path in IGNORED_MISSING_PATCH_PATTERNS says the file is
        generated and needs no review. That must reopen a pull request already
        recorded as withheld over it, or the operator would have to know to pass
        --force for the setting to take effect.
        """

        undiffable = [
            {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"},
            {
                "filename": "scripts/generated/data.json",
                "status": "modified",
                "additions": 9000,
                "deletions": 8000,
                "changes": 17000,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }

            first = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=undiffable,
            )
            self.assertEqual(
                first["record"]["unread_paths"],
                ["scripts/generated/data.json"],
            )
            first["submit_approval"].assert_not_called()

            again = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=undiffable,
                ignored_missing_patch_patterns=("scripts/generated/*",),
            )

            self.assertNotIn("Skipped: approval stays withheld", again["terminal"])
            again["submit_approval"].assert_called_once()
            self.assertEqual(again["record"]["status"], "delivered")

    GENERATED = "src/api/generated/client.ts"

    def generated_change(self) -> list:
        return [
            {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"},
            {
                "filename": self.GENERATED,
                "status": "modified",
                "additions": 9000,
                "deletions": 8000,
                "changes": 17000,
            },
        ]

    def approving_engine(self) -> Mock:
        engine = Mock()
        engine.review.return_value = {
            "verdict": "approve",
            "approval_reason": "No qualifying defects remain.",
            "findings": [],
        }
        return engine

    def test_a_file_the_base_marks_generated_keeps_the_pull_request_approvable(
        self,
    ) -> None:
        """A repository already tells GitHub which files are machine-made. A file it
        marks `linguist-generated` on its base branch needs no review when GitHub
        omits its patch, exactly as a file the ignore list names."""

        with tempfile.TemporaryDirectory() as directory:
            result = self.run_partial_review(
                directory,
                self.approving_engine(),
                max_diff_bytes=100000,
                changed_files=self.generated_change(),
                generated_marks={self.GENERATED: True},
            )

        result["read_marks"].assert_called_once()
        self.assertEqual(result["read_marks"].call_args.args[2], (self.GENERATED,))
        self.assertIn(
            "marked linguist-generated on main: " + self.GENERATED,
            result["terminal"],
        )
        result["submit_approval"].assert_called_once()
        self.assertEqual(result["record"]["status"], "delivered")

    def test_a_base_mark_added_later_reopens_the_withheld_approval(self) -> None:
        """The skip that saves a re-review must not outlive what it rests on: once
        the base branch marks the unread file generated, the next run reviews again
        and may approve, while an unchanged base still skips."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.approving_engine()
            first = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.generated_change(),
                generated_marks={self.GENERATED: None},
            )
            self.assertEqual(first["record"]["status"], "approval_withheld")
            self.assertEqual(
                first["record"]["unread_generated_marks"],
                {self.GENERATED: "unmarked"},
            )

            unchanged = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.generated_change(),
                generated_marks={self.GENERATED: None},
            )
            self.assertIn("Skipped: approval stays withheld", unchanged["terminal"])
            self.assertEqual(engine.review.call_count, 1)

            marked = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.generated_change(),
                generated_marks={self.GENERATED: True},
            )

        self.assertIn(
            "The base branch's marks now leave " + self.GENERATED
            + " needing no review",
            marked["terminal"],
        )
        marked["submit_approval"].assert_called_once()
        self.assertEqual(marked["record"]["status"], "delivered")

    def test_only_a_new_generated_mark_reopens_a_withheld_approval(self) -> None:
        """Any other answer leaves the file unread again. A lookup that failed, or an
        unmark, counted as a change and spent a review to record the same result,
        and a transient failure would have done so twice, once each way."""

        for later in ({self.GENERATED: False}, None):
            with self.subTest(later=later):
                with tempfile.TemporaryDirectory() as directory:
                    engine = self.approving_engine()
                    self.run_partial_review(
                        directory,
                        engine,
                        max_diff_bytes=100000,
                        changed_files=self.generated_change(),
                        generated_marks={self.GENERATED: None},
                    )

                    again = self.run_partial_review(
                        directory,
                        engine,
                        max_diff_bytes=100000,
                        changed_files=self.generated_change(),
                        generated_marks=later,
                        marks_unreadable=later is None,
                    )

                self.assertIn(
                    "Skipped: approval stays withheld", again["terminal"]
                )
                self.assertEqual(engine.review.call_count, 1)

    def test_a_record_from_before_the_marks_reopens_once_the_base_marks_it(
        self,
    ) -> None:
        """A withholding recorded before the marks were read names no omitted files.
        Its unread paths are asked about, or a mark on the base could never reopen a
        pull request already waiting on that file."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.approving_engine()
            first = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.generated_change(),
                generated_marks={self.GENERATED: None},
            )
            state_file = Path(directory) / "state.json"
            with JsonStateStore(state_file) as store:
                key = store.key("owner/app", 1, "head", "github")
                older = dict(store.get(key))
                del older["unread_generated_marks"]
                store.set_status(key, older.pop("status"), **{
                    name: value
                    for name, value in older.items()
                    if name != "updated_at"
                })

            marked = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.generated_change(),
                generated_marks={self.GENERATED: True},
            )

        self.assertEqual(first["record"]["status"], "approval_withheld")
        self.assertEqual(
            marked["read_marks"].call_args_list[0].args[2], (self.GENERATED,)
        )
        marked["submit_approval"].assert_called_once()

    def test_unreadable_marks_keep_even_a_listed_file_unread(self) -> None:
        """Any unreadable mark might have been an unmark, so a lookup that failed
        must not let a pattern ignore the file: success could have withheld the
        approval that the failure would otherwise grant."""

        lockfile = [
            {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"},
            {
                "filename": "package-lock.json",
                "status": "modified",
                "additions": 9000,
                "deletions": 8000,
                "changes": 17000,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_partial_review(
                directory,
                self.approving_engine(),
                max_diff_bytes=100000,
                changed_files=lockfile,
                ignored_missing_patch_patterns=("package-lock.json",),
                marks_unreadable=True,
            )

        result["submit_approval"].assert_not_called()
        self.assertEqual(result["record"]["unread_paths"], ["package-lock.json"])

    def test_a_base_dropping_its_unmark_reopens_a_listed_file(self) -> None:
        """A file the patterns name stayed unread only because the base unmarked it.
        Once the base drops the unmark, another review would ignore it and could
        approve, so the next run must review again."""

        lockfile = [
            {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"},
            {
                "filename": "package-lock.json",
                "status": "modified",
                "additions": 9000,
                "deletions": 8000,
                "changes": 17000,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            engine = self.approving_engine()
            first = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=lockfile,
                ignored_missing_patch_patterns=("package-lock.json",),
                generated_marks={"package-lock.json": False},
            )
            self.assertEqual(
                first["record"]["unread_generated_marks"],
                {"package-lock.json": "not_generated"},
            )

            dropped = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=lockfile,
                ignored_missing_patch_patterns=("package-lock.json",),
                generated_marks={"package-lock.json": None},
            )

        self.assertIn("needing no review; reconsidering", dropped["terminal"])
        dropped["submit_approval"].assert_called_once()

        # A lookup that fails proves nothing, even for a file a pattern names: the
        # review it would reopen keeps that file unread and withholds again.
        with tempfile.TemporaryDirectory() as directory:
            engine = self.approving_engine()
            self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=lockfile,
                ignored_missing_patch_patterns=("package-lock.json",),
                generated_marks={"package-lock.json": False},
            )
            failed = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=lockfile,
                ignored_missing_patch_patterns=("package-lock.json",),
                marks_unreadable=True,
            )

        self.assertIn("Skipped: approval stays withheld", failed["terminal"])
        self.assertEqual(engine.review.call_count, 1)

    def test_marks_that_cannot_be_read_withhold_the_approval(self) -> None:
        """Failing to read the marks must cost an approval, never grant one."""

        with tempfile.TemporaryDirectory() as directory:
            result = self.run_partial_review(
                directory,
                self.approving_engine(),
                max_diff_bytes=100000,
                changed_files=self.generated_change(),
                marks_unreadable=True,
            )

        result["submit_approval"].assert_not_called()
        self.assertEqual(result["record"]["status"], "approval_withheld")
        self.assertEqual(
            result["record"]["unread_generated_marks"],
            {self.GENERATED: "unavailable"},
        )

    CLEAN_CHANGE = [{"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}]
    FAILED = (PullRequestCheck("compile-code", "completed", "failure"),)
    PASSED = (PullRequestCheck("compile-code", "completed", "success"),)

    def test_a_failed_check_withholds_the_approval(self) -> None:
        """A dependency pull request [pr:kwc5] was approved while its required build
        check failed on exactly the defect the reviewer had raised and the verifier
        rejected. No review verdict outweighs the head failing the team's own test."""

        with tempfile.TemporaryDirectory() as directory:
            result = self.run_partial_review(
                directory,
                self.approving_engine(),
                max_diff_bytes=100000,
                changed_files=self.CLEAN_CHANGE,
                checks=self.FAILED,
            )

        result["submit_approval"].assert_not_called()
        self.assertEqual(result["record"]["status"], "approval_withheld")
        self.assertEqual(result["record"]["failed_checks"], ["compile-code"])
        self.assertIn(
            "Approval withheld: GitHub check compile-code failed at this head",
            result["terminal"],
        )

    def test_the_checks_are_read_again_just_before_approving(self) -> None:
        """CI often finishes while a review runs: the checks the review started
        with can all be pending and then fail before the approval would land."""

        with tempfile.TemporaryDirectory() as directory:
            result = self.run_partial_review(
                directory,
                self.approving_engine(),
                max_diff_bytes=100000,
                changed_files=self.CLEAN_CHANGE,
                checks=[(), self.FAILED],
            )

        self.assertEqual(result["list_checks"].call_count, 2)
        result["submit_approval"].assert_not_called()

    def test_a_cancelled_or_skipped_check_does_not_withhold(self) -> None:
        """A run cancelled because no runner came says nothing about the code."""

        checks = (
            PullRequestCheck("install (macos)", "completed", "cancelled"),
            PullRequestCheck("deploy", "completed", "skipped"),
            PullRequestCheck("e2e", "in_progress", None),
        )
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_partial_review(
                directory,
                self.approving_engine(),
                max_diff_bytes=100000,
                changed_files=self.CLEAN_CHANGE,
                checks=checks,
            )

        result["submit_approval"].assert_called_once()

    def test_a_finding_is_still_delivered_over_a_failed_check(self) -> None:
        """Only the approval depends on the checks; a confirmed defect does not."""

        engine = Mock()
        engine.review.side_effect = [finding_response("app.py", 1), CONFIRMED_JUDGMENT]
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "app.py").write_text("value = 1\n", encoding="utf-8")
            result = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.CLEAN_CHANGE,
                checks=self.FAILED,
            )

        result["submit_review"].assert_called_once()
        result["submit_approval"].assert_not_called()

    def test_a_withheld_approval_waits_for_the_check_to_pass(self) -> None:
        """Re-reviewing while the check still fails would spend a review to record
        the same answer; a re-run that passes reopens it and may approve."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.approving_engine()
            self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.CLEAN_CHANGE,
                checks=self.FAILED,
            )
            still = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.CLEAN_CHANGE,
                checks=self.FAILED,
            )
            self.assertIn(
                "Skipped: approval stays withheld while check compile-code failed",
                still["terminal"],
            )
            self.assertEqual(engine.review.call_count, 1)

            passed = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.CLEAN_CHANGE,
                checks=self.PASSED,
            )

        self.assertIn("No check fails at this head any more", passed["terminal"])
        self.assertEqual(engine.review.call_count, 2)
        passed["submit_approval"].assert_called_once()

    def test_unreadable_checks_keep_a_withheld_approval_without_a_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.approving_engine()
            self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.CLEAN_CHANGE,
                checks=self.FAILED,
            )
            again = self.run_partial_review(
                directory,
                engine,
                max_diff_bytes=100000,
                changed_files=self.CLEAN_CHANGE,
                checks=[CommandError("gh exited with 1: HTTP 502")],
            )

        self.assertIn("checks could not be read", again["terminal"])
        self.assertEqual(engine.review.call_count, 1)

    def test_a_larger_budget_reopens_a_pull_request_left_unread(self) -> None:
        """Changing what can be read must make the approval reachable again."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            self.run_partial_review(directory, engine)

            again = self.run_partial_review(directory, engine, max_diff_bytes=100000)

            self.assertNotIn("Skipped: approval stays withheld", again["terminal"])
            self.assertEqual(engine.review.call_count, 2)

    def test_a_forced_run_reviews_an_unread_pull_request_again(self) -> None:
        """--force stays the operator's override over every recorded skip."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            self.run_partial_review(directory, engine)

            again = self.run_partial_review(directory, engine, force=True)

            self.assertNotIn("Skipped: approval stays withheld", again["terminal"])
            self.assertEqual(engine.review.call_count, 2)

    def test_oversized_notification_failure_remains_retryable_without_raising(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "max_diff_bytes": 10})
            engine = Mock()

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {
                        "filename": "app.py",
                        "patch": "@@ -0,0 +1 @@\n+value = 'too large'",
                    }
                ],
            ), patch(
                "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
            ), patch(
                "prwarden.cli.send_slack_message",
                side_effect=cli.DeliveryError("Slack rejected the message"),
            ):
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
                    key = store.key(repository.slug, 1, "head", "github")
                    self.assertEqual(
                        store.get(key)["status"],
                        "oversized_notification_pending",
                    )
                    self.assertFalse(store.is_terminal(key))
                    # The request itself is nonterminal too, so a later run asks
                    # again rather than losing it with this head.
                    self.assertFalse(
                        store.is_settled_for_pull_request(
                            repository.slug, 1, "oversized"
                        )
                    )

            engine.review.assert_not_called()

    def defect_review_response(self) -> dict:
        return {
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

    def run_until_final_snapshot(
        self,
        directory: str,
        engine: Mock,
        final_snapshot: PullRequestSnapshot,
    ) -> dict:
        repository = Repository("app", "owner/app", Path(directory))
        pull_request = PullRequest(1, "Title", "", "author", "head", "url")
        settings = self.settings(directory, repository)
        (Path(directory) / "app.py").write_text("value = 1\n", encoding="utf-8")

        with patch("prwarden.cli.has_current_review", return_value=False), patch(
            "prwarden.cli.list_prior_review_comments", return_value=()
        ), patch(
            "prwarden.cli.list_changed_files",
            return_value=[
                {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
            ],
        ), patch(
            "prwarden.cli.get_pull_request_snapshot",
            side_effect=[READY_SNAPSHOT, final_snapshot],
        ), patch(
            "prwarden.cli.review_workspace"
        ) as workspace, patch(
            "prwarden.cli.submit_review", return_value="review-1"
        ) as submit_review, patch(
            "prwarden.cli.submit_approval", return_value="approval-1"
        ) as submit_approval, patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
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
                record = store.get(store.key(repository.slug, 1, "head", "github"))
        return {
            "record": record or {},
            "terminal": output.getvalue(),
            "submit_review": submit_review,
            "submit_approval": submit_approval,
        }

    def test_a_merge_during_review_delivers_findings_without_approving(self) -> None:
        """A merge ends the approval decision, not the defect.

        The finding is already confirmed and now sits in the base branch, so
        withholding it would lose the review's entire result to timing.
        """

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                self.defect_review_response(),
                CONFIRMED_JUDGMENT,
            ]
            merged = PullRequestSnapshot(
                "head", "closed", False, frozenset(), 1, None
            )

            result = self.run_until_final_snapshot(directory, engine, merged)

            result["submit_review"].assert_called_once()
            result["submit_approval"].assert_not_called()
            self.assertEqual(result["record"]["status"], "delivered")
            self.assertTrue(result["record"]["closed_during_review"])
            self.assertIn(
                "delivering findings without an approval", result["terminal"]
            )

    def test_a_clean_result_is_not_approved_after_a_merge(self) -> None:
        """No LGTM on a pull request that is no longer open."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            }
            merged = PullRequestSnapshot(
                "head", "closed", False, frozenset(), 1, None
            )

            result = self.run_until_final_snapshot(directory, engine, merged)

            result["submit_approval"].assert_not_called()
            result["submit_review"].assert_not_called()
            self.assertEqual(result["record"]["status"], "skipped_ineligible")
            self.assertIn("nothing was delivered", result["terminal"])

    def test_a_draft_during_review_still_withholds_findings(self) -> None:
        """A draft is the author saying "not now", which delivery respects."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                self.defect_review_response(),
                CONFIRMED_JUDGMENT,
            ]
            drafted = PullRequestSnapshot(
                "head", "open", True, frozenset(), 1, True
            )

            result = self.run_until_final_snapshot(directory, engine, drafted)

            result["submit_review"].assert_not_called()
            result["submit_approval"].assert_not_called()
            self.assertEqual(result["record"]["status"], "skipped_ineligible")

    def test_final_snapshot_skips_delivery_when_pr_becomes_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            pull_request = PullRequest(1, "Title", "", "author", "head", "url")
            settings = self.settings(directory, repository)
            engine = Mock()
            review_response = {
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
            engine.review.side_effect = [review_response, CONFIRMED_JUDGMENT]
            draft_snapshot = PullRequestSnapshot(
                "head", "open", True, frozenset(), 1, True
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
                side_effect=[READY_SNAPSHOT, draft_snapshot],
            ), patch(
                "prwarden.cli.review_workspace"
            ) as workspace, patch("prwarden.cli.submit_review") as submit_review:
                (Path(directory) / "app.py").write_text("value = 1\n")
                workspace.return_value.__enter__.return_value = repository
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
                    key = store.key(repository.slug, 1, "head", "github")
                    self.assertEqual(
                        store.get(key)["status"], "skipped_ineligible"
                    )
                    self.assertFalse(store.is_terminal(key))

            submit_review.assert_not_called()

    def test_initial_ineligible_snapshot_is_a_retryable_skip(self) -> None:
        cases = (
            (
                "closed",
                PullRequestSnapshot(
                    "head", "closed", False, frozenset(), 1, False
                ),
                "pull request is no longer open",
            ),
            (
                "draft",
                PullRequestSnapshot(
                    "head", "open", True, frozenset(), 1, True
                ),
                "pull request became a draft during review",
            ),
            (
                "excluded label",
                PullRequestSnapshot(
                    "head", "open", False, frozenset({"in progress"}), 1, True
                ),
                "pull request gained an excluded label during review: in progress",
            ),
        )

        for name, snapshot, reason in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                repository = Repository("app", "owner/app", Path(directory))
                pull_request = PullRequest(
                    1, "Title", "", "author", "head", "url"
                )
                settings = self.settings(directory, repository)
                settings = Settings(
                    **{
                        **settings.__dict__,
                        "excluded_pr_labels": frozenset({"in progress"}),
                    }
                )
                engine = Mock()

                with patch(
                    "prwarden.cli.has_current_review", return_value=False
                ), patch(
                    "prwarden.cli.get_pull_request_snapshot", return_value=snapshot
                ), patch("sys.stdout", new_callable=StringIO) as output:
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
                        key = store.key(repository.slug, 1, "head", "github")
                        record = store.get(key)
                        self.assertEqual(record["status"], "skipped_ineligible")
                        self.assertEqual(record["reason"], reason)
                        self.assertFalse(store.is_terminal(key))

                self.assertIn(
                    f"Skipped: {reason}; nothing was delivered",
                    output.getvalue(),
                )
                engine.review.assert_not_called()

    def test_sequential_run_separates_consecutive_pull_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})
            pull_requests = [
                PullRequest(1, "First", "", "author", "head1", "url"),
                PullRequest(2, "Second", "", "author", "head2", "url"),
            ]
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
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=pull_requests), patch(
                "prwarden.cli._process_pull_request_with_head_restarts"
            ) as process, patch("sys.stdout", new_callable=StringIO) as output:
                process.side_effect = lambda *_, **__: print("    - PR entry")
                self.assertEqual(cli.run(args), 0)

        lines = output.getvalue().splitlines()
        divider_index = lines.index(cli.REPOSITORY_DIVIDER)
        self.assertEqual(lines[divider_index - 1], "")
        self.assertEqual(
            lines[divider_index + 1 :],
            [
                "Reviewing 2 pull requests, one at a time:",
                "    - app  #1 First by @author -> GITHUB",
                "    - app  #2 Second by @author -> GITHUB",
                "",
                "    - PR entry",
                "",
                "    - PR entry",
            ],
        )

    def test_the_plan_states_how_many_reviews_will_really_run_together(self) -> None:
        """A single pull request is never reviewed "3 at a time"."""

        repository = Repository("app", "owner/app", Path("."))
        cases = (
            (1, 3, "Reviewing 1 pull request:"),
            (1, 1, "Reviewing 1 pull request:"),
            (2, 3, "Reviewing 2 pull requests, 2 at a time:"),
            (3, 2, "Reviewing 3 pull requests, 2 at a time:"),
            (2, 1, "Reviewing 2 pull requests, one at a time:"),
        )
        for count, concurrency, heading in cases:
            with self.subTest(count=count, concurrency=concurrency):
                tasks = [
                    cli._ReviewTask(
                        repository,
                        PullRequest(number, "t", "", "author", "head", "url"),
                    )
                    for number in range(1, count + 1)
                ]
                with patch("sys.stdout", new_callable=StringIO) as output:
                    cli._print_review_plan(tasks, concurrency, "reviewer")

                self.assertIn(heading, output.getvalue().splitlines())

    def parallel_run_args(self, directory: str, concurrency: int) -> Namespace:
        return Namespace(
            env_file=Path(directory) / ".env",
            repositories_file=None,
            state_file=None,
            repo=[],
            pr=None,
            live=False,
            force=False,
            engine=None,
            concurrency=concurrency,
        )

    def test_run_reviews_pull_requests_up_to_the_concurrency_limit(self) -> None:
        """The pool must overlap reviews without exceeding the configured limit."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})
            pull_requests = [
                PullRequest(number, f"PR {number}", "", "author", f"h{number}", "url")
                for number in range(1, 5)
            ]
            active = 0
            peak = 0
            guard = threading.Lock()

            def fake_process(*args, **kwargs):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.05)
                with guard:
                    active -= 1

            args = self.parallel_run_args(directory, 2)
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=pull_requests), patch(
                "prwarden.cli._process_pull_request_with_head_restarts",
                side_effect=fake_process,
            ), patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(cli.run(args), 0)

        self.assertEqual(peak, 2)

    def test_run_prints_the_plan_then_the_log_in_planned_order(self) -> None:
        """A slow first review must not reorder the reported log."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})
            pull_requests = [
                PullRequest(1, "Slow one", "", "author", "h1", "url"),
                PullRequest(2, "Fast one", "", "author", "h2", "url"),
            ]

            def fake_process(settings_, repository_, pull_request, *args, **kwargs):
                if pull_request.number == 1:
                    time.sleep(0.15)
                print(f"      -> Review completed for #{pull_request.number}")

            args = self.parallel_run_args(directory, 2)
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=pull_requests), patch(
                "prwarden.cli._process_pull_request_with_head_restarts",
                side_effect=fake_process,
            ), patch("sys.stdout", new_callable=StringIO) as output:
                self.assertEqual(cli.run(args), 0)

        lines = [line for line in output.getvalue().splitlines() if line.strip()]
        plan_index = lines.index("Reviewing 2 pull requests, 2 at a time:")
        self.assertEqual(
            lines[plan_index + 1 : plan_index + 3],
            [
                "    - app  #1 Slow one by @author -> GITHUB",
                "    - app  #2 Fast one by @author -> GITHUB",
            ],
        )
        log_index = lines.index("Full review log:")
        reported = [line for line in lines[log_index:] if "Review completed" in line]
        self.assertEqual(
            reported,
            [
                "      -> Review completed for #1",
                "      -> Review completed for #2",
            ],
        )

    def test_run_keeps_each_parallel_review_report_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})
            pull_requests = [
                PullRequest(1, "First", "", "author", "h1", "url"),
                PullRequest(2, "Second", "", "author", "h2", "url"),
            ]
            started = threading.Barrier(2, timeout=10)

            def fake_process(settings_, repository_, pull_request, *args, **kwargs):
                number = pull_request.number
                print(f"      -> step one for #{number}")
                # Interleave the reviews so unseparated output would mix.
                started.wait()
                print(f"      -> step two for #{number}", file=sys.stderr)

            args = self.parallel_run_args(directory, 2)
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=pull_requests), patch(
                "prwarden.cli._process_pull_request_with_head_restarts",
                side_effect=fake_process,
            ), patch("sys.stdout", new_callable=StringIO) as output, patch(
                "sys.stderr", new_callable=StringIO
            ) as errors:
                self.assertEqual(cli.run(args), 0)

        block = output.getvalue().split("Full review log:")[1]
        self.assertLess(block.index("step one for #1"), block.index("step one for #2"))
        # Each review's stderr report replays to stderr, in its own order.
        reported = errors.getvalue()
        self.assertLess(
            reported.index("step two for #1"),
            reported.index("step two for #2"),
        )

    def test_run_reports_every_parallel_failure_in_repository_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})
            pull_requests = [
                PullRequest(1, "First", "", "author", "h1", "url"),
                PullRequest(2, "Second", "", "author", "h2", "url"),
            ]

            def fake_process(settings_, repository_, pull_request, *args, **kwargs):
                raise EngineError(f"engine failed for #{pull_request.number}")

            args = self.parallel_run_args(directory, 2)
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=pull_requests), patch(
                "prwarden.cli._process_pull_request_with_head_restarts",
                side_effect=fake_process,
            ), patch("sys.stdout", new_callable=StringIO) as output, patch(
                "sys.stderr", new_callable=StringIO
            ) as errors:
                self.assertEqual(cli.run(args), 1)

        self.assertIn("Full review log:", output.getvalue())
        reported = errors.getvalue()
        self.assertIn("engine failed for #1", reported)
        self.assertIn("engine failed for #2", reported)

    def crash_first_review(self, directory: str, concurrency: int, slack: bool = True):
        """Run two live reviews where the first one hits a defect in this tool."""

        repository = Repository("app", "owner/app", Path(directory), slack=slack)
        settings = self.settings(directory, repository)
        pull_requests = [
            PullRequest(1, "First", "", "author", "h1", "url"),
            PullRequest(2, "Second", "", "author", "h2", "url"),
        ]

        def fake_process(settings_, repository_, pull_request, *args, **kwargs):
            if pull_request.number == 1:
                raise AttributeError("'MovedFile' object has no attribute 'new_path'")
            print(f"      -> Review completed for #{pull_request.number}")

        args = Namespace(
            **{**vars(self.parallel_run_args(directory, concurrency)), "live": True}
        )
        with patch("prwarden.cli.load_settings", return_value=settings), patch(
            "prwarden.cli.create_review_engine"
        ), patch("prwarden.cli.list_open_prs", return_value=pull_requests), patch(
            "prwarden.cli._process_pull_request_with_head_restarts",
            side_effect=fake_process,
        ), patch(
            "prwarden.cli.send_slack_message", return_value="http-200"
        ) as send, patch("sys.stdout", new_callable=StringIO) as output, patch(
            "sys.stderr", new_callable=StringIO
        ) as errors:
            code = cli.run(args)
        return code, output.getvalue(), errors.getvalue(), send

    def test_a_defect_in_one_review_is_reported_and_the_run_carries_on(self) -> None:
        """A crash once ended four scheduled runs with no summary and no message."""

        for concurrency in (1, 2):
            with self.subTest(concurrency=concurrency):
                with tempfile.TemporaryDirectory() as directory:
                    code, output, errors, send = self.crash_first_review(
                        directory, concurrency
                    )

                self.assertEqual(code, 1)
                self.assertIn("Review completed for #2", output)
                # The traceback stays local, where the defect can be found.
                self.assertIn("Traceback (most recent call last)", errors)
                send.assert_called_once()
                self.assertIn(
                    "owner/app#1: crashed with AttributeError: 'MovedFile' object "
                    "has no attribute 'new_path'",
                    send.call_args.args[1],
                )

    def test_a_defect_in_a_repository_kept_out_of_slack_stays_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code, _, errors, send = self.crash_first_review(directory, 2, slack=False)

        self.assertEqual(code, 1)
        self.assertIn("crashed with AttributeError", errors)
        send.assert_not_called()

    def test_run_discovers_pull_requests_across_repositories_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Repository("app", "owner/app", Path(directory))
            second = Repository("api", "owner/api", Path(directory))
            settings = self.settings(directory, first)
            settings = Settings(
                **{**settings.__dict__, "dry_run": True, "repositories": [first, second]}
            )
            listings = {
                "owner/app": [PullRequest(1, "First", "", "author", "h1", "url")],
                "owner/api": [PullRequest(9, "Ninth", "", "author", "h9", "url")],
            }
            reviewed: "list[str]" = []

            def fake_process(settings_, repository_, pull_request, *args, **kwargs):
                reviewed.append(f"{repository_.slug}#{pull_request.number}")

            args = self.parallel_run_args(directory, 1)
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch(
                "prwarden.cli.list_open_prs",
                side_effect=lambda repository_, labels: listings[repository_.slug],
            ), patch(
                "prwarden.cli._process_pull_request_with_head_restarts",
                side_effect=fake_process,
            ), patch("sys.stdout", new_callable=StringIO) as output:
                self.assertEqual(cli.run(args), 0)

        lines = [line for line in output.getvalue().splitlines() if line.strip()]
        plan_index = lines.index("Reviewing 2 pull requests, one at a time:")
        self.assertEqual(
            lines[plan_index + 1 : plan_index + 3],
            [
                "    - app  #1 First by @author -> GITHUB",
                "    - api  #9 Ninth by @author -> GITHUB",
            ],
        )
        self.assertEqual(reviewed, ["owner/app#1", "owner/api#9"])

    def test_concurrency_override_must_stay_within_the_supported_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)

            self.assertEqual(cli._selected_concurrency(settings, None), 2)
            self.assertEqual(cli._selected_concurrency(settings, 5), 5)
            for invalid in (0, -1, cli.MAX_REVIEW_CONCURRENCY + 1):
                with self.assertRaisesRegex(ConfigError, "--concurrency must be"):
                    cli._selected_concurrency(settings, invalid)

    def test_board_reports_each_review_status_and_stays_in_plan_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            tasks = [
                cli._ReviewTask(
                    repository,
                    PullRequest(number, f"PR {number}", "", "author", f"h{number}", "u"),
                )
                for number in (7, 8)
            ]
            board = cli._ReviewBoard(tasks, StringIO())

            self.assertEqual(
                [row.split("  ")[1] for row in board._rows(columns=200)],
                ["app#7", "app#8"],
            )
            self.assertIn("queued", board._rows(columns=200)[0])

            recorder = progress_module.Recorder()
            recorder.record("      -> Running antigravity review\n", error=False)
            board.start(tasks[0], recorder)
            self.assertIn("Running antigravity review", board._rows(columns=200)[0])
            self.assertIn("queued", board._rows(columns=200)[1])
            aggregate = re.sub(r"\033\[[0-9;]*m", "", board._aggregate_row(0, columns=200) or "")
            self.assertIn("1 review in progress", aggregate)

            board.finish(tasks[0], "LGTM")
            self.assertIn("LGTM", board._rows(columns=200)[0])
            self.assertIsNone(board._aggregate_row(0, columns=200))

    def test_recording_replays_both_streams_in_reported_order(self) -> None:
        """One redirected log must keep each report inside its own block."""

        recorder = progress_module.Recorder()
        recorder.record("    - app #1\n", error=False)
        recorder.record("Transient failure; retrying\n", error=True)
        recorder.record("      -> Review completed\n", error=False)

        combined = StringIO()
        recorder.replay(combined, combined)

        self.assertEqual(
            combined.getvalue().splitlines(),
            [
                "    - app #1",
                "Transient failure; retrying",
                "      -> Review completed",
            ],
        )

    def board_rows(self, columns: str, lines: str) -> "list[str]":
        """Render one board frame for a terminal of the given size."""

        tasks = [
            cli._ReviewTask(
                Repository("example-app", "owner/example-app", Path(".")),
                PullRequest(number, "t", "", "author", "head", "url"),
            )
            for number in range(19, 12, -1)
        ]
        stream = StringIO()
        with patch.dict(os.environ, {"COLUMNS": columns, "LINES": lines}):
            board = cli._ReviewBoard(tasks, stream)
            recorder = progress_module.Recorder()
            recorder.record(
                "      -> Running antigravity review (timeout: 10m)...\n",
                error=False,
            )
            board.start(tasks[0], recorder)
            board._render(step=3)
        written = re.sub(r"\033\[[0-9;]*m", "", stream.getvalue())
        return [
            row
            for row in re.findall(r"\r\033\[2K([^\n]*)", written)
            if row.strip()
        ]

    def test_board_rows_never_reach_the_edge_of_a_narrow_terminal(self) -> None:
        """A wrapped row would desynchronize the cursor-up redraw."""

        for columns in ("44", "71", "80", "200"):
            rows = self.board_rows(columns, "40")
            self.assertTrue(rows)
            for row in rows:
                self.assertLess(len(row), int(columns), row)

    def test_board_summarizes_the_rows_a_short_window_cannot_show(self) -> None:
        """Scrolled-away rows would leave the redraw overwriting the wrong lines."""

        rows = self.board_rows("100", "6")
        self.assertLessEqual(len(rows), 6)
        self.assertIn("…and 4 more pull requests", rows[-2])

    def test_interrupted_parallel_run_still_reports_what_reviews_recorded(self) -> None:
        """Ctrl+C must not discard the output captured for the log."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})
            pull_requests = [
                PullRequest(number, f"PR {number}", "", "author", f"h{number}", "url")
                for number in (1, 2)
            ]

            def review(settings_, repository_, pull_request, *args, **kwargs):
                if pull_request.number == 2:
                    raise KeyboardInterrupt
                print(f"      -> Reviewed #{pull_request.number}")

            out = StringIO()
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=pull_requests), patch(
                "prwarden.cli._process_pull_request_with_head_restarts",
                side_effect=review,
            ), patch("sys.stdout", out):
                with self.assertRaises(KeyboardInterrupt):
                    cli.run(self.parallel_run_args(directory, 2))

            printed = out.getvalue()
            self.assertIn("Full review log (interrupted):", printed)
            self.assertIn("      -> Reviewed #1", printed)

    def test_interrupt_reports_the_signalled_exit_code(self) -> None:
        """An interrupted run is stopped on purpose, not a failure to report."""

        err = StringIO()
        with patch("prwarden.cli.build_parser") as parser, \
                patch("prwarden.cli.run", side_effect=KeyboardInterrupt), \
                patch("sys.stderr", err):
            parser.return_value.parse_args.return_value = Namespace()
            self.assertEqual(cli.main([]), cli.INTERRUPTED_EXIT_CODE)
        self.assertIn("Interrupted; stopped the run", err.getvalue())

    def test_run_keeps_a_transcript_of_what_it_reported(self) -> None:
        """A manual run's output only existed in the terminal it scrolled past."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})
            pull_requests = [
                PullRequest(1, "First", "", "author", "h1", "url"),
            ]

            def review(settings_, repository_, pull_request, *args, **kwargs):
                print("      -> Reviewed it")

            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=pull_requests), patch(
                "prwarden.cli._process_pull_request_with_head_restarts",
                side_effect=review,
            ), patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(cli.run(self.parallel_run_args(directory, 2)), 0)

            logs = sorted(
                (Path(directory) / cli.RUN_LOG_DIRECTORY_NAME).glob("run-*.log")
            )
            self.assertEqual(len(logs), 1)
            written = logs[0].read_text(encoding="utf-8")
            self.assertIn("=== Automated Code Review (DRY RUN) ===", written)
            self.assertIn("      -> Reviewed it", written)
            self.assertNotIn("\033[", written)

    def test_a_parallel_run_journals_each_step_while_it_is_still_running(self):
        """Nothing showed a running parallel run's progress until it ended."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})
            pull_requests = [
                PullRequest(1, "First", "", "author", "h1", "url"),
                PullRequest(2, "Second", "", "author", "h2", "url"),
            ]
            logs = Path(directory) / cli.RUN_LOG_DIRECTORY_NAME
            seen: "list[str]" = []

            def review(settings_, repository_, pull_request, *args, **kwargs):
                print(f"      -> Reviewing #{pull_request.number} now")
                # The transcript has to show the step before this review returns,
                # or a run in progress is again a black box.
                deadline = time.time() + 10
                while time.time() < deadline:
                    written = "".join(
                        found.read_text(encoding="utf-8")
                        for found in logs.glob("run-*.log")
                    )
                    if f"-> Reviewing #{pull_request.number} now" in written:
                        seen.append(written)
                        return
                    time.sleep(0.02)

            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=pull_requests), patch(
                "prwarden.cli._process_pull_request_with_head_restarts",
                side_effect=review,
            ), patch("sys.stdout", new_callable=StringIO):
                self.assertEqual(cli.run(self.parallel_run_args(directory, 2)), 0)

            self.assertEqual(len(seen), 2, "steps were not journaled live")
            written = sorted(logs.glob("run-*.log"))[0].read_text(encoding="utf-8")
            self.assertIn(
                "Live progress, timestamped as each step was reported:", written
            )
            # Each journaled step names its review and the time it was reported.
            self.assertRegex(
                written, r"\n\d\d:\d\d:\d\d  app #1  -> Reviewing #1 now\n"
            )
            self.assertRegex(
                written, r"\n\d\d:\d\d:\d\d  app #2  -> Reviewing #2 now\n"
            )
            # The grouped report still closes the transcript, unlabeled.
            self.assertIn("Full review log:", written)
            self.assertIn("      -> Reviewing #1 now", written)
            self.assertNotIn("\033[", written)

    def test_transcript_leaves_progress_rendering_on_the_terminal(self) -> None:
        """Redrawn frames in the file would make it unreadable."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-logs" / "run-1.log"
            terminal = StringIO()
            with patch("sys.stdout", terminal), patch("sys.stderr", StringIO()):
                with progress_module.transcript(path):
                    self.assertIs(
                        progress_module.terminal_stream(sys.stdout), terminal
                    )
                    print("reported text")
            self.assertIn("reported text", path.read_text(encoding="utf-8"))

    def test_transcript_never_overwrites_a_concurrent_runs_log(self) -> None:
        """Overlapping runs would otherwise truncate each other's evidence."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run-logs" / "run-20260821-120000-41.log"
            path.parent.mkdir(parents=True)
            path.write_text("another run's output\n", encoding="utf-8")

            with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
                with progress_module.transcript(path) as resolved:
                    print("this run's output")

            self.assertNotEqual(resolved, path)
            self.assertEqual(
                path.read_text(encoding="utf-8"), "another run's output\n"
            )
            self.assertIn("this run's output", resolved.read_text(encoding="utf-8"))

    def test_run_transcript_name_identifies_the_process(self) -> None:
        """A date and time alone cannot separate two runs of the same second."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})

            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=[]), patch(
                "sys.stdout", new_callable=StringIO
            ):
                cli.run(self.parallel_run_args(directory, 2))

            logs = list((Path(directory) / cli.RUN_LOG_DIRECTORY_NAME).glob("*.log"))
            self.assertEqual(len(logs), 1)
            self.assertRegex(logs[0].name, r"^run-\d{8}-\d{6}-\d+\.log$")
            self.assertIn(str(os.getpid()), logs[0].name)

    def test_a_transcript_is_readable_only_by_its_owner(self) -> None:
        """Transcripts hold repository names, PR titles, and finding text."""

        previous = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "run-logs" / "run-1.log"
                with patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()):
                    with progress_module.transcript(path) as resolved:
                        print("reported text")

                self.assertEqual(
                    stat.S_IMODE(resolved.stat().st_mode),
                    progress_module.TRANSCRIPT_FILE_MODE,
                )
                self.assertIn("reported text", resolved.read_text(encoding="utf-8"))
        finally:
            os.umask(previous)

    def test_transcripts_older_than_the_retention_window_are_deleted(self) -> None:
        """A run every few hours would otherwise keep every log forever."""

        with tempfile.TemporaryDirectory() as directory:
            log_directory = Path(directory)
            now = time.time()
            ages = {
                "run-20260825-080000-1.log": 0,
                "run-20260824-080000-2.log": 1,
                "run-20260819-080000-3.log": 6,
                "run-20260818-080000-4.log": 8,
                "run-20260701-080000-5.log": 55,
            }
            for name, days in ages.items():
                written = log_directory / name
                written.write_text("x", encoding="utf-8")
                stamp = now - days * progress_module.SECONDS_PER_DAY
                os.utime(written, (stamp, stamp))
            unrelated = log_directory / "keep-me.txt"
            unrelated.write_text("x", encoding="utf-8")
            os.utime(unrelated, (now - 90 * progress_module.SECONDS_PER_DAY,) * 2)

            progress_module.prune_transcripts(log_directory, 7)

            remaining = sorted(path.name for path in log_directory.glob("run-*.log"))
            self.assertEqual(
                remaining,
                [
                    "run-20260819-080000-3.log",
                    "run-20260824-080000-2.log",
                    "run-20260825-080000-1.log",
                ],
            )
            self.assertTrue(unrelated.exists())

    def test_pruning_rejects_a_window_shorter_than_a_day(self) -> None:
        """Zero days would delete the transcript of the run doing the pruning."""

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                progress_module.prune_transcripts(Path(directory), 0)

    def test_run_prunes_transcripts_with_the_configured_window(self) -> None:
        """The window is configuration, so the run must not hard-code one."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(
                **{**settings.__dict__, "dry_run": True, "run_log_retention_days": 3}
            )

            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=[]), patch(
                "prwarden.cli.progress.prune_transcripts"
            ) as prune, patch("sys.stdout", new_callable=StringIO):
                cli.run(self.parallel_run_args(directory, 2))

            prune.assert_called_once_with(
                Path(directory) / cli.RUN_LOG_DIRECTORY_NAME, 3
            )

    def test_board_stays_disabled_for_redirected_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            tasks = [
                cli._ReviewTask(
                    repository,
                    PullRequest(1, "PR 1", "", "author", "h1", "u"),
                )
            ]

            with patch("prwarden.cli.threading.Thread") as thread:
                with cli._ReviewBoard(tasks, StringIO()):
                    pass

            thread.assert_not_called()

    def test_live_flag_alone_cannot_enable_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})
            args = Namespace(
                env_file=Path(directory) / ".env",
                repositories_file=None,
                state_file=None,
                repo=[],
                pr=None,
                live=True,
                force=False,
                engine=None,
            )
            with patch("prwarden.cli.load_settings", return_value=settings):
                with self.assertRaisesRegex(
                    ConfigError, "requires AUTO_REVIEW_DRY_RUN=0"
                ):
                    cli.run(args)

    def test_a_reviewer_without_a_login_stops_before_github_is_read(self) -> None:
        """Reading as whichever account is active failed every repository."""

        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            args = Namespace(
                env_file=Path(directory) / ".env",
                repositories_file=None,
                state_file=None,
                repo=[],
                pr=None,
                live=True,
                force=False,
                engine=None,
            )
            refusal = CommandError("GitHub CLI has no login for reviewer", command="gh")
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.pin_github_account", side_effect=refusal
            ) as pin, patch("prwarden.cli.create_review_engine"), patch(
                "prwarden.cli.list_open_prs", return_value=[]
            ) as discover:
                with self.assertRaises(CommandError):
                    cli.run(args)

            pin.assert_called_once_with(
                settings.reviewer_username, settings.repositories
            )
            discover.assert_not_called()

    def test_dry_run_opens_state_without_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            settings = Settings(**{**settings.__dict__, "dry_run": True})
            args = Namespace(
                env_file=Path(directory) / ".env",
                repositories_file=None,
                state_file=None,
                repo=[],
                pr=None,
                live=False,
                force=False,
                engine=None,
            )
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.JsonStateStore"
            ) as state_store, patch("prwarden.cli.create_review_engine"), patch(
                "prwarden.cli.list_open_prs", return_value=[]
            ):
                state_store.return_value.__enter__.return_value = Mock()
                self.assertEqual(cli.run(args), 0)
            state_store.assert_called_once_with(settings.state_file, lock_enabled=False)

    def test_live_run_reports_github_incident_in_terminal_and_slack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            args = Namespace(
                env_file=Path(directory) / ".env",
                repositories_file=None,
                state_file=None,
                repo=[],
                pr=None,
                live=True,
                force=False,
                engine=None,
            )
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch(
                "prwarden.cli.list_open_prs",
                side_effect=CommandError(
                    "gh exited with 1: HTTP 503: No server is currently available",
                    command="gh",
                ),
            ), patch(
                "prwarden.cli.describe_github_incident",
                return_value="GitHub status: Partial System Outage | open incidents: "
                "Incident with GitHub.com (investigating)",
            ) as describe, patch(
                "prwarden.cli.send_slack_message", return_value="http-200"
            ) as send, patch(
                "sys.stderr", new_callable=StringIO
            ) as terminal:
                self.assertEqual(cli.run(args), 1)

            describe.assert_called_once_with()
            self.assertIn(
                "      -> GitHub status: Partial System Outage",
                terminal.getvalue(),
            )
            message = send.call_args.args[1]
            self.assertIn("HTTP 503", message)
            self.assertIn(
                "_GitHub status: Partial System Outage | open incidents: "
                "Incident with GitHub.com (investigating)_",
                message,
            )

    def test_live_run_sends_one_aggregated_failure_notification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = self.settings(directory, repository)
            args = Namespace(
                env_file=Path(directory) / ".env",
                repositories_file=None,
                state_file=None,
                repo=[],
                pr=None,
                live=True,
                force=False,
                engine=None,
            )
            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch(
                "prwarden.cli.list_open_prs",
                side_effect=cli.CommandError("error connecting to api.github.com"),
            ), patch(
                "prwarden.cli.send_slack_message", return_value="http-200"
            ) as send:
                self.assertEqual(cli.run(args), 1)

            send.assert_called_once()
            self.assertEqual(send.call_args.args[0], settings.slack_webhook_url)
            message = send.call_args.args[1]
            self.assertIn("*PR Warden live run completed with errors*", message)
            self.assertIn("1 review operation could not be completed:", message)
            self.assertNotIn("live run failed", message)
            self.assertIn("owner/app", message)
            self.assertIn("error connecting to api.github.com", message)

    def test_startup_failure_sends_notification_for_requested_live_run(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AUTO_REVIEW_DRY_RUN": "0",
                "SLACK_WEBHOOK_URL": "https://example.invalid/webhook",
            },
            clear=True,
        ), patch(
            "prwarden.cli.run",
            side_effect=ConfigError("Cannot resolve the current GitHub CLI user"),
        ), patch(
            "prwarden.cli.send_slack_message", return_value="http-200"
        ) as send:
            self.assertEqual(cli.main(["--live"]), 2)

        send.assert_called_once()
        self.assertIn("run:", send.call_args.args[1])

    def test_a_crash_outside_any_review_still_notifies(self) -> None:
        """Without a repository to attribute it to, the message names none.

        The error's own text could name a repository kept out of Slack, so only the
        defect's type and where it happened in this tool's code are sent.
        """

        def crash(args):
            raise KeyError("private-org/secret-repo")

        with patch.dict(
            "os.environ",
            {
                "AUTO_REVIEW_DRY_RUN": "0",
                "SLACK_WEBHOOK_URL": "https://example.invalid/webhook",
            },
            clear=True,
        ), patch("prwarden.cli.run", side_effect=crash), patch(
            "prwarden.cli.send_slack_message", return_value="http-200"
        ) as send, patch("sys.stderr", new_callable=StringIO) as errors:
            self.assertEqual(cli.main(["--live"]), 1)

        self.assertIn("Traceback (most recent call last)", errors.getvalue())
        self.assertIn("secret-repo", errors.getvalue())
        send.assert_called_once()
        message = send.call_args.args[1]
        self.assertIn("run: crashed with KeyError in tests/test_cli.py:", message)
        self.assertIn("(crash)", message)
        self.assertNotIn("secret-repo", message)

    def test_a_crash_in_a_dry_run_notifies_nobody(self) -> None:
        with patch.dict(
            "os.environ",
            {"SLACK_WEBHOOK_URL": "https://example.invalid/webhook"},
            clear=True,
        ), patch("prwarden.cli.run", side_effect=KeyError("x")), patch(
            "prwarden.cli.send_slack_message"
        ) as send, patch("sys.stderr", new_callable=StringIO):
            self.assertEqual(cli.main([]), 1)

        send.assert_not_called()

    def test_an_overlapping_run_is_a_safe_skip_and_never_notifies(self) -> None:
        """Meeting a manual run is the lock working, not something to report.

        The operator started that run, so paging them about it would be noise,
        and a scheduled slot that correctly stood down did not fail.
        """

        stream = StringIO()
        with patch.dict(
            "os.environ",
            {
                "AUTO_REVIEW_DRY_RUN": "0",
                "SLACK_WEBHOOK_URL": "https://example.invalid/webhook",
            },
            clear=True,
        ), patch(
            "prwarden.cli.run",
            side_effect=StateLockedError(
                "Another reviewer process holds .state/review-state.json.lock"
            ),
        ), patch(
            "prwarden.cli.send_slack_message"
        ) as send, patch(
            "sys.stdout", stream
        ):
            self.assertEqual(cli.main(["--live"]), 0)

        send.assert_not_called()
        self.assertIn("Skipped: Another reviewer process holds", stream.getvalue())

    def test_an_unusable_state_file_is_still_a_failure(self) -> None:
        """Only the lock collision is safe; a state file that cannot be used is not."""

        with patch.dict(
            "os.environ",
            {
                "AUTO_REVIEW_DRY_RUN": "0",
                "SLACK_WEBHOOK_URL": "https://example.invalid/webhook",
            },
            clear=True,
        ), patch(
            "prwarden.cli.run", side_effect=StateError("Cannot write state file")
        ), patch(
            "prwarden.cli.send_slack_message", return_value="http-200"
        ) as send, patch(
            "sys.stderr", StringIO()
        ):
            self.assertEqual(cli.main(["--live"]), 1)

        send.assert_called_once()

    def test_dry_run_failure_does_not_send_notification(self) -> None:
        with patch.dict("os.environ", {}, clear=True), patch(
            "prwarden.cli.run",
            side_effect=ConfigError("Cannot resolve the current GitHub CLI user"),
        ), patch("prwarden.cli.send_slack_message") as send:
            self.assertEqual(cli.main([]), 2)

        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
