import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, TextIO, Union

from . import __version__, progress
from .comment_template import CommentTemplateError
from .commands import CommandError
from .config import (
    MAX_REVIEW_CONCURRENCY,
    ConfigError,
    Settings,
    load_settings,
)
from .delivery import (
    DeliveryError,
    DeliveryUncertainError,
    format_design_slack_message,
    format_docs_slack_message,
    format_manual_review_message,
    format_slack_lgtm_message,
    format_slack_message,
    format_stale_review_message,
    send_slack_message,
)
from .engine import (
    code_candidate_ref,
    is_exhausted_quota,
    DESIGN_LEVEL,
    DOCS_LEVEL,
    create_fallback_engine,
    fallback_engine_settings,
    EngineError,
    ReviewEngine,
    create_review_engine,
    create_verification_engine,
    take_invocation_credits,
    verification_engine_settings,
    verify_fallback_engine_settings,
)
from .github import (
    DESIGN_COMMENT_MARKER,
    DOCS_COMMENT_MARKER,
    MAX_PRIOR_REVIEW_BYTES,
    delivered_review_findings_resolved,
    format_design_comment,
    format_docs_comment,
    format_lgtm_comment,
    get_branch_head_sha,
    get_pull_request_snapshot,
    has_current_review,
    hide_earlier_lgtm_comments,
    list_changed_files,
    list_closing_issues,
    list_open_prs,
    list_own_advisory_notes,
    list_pull_request_checks,
    list_prior_review_comments,
    pin_github_account,
    reviewer_account,
    submit_approval,
    submit_design_notes,
    submit_docs_notes,
    submit_lgtm_comment,
    submit_review,
    validate_object_name,
)
from .github_status import describe_github_incident
from .models import (
    DesignAssessment,
    DocsAssessment,
    DiffBudget,
    LinkedIssue,
    HistoryCommit,
    Finding,
    PriorReviewComment,
    PullRequest,
    PullRequestSnapshot,
    Repository,
    ReviewMaterial,
    ReviewRules,
)
from .linked_issues import find_jira_keys, read_work_item
from .metrics import ReviewMetrics
from .paths import PACKAGE_ROOT
from .review import (
    build_bundled_review_material,
    BACKGROUND_FILE,
    UNCLASSIFIED_REJECTION,
    CandidateRejection,
    DiffTooLargeError,
    ReviewError,
    build_review_material,
    collect_rejection_reasons,
    discover_documentation,
    failed_checks,
    documentation_change_fingerprint,
    dominant_directories,
    read_background,
    collect_review_rules,
    format_byte_size,
    history_paths,
    ignores_omitted_patch,
    is_review_rule_source,
    omitted_patch_paths,
    review_rule_pathspecs,
    validate_approval_reason,
    validate_approval_verdict,
    validate_blocking_comment_ids,
    validate_code_judgments,
    validate_design_findings,
    validate_design_judgments,
    validate_docs_findings,
    validate_docs_judgments,
    validate_findings,
)
from .reviewer_identity import format_review_attribution, format_review_plan
from .state import JsonStateStore, StateError, StateLockedError
from .workspace import (
    UnsupportedGitError,
    WorkspaceError,
    read_base_files,
    read_file_history,
    read_generated_marks,
    review_workspace,
)


MAX_FAILURE_NOTIFICATION_ERRORS = 10
# Slack accepts about 40,000 characters in one incoming-webhook message and drops
# what will not fit, so the whole notification is bounded rather than each error in
# it. An error cut in the middle hid the part that differed, which is the only part
# worth reading; dropping a whole trailing error says plainly that it was dropped.
MAX_FAILURE_NOTIFICATION_CHARACTERS = 39000
MAX_OUTPUT_VALIDATION_ATTEMPTS = 2
MERGEABILITY_RECHECK_DELAYS_SECONDS = (5, 15, 30, 60)
MAX_HEAD_CHANGE_RESTARTS = 5
REPOSITORY_DIVIDER = "-" * 36
REVIEW_FAILURES = (
    CommandError,
    CommentTemplateError,
    ConfigError,
    DeliveryError,
    DeliveryUncertainError,
    EngineError,
    ReviewError,
    StateError,
    WorkspaceError,
)
PROGRESS_DELAY_SECONDS = 5.0
PROGRESS_INTERVAL_SECONDS = 0.08
PROGRESS_HIGHLIGHT_WIDTH = 7
BOARD_STATUS_WIDTH = 52
BOARD_MIN_STATUS_WIDTH = 12
BOARD_ELAPSED_WIDTH = 7
BOARD_FALLBACK_TERMINAL_SIZE = (80, 24)
INTERRUPTED_EXIT_CODE = 130
RUN_LOG_DIRECTORY_NAME = "run-logs"
# Commands that look after this installation instead of reviewing.
ADMIN_COMMANDS = ("doctor", "schedule", "setup")
PLAN_TITLE_WIDTH = 60
# The progress line is styled with the terminal's own text colour, never a palette
# colour: a theme decides what each palette entry looks like, and Solarized Dark sets
# bright black to its background, which made the dim part of the line invisible.
# Faint and bold are drawn relative to the foreground, so they stay readable in any
# theme. 22 clears both before each change, so the two never combine.
PROGRESS_DIM_STYLE = "\033[22;2m"
PROGRESS_HIGHLIGHT_STYLE = "\033[22;1m"
PROGRESS_STYLE_RESET = "\033[0m"


class PullRequestHeadChangedError(ReviewError):
    """Signal that analysis must restart against a newer pull-request head."""

    def __init__(self, expected_head_sha: str, found_head_sha: str):
        self.expected_head_sha = expected_head_sha
        self.found_head_sha = found_head_sha
        super().__init__(
            "Pull-request head changed during review: "
            f"expected {expected_head_sha}, found {found_head_sha}"
        )


class PullRequestIneligibleError(ReviewError):
    """Signal that a pull request became ineligible before delivery."""


class PullRequestClosedError(PullRequestIneligibleError):
    """Signal that a pull request closed or merged before delivery.

    Separated from the other eligibility changes because it means something
    different. A draft or an excluded label is the author saying "not now", so
    delivery stops. A merge only ends the *approval* decision: a confirmed defect
    is still in the branch it was merged into, and the reviewer already found it,
    so the finding is delivered and the approval is not.
    """


def _recorded_unread_paths(record: "dict[str, Any]") -> "tuple[str, ...]":
    """Return the paths a stored withheld approval reports as unread."""

    recorded = record.get("unread_paths")
    if not isinstance(recorded, list) or not recorded:
        return ()
    if not all(isinstance(path, str) and path for path in recorded):
        return ()
    return tuple(recorded)


def _recorded_failed_checks(record: "dict[str, Any]") -> "tuple[str, ...]":
    """Return the checks a stored withheld approval was withheld over."""

    recorded = record.get("failed_checks")
    if not isinstance(recorded, list) or not recorded:
        return ()
    if not all(isinstance(name, str) and name for name in recorded):
        return ()
    return tuple(recorded)


def _describe_failed_checks(names: "Sequence[str]") -> str:
    word = "check" if len(names) == 1 else "checks"
    return f"{word} {', '.join(names)}"


def _unread_settings_fingerprint(settings: Settings) -> str:
    """Return a fingerprint of the settings that decide what goes unread.

    For one immutable head, which files GitHub refuses to diff and which files
    exceed the byte budget are decided by that head, these two settings, and how
    the base branch marks the omitted files as generated. The marks live in the
    repository rather than in the settings, so they are recorded beside this as
    `unread_generated_marks` and compared on their own. A matching fingerprint and
    matching marks prove another review would defer the same files and withhold the
    approval for the same reason. Anything else about the run may differ freely,
    including the engine and the prompt.
    """

    return json.dumps(
        {
            "max_diff_bytes": settings.max_diff_bytes,
            "ignored_missing_patch_patterns": list(
                settings.ignored_missing_patch_patterns
            ),
        },
        sort_keys=True,
    )


def _feedback_fingerprint(comments: tuple[PriorReviewComment, ...]) -> str:
    payload = [
        {
            "id": comment.comment_id,
            "kind": comment.kind,
            "author": comment.author,
            "body": comment.body,
            "path": comment.path,
            "line": comment.line,
            "resolved": comment.resolved,
        }
        for comment in comments
    ]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _format_duration(seconds: float) -> str:
    total_seconds = int(max(0.0, seconds) + 0.5)
    minutes, seconds_part = divmod(total_seconds, 60)
    hours, minutes_part = divmod(minutes, 60)
    if hours:
        formatted = f"{hours}h"
        if minutes_part:
            formatted += f"{minutes_part:02d}m"
        if seconds_part:
            formatted += f"{seconds_part:02d}s"
        return formatted
    if minutes:
        return f"{minutes}m{seconds_part:02d}s" if seconds_part else f"{minutes}m"
    return f"{seconds_part}s"


def _clamp(text: str, width: int) -> str:
    """Trim ``text`` to ``width`` characters without disturbing its spacing.

    Board rows are padded into columns, so they cannot be collapsed the way a
    free-text status can.
    """
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _shorten(text: str, width: int) -> str:
    """Trim ``text`` to ``width`` characters, marking anything cut away."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= width:
        return collapsed
    return collapsed[: max(width - 1, 0)] + "…"


def _highlight_sweep(text: str, offset: int, width: int) -> str:
    """Dim a progress line and embolden a window of characters at ``offset``."""
    start = min(max(offset, 0), len(text))
    end = min(max(offset + width, 0), len(text))
    if start >= end:
        return f"{PROGRESS_DIM_STYLE}{text}{PROGRESS_STYLE_RESET}"
    return (
        PROGRESS_DIM_STYLE
        + text[:start]
        + PROGRESS_HIGHLIGHT_STYLE
        + text[start:end]
        + PROGRESS_DIM_STYLE
        + text[end:]
        + PROGRESS_STYLE_RESET
    )


class _ElapsedProgressLine:
    """Show delayed in-place progress without polluting redirected logs."""

    def __init__(
        self,
        phase_title: str,
        started: float,
        stream: Optional[TextIO] = None,
        highlight_width: int = PROGRESS_HIGHLIGHT_WIDTH,
    ) -> None:
        if highlight_width <= 0:
            raise ValueError("Progress highlight width must be positive")
        self.phase_title = phase_title
        self.started = started
        self.stream = progress.progress_stream(
            stream if stream is not None else sys.stdout
        )
        self.highlight_width = highlight_width
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._displayed = False

    def __enter__(self) -> "_ElapsedProgressLine":
        try:
            interactive = self.stream.isatty()
        except (AttributeError, OSError):
            interactive = False
        progress.set_active(self)
        if interactive:
            self._thread = threading.Thread(
                target=self._animate,
                name="prwarden-progress",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        progress.release_active(self)
        with progress.rendering():
            if self._displayed:
                self._clear()

    def _animate(self) -> None:
        if self._stop.wait(PROGRESS_DELAY_SECONDS):
            return
        step = self.highlight_width - 1
        while not self._stop.is_set():
            self._display_frame(step)
            step += 1
            if self._stop.wait(PROGRESS_INTERVAL_SECONDS):
                break

    def _display_frame(self, step: int) -> None:
        elapsed = _format_duration(time.monotonic() - self.started)
        line = f"{self.phase_title} in progress — {elapsed} elapsed"
        # Keep at least one bright character on screen at every step so the
        # sweep never blinks fully dim between cycles.
        span = len(line) + self.highlight_width - 1
        offset = (step % span) - (self.highlight_width - 1)
        with progress.rendering():
            try:
                self.stream.write(
                    "\r\033[2K      "
                    + _highlight_sweep(line, offset, self.highlight_width)
                )
                self.stream.flush()
                self._displayed = True
            except (OSError, ValueError):
                self._stop.set()

    def suspend(self) -> None:
        """Erase the line so an interrupting message starts on a clean row."""
        with progress.rendering():
            if self._displayed:
                self._clear()
                self._displayed = False

    def _clear(self) -> None:
        try:
            self.stream.write("\r\033[2K")
            self.stream.flush()
        except (OSError, ValueError):
            pass


class _ReviewBoard:
    """Show one row per selected pull request while reviews run in parallel.

    Each row reports that review's latest reported step, taken from its own
    recorded output, so the board needs no separate progress protocol. The rows
    stay in the planned order rather than the order reviews happen to finish.
    """

    def __init__(
        self,
        tasks: "Sequence[_ReviewTask]",
        stream: Optional[TextIO] = None,
        highlight_width: int = PROGRESS_HIGHLIGHT_WIDTH,
    ) -> None:
        self.tasks = list(tasks)
        base = stream if stream is not None else sys.stdout
        self.stream = progress.progress_stream(base)
        self.highlight_width = highlight_width
        self._recorders: "dict[str, progress.Recorder]" = {}
        self._started: "dict[str, float]" = {}
        self._outcomes: "dict[str, tuple[str, float]]" = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._rendered_rows = 0

    @staticmethod
    def key(task: "_ReviewTask") -> str:
        return f"{task.repository.slug}#{task.pull_request.number}"

    def label(self, task: "_ReviewTask") -> str:
        return f"{task.repository.name}#{task.pull_request.number}"

    def __enter__(self) -> "_ReviewBoard":
        try:
            interactive = self.stream.isatty()
        except (AttributeError, OSError):
            interactive = False
        if interactive:
            self._thread = threading.Thread(
                target=self._animate,
                name="prwarden-board",
                daemon=True,
            )
            self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            # Leave the finished board on screen as the run's status summary.
            self._render(final=True)

    def start(self, task: "_ReviewTask", recorder: progress.Recorder) -> None:
        with self._lock:
            self._recorders[self.key(task)] = recorder
            self._started[self.key(task)] = time.monotonic()

    def finish(self, task: "_ReviewTask", outcome: str) -> None:
        key = self.key(task)
        with self._lock:
            started = self._started.get(key, time.monotonic())
            self._outcomes[key] = (outcome, time.monotonic() - started)

    def _status(self, task: "_ReviewTask") -> tuple[str, str]:
        key = self.key(task)
        outcome = self._outcomes.get(key)
        if outcome is not None:
            return outcome[0], _format_duration(outcome[1])
        started = self._started.get(key)
        if started is None:
            return "queued", ""
        recorder = self._recorders.get(key)
        step = recorder.last_step() if recorder is not None else ""
        return step or "starting", _format_duration(time.monotonic() - started)

    def _terminal_size(self) -> "tuple[int, int]":
        """Return the terminal's current columns and lines.

        The size is read for every frame because a window can be resized while
        reviews run.
        """
        try:
            size = os.get_terminal_size(self.stream.fileno())
        except (AttributeError, OSError, ValueError):
            size = shutil.get_terminal_size(BOARD_FALLBACK_TERMINAL_SIZE)
        return size.columns, size.lines

    def _rows(self, columns: int) -> "list[str]":
        """Render one row per pull request, none wider than the terminal.

        A row that reaches the last column makes the terminal wrap it onto a
        second line, which the cursor-up redraw does not account for, so the
        board would overwrite itself. Every row is therefore built for, and
        finally clamped to, the width actually available.
        """
        budget = max(columns - 1, BOARD_MIN_STATUS_WIDTH)
        with self._lock:
            width = max((len(self.label(task)) for task in self.tasks), default=0)
            reserved = len("  ") + width + len("  ") * 2 + BOARD_ELAPSED_WIDTH
            status_width = min(
                BOARD_STATUS_WIDTH,
                max(budget - reserved, BOARD_MIN_STATUS_WIDTH),
            )
            rows = []
            for task in self.tasks:
                status, elapsed = self._status(task)
                status = _shorten(status, status_width)
                row = (
                    f"  {self.label(task):<{width}}  "
                    f"{status:<{status_width}}  "
                    f"{elapsed:>{BOARD_ELAPSED_WIDTH}}"
                ).rstrip()
                rows.append(_clamp(row, budget))
            return rows

    def _aggregate_row(self, step: int, columns: int) -> Optional[str]:
        with self._lock:
            active = [
                started
                for key, started in self._started.items()
                if key not in self._outcomes
            ]
        if not active:
            return None
        elapsed = _format_duration(time.monotonic() - min(active))
        reviews = "review" if len(active) == 1 else "reviews"
        line = _clamp(
            f"{len(active)} {reviews} in progress — {elapsed} elapsed",
            max(columns - len("  ") - 1, 1),
        )
        span = len(line) + self.highlight_width - 1
        offset = (step % span) - (self.highlight_width - 1)
        return "  " + _highlight_sweep(line, offset, self.highlight_width)

    def _animate(self) -> None:
        step = self.highlight_width - 1
        while not self._stop.is_set():
            self._render(step=step)
            step += 1
            if self._stop.wait(PROGRESS_INTERVAL_SECONDS):
                break

    def _render(self, step: int = 0, *, final: bool = False) -> None:
        columns, lines = self._terminal_size()
        rows = self._rows(columns)
        aggregate = None if final else self._aggregate_row(step, columns)
        # Redrawing by cursor movement only works while the whole board is on
        # screen, so summarize the overflow rather than scroll out of reach.
        capacity = max(lines - (2 if aggregate is not None else 1), 1)
        if len(rows) > capacity:
            hidden = len(rows) - capacity + 1
            rows = rows[: capacity - 1]
            rows.append(_clamp(f"  …and {hidden} more pull requests", columns - 1))
        if aggregate is not None:
            rows.append(aggregate)
        with progress.rendering():
            try:
                if self._rendered_rows:
                    self.stream.write(f"\033[{self._rendered_rows}A")
                for row in rows:
                    self.stream.write(f"\r\033[2K{row}\n")
                # Erase rows the board no longer needs, such as the aggregate
                # row after the last review finishes.
                for _ in range(max(self._rendered_rows - len(rows), 0)):
                    self.stream.write("\r\033[2K\n")
                self.stream.flush()
                self._rendered_rows = max(self._rendered_rows, len(rows))
                if final:
                    self._rendered_rows = 0
            except (OSError, ValueError):
                self._stop.set()


def _review_with_validation_retry(
    settings: Settings,
    engine: ReviewEngine,
    repository: Repository,
    pull_request: PullRequest,
    prompt_material: ReviewMaterial,
    validation_material: ReviewMaterial,
    verification_candidates: Sequence[Finding] = (),
    *,
    phase: str,
    timeout_seconds: Optional[int] = None,
) -> tuple[Any, list[Finding], bool, str, tuple[str, ...]]:
    """Run one review phase and retry once when controller validation rejects it.

    ``timeout_seconds`` overrides the configured per-invocation budget for this
    phase, which is how a verification pass is given time proportional to the
    number of candidates it has to check.
    """
    current_material = prompt_material
    phase_title = phase.capitalize()
    for attempt in range(1, MAX_OUTPUT_VALIDATION_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            with _ElapsedProgressLine(phase_title, started):
                structured_output = engine.review(
                    repository,
                    pull_request,
                    current_material,
                    timeout_seconds=timeout_seconds,
                )
        except Exception:
            elapsed = _format_duration(time.monotonic() - started)
            print(f"      -> {phase_title} failed after {elapsed}", flush=True)
            raise
        elapsed = _format_duration(time.monotonic() - started)
        print(f"      -> {phase_title} completed in {elapsed}", flush=True)

        try:
            if verification_candidates:
                findings = validate_code_judgments(
                    structured_output,
                    verification_candidates,
                )
            else:
                findings = validate_findings(
                    structured_output,
                    validation_material,
                    settings.max_findings,
                    repository.path,
                )
            approve = validate_approval_verdict(structured_output, findings)
            approval_reason = validate_approval_reason(structured_output)
            blocking_comment_ids = validate_blocking_comment_ids(
                structured_output,
                findings,
                approve,
                validation_material.prior_comments,
            )
            return (
                structured_output,
                findings,
                approve,
                approval_reason,
                blocking_comment_ids,
            )
        except ReviewError as error:
            if attempt >= MAX_OUTPUT_VALIDATION_ATTEMPTS:
                print(
                    f"      -> {phase_title} correction retry rejected: {error}",
                    flush=True,
                )
                raise
            print(
                f"      -> {phase_title} output rejected: {error}",
                flush=True,
            )
            print(
                f"      -> Retrying {phase} once with controller validation feedback...",
                flush=True,
            )
            current_material = replace(
                prompt_material,
                validation_error=str(error),
            )

    raise AssertionError("unreachable review validation retry state")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prwarden",
        description=(
            "Review configured GitHub pull requests with a pluggable review engine."
        ),
        epilog="Other commands: `prwarden setup` writes a configuration, "
        "`prwarden doctor` checks it, and `prwarden schedule` reviews on a timer.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"PR Warden {__version__}",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help=(
            "dotenv file to load (default: .env in the configuration directory: "
            "the checkout root, $PRWARDEN_HOME, or ~/.config/prwarden)"
        ),
    )
    parser.add_argument(
        "--repositories-file",
        type=Path,
        help="repository JSON file; overrides REPOSITORIES_FILE",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        help="JSON state file; overrides STATE_FILE",
    )
    parser.add_argument(
        "--engine",
        choices=("antigravity", "codex", "claude", "kiro", "command"),
        help="review engine for this run; overrides REVIEW_ENGINE",
    )
    parser.add_argument(
        "--model",
        help=(
            "model for the review engine; overrides REVIEW_MODEL. A model ID belongs "
            "to one provider, so name it alongside --engine when changing both"
        ),
    )
    parser.add_argument(
        "--verify-engine",
        choices=("antigravity", "codex", "claude", "kiro"),
        help="engine that verifies candidate findings; overrides REVIEW_VERIFY_ENGINE",
    )
    parser.add_argument(
        "--verify-model",
        help="model for the verification engine; overrides REVIEW_VERIFY_MODEL",
    )
    parser.add_argument(
        "--fallback-engine",
        choices=("antigravity", "codex", "claude", "kiro"),
        help=(
            "engine that reviews when the primary fails; overrides "
            "REVIEW_FALLBACK_ENGINE"
        ),
    )
    parser.add_argument(
        "--fallback-model",
        help="model for the fallback engine; overrides REVIEW_FALLBACK_MODEL",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help=(
            "review with the primary engine alone, so its failure is reported "
            "rather than covered by another engine"
        ),
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="review only a repository name or owner/repository slug (repeatable)",
    )
    parser.add_argument("--pr", type=int, help="review only one pull-request number")
    parser.add_argument(
        "--live",
        action="store_true",
        help="enable delivery; also requires AUTO_REVIEW_DRY_RUN=0",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore terminal JSON state and process the selected head again",
    )
    parser.add_argument(
        "--metrics",
        metavar="PATH",
        help=(
            "write what this run's reviews decided to PATH as JSON, for comparing "
            "one prompt or model against another"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        help=(
            "pull requests to review at the same time, overriding "
            f"REVIEW_CONCURRENCY (1 to {MAX_REVIEW_CONCURRENCY})"
        ),
    )
    return parser


def _selected_concurrency(settings: Settings, requested: Optional[int]) -> int:
    """Resolve how many pull requests may be reviewed at the same time."""
    if requested is None:
        return settings.review_concurrency
    if not 1 <= requested <= MAX_REVIEW_CONCURRENCY:
        raise ConfigError(
            "--concurrency must be between 1 and "
            f"{MAX_REVIEW_CONCURRENCY}, got {requested}"
        )
    return requested


def _selected_repositories(
    repositories: list[Repository],
    requested: list[str],
) -> list[Repository]:
    if not requested:
        return repositories
    requested_set = set(requested)
    selected = [
        repository
        for repository in repositories
        if repository.name in requested_set or repository.slug in requested_set
    ]
    matched = {
        value for repository in selected for value in (repository.name, repository.slug)
    }
    missing = sorted(value for value in requested_set if value not in matched)
    if missing:
        raise ConfigError(f"Unknown repository filter: {', '.join(missing)}")
    return selected


# Where the reviewer's own pull request goes when its repository keeps out of Slack.
# Its own destination rather than "github": GitHub forbids approving your own pull
# request, so nothing on this route may reach an approval, and the state key keeps
# it apart from a teammate's review of the same head.
OWN_GITHUB_DESTINATION = "github-own"
_DESTINATION_LABELS = {
    "github": "GITHUB",
    "slack": "SLACK",
    OWN_GITHUB_DESTINATION: "GITHUB, own pull request",
}


def _destination(
    repository: Repository, pull_request: PullRequest, reviewer_username: str
) -> str:
    # GitHub compares logins without regard to case, and a hand-typed account in a
    # repository's entry must not turn the account's own pull request into a
    # teammate's, which is the route that approves.
    account = reviewer_account(repository, reviewer_username)
    if pull_request.author.casefold() != account.casefold():
        return "github"
    return "slack" if repository.slack else OWN_GITHUB_DESTINATION


def _repository_header(text: str) -> str:
    """Return a repository section header preceded by a blank line and divider."""

    return f"\n{REPOSITORY_DIVIDER}\n{text}"


def _is_github_command_failure(error: BaseException) -> bool:
    return isinstance(error, CommandError) and error.command == "gh"


def _fallback_identity(settings: Settings) -> tuple[Optional[str], Optional[str]]:
    """Return the configured fallback engine and model, or (None, None)."""

    fallback_settings = fallback_engine_settings(settings)
    if fallback_settings is None:
        return None, None
    return fallback_settings.review_engine, fallback_settings.review_model


def _verify_fallback_identity(
    settings: Settings,
) -> "tuple[Optional[str], Optional[str], Optional[str]]":
    """Return the verifier's configured fallback engine, model, and effort."""

    standby = verify_fallback_engine_settings(settings)
    if standby is None:
        return None, None, None
    return standby.review_engine, standby.review_model, standby.review_effort


def _verifier_identity(settings: Settings) -> tuple[Optional[str], Optional[str]]:
    """Return the configured verifying engine and model, or (None, None).

    A single-engine run reports one reviewer. Callers that describe completed
    work must also confirm the pass ran, because verification only happens for
    a review that produced candidate findings.
    """

    verification_settings = verification_engine_settings(settings)
    if verification_settings is None:
        return None, None
    return verification_settings.review_engine, verification_settings.review_model


def _engine_label(settings: Settings) -> str:
    if settings.review_model:
        return f"{settings.review_engine} ({settings.review_model})"
    return settings.review_engine


@dataclass(frozen=True)
class FallbackReviewer:
    """The engine and settings that retry a review after an engine failure."""

    engine: ReviewEngine
    settings: Settings


def _create_fallback_reviewer(settings: Settings) -> Optional[FallbackReviewer]:
    """Return the configured fallback reviewer, or None when there is none."""

    fallback_settings = fallback_engine_settings(settings)
    if fallback_settings is None:
        return None
    engine = create_fallback_engine(settings)
    assert engine is not None
    return FallbackReviewer(engine=engine, settings=fallback_settings)


@dataclass(frozen=True)
class VerificationPass:
    """The engine and settings that run the independent verification pass.

    ``fallback`` is the verifier that takes over when this one fails at the engine
    level, or None when there is none.
    """

    engine: ReviewEngine
    settings: Settings
    fallback: Optional["VerificationPass"] = None


def _create_verification_pass(
    settings: Settings,
    engine: ReviewEngine,
) -> Optional[VerificationPass]:
    """Return a separate verification pass when one is configured."""

    verification_settings = verification_engine_settings(settings)
    if verification_settings is None:
        return None
    fallback_settings = verify_fallback_engine_settings(settings)
    return VerificationPass(
        engine=create_verification_engine(settings, engine),
        settings=verification_settings,
        fallback=(
            VerificationPass(
                engine=create_review_engine(fallback_settings),
                settings=fallback_settings,
            )
            if fallback_settings is not None
            else None
        ),
    )


class _FallbackVerifier:
    """The verification engine of one pull request, handed to its fallback once.

    Kiro served both review and verification, so while it was down the review
    fallback still reviewed and nothing it found could be verified or delivered; a
    workspace Kiro refused cost the verification the same way. Only an engine
    failure hands over, as for the reviewer: a malformed answer stays with the
    engine the controller tells how to correct it. Once handed over, the rest of
    the pull request's verification stays on the fallback, so a correction retry
    reaches the engine whose answer it corrects.
    """

    def __init__(self, verification: VerificationPass) -> None:
        assert verification.fallback is not None
        self.primary_label = _engine_label(verification.settings)
        self.primary_error: Optional[EngineError] = None
        self.served_settings = verification.settings
        self.engine = verification.engine
        self.fallback: Optional[VerificationPass] = verification.fallback

    @property
    def handed_over(self) -> bool:
        return self.primary_error is not None

    def review(
        self,
        repository: Repository,
        pull_request: PullRequest,
        material: ReviewMaterial,
        **kwargs: Any,
    ) -> Any:
        try:
            return self.engine.review(repository, pull_request, material, **kwargs)
        except EngineError as error:
            if self.fallback is None:
                if self.primary_error is not None:
                    raise EngineError(
                        f"{self.primary_label} verification failed: "
                        f"{self.primary_error}; {_engine_label(self.served_settings)} "
                        f"fallback also failed: {error}"
                    ) from error
                raise
            print(
                f"      -> {self.primary_label} verification engine failed: {error}",
                flush=True,
            )
            self.primary_error = error
            self.served_settings = self.fallback.settings
            self.engine = self.fallback.engine
            self.fallback = None
            print(
                f"      -> Verifying with fallback "
                f"{_engine_label(self.served_settings)}...",
                flush=True,
            )
            return self.review(repository, pull_request, material, **kwargs)


def _with_verification_fallback(
    verification: Optional[VerificationPass],
) -> Optional[VerificationPass]:
    """Return the pass one pull request verifies with, its fallback wrapped in."""

    if verification is None or verification.fallback is None:
        return verification
    return replace(
        verification, engine=_FallbackVerifier(verification), fallback=None
    )


def _verification_identity(
    verification: Optional[VerificationPass], settings: Settings
) -> "tuple[Optional[str], Optional[str]]":
    """Return the verifier that judged, which is the fallback once it took over."""

    engine = verification.engine if verification is not None else None
    if isinstance(engine, _FallbackVerifier) and engine.handed_over:
        return engine.served_settings.review_engine, engine.served_settings.review_model
    return _verifier_identity(settings)


def _delivered_verifier(
    verification: Optional[VerificationPass],
    settings: Settings,
    review_settings: Settings,
) -> "tuple[Optional[str], Optional[str]]":
    """Return the verifier a delivery names: none when it is the reviewer itself."""

    verifier = _verification_identity(verification, settings)
    if verifier == (review_settings.review_engine, review_settings.review_model):
        return None, None
    return verifier


def _handed_over_verifier(
    verification_engine: Any,
) -> "tuple[Optional[str], Optional[str]]":
    """Return the fallback verifier that judged, or (None, None) if none took over."""

    if isinstance(verification_engine, _FallbackVerifier):
        if verification_engine.handed_over:
            served = verification_engine.served_settings
            return served.review_engine, served.review_model
    return None, None


def _advisory_verifier_identity(
    settings: Settings, assessment: "Union[DesignAssessment, DocsAssessment]"
) -> "tuple[Optional[str], Optional[str]]":
    """Return the verifier that judged an advisory level's proposals."""

    if assessment.verifier_engine is None:
        return _verifier_identity(settings)
    return assessment.verifier_engine, assessment.verifier_model


class _ModelUsage:
    """Counts model invocations and the wall-clock they spent, by level.

    The only reason anybody knew one pull request [pr:xmf2] had burned ten identical
    reviews was that someone grepped a week of transcripts by hand. What a review
    costs is the number this operator acts on, so the run reports it rather than
    leaving it to be reconstructed. Tokens are not available: the engines are
    coding-agent CLIs that do not report them, and wall-clock is what the schedule
    actually spends. Credits are, for an engine that reports them, which Kiro does
    after every call.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._invocations: "dict[str, int]" = {}
        self._seconds = 0.0
        # Only some engines report a cost, so a spend with none reported says nothing
        # about credits rather than claiming the run was free.
        self._credits: Optional[float] = None
        # By label, because the total alone cannot say which stage to cut: a label
        # is here only once one of its calls reported a cost.
        self._label_credits: "dict[str, float]" = {}

    def record(
        self, label: str, seconds: float, credits: Optional[float] = None
    ) -> None:
        with self._lock:
            self._invocations[label] = self._invocations.get(label, 0) + 1
            self._seconds += seconds
            if credits is not None:
                self._credits = (self._credits or 0.0) + credits
                self._label_credits[label] = (
                    self._label_credits.get(label, 0.0) + credits
                )

    @property
    def invocations(self) -> int:
        with self._lock:
            return sum(self._invocations.values())

    @property
    def seconds(self) -> float:
        with self._lock:
            return self._seconds

    @property
    def credits(self) -> Optional[float]:
        with self._lock:
            return self._credits

    def merge(self, other: "_ModelUsage") -> None:
        with other._lock:
            counts = dict(other._invocations)
            seconds = other._seconds
            credits = other._credits
            label_credits = dict(other._label_credits)
        with self._lock:
            for label, count in counts.items():
                self._invocations[label] = self._invocations.get(label, 0) + count
            self._seconds += seconds
            if credits is not None:
                self._credits = (self._credits or 0.0) + credits
            for label, cost in label_credits.items():
                self._label_credits[label] = self._label_credits.get(label, 0.0) + cost

    def describe(self) -> str:
        """Return the spend as one line, or an empty string when nothing ran."""

        with self._lock:
            if not self._invocations:
                return ""
            total = sum(self._invocations.values())
            counts = sorted(self._invocations.items())
            seconds = self._seconds
            credits = self._credits
            label_credits = dict(self._label_credits)
        if credits is None:
            breakdown = ", ".join(
                f"{label} {count}" if count > 1 else label for label, count in counts
            )
        else:
            breakdown = ", ".join(
                _priced_label(label, count, label_credits.get(label))
                for label, count in counts
            )
        cost = f" and {credits:.2f} credits" if credits is not None else ""
        return (
            f"{_format_duration(seconds)} of model time{cost} across {total} "
            f"invocation{'s' if total != 1 else ''} ({breakdown})"
        )


def _priced_label(label: str, count: int, credits: Optional[float]) -> str:
    """Return one stage of a spend that reported credits, with what it cost.

    Once credits appear a bare number would read as a cost, so a count is written as
    calls, and a stage whose engine reported nothing says so rather than looking free.
    """

    calls = f" over {count} calls" if count > 1 else ""
    if credits is None:
        return f"{label} with no credits reported{calls}"
    return f"{label} {credits:.2f}{calls}"


def _usage_label(material: ReviewMaterial, role: str) -> str:
    """Return the name of the invocation described by ``material``."""

    verifying = bool(
        material.verification_candidates
        or material.design_candidates
        or material.docs_candidates
    )
    if role == "fallback":
        # Every level can hand over now, so the label says which one did. With no
        # separate verifier the fallback also verifies what it found.
        label = f"{material.level} fallback"
        return f"{label} verification" if verifying else label
    if role == "verify fallback":
        return f"{material.level} verification fallback"
    return f"{material.level} verification" if verifying else material.level


def _advisory_reviewer_identity(
    settings: Settings, assessment: "Union[DesignAssessment, DocsAssessment]"
) -> "tuple[str, Optional[str]]":
    """Return the engine and model that reviewed an advisory level."""

    if assessment.reviewer_engine is None:
        return settings.review_engine, settings.review_model
    return assessment.reviewer_engine, assessment.reviewer_model


class _MeasuredEngine:
    """A review engine that reports what each invocation cost and decided.

    A wrapper rather than a counter inside each level: every level already calls one
    method, so measuring here cannot miss an invocation a future level adds, and no
    level has to remember to record anything. It is also the one place that sees both
    the material going in and the payload coming back, which is what makes a
    structured measurement of a run possible without threading a recorder through
    every level.
    """

    def __init__(
        self,
        engine: ReviewEngine,
        usage: _ModelUsage,
        role: str,
        metrics: Optional[ReviewMetrics] = None,
        subject: Optional[Repository] = None,
    ) -> None:
        self._engine = engine
        self._usage = usage
        self._role = role
        self._metrics = metrics
        self._subject = subject

    def review(
        self,
        repository: Repository,
        pull_request: PullRequest,
        material: ReviewMaterial,
        **kwargs: Any,
    ) -> Any:
        started = time.monotonic()
        payload: Any = None
        failure: Optional[str] = None
        # Cleared first, so a cost an unmeasured call left on this thread is never
        # charged to this one.
        take_invocation_credits()
        try:
            payload = self._engine.review(
                repository, pull_request, material, **kwargs
            )
            return payload
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            raise
        finally:
            label = _usage_label(material, self._role)
            elapsed = time.monotonic() - started
            credits = take_invocation_credits()
            self._usage.record(label, elapsed, credits)
            if self._metrics is not None:
                self._metrics.record(
                    slug=(self._subject or repository).slug,
                    number=pull_request.number,
                    head_sha=pull_request.head_sha,
                    label=label,
                    seconds=elapsed,
                    level=material.level,
                    corrected=bool(material.validation_error),
                    payload=payload,
                    error=failure,
                    credits=credits,
                )

    def __getattr__(self, name: str) -> Any:
        # Anything else an adapter exposes stays reachable, so wrapping an engine
        # cannot change what a level can ask of it.
        return getattr(self._engine, name)


class _VerificationActivity:
    """Records whether the separate verification pass judged any candidates.

    Attribution must describe work that happened. A run whose reviews all
    failed, or that produced no candidate findings, never reaches the
    verification engine, so naming a verifier afterwards would report a pass
    that never ran.
    """

    def __init__(self) -> None:
        self._performed = False

    @property
    def performed(self) -> bool:
        return self._performed

    def record(self) -> None:
        self._performed = True


class _GithubIncidentNotice:
    """Reports announced GitHub incidents once per run, after a GitHub failure.

    A failed GitHub command has already exhausted its transient retry window, so
    the status page is consulted at most once and only when it can explain that
    failure. The resolved note is reused by the aggregated Slack notification.
    """

    def __init__(self) -> None:
        self._checked = False
        self._note: Optional[str] = None

    @property
    def note(self) -> Optional[str]:
        return self._note

    def report(self, error: BaseException) -> None:
        if self._checked or not _is_github_command_failure(error):
            return
        self._checked = True
        self._note = describe_github_incident()
        if self._note:
            progress.notify(f"      -> {self._note}")


def read_review_rules(
    repository: Repository, pull_request: PullRequest, commit: Optional[str] = None
) -> ReviewRules:
    """Return the code review rules on the pull request's base branch.

    Read from the base branch's current tip rather than the reviewed head, so a pull
    request cannot write the rules it is judged by; changing a rule is a change of
    its own, merged first. A failure to read them fails the review rather than
    letting it pass without them.
    """

    base_ref = pull_request.base_ref
    if commit is None:
        if not base_ref:
            raise WorkspaceError(
                f"{repository.slug}#{pull_request.number} names no base branch to "
                "read its code review rules from"
            )
        commit = get_branch_head_sha(repository, base_ref)
    commit = validate_object_name(commit, f"{repository.slug} base commit")
    documents = read_base_files(
        repository, commit, review_rule_pathspecs(), is_review_rule_source
    )
    return collect_review_rules(documents, base_ref or commit[:12], commit)


def _report_review_rules(rules: ReviewRules) -> None:
    sources = ", ".join(dict.fromkeys(section.path for section in rules.sections))
    print(
        f"      -> Following the code review rules on {rules.base_ref} in {sources}"
    )
    if rules.truncated:
        print(
            "      -> The code review rules are longer than one review carries; "
            "the rest are not enforced"
        )


def _print_findings(findings: list[Finding], approve: bool) -> None:
    if not findings:
        if approve:
            print("      -> LGTM")
        else:
            print("      -> No new findings; approval withheld")
        return
    for finding in findings:
        print(
            f"      -> [{finding.severity.upper()}] {finding.path}:{finding.line} "
            f"{finding.title}"
        )
        print(f"         Problem: {finding.problem}")
        if finding.rule:
            print(f"         Rule ({finding.rule_source}): {finding.rule}")
        print(f"         Trigger: {finding.trigger}")
        print(f"         Impact: {finding.impact}")
        print(f"         Change: {finding.suggestion}")


class _PrimaryEngineState:
    """Remembers, for one run, that the primary engine is out of quota.

    Learning this costs a full refusal — 2m30s on Antigravity, which retries inside
    its own CLI before reporting — and every pull request after the first was paying
    it again to reach the same answer. A quota is an account-wide fact, so one
    refusal settles it for the run: later reviews hand straight to the fallback.

    Held for the run only, never persisted. A window that resets is not something
    this tool should be predicting across runs, and a scheduled run an hour later
    deserves to find out for itself.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason = ""

    def reset(self) -> None:
        with self._lock:
            self._reason = ""

    def record_exhausted(self, reason: str) -> None:
        with self._lock:
            if not self._reason:
                self._reason = " ".join(reason.split())[:200]

    @property
    def exhausted_reason(self) -> str:
        with self._lock:
            return self._reason


_PRIMARY_ENGINE = _PrimaryEngineState()


def _note_exhausted_quota(error: Exception) -> None:
    """Record a spent quota whichever level met it first.

    The advisory levels run before the code review and share its engine, so on the
    first pull request of an exhausted run all three paid a refusal to learn the one
    fact: 08:02:57, 08:05:22, and 08:07:58 on 2026-09-23, about five minutes spent
    reaching an answer the first refusal had already given.
    """

    if is_exhausted_quota(str(error)):
        _PRIMARY_ENGINE.record_exhausted(str(error))


def _print_rejected_candidates(
    candidates: "tuple[Finding, ...]",
    confirmed: "list[Finding]",
    reasons: "dict[str, CandidateRejection]",
) -> None:
    """Report the candidates verification threw out, for later analysis.

    A count alone cannot distinguish a verifier that rejects sound candidates
    from a review that produces weak ones, so each candidate is paired with the
    reason the verifier gave for that specific candidate. The verdict reason is
    deliberately not used as a substitute: it explains the approval decision,
    which is often a different subject entirely. These candidates are unconfirmed
    by definition, so they are reported to the terminal and the run transcript
    only and never reach GitHub or Slack.
    """
    # Identified by reference rather than by position. Two candidates can share a
    # line, and comparing positions then reported the rejected one as confirmed: it
    # vanished from the transcript entirely, taking the verifier's reason with it.
    confirmed_refs = {
        code_candidate_ref(index + 1)
        for index, candidate in enumerate(candidates)
        if any(candidate is finding for finding in confirmed)
        or any(
            candidate.path == finding.path
            and candidate.line == finding.line
            and candidate.title == finding.title
            for finding in confirmed
        )
    }
    rejected = [
        (code_candidate_ref(index + 1), candidate)
        for index, candidate in enumerate(candidates)
        if code_candidate_ref(index + 1) not in confirmed_refs
    ]
    for reference, candidate in rejected:
        print(
            f"      -> Rejected candidate [{candidate.severity.upper()}] "
            f"{candidate.path}:{candidate.line} {candidate.title}"
        )
        print(f"         Claimed: {candidate.problem}")
        if candidate.trigger:
            print(f"         Trigger: {candidate.trigger}")
        rejection = reasons.get(reference)
        if rejection is None:
            print(
                f"         Rejected [{UNCLASSIFIED_REJECTION}] because: the verifier "
                "gave no reason for this candidate"
            )
            continue
        # Every rejection line carries exactly one bracketed class, so a week of
        # transcripts tallies with a single grep over the run logs.
        print(
            f"         Rejected [{rejection.rejection_class}] because: "
            f"{rejection.reason}"
        )


def _format_failure_notification(
    errors: list[str],
    incident_note: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> str:
    lines = [
        "*PR Warden live run completed with errors*",
        f"{len(errors)} review operation"
        f"{'s' if len(errors) != 1 else ''} could not be completed:",
    ]
    if incident_note:
        lines.append(f"_{' '.join(incident_note.split())}_")
    trailer = [""] + [f"_{reviewer}_"] if reviewer else []
    reported = 0
    for error in errors[:MAX_FAILURE_NOTIFICATION_ERRORS]:
        candidate = lines + [f"• {' '.join(error.split())}"]
        omitted = len(errors) - (reported + 1)
        if omitted > 0:
            candidate = candidate + [
                f"• …and {omitted} more; inspect the local run log"
            ]
        if len("\n".join(candidate + trailer)) > MAX_FAILURE_NOTIFICATION_CHARACTERS:
            break
        lines.append(f"• {' '.join(error.split())}")
        reported += 1
    omitted = len(errors) - reported
    if omitted > 0:
        lines.append(f"• …and {omitted} more; inspect the local run log")
    lines.extend(trailer)
    return "\n".join(lines)


def _errors_for_slack(
    errors: "Sequence[str]", repositories: "Sequence[Repository]"
) -> list[str]:
    """Return the run errors the Slack notification may carry.

    Every error a run collects starts with the repository it is about, as
    `owner/name: ...` or `owner/name#123: ...`. One about a repository kept out of
    Slack is left on the terminal and in the run log, where it was already printed.
    """

    kept_out = tuple(
        repository.slug for repository in repositories if not repository.slack
    )
    return [
        error
        for error in errors
        if not any(
            error.startswith(f"{slug}:") or error.startswith(f"{slug}#")
            for slug in kept_out
        )
    ]


def _notify_run_failure(
    webhook_url: Optional[str],
    errors: list[str],
    incident_note: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> None:
    if not webhook_url or not errors:
        return
    try:
        send_slack_message(
            webhook_url,
            _format_failure_notification(errors, incident_note, reviewer),
        )
    except (DeliveryError, DeliveryUncertainError) as error:
        print(
            f"ERROR: Could not send Slack run-error notification: {error}",
            file=sys.stderr,
        )
    else:
        print("Sent run-error notification to Slack", file=sys.stderr)


def _startup_failure_notifications_enabled(args: argparse.Namespace) -> bool:
    dry_run_value = os.getenv("AUTO_REVIEW_DRY_RUN", "1").strip().casefold()
    return bool(args.live and dry_run_value in {"0", "false", "no", "off"})


def _crash_location(error: BaseException) -> str:
    """Return where in this tool's own code an unexpected error was raised."""

    frames = traceback.extract_tb(error.__traceback__)
    for frame in reversed(frames):
        try:
            relative = Path(frame.filename).resolve().relative_to(PACKAGE_ROOT)
        except ValueError:
            continue
        return f"{relative.as_posix()}:{frame.lineno} ({frame.name})"
    if frames:
        return f"{Path(frames[-1].filename).name}:{frames[-1].lineno}"
    return "an unknown place"


def _report_crashed_review(task: "_ReviewTask", error: Exception) -> str:
    """Report a defect one review hit, and return it as that review's run error.

    It is this tool's bug rather than a review failure, but it is still one
    review's: the run carries on with the others, prints its summary, and sends
    the run-error notification. A defect that escaped did none of those, so four
    scheduled runs once ended in a traceback that nobody was told about.
    """

    traceback.print_exception(type(error), error, error.__traceback__)
    detail = f"crashed with {type(error).__name__}: {error}"
    print(f"      -> ERROR: {detail}", file=sys.stderr)
    return f"{task.repository.slug}#{task.pull_request.number}: {detail}"


def _await_mergeability(
    repository: Repository,
    pull_request: PullRequest,
    excluded_labels: "frozenset[str]",
    snapshot: PullRequestSnapshot,
) -> PullRequestSnapshot:
    """Give GitHub bounded time to finish calculating mergeability.

    GitHub computes mergeability in a background job and reports null until it
    finishes, which is normal shortly after a push or rebase. Failing on the
    first null would discard a completed review and spend the review budget
    again on the next run, so recheck a bounded number of times. Each recheck
    revalidates the snapshot, so a head that moves during the wait still raises
    the head-changed restart.
    """

    for delay in MERGEABILITY_RECHECK_DELAYS_SECONDS:
        if snapshot.mergeable is not None:
            return snapshot
        print(
            f"      -> GitHub is still calculating mergeability; "
            f"rechecking in {delay}s..."
        )
        time.sleep(delay)
        snapshot = get_pull_request_snapshot(repository, pull_request)
        _validate_pull_request_snapshot(snapshot, pull_request, excluded_labels)
    return snapshot


def _validate_pull_request_snapshot(
    snapshot: PullRequestSnapshot,
    pull_request: PullRequest,
    excluded_labels: frozenset[str],
) -> None:
    if snapshot.head_sha != pull_request.head_sha:
        raise PullRequestHeadChangedError(
            pull_request.head_sha,
            snapshot.head_sha,
        )
    if snapshot.state != "open":
        raise PullRequestClosedError("pull request is no longer open")
    if snapshot.is_draft:
        raise PullRequestIneligibleError("pull request became a draft during review")
    excluded = snapshot.labels.intersection(excluded_labels)
    if excluded:
        raise PullRequestIneligibleError(
            "pull request gained an excluded label during review: "
            + ", ".join(sorted(excluded))
        )


def _read_generated_marks(
    repository: Repository,
    pull_request: PullRequest,
    paths: "Sequence[str]",
) -> "Optional[dict[str, Optional[bool]]]":
    """Return how the base branch marks each omitted file, or None when unknown.

    A repository tells GitHub which files are machine-made with
    `linguist-generated` in its `.gitattributes`. Read from the base branch's
    current tip rather than the reviewed head, for the reason the code review rules
    are: a pull request must not be able to mark its own new file as generated and
    so leave it unread and approvable. Not being able to read the marks costs an
    approval rather than granting one: None keeps every omitted file unread, since
    any mark might have been an unmark. A Git too old to read attributes from a
    commit is a lasting fact of the installation rather than a failure, so it
    returns no marks, and the ignore list decides as it did before marks existed.
    """

    if not paths:
        return {}
    base_ref = pull_request.base_ref
    try:
        if not base_ref:
            raise WorkspaceError("the pull request names no base branch")
        commit = validate_object_name(
            get_branch_head_sha(repository, base_ref),
            f"{repository.slug} base commit",
        )
        return read_generated_marks(repository, commit, tuple(paths))
    except UnsupportedGitError as error:
        print(
            f"      -> Not reading .gitattributes on {base_ref}: {error}; only "
            "IGNORED_MISSING_PATCH_PATTERNS decide which omitted files need no review"
        )
        return {}
    except (CommandError, WorkspaceError) as error:
        print(
            f"      -> Could not read .gitattributes on {base_ref or 'the base branch'}"
            f", so every omitted file stays unread: {error}"
        )
        return None


def _generated_mark_states(
    marks: "Optional[Mapping[str, Optional[bool]]]",
    paths: "Sequence[str]",
) -> "dict[str, str]":
    """Return what the base branch said about each path, as recorded state."""

    if marks is None:
        return {path: "unavailable" for path in paths}
    names = {True: "generated", False: "not_generated", None: "unmarked"}
    return {path: names[marks.get(path)] for path in paths}


def _omitted_file_now_ignored(
    settings: Settings,
    repository: Repository,
    pull_request: PullRequest,
    record: "dict[str, Any]",
) -> bool:
    """Return whether a recorded unread file now needs no review.

    The same question the material builder asks, `ignores_omitted_patch`, put to
    the base branch's current marks: a file it now marks generated, or one it no
    longer unmarks while a pattern names it, would be ignored by another review, so
    that review could approve. Any other answer leaves the file unread and would
    spend a review to record the same result, and a lookup that fails proves
    nothing either way, so neither reopens.

    A record written before the marks were read names no omitted files, so all of
    its unread paths are asked about; a newer one names its omitted unread files,
    and one with none costs no lookup.
    """

    recorded = record.get("unread_generated_marks")
    if isinstance(recorded, dict):
        paths = tuple(sorted(path for path in recorded if isinstance(path, str)))
    else:
        paths = _recorded_unread_paths(record)
    if not paths:
        return False
    marks = _read_generated_marks(repository, pull_request, paths)
    if marks is None:
        return False
    ignored = [
        path
        for path in paths
        if ignores_omitted_patch(
            path, settings.ignored_missing_patch_patterns, marks.get(path)
        )
    ]
    if not ignored:
        return False
    print(
        "      -> The base branch's marks now leave "
        + ", ".join(ignored)
        + " needing no review; reconsidering withheld approval"
    )
    return True


def _read_linked_issues(
    repository: Repository, pull_request: PullRequest
) -> "tuple[LinkedIssue, ...]":
    """Return the issues the pull request names, as the intent behind the change.

    The GitHub issues GitHub records it as closing, and the Jira work items of the
    repository's own projects that its title, description, or branch name names,
    read through the operator's `acli`. This is context, never a gate: an issue that
    cannot be read is reported and left out, and the review goes ahead as it did
    before issues were read.
    """

    issues: "list[LinkedIssue]" = []
    try:
        issues.extend(list_closing_issues(repository, pull_request))
    except CommandError as error:
        print(f"      -> Could not read the GitHub issues this closes: {error}")
    keys = find_jira_keys(
        (pull_request.title, pull_request.body, pull_request.head_ref),
        repository.jira_projects,
    )
    for key in keys:
        try:
            issues.append(read_work_item(key))
        except (CommandError, ValueError) as error:
            print(f"      -> Could not read Jira work item {key}: {error}")
    if issues:
        print(
            "      -> Reading what this was asked to do in "
            + ", ".join(issue.reference for issue in issues)
        )
    return tuple(issues)


def _read_file_history(
    repository: Repository,
    pull_request: PullRequest,
    files: "list[dict[str, Any]]",
    commit: Optional[str] = None,
) -> "tuple[HistoryCommit, ...]":
    """Return the base branch's recent commits on the files this change edits.

    A commit message often says why a guard, a limit, or a workaround exists, which
    no diff shows. Read from the base branch's tip, or ``commit`` when a replay pins
    one, so the history is what the team merged rather than what the pull request
    says. This is context, never a gate: history that cannot be read is reported
    and left out, and the review goes ahead without it.
    """

    paths = history_paths(files)
    if not paths:
        return ()
    base_ref = pull_request.base_ref
    try:
        if commit is None:
            if not base_ref:
                raise WorkspaceError("the pull request names no base branch")
            commit = get_branch_head_sha(repository, base_ref)
        commit = validate_object_name(commit, f"{repository.slug} base commit")
        history = read_file_history(repository, commit, paths)
    except (CommandError, WorkspaceError) as error:
        print(f"      -> Could not read the history of the changed files: {error}")
        return ()
    if history:
        print(
            f"      -> Reading {len(history)} earlier "
            f"commit{'s' if len(history) != 1 else ''} on the changed files"
        )
    return history


def _bundled_material(
    files: "list[dict[str, Any]]",
    settings: Settings,
    prior_comments: "tuple[PriorReviewComment, ...]",
    generated_marks: "Optional[Mapping[str, Optional[bool]]]" = None,
    marks_unreadable: bool = False,
) -> "tuple[ReviewMaterial, tuple[ReviewMaterial, ...]]":
    """Return the first bundle, carrying the whole change's budget, and the rest.

    Only a change that does not fit one review is bundled, so this returns exactly
    what a single review received whenever the change fits, or whenever bundling is
    switched off. The first bundle's budget is replaced by the whole change's, which
    is what every later decision reads: whether an approval may be given, what the
    run reports, and whether anyone needs telling that a file went unread.
    """

    bundles, unread = build_bundled_review_material(
        files,
        settings.max_diff_bytes,
        settings.max_review_bundles,
        prior_comments,
        settings.ignored_missing_patch_patterns,
        generated_marks,
        marks_unreadable,
    )
    first = bundles[0]
    if len(bundles) == 1:
        return first, ()
    budget = first.diff_budget
    aggregate = replace(
        budget,
        reviewed_bytes=sum(bundle.diff_budget.reviewed_bytes for bundle in bundles),
        reviewed_files=sum(bundle.diff_budget.reviewed_files for bundle in bundles),
        # What no bundle read, plus the files no budget could have helped with. A
        # change fully covered by bundles is no longer a partial review, which is the
        # whole point: nobody has to withhold an approval over a file that was read.
        deferred=unread + budget.unreadable,
    )
    return replace(first, diff_budget=aggregate), tuple(bundles[1:])


def _bundle_material_from(
    primary: ReviewMaterial,
    bundle: ReviewMaterial,
) -> ReviewMaterial:
    """Return one bundle's material carrying the primary's pull-request context.

    Checks, background, prior comments and linked dependencies describe the pull
    request rather than a bundle of it, so they are shared. Only the diff, the lines
    it added, and the budget that measured it differ.
    """

    return replace(
        primary,
        diff=bundle.diff,
        added_lines=bundle.added_lines,
        diff_budget=bundle.diff_budget,
    )


def _report_diff_budget(material: ReviewMaterial) -> None:
    """Report what the change measures and what the budget could not fit."""

    budget = material.diff_budget
    if budget is None or not budget.is_partial:
        return
    if budget.exceeds_limit:
        print(
            f"      -> Diff is {format_byte_size(budget.total_bytes)} across "
            f"{budget.total_files} files, {budget.limit_ratio:.2f}x the "
            f"{format_byte_size(budget.limit_bytes)} budget"
        )
    print(
        f"      -> Reviewing {budget.reviewed_files} files that fit "
        f"({format_byte_size(budget.reviewed_bytes)}); "
        f"{len(budget.deferred)} need a human"
    )
    for directory, size_bytes in dominant_directories(budget.oversized):
        print(
            f"      -> Not reviewed: {directory} "
            f"({format_byte_size(size_bytes)})"
        )
    # Named individually rather than grouped by size: there is no size to rank
    # them by, and a directory summary would drop the one path an operator has to
    # decide about.
    for file in budget.unreadable:
        print(f"      -> Not reviewed: {file.path} ({file.reason})")


def _partial_review_reason(budget: DiffBudget) -> str:
    """Return why a review could not read a whole change."""

    if not budget.exceeds_limit:
        return (
            f"GitHub omitted the patch of {len(budget.unreadable)} of "
            f"{budget.total_files} changed files, so nothing local could read "
            "them: " + ", ".join(file.path for file in budget.unreadable)
        )
    reason = (
        f"The complete diff is {format_byte_size(budget.total_bytes)} across "
        f"{budget.total_files} files, {budget.limit_ratio:.2f}x the "
        f"{format_byte_size(budget.limit_bytes)} MAX_DIFF_BYTES budget"
    )
    if budget.unreadable:
        reason += (
            f", and GitHub omitted the patch of {len(budget.unreadable)} more: "
            + ", ".join(file.path for file in budget.unreadable)
        )
    return reason


def _partial_review_note(budget: Optional[DiffBudget]) -> Optional[str]:
    """Return the review body stating what a partial review did not read."""

    if budget is None or not budget.is_partial:
        return None
    dominant = ", ".join(
        f"`{directory}`" for directory, _ in dominant_directories(budget.oversized)
    )
    unreadable = ", ".join(f"`{file.path}`" for file in budget.unreadable)
    note = (
        f"This review read {budget.reviewed_files} of {budget.total_files} changed "
        "files."
    )
    if budget.exceeds_limit:
        note += (
            f" The complete diff is {format_byte_size(budget.total_bytes)}, over "
            f"the {format_byte_size(budget.limit_bytes)} this reviewer reads at "
            f"once, so {len(budget.oversized)} files were not reviewed"
            + (f", mostly under {dominant}" if dominant else "")
            + "."
        )
    if unreadable:
        note += (
            f" GitHub does not serve a patch for {unreadable}, so nothing local "
            "could read that change."
        )
    return (
        note + " Nothing here says those files are correct, and no approval was "
        "given for this change."
    )


def _only_became_resolved(
    reviewed: PriorReviewComment,
    latest: PriorReviewComment,
) -> bool:
    """Return whether the sole change to a comment is that it is now resolved."""

    return (
        latest.resolved is True
        and reviewed.resolved is not True
        and replace(reviewed, resolved=latest.resolved) == latest
    )


def _feedback_objections(
    reviewed: Sequence[PriorReviewComment],
    latest: Sequence[PriorReviewComment],
) -> tuple[PriorReviewComment, ...]:
    """Return feedback that arrived or changed since this review read it.

    A comment that disappeared, or that only became resolved, takes an objection
    away: a result reached without knowing that is still sound, so it no longer
    discards a finished review. A new thread, a reopened one, or an edited one may
    say something this review never read, and only those are held against it.
    """

    reviewed_by_id = {comment.comment_id: comment for comment in reviewed}
    objections = []
    for comment in latest:
        previous = reviewed_by_id.get(comment.comment_id)
        if previous == comment:
            continue
        if previous is not None and _only_became_resolved(previous, comment):
            continue
        objections.append(comment)
    return tuple(objections)


def _objections_touching_findings(
    objections: Sequence[PriorReviewComment],
    findings: Sequence[Finding],
) -> tuple[PriorReviewComment, ...]:
    """Return the new feedback sharing an anchor with a finding about to post.

    A shared line is not proof of a duplicate, which is why the reviewer is told
    not to treat it as one. It is proof that this controller cannot tell, and a
    finding nobody can distinguish from a human's newer comment is the one worth
    holding back.
    """

    anchors = {(finding.path, finding.line) for finding in findings}
    return tuple(
        comment for comment in objections if (comment.path, comment.line) in anchors
    )


def _describe_feedback_objections(
    objections: Sequence[PriorReviewComment],
) -> str:
    """Say where new feedback landed, never what it said."""

    described = []
    for comment in objections[:3]:
        if comment.path and comment.line:
            where = f"{comment.path}:{comment.line}"
        elif comment.path:
            where = comment.path
        else:
            where = "the conversation"
        described.append(f"@{comment.author} on {where}")
    remaining = len(objections) - len(described)
    if remaining > 0:
        described.append(f"and {remaining} more")
    return ", ".join(described)


def _record_stale_feedback_skip(
    settings: Settings,
    repository: Repository,
    pull_request: PullRequest,
    store: JsonStateStore,
    state_key: str,
    objections: Sequence[PriorReviewComment],
    touched: Sequence[PriorReviewComment],
    findings: Sequence[Finding],
    reviewer_engine: str,
    reviewer_model: Optional[str],
) -> None:
    """Report a result newer human feedback overtook, and keep it retryable.

    This is not a fault of the run: somebody is reviewing the same pull request
    and their comment is newer than this judgment. So it reports as a skip and
    leaves the exit code alone, while still notifying Slack, because the operator
    is the one who decides whether to run the reviewer again. The state stays
    nonterminal so the next run judges the feedback as it stands then.
    """

    detail = _describe_feedback_objections(objections)
    if touched:
        reason = (
            f"New review feedback shares the lines of {len(touched)} of "
            f"{len(findings)} findings, so none were delivered: {detail}"
        )
    else:
        reason = (
            "Review feedback newer than this approval arrived during analysis, "
            f"so no approval was given: {detail}"
        )
    store.set_status(
        state_key,
        "feedback_changed",
        reason=reason,
        objection_comment_ids=[comment.comment_id for comment in objections],
    )
    print(f"      -> Skipped: {reason}")
    print("      -> Run the reviewer again to judge the current feedback")
    if not repository.slack:
        print(
            "      -> Stale-review notification not sent to Slack: this "
            "repository is kept out of it"
        )
        return
    if not settings.slack_webhook_url:
        print(
            "      -> Stale-review Slack notification skipped: "
            "SLACK_WEBHOOK_URL is not configured"
        )
        return
    try:
        send_slack_message(
            settings.slack_webhook_url,
            format_stale_review_message(
                repository,
                pull_request,
                reason,
                reviewer_engine,
                reviewer_model,
            ),
        )
    except (DeliveryError, DeliveryUncertainError) as notification_error:
        print(f"      -> Stale-review Slack notification failed: {notification_error}")
        return
    print("      -> Sent stale-review notification to Slack")


def _notify_manual_review(
    settings: Settings,
    repository: Repository,
    pull_request: PullRequest,
    store: JsonStateStore,
    reason: str,
    budget: Optional[DiffBudget],
) -> bool:
    """Ask a human to review a change too large to review completely, once.

    Returns whether the request is settled. A caller that skipped the review
    entirely keeps its own record nonterminal while this is False, so a message
    Slack refused is retried rather than lost with the head that needed it.

    Keyed by pull request, not by head: the size of a change is a property of the
    pull request, so pushing to an oversized pull request should not send the same
    message again. A notification that could not be sent stays nonterminal and is
    retried on a later run.
    """

    state_key = store.pull_request_key(
        repository.slug,
        pull_request.number,
        "oversized",
    )
    if store.is_settled_for_pull_request(
        repository.slug,
        pull_request.number,
        "oversized",
    ):
        print(
            "      -> Manual review was already requested for this pull request; "
            "not asking again"
        )
        return True
    details = {"reason": reason, "max_diff_bytes": settings.max_diff_bytes}
    coverage = None
    not_reviewed = None
    if budget is not None and budget.is_partial:
        coverage = (
            f"read {budget.reviewed_files} of {budget.total_files} changed files "
            f"({format_byte_size(budget.reviewed_bytes)} of "
            f"{format_byte_size(budget.total_bytes)})"
        )
        not_reviewed = ", ".join(
            [
                f"{directory} ({format_byte_size(size_bytes)})"
                for directory, size_bytes in dominant_directories(budget.oversized)
            ]
            + [f"{file.path} ({file.reason})" for file in budget.unreadable]
        )
        details["reviewed_files"] = budget.reviewed_files
        details["deferred_files"] = len(budget.deferred)
    if not repository.slack:
        store.set_status(state_key, "oversized_not_notified", **details)
        print(
            "      -> Manual review needed, not sent to Slack: this repository "
            "is kept out of it"
        )
        return True
    if not settings.slack_webhook_url:
        store.set_status(
            state_key,
            "oversized_notification_pending",
            notification_error="SLACK_WEBHOOK_URL is not configured",
            **details,
        )
        print(
            "      -> Manual-review Slack notification pending: "
            "SLACK_WEBHOOK_URL is not configured"
        )
        return False
    try:
        receipt_id = send_slack_message(
            settings.slack_webhook_url,
            format_manual_review_message(
                repository,
                pull_request,
                reason,
                settings.review_engine,
                settings.review_model,
                coverage,
                not_reviewed,
            ),
        )
    except (DeliveryError, DeliveryUncertainError) as notification_error:
        store.set_status(
            state_key,
            "oversized_notification_pending",
            notification_error=str(notification_error),
            **details,
        )
        print(
            "      -> Manual-review Slack notification pending: "
            f"{notification_error}"
        )
        return False
    store.set_status(
        state_key,
        "oversized_notified",
        notification_receipt_id=receipt_id,
        **details,
    )
    print("      -> Sent manual-review notification to Slack")
    return True


def _record_ineligible_skip(
    store: JsonStateStore,
    state_key: str,
    error: PullRequestIneligibleError,
    *,
    live: bool,
) -> None:
    if live:
        store.set_status(state_key, "skipped_ineligible", reason=str(error))
    print(f"      -> Skipped: {error}; nothing was delivered")


def _verify_code_candidates(
    settings: Settings,
    verification: Optional["VerificationPass"],
    review_settings: Settings,
    review_engine: ReviewEngine,
    exact_repository: Repository,
    pull_request: PullRequest,
    material: ReviewMaterial,
    findings: "list[Finding]",
    *,
    verification_activity: Optional[_VerificationActivity] = None,
) -> "tuple[Any, list[Finding], bool, str, tuple[str, ...]]":
    """Have the adversarial pass judge each candidate, and report what it threw out.

    Extracted so the replay harness measures this pass rather than a copy of it: a
    harness that reimplemented verification would report on code nobody ships.
    """

    verification_material = replace(
        material,
        verification_candidates=tuple(findings),
    )
    verification_settings = (
        verification.settings if verification is not None else review_settings
    )
    verification_engine = (
        verification.engine if verification is not None else review_engine
    )
    verification_timeout = _verification_timeout_seconds(settings, len(findings))
    print(
        f"      -> Adversarially verifying {len(findings)} candidate "
        f"finding{'s' if len(findings) != 1 else ''} with "
        f"{_engine_label(verification_settings)} "
        f"(timeout: {_format_duration(verification_timeout)})...",
        flush=True,
    )
    candidates = tuple(findings)
    if verification_activity is not None:
        verification_activity.record()
    (
        structured_output,
        confirmed,
        approve,
        approval_reason,
        blocking_comment_ids,
    ) = _review_with_validation_retry(
        verification_settings,
        verification_engine,
        exact_repository,
        pull_request,
        verification_material,
        material,
        candidates,
        phase="verification",
        timeout_seconds=verification_timeout,
    )
    print(
        f"      -> Confirmed {len(confirmed)} of {len(candidates)} candidate findings"
    )
    _print_rejected_candidates(
        candidates,
        confirmed,
        collect_rejection_reasons(structured_output, candidates),
    )
    return (
        structured_output,
        confirmed,
        approve,
        approval_reason,
        blocking_comment_ids,
    )


def _review_with_engine_fallback(
    settings: Settings,
    engine: ReviewEngine,
    fallback: Optional["FallbackReviewer"],
    repository: Repository,
    pull_request: PullRequest,
    material: ReviewMaterial,
) -> tuple[Any, list[Finding], bool, str, tuple[str, ...], Settings, ReviewEngine]:
    """Review with the primary engine, retrying once on a fallback engine.

    Only an engine-level failure hands over. A provider that is unavailable,
    unauthenticated, or refusing requests cannot review at all, so a second
    engine is the only way to finish the run. A controller validation failure is
    not a handover reason, because the review phase already retries it with
    feedback and shopping for a compliant answer would weaken validation.

    Returns the review result together with the settings and engine that
    produced it, so delivery attributes the engine that actually reviewed.
    """

    spent = _PRIMARY_ENGINE.exhausted_reason
    if spent and fallback is not None:
        print(
            f"      -> {_engine_label(settings)} is out of quota for this run "
            f"({spent}); reviewing with {_engine_label(fallback.settings)} directly",
            flush=True,
        )
    else:
        print(
            f"      -> Running {settings.review_engine} review "
            f"(timeout: {_format_duration(settings.review_timeout_seconds)})...",
            flush=True,
        )
        try:
            result = _review_with_validation_retry(
                settings,
                engine,
                repository,
                pull_request,
                material,
                material,
                phase="review",
            )
            return (*result, settings, engine)
        except EngineError as error:
            if is_exhausted_quota(str(error)):
                _PRIMARY_ENGINE.record_exhausted(str(error))
            if fallback is None:
                raise
            print(
                f"      -> {_engine_label(settings)} review engine failed: {error}"
            )
            return _review_with_fallback_engine(
                settings, fallback, repository, pull_request, material, error
            )
    return _review_with_fallback_engine(
        settings, fallback, repository, pull_request, material, None
    )


def _review_with_fallback_engine(
    settings: Settings,
    fallback: "FallbackReviewer",
    repository: Repository,
    pull_request: PullRequest,
    material: ReviewMaterial,
    error: Optional[EngineError],
) -> "tuple[Any, list[Finding], bool, str, tuple[str, ...], Settings, ReviewEngine]":
    """Review with the fallback engine, naming both failures if it fails too."""

    print(
        f"      -> Running fallback review with "
        f"{_engine_label(fallback.settings)} "
        f"(timeout: {_format_duration(settings.review_timeout_seconds)})...",
        flush=True,
    )
    try:
        result = _review_with_validation_retry(
            fallback.settings,
            fallback.engine,
            repository,
            pull_request,
            material,
            material,
            phase="review",
        )
    except EngineError as fallback_error:
        if error is None:
            raise
        raise EngineError(
            f"{_engine_label(settings)} review failed: {error}; "
            f"{_engine_label(fallback.settings)} fallback also failed: "
            f"{fallback_error}"
        ) from fallback_error
    return (*result, fallback.settings, fallback.engine)


def _process_pull_request(
    settings: Settings,
    repository: Repository,
    pull_request: PullRequest,
    store: JsonStateStore,
    engine: ReviewEngine,
    *,
    live: bool,
    force: bool,
    verification: Optional[VerificationPass] = None,
    verification_activity: Optional[_VerificationActivity] = None,
    fallback: Optional[FallbackReviewer] = None,
) -> None:
    destination = _destination(repository, pull_request, settings.reviewer_username)
    account = reviewer_account(repository, settings.reviewer_username)
    verifier: tuple[Optional[str], Optional[str]] = (None, None)
    reviewer_engine = settings.review_engine
    reviewer_model = settings.review_model
    state_key = store.key(
        repository.slug,
        pull_request.number,
        pull_request.head_sha,
        destination,
    )

    print(
        f"    - PR #{pull_request.number} ({pull_request.title!r}) "
        f"by @{pull_request.author} -> {_DESTINATION_LABELS[destination]}"
    )

    prior_comments: Optional[tuple[PriorReviewComment, ...]] = None
    reconsidering_delivered_review = False
    if not force and store.is_terminal(state_key):
        record = store.get(state_key) or {}
        status = record.get("status")
        receipt_id = record.get("receipt_id")
        if (
            status == "delivered"
            and destination == "github"
            and isinstance(receipt_id, str)
            and delivered_review_findings_resolved(
                repository,
                pull_request,
                receipt_id,
            )
        ):
            reconsidering_delivered_review = True
            print(
                "      -> Delivered review findings are resolved; "
                "reconsidering approval"
            )
        elif status != "approval_withheld":
            print(f"      -> Skipped: state is {record.get('status')}")
            return
        else:
            prior_comments = list_prior_review_comments(
                repository,
                pull_request,
                account,
            )
            current_fingerprint = _feedback_fingerprint(prior_comments)
            recorded_blockers = record.get("blocking_comment_ids")
            valid_recorded_blockers = (
                isinstance(recorded_blockers, list)
                and bool(recorded_blockers)
                and all(
                    isinstance(comment_id, str) and bool(comment_id)
                    for comment_id in recorded_blockers
                )
            )
            active_comment_ids = {
                comment.comment_id
                for comment in prior_comments
                if comment.resolved is not True
            }
            active_recorded_blockers = (
                set(recorded_blockers).intersection(active_comment_ids)
                if valid_recorded_blockers
                else set()
            )
            if (
                active_recorded_blockers
                and record.get("feedback_fingerprint") == current_fingerprint
            ):
                reason = record.get("reason")
                detail = f": {reason}" if isinstance(reason, str) and reason else ""
                print(f"      -> Skipped: state is approval_withheld{detail}")
                return
            recorded_unread = _recorded_unread_paths(record)
            if (
                recorded_unread
                and record.get("unread_settings_fingerprint")
                == _unread_settings_fingerprint(settings)
                and not _omitted_file_now_ignored(
                    settings, repository, pull_request, record
                )
            ):
                # The controller withheld this approval itself, because it could
                # not read these files. That answer is not the engine's opinion to
                # revisit: for this head and these settings another review would
                # defer the same files and withhold the approval again, so it
                # would spend a full review to reach the recorded result.
                print(
                    "      -> Skipped: approval stays withheld while "
                    + ", ".join(recorded_unread)
                    + " cannot be read; a later head, an ignore pattern, a larger "
                    "MAX_DIFF_BYTES, or --force reopens it"
                )
                return
            recorded_failed = _recorded_failed_checks(record)
            if recorded_failed:
                # Another review of this head would be withheld again while any of
                # its checks still fails, so it is not spent until one could pass.
                try:
                    failing = failed_checks(
                        list_pull_request_checks(repository, pull_request)
                    )
                except CommandError as error:
                    print(
                        "      -> Skipped: approval stays withheld; this head's "
                        f"checks could not be read: {error}"
                    )
                    return
                if failing:
                    print(
                        "      -> Skipped: approval stays withheld while "
                        f"{_describe_failed_checks(failing)} failed at this head; a "
                        "re-run that passes, a later head, or --force reopens it"
                    )
                    return
                print(
                    "      -> No check fails at this head any more; reconsidering "
                    "withheld approval"
                )
            elif not valid_recorded_blockers:
                print(
                    "      -> Stored withheld approval lacks validated blockers; "
                    "reconsidering"
                )
            elif not active_recorded_blockers:
                print(
                    "      -> Recorded blocking feedback is resolved; "
                    "reconsidering withheld approval"
                )
            else:
                print(
                    "      -> Existing review feedback changed; "
                    "reconsidering withheld approval"
                )

    if (
        destination == "github"
        and not reconsidering_delivered_review
        and has_current_review(
            repository,
            pull_request,
            account,
        )
    ):
        print("      -> Skipped: current head already has a submitted review")
        if live:
            store.set_status(
                state_key,
                "delivered",
                receipt_id="existing-github-review",
            )
        return

    if live and destination == "slack" and not settings.slack_webhook_url:
        raise ConfigError("SLACK_WEBHOOK_URL is required for live Slack delivery")

    recorded_conflict_base = (
        None if force else _recorded_conflict_base(store, state_key)
    )

    if live:
        store.set_status(state_key, "in_progress")

    unread_paths: "tuple[str, ...]" = ()
    unread_marks: "dict[str, str]" = {}

    try:
        initial_snapshot = get_pull_request_snapshot(repository, pull_request)
        _validate_pull_request_snapshot(
            initial_snapshot,
            pull_request,
            settings.excluded_pr_labels,
        )
        settled_conflict = (
            None
            if recorded_conflict_base is None
            else _settled_conflict(
                repository,
                pull_request,
                settings.excluded_pr_labels,
                initial_snapshot,
                recorded_conflict_base,
            )
        )
        if settled_conflict is not None:
            reason, conflict_base = settled_conflict
            print(f"      -> Skipped: {reason}")
            if live:
                store.set_status(
                    state_key,
                    "merge_conflict",
                    base_sha=conflict_base,
                )
            return
        if prior_comments is None:
            prior_comments = list_prior_review_comments(
                repository,
                pull_request,
                account,
            )
        if prior_comments:
            print(
                f"      -> Considering {len(prior_comments)} existing review "
                f"comment{'s' if len(prior_comments) != 1 else ''} for duplicates"
            )
        files = list_changed_files(
            repository,
            pull_request,
            initial_snapshot.changed_files,
        )
        generated_marks = _read_generated_marks(
            repository, pull_request, omitted_patch_paths(files)
        )
        material, extra_bundles = _bundled_material(
            files,
            settings,
            prior_comments,
            generated_marks,
            marks_unreadable=generated_marks is None,
        )
        if extra_bundles:
            print(
                f"      -> Reviewing in {len(extra_bundles) + 1} bundles: the "
                f"change is larger than one review can read"
            )
        _report_diff_budget(material)
        partial_budget = material.diff_budget
        if live and partial_budget is not None and partial_budget.is_partial:
            _notify_manual_review(
                settings,
                repository,
                pull_request,
                store,
                _partial_review_reason(partial_budget),
                partial_budget,
            )
        checks = list_pull_request_checks(repository, pull_request)
        material = replace(
            material,
            checks=checks,
            linked_issues=_read_linked_issues(repository, pull_request),
            file_history=_read_file_history(repository, pull_request, files),
        )
        if checks:
            print(
                f"      -> Considering {len(checks)} exact-head GitHub "
                f"check{'s' if len(checks) != 1 else ''}"
            )
        for path in material.ignored_missing_patch_files:
            if (generated_marks or {}).get(path) is True:
                print(
                    "      -> Skipping omitted patch marked linguist-generated on "
                    f"{pull_request.base_ref}: {path}"
                )
            else:
                print(
                    "      -> Skipping omitted patch matching configured pattern: "
                    f"{path}"
                )
        for path in material.textless_files:
            print(
                "      -> Reviewing without a changed line GitHub could diff, "
                f"such as a binary asset: {path}"
            )
        for moved in material.moved_files:
            origin = (
                f" from {moved.previous_path}"
                if moved.previous_path is not None
                else ""
            )
            print(f"      -> Noting rename without content change{origin}: {moved.path}")
        print(
            f"      -> Preparing exact PR workspace at "
            f"{pull_request.head_sha[:12]}...",
            flush=True,
        )
        workspace_root = settings.state_file.parent / "worktrees"
        workspace = review_workspace(
            repository,
            pull_request,
            workspace_root,
        )
        with workspace as exact_repository:
            background = read_background(exact_repository.path)
            if background:
                material = replace(material, background=background)
                print(
                    f"      -> Read {len(background)} characters of repository "
                    f"background from {BACKGROUND_FILE}"
                )
            if workspace.dependency_links:
                material = replace(
                    material,
                    dependency_links=workspace.dependency_links,
                )
                for link in workspace.dependency_links:
                    if link.lockfiles:
                        print(
                            f"      -> Linked installed {link.name} for "
                            f"{', '.join(link.lockfiles)} verification"
                        )
                    else:
                        print(
                            f"      -> Linked {link.packages} installed "
                            f"{link.name} packages at unchanged versions"
                        )
                for note in workspace.dependency_notes:
                    print(f"      -> Dependency evidence: {note}")
            if settings.design_level:
                _run_design_level(
                    settings,
                    repository,
                    exact_repository,
                    pull_request,
                    material,
                    engine,
                    verification,
                    store,
                    destination,
                    verification_activity,
                    live=live,
                    force=force,
                    fallback=fallback,
                )
            if settings.docs_level:
                _run_docs_level(
                    settings,
                    repository,
                    exact_repository,
                    pull_request,
                    material,
                    engine,
                    verification,
                    store,
                    destination,
                    verification_activity,
                    live=live,
                    force=force,
                    docs_fingerprint=documentation_change_fingerprint(files),
                    fallback=fallback,
                )
            # Read here, after the advisory levels: the rules bind the code level
            # alone, so failing to read them must not cost the other levels.
            review_rules = read_review_rules(repository, pull_request)
            if review_rules.sections:
                material = replace(material, review_rules=review_rules)
                _report_review_rules(review_rules)
            review_material = material
            rejected_candidates = 0
            (
                structured_output,
                findings,
                approve,
                approval_reason,
                blocking_comment_ids,
                review_settings,
                review_engine,
            ) = _review_with_engine_fallback(
                settings,
                engine,
                fallback,
                exact_repository,
                pull_request,
                review_material,
            )
            rejected_candidates = len(findings)
            if findings:
                (
                    structured_output,
                    findings,
                    approve,
                    approval_reason,
                    blocking_comment_ids,
                ) = _verify_code_candidates(
                    settings,
                    verification,
                    review_settings,
                    review_engine,
                    exact_repository,
                    pull_request,
                    material,
                    findings,
                    verification_activity=verification_activity,
                )
                # Attribution for delivery: name the verifier only when it was a
                # different engine or model than the one that reviewed, and name the
                # verifier's fallback when that is the engine that judged.
                verifier = _delivered_verifier(verification, settings, review_settings)
            for index, bundle in enumerate(extra_bundles, start=2):
                bundle_material = _bundle_material_from(material, bundle)
                print(
                    f"      -> Reviewing bundle {index} of "
                    f"{len(extra_bundles) + 1}: "
                    f"{bundle.diff_budget.reviewed_files} more files",
                    flush=True,
                )
                (
                    _,
                    bundle_findings,
                    _,
                    _,
                    _,
                    bundle_settings,
                    bundle_engine,
                ) = _review_with_engine_fallback(
                    settings,
                    engine,
                    fallback,
                    exact_repository,
                    pull_request,
                    bundle_material,
                )
                rejected_candidates += len(bundle_findings)
                if bundle_findings:
                    (
                        _,
                        bundle_findings,
                        _,
                        _,
                        _,
                    ) = _verify_code_candidates(
                        settings,
                        verification,
                        bundle_settings,
                        bundle_engine,
                        exact_repository,
                        pull_request,
                        bundle_material,
                        bundle_findings,
                        verification_activity=verification_activity,
                    )
                    # A later bundle may be the one that met a failed verifier.
                    verifier = _delivered_verifier(
                        verification, settings, review_settings
                    )
                if bundle_findings:
                    # A bundle speaks for its own files only. It may add findings and
                    # it may take an approval away, but no bundle can grant one: an
                    # approval speaks for the whole pull request.
                    findings = tuple(findings) + tuple(bundle_findings)
                    approve = False
                    approval_reason = ""
        budget = material.diff_budget
        if budget is not None and budget.is_partial:
            # The spec forbids approving a partial review, and the model was never
            # shown the deferred files, so its verdict cannot speak for them.
            approve = False
            approval_reason = (
                f"The review read {budget.reviewed_files} of "
                f"{budget.total_files} changed files, so "
                f"{len(budget.deferred)} were not reviewed"
            )
            unread_paths = tuple(
                sorted(deferred.path for deferred in budget.deferred)
            )
            unread_marks = _generated_mark_states(
                generated_marks,
                tuple(sorted(deferred.path for deferred in budget.unreadable)),
            )
        reviewer_engine = review_settings.review_engine
        reviewer_model = review_settings.review_model
        raw_findings = structured_output.get("findings", [])
        for raw_finding, finding in zip(raw_findings, findings):
            if not isinstance(raw_finding, dict):
                continue
            reported_path = raw_finding.get("path")
            if isinstance(reported_path, str) and reported_path != finding.path:
                print(
                    f"      -> Corrected finding path: "
                    f"{reported_path} -> {finding.path}"
                )
    except PullRequestIneligibleError as error:
        _record_ineligible_skip(store, state_key, error, live=live)
        return
    except DiffTooLargeError as error:
        print(f"      -> Skipped oversized pull request: {error}")
        if not live:
            return
        notified = _notify_manual_review(
            settings,
            repository,
            pull_request,
            store,
            str(error),
            None,
        )
        # Nothing was reviewed, so this head is only settled once a human has been
        # asked to review it. Recording it terminal first would lose the request
        # with the head that needed it.
        store.set_status(
            state_key,
            "skipped_oversized" if notified else "oversized_notification_pending",
            reason=str(error),
            max_diff_bytes=settings.max_diff_bytes,
        )
        return
    except PullRequestHeadChangedError as error:
        if live:
            store.set_status(
                state_key,
                "superseded",
                replacement_head_sha=error.found_head_sha,
            )
        raise
    except Exception as error:
        if live:
            store.set_status(state_key, "failed", error=str(error))
        raise

    clean_candidate = not findings and approve
    if not live:
        _print_findings(findings, approve)
        print("      -> Dry run: no external delivery and no terminal state recorded")
        return

    if not clean_candidate:
        _print_findings(findings, approve)

    if not findings and not approve:
        store.set_status(
            state_key,
            "approval_withheld",
            reason=approval_reason,
            blocking_comment_ids=list(blocking_comment_ids),
            feedback_fingerprint=_feedback_fingerprint(prior_comments),
            unread_paths=list(unread_paths),
            unread_settings_fingerprint=(
                _unread_settings_fingerprint(settings) if unread_paths else ""
            ),
            unread_generated_marks=unread_marks if unread_paths else {},
        )
        print(f"      -> Recorded as approval_withheld: {approval_reason}")
        return

    try:
        latest_prior_comments = list_prior_review_comments(
            repository,
            pull_request,
            account,
        )
    except CommandError as error:
        store.set_status(state_key, "failed", error=str(error))
        raise
    objections = _feedback_objections(prior_comments, latest_prior_comments)
    if objections:
        # An approval speaks for the whole pull request, so any unread objection
        # overtakes it. A finding speaks for one line, and feedback landing
        # somewhere else cannot have answered it.
        touched = _objections_touching_findings(objections, findings)
        if not findings or touched:
            _record_stale_feedback_skip(
                settings,
                repository,
                pull_request,
                store,
                state_key,
                objections,
                touched,
                findings,
                reviewer_engine,
                reviewer_model,
            )
            return
        print(
            "      -> Review feedback arrived during analysis away from these "
            f"findings: {_describe_feedback_objections(objections)}"
        )

    closed_during_review = False
    try:
        final_snapshot = get_pull_request_snapshot(repository, pull_request)
        _validate_pull_request_snapshot(
            final_snapshot,
            pull_request,
            settings.excluded_pr_labels,
        )
    except PullRequestClosedError as error:
        # A merge or a close ends the approval, not the finding. Discarding a
        # confirmed defect here would throw away the review's whole result over
        # timing, and the defect is now in the base branch where nobody is
        # looking for it.
        if not findings:
            _record_ineligible_skip(store, state_key, error, live=live)
            return
        closed_during_review = True
        print(f"      -> {error}; delivering findings without an approval")
    except PullRequestIneligibleError as error:
        _record_ineligible_skip(store, state_key, error, live=live)
        return
    except (CommandError, ReviewError) as error:
        store.set_status(state_key, "failed", error=str(error))
        raise

    if not findings:
        if final_snapshot.mergeable is None:
            try:
                final_snapshot = _await_mergeability(
                    repository,
                    pull_request,
                    settings.excluded_pr_labels,
                    final_snapshot,
                )
            except PullRequestIneligibleError as error:
                _record_ineligible_skip(store, state_key, error, live=live)
                return
            except (CommandError, ReviewError) as error:
                store.set_status(state_key, "failed", error=str(error))
                raise
        mergeable = final_snapshot.mergeable
        if mergeable is None:
            error = ReviewError(
                "GitHub has not finished calculating pull-request mergeability; "
                "retry later"
            )
            store.set_status(state_key, "failed", error=str(error))
            raise error
        if not mergeable:
            _print_findings(findings, False)
            print("      -> Approval withheld: pull request has merge conflicts")
            base_sha = _conflict_base_sha(repository, final_snapshot)
            store.set_status(state_key, "merge_conflict", base_sha=base_sha)
            if base_sha:
                print(
                    "      -> Recorded as merge_conflict against "
                    f"{final_snapshot.base_ref} at {base_sha[:12]}; retried when "
                    "either side moves"
                )
            else:
                print(
                    "      -> Recorded as merge_conflict; this head remains retryable"
                )
            return
        # Read again rather than reusing the checks the review started with: CI
        # often finishes while a review runs, and an approval speaks for the head
        # as it is now. A failed check is the head failing a test the team chose,
        # which no review verdict outweighs.
        try:
            failing = failed_checks(list_pull_request_checks(repository, pull_request))
        except CommandError as error:
            store.set_status(state_key, "failed", error=str(error))
            raise
        if failing:
            _print_findings(findings, False)
            reason = f"GitHub {_describe_failed_checks(failing)} failed at this head"
            print(f"      -> Approval withheld: {reason}")
            store.set_status(
                state_key,
                "approval_withheld",
                reason=reason,
                blocking_comment_ids=[],
                feedback_fingerprint=_feedback_fingerprint(latest_prior_comments),
                failed_checks=list(failing),
            )
            print(
                "      -> Recorded as approval_withheld; a re-run that passes or a "
                "later head reopens it"
            )
            return

    if clean_candidate:
        _print_findings(findings, approve)

    store.set_status(state_key, "delivery_started")
    try:
        if not findings and destination == "github":
            receipt_id = submit_approval(repository, pull_request)
        elif not findings and destination == OWN_GITHUB_DESTINATION:
            idle_verifier = _verifier_identity(settings)
            if idle_verifier == (reviewer_engine, reviewer_model):
                idle_verifier = (None, None)
            receipt_id = submit_lgtm_comment(
                repository,
                pull_request,
                format_lgtm_comment(
                    pull_request,
                    reviewer_engine,
                    reviewer_model,
                    *verifier,
                    rejected_candidates=rejected_candidates,
                    idle_verifier=idle_verifier,
                ),
            )
        elif not findings:
            assert settings.slack_webhook_url is not None
            receipt_id = send_slack_message(
                settings.slack_webhook_url,
                format_slack_lgtm_message(
                    repository,
                    pull_request,
                    reviewer_engine,
                    reviewer_model,
                    *verifier,
                    rejected_candidates=rejected_candidates,
                ),
            )
        elif destination == "slack":
            assert settings.slack_webhook_url is not None
            receipt_id = send_slack_message(
                settings.slack_webhook_url,
                format_slack_message(
                    repository,
                    pull_request,
                    findings,
                    reviewer_engine,
                    reviewer_model,
                    *verifier,
                ),
            )
        else:
            receipt_id = submit_review(
                repository,
                pull_request,
                findings,
                reviewer_engine,
                reviewer_model,
                settings.github_comment_template,
                *verifier,
                body=_partial_review_note(material.diff_budget),
            )
    except DeliveryError as error:
        store.set_status(state_key, "failed", error=str(error))
        raise
    except CommentTemplateError as error:
        store.set_status(state_key, "failed", error=str(error))
        raise
    except (DeliveryUncertainError, CommandError) as error:
        store.set_status(state_key, "delivery_uncertain", error=str(error))
        raise DeliveryUncertainError(str(error)) from error

    delivery_details = {"receipt_id": receipt_id}
    if closed_during_review:
        delivery_details["closed_during_review"] = True
    store.set_status(state_key, "delivered", **delivery_details)
    action = (
        "Approved"
        if not findings and destination == "github"
        else "Posted LGTM"
        if not findings and destination == OWN_GITHUB_DESTINATION
        else "Sent LGTM" if not findings else "Delivered"
    )
    print(f"      -> {action} successfully (receipt: {receipt_id})")
    if destination == OWN_GITHUB_DESTINATION:
        _hide_earlier_lgtm_comments(
            repository,
            pull_request,
            account,
            receipt_id if not findings else None,
        )


def _hide_earlier_lgtm_comments(
    repository: Repository,
    pull_request: PullRequest,
    account: str,
    keep_comment_id: Optional[str],
) -> None:
    """Collapse the LGTM comments this result has replaced, after it is posted.

    Only after: the pull request must never be left without a verdict. A failure
    here is cosmetic, since the current result is delivered and recorded, so it is
    reported and the run carries on; hiding is never retried blindly.
    """

    try:
        hidden = hide_earlier_lgtm_comments(
            repository, pull_request, account, keep_comment_id
        )
    except CommandError as error:
        print(f"      -> Earlier LGTM comments were left visible: {error}")
        return
    if hidden:
        print(
            f"      -> Marked {hidden} earlier LGTM comment"
            f"{'s' if hidden != 1 else ''} as outdated"
        )


def _process_pull_request_with_head_restarts(
    settings: Settings,
    repository: Repository,
    pull_request: PullRequest,
    store: JsonStateStore,
    engine: ReviewEngine,
    *,
    live: bool,
    force: bool,
    verification: Optional[VerificationPass] = None,
    verification_activity: Optional[_VerificationActivity] = None,
    fallback: Optional[FallbackReviewer] = None,
    usage: Optional[_ModelUsage] = None,
    metrics: Optional[ReviewMetrics] = None,
) -> None:
    current_pull_request = pull_request
    # Measured per pull request, restarts included: a head that moved mid-review
    # spends a second full review, and that is exactly the cost worth seeing.
    pull_request_usage = _ModelUsage()

    def measured(engine_to_wrap: ReviewEngine, role: str) -> _MeasuredEngine:
        return _MeasuredEngine(
            engine_to_wrap,
            pull_request_usage,
            role,
            metrics,
            repository,
        )

    measured_engine = measured(engine, "review")
    measured_verification = (
        replace(
            verification,
            engine=measured(verification.engine, "verify"),
            fallback=(
                replace(
                    verification.fallback,
                    engine=measured(verification.fallback.engine, "verify fallback"),
                )
                if verification.fallback is not None
                else None
            ),
        )
        if verification is not None
        else None
    )
    # Wrapped per pull request, so a hand-over holds for this pull request's
    # verification and never decides another's.
    measured_verification = _with_verification_fallback(measured_verification)
    measured_fallback = (
        replace(fallback, engine=measured(fallback.engine, "fallback"))
        if fallback is not None
        else None
    )
    try:
        _review_with_head_restarts(
            settings,
            repository,
            current_pull_request,
            store,
            measured_engine,
            live=live,
            force=force,
            verification=measured_verification,
            verification_activity=verification_activity,
            fallback=measured_fallback,
        )
    finally:
        spend = pull_request_usage.describe()
        if spend:
            print(f"      -> Spent {spend}")
        if usage is not None:
            usage.merge(pull_request_usage)


def _write_metrics(
    path: str,
    metrics: ReviewMetrics,
    settings: Settings,
) -> None:
    """Write the run's measurement, reporting a failure without failing the run."""

    document = metrics.document(
        engine=settings.review_engine,
        model=settings.review_model,
    )
    try:
        destination = Path(path)
        if destination.parent != Path(""):
            destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        print(f"Could not write the run measurement to {path}: {error}")
        return
    summary = document["summary"]
    print(
        f"Measurement written to {path}: {summary['invocations']} invocations "
        f"across {summary['pull_requests']} pull request"
        f"{'s' if summary['pull_requests'] != 1 else ''}"
    )


def _review_with_head_restarts(
    settings: Settings,
    repository: Repository,
    pull_request: PullRequest,
    store: JsonStateStore,
    engine: ReviewEngine,
    *,
    live: bool,
    force: bool,
    verification: Optional[VerificationPass] = None,
    verification_activity: Optional[_VerificationActivity] = None,
    fallback: Optional[FallbackReviewer] = None,
) -> None:
    current_pull_request = pull_request
    for restart_count in range(MAX_HEAD_CHANGE_RESTARTS + 1):
        try:
            _process_pull_request(
                settings,
                repository,
                current_pull_request,
                store,
                engine,
                live=live,
                force=force,
                verification=verification,
                verification_activity=verification_activity,
                fallback=fallback,
            )
            return
        except PullRequestHeadChangedError as error:
            if restart_count >= MAX_HEAD_CHANGE_RESTARTS:
                raise ReviewError(
                    "Pull-request head changed after "
                    f"{MAX_HEAD_CHANGE_RESTARTS} review restarts; latest transition: "
                    f"{error.expected_head_sha} -> {error.found_head_sha}"
                ) from error
            print(
                "      -> Pull-request head changed; restarting review "
                f"{restart_count + 1}/{MAX_HEAD_CHANGE_RESTARTS} against "
                f"{error.found_head_sha[:12]}...",
                flush=True,
            )
            current_pull_request = replace(
                current_pull_request,
                head_sha=error.found_head_sha,
            )

    raise AssertionError("unreachable")


@dataclass(frozen=True)
class _ReviewTask:
    """One pull request selected for review."""

    repository: Repository
    pull_request: PullRequest


def _discover_review_tasks(
    settings: Settings,
    repositories: "Sequence[Repository]",
    *,
    requested_pr: Optional[int],
    errors: "list[str]",
    incident_notice: "_GithubIncidentNotice",
) -> "list[_ReviewTask]":
    """List every eligible pull request before any review starts.

    Discovery finishes first so the run can report the complete plan, and so a
    bounded pool can draw work from all repositories rather than emptying one
    repository at a time.
    """
    tasks: "list[_ReviewTask]" = []
    for repository in repositories:
        try:
            pull_requests = list_open_prs(repository, settings.excluded_pr_labels)
        except CommandError as error:
            errors.append(f"{repository.slug}: {error}")
            print(
                _repository_header(f"Checking {repository.name}: ERROR: {error}"),
                file=sys.stderr,
            )
            incident_notice.report(error)
            continue
        if requested_pr is not None:
            pull_requests = [pr for pr in pull_requests if pr.number == requested_pr]
            if not pull_requests:
                errors.append(
                    f"{repository.slug}#{requested_pr}: pull request is not open "
                    "or is excluded from review"
                )
                print(
                    _repository_header(
                        f"Checking {repository.name}: PR #{requested_pr} is not open "
                        "or is excluded from review"
                    )
                )
                continue
        tasks.extend(
            _ReviewTask(repository, pull_request) for pull_request in pull_requests
        )
    return tasks


def _print_review_plan(
    tasks: "Sequence[_ReviewTask]",
    concurrency: int,
    reviewer_username: str,
) -> None:
    """Report every selected pull request before the first review starts."""
    if not tasks:
        print(_repository_header("No pull requests selected for review."))
        return
    subject = "pull request" if len(tasks) == 1 else "pull requests"
    # The pool never runs more reviews than there are pull requests, and a single
    # one is reviewed on its own, so the plan states what will actually happen.
    together = min(concurrency, len(tasks))
    if len(tasks) == 1:
        pace = ""
    elif together == 1:
        pace = ", one at a time"
    else:
        pace = f", {together} at a time"
    print(_repository_header(f"Reviewing {len(tasks)} {subject}{pace}:"))
    width = max(len(task.repository.name) for task in tasks)
    for task in tasks:
        pull_request = task.pull_request
        destination = _DESTINATION_LABELS[
            _destination(task.repository, pull_request, reviewer_username)
        ]
        print(
            f"    - {task.repository.name:<{width}}  "
            f"#{pull_request.number} {_shorten(pull_request.title, PLAN_TITLE_WIDTH)} "
            f"by @{pull_request.author} -> {destination}"
        )


def _execute_review_tasks(
    tasks: "Sequence[_ReviewTask]",
    settings: Settings,
    store: JsonStateStore,
    engine: ReviewEngine,
    *,
    concurrency: int,
    live: bool,
    force: bool,
    verification: Optional["_VerificationPass"],
    verification_activity: "_VerificationActivity",
    fallback: Optional["_FallbackReviewer"],
    incident_notice: "_GithubIncidentNotice",
    usage: Optional["_ModelUsage"] = None,
    metrics: Optional[ReviewMetrics] = None,
) -> "list[str]":
    """Review the selected pull requests and return their failure details.

    One review at a time keeps its live report on the terminal. Reviews running
    together would interleave into an unreadable stream, so each one records its
    own output, the board reports progress while they run, and the recordings
    print in the planned order once every review has finished.
    """
    if not tasks:
        return []
    if concurrency == 1 or len(tasks) == 1:
        return _review_tasks_in_sequence(
            tasks,
            settings,
            store,
            engine,
            live=live,
            force=force,
            verification=verification,
            verification_activity=verification_activity,
            fallback=fallback,
            incident_notice=incident_notice,
            usage=usage,
            metrics=metrics,
        )

    errors: "dict[str, str]" = {}
    # Keyed as reviews start rather than returned when they finish, so an
    # interrupted run can still report everything its reviews had reported.
    recordings: "dict[str, progress.Recorder]" = {}
    recordings_guard = threading.Lock()
    interrupted = False
    print()
    # The board draws on the terminal and each report is printed after every
    # review ends, so without this the transcript of a running parallel run shows
    # its plan and nothing else until the last review finishes.
    progress.journal("")
    progress.journal("Live progress, timestamped as each step was reported:")
    with progress.routed_output():
        with _ReviewBoard(tasks) as board:

            def review(task: _ReviewTask) -> None:
                label = (
                    f"{task.repository.name} #{task.pull_request.number}"
                )
                with progress.recording(label) as recorder:
                    with recordings_guard:
                        recordings[_ReviewBoard.key(task)] = recorder
                    board.start(task, recorder)
                    try:
                        _process_pull_request_with_head_restarts(
                            settings,
                            task.repository,
                            task.pull_request,
                            store,
                            engine,
                            live=live,
                            force=force,
                            verification=verification,
                            verification_activity=verification_activity,
                            fallback=fallback,
                            usage=usage,
                            metrics=metrics,
                        )
                    except REVIEW_FAILURES as error:
                        key = (
                            f"{task.repository.slug}#{task.pull_request.number}"
                        )
                        errors[key] = f"{key}: {error}"
                        print(f"      -> ERROR: {error}", file=sys.stderr)
                        incident_notice.report(error)
                    except Exception as error:
                        key = (
                            f"{task.repository.slug}#{task.pull_request.number}"
                        )
                        errors[key] = _report_crashed_review(task, error)
                    board.finish(task, _outcome(recorder))

            try:
                with ThreadPoolExecutor(
                    max_workers=concurrency,
                    thread_name_prefix="prwarden-review",
                ) as pool:
                    list(pool.map(review, tasks))
            except KeyboardInterrupt:
                # Ctrl+C reaches the review commands too, so the workers are
                # already unwinding. Report what they recorded before raising.
                interrupted = True

    heading = "Full review log (interrupted):" if interrupted else "Full review log:"
    print(_repository_header(heading))
    for task in tasks:
        recorder = recordings.get(_ReviewBoard.key(task))
        if recorder is None:
            continue
        print()
        print(
            f"    - {task.repository.name} "
            f"#{task.pull_request.number} ({task.pull_request.title!r})"
        )
        recorder.replay()
    if interrupted:
        raise KeyboardInterrupt
    return [errors[key] for key in sorted(errors)]


def _review_tasks_in_sequence(
    tasks: "Sequence[_ReviewTask]",
    settings: Settings,
    store: JsonStateStore,
    engine: ReviewEngine,
    *,
    live: bool,
    force: bool,
    verification: Optional["_VerificationPass"],
    verification_activity: "_VerificationActivity",
    fallback: Optional["_FallbackReviewer"],
    incident_notice: "_GithubIncidentNotice",
    usage: Optional["_ModelUsage"] = None,
    metrics: Optional[ReviewMetrics] = None,
) -> "list[str]":
    """Review one pull request at a time, reporting each step as it happens."""
    errors: "list[str]" = []
    print()
    for index, task in enumerate(tasks):
        if index:
            print()
        try:
            _process_pull_request_with_head_restarts(
                settings,
                task.repository,
                task.pull_request,
                store,
                engine,
                live=live,
                force=force,
                verification=verification,
                verification_activity=verification_activity,
                fallback=fallback,
                usage=usage,
                metrics=metrics,
            )
        except REVIEW_FAILURES as error:
            errors.append(
                f"{task.repository.slug}#{task.pull_request.number}: {error}"
            )
            print(f"      -> ERROR: {error}", file=sys.stderr)
            incident_notice.report(error)
        except Exception as error:
            errors.append(_report_crashed_review(task, error))
    return errors


class _AdvisoryReviewer:
    """The engine an advisory level reviews with, handed to the fallback once.

    The code level always had a fallback and the advisory levels had none, so a
    primary that could not answer cost them every review: from 2026-10-13 a model
    Antigravity had withdrawn failed 48 design and 48 documentation reviews in a week
    while the code level carried on through its fallback. They now hand over the same
    way, on an engine failure only. A validation failure stays with the engine that
    produced the answer, because that is the engine told how to correct it.

    Only the primary's refusals are recorded against the primary: a spent fallback
    says nothing about whether the primary can answer the next level.
    """

    def __init__(
        self,
        noun: str,
        settings: Settings,
        engine: ReviewEngine,
        fallback: Optional[FallbackReviewer],
    ) -> None:
        self.noun = noun
        self.primary_label = _engine_label(settings)
        self.primary_error: Optional[EngineError] = None
        self.settings = settings
        self.engine = engine
        self.fallback = fallback
        self.on_primary = True
        spent = _PRIMARY_ENGINE.exhausted_reason
        if not spent:
            return
        if fallback is None:
            # With the quota gone and nothing to hand over to, a call can only spend
            # a refusal to reach the same answer. Saying so costs nothing and keeps
            # the code level's budget for the one level that can approve.
            raise EngineError(
                f"{self.primary_label} is out of quota for this run ({spent})"
            )
        print(
            f"      -> {self.primary_label} is out of quota for this run ({spent}); "
            f"running the {noun} with {_engine_label(fallback.settings)} directly",
            flush=True,
        )
        self._hand_over()

    def _hand_over(self) -> None:
        assert self.fallback is not None
        self.settings = self.fallback.settings
        self.engine = self.fallback.engine
        self.fallback = None
        self.on_primary = False

    def review(
        self,
        repository: Repository,
        pull_request: PullRequest,
        material: ReviewMaterial,
    ) -> Any:
        heading = self.noun[0].upper() + self.noun[1:]
        started = time.monotonic()
        try:
            with _ElapsedProgressLine(heading, started):
                output = self.engine.review(repository, pull_request, material)
        except Exception as error:
            elapsed = _format_duration(time.monotonic() - started)
            print(f"      -> {heading} failed after {elapsed}", flush=True)
            if not self.on_primary:
                if self.primary_error is not None and isinstance(error, EngineError):
                    raise EngineError(
                        f"{self.primary_label} {self.noun} failed: "
                        f"{self.primary_error}; {_engine_label(self.settings)} "
                        f"fallback also failed: {error}"
                    ) from error
                raise
            _note_exhausted_quota(error)
            if self.fallback is None or not isinstance(error, EngineError):
                raise
            print(
                f"      -> {self.primary_label} {self.noun} engine failed: {error}",
                flush=True,
            )
            self.primary_error = error
            self._hand_over()
            print(
                f"      -> Running fallback {self.noun} with "
                f"{_engine_label(self.settings)} (timeout: "
                f"{_format_duration(self.settings.review_timeout_seconds)})...",
                flush=True,
            )
            return self.review(repository, pull_request, material)
        print(
            f"      -> {heading} completed in "
            f"{_format_duration(time.monotonic() - started)}",
            flush=True,
        )
        return output


def _design_level(
    settings: Settings,
    engine: ReviewEngine,
    verification: Optional[VerificationPass],
    repository: Repository,
    pull_request: PullRequest,
    material: ReviewMaterial,
    verification_activity: Optional[_VerificationActivity] = None,
    fallback: Optional[FallbackReviewer] = None,
) -> DesignAssessment:
    """Review the solution design, then have the verifier judge each candidate.

    Raises ReviewError when this level cannot produce a verified judgment. The
    caller reports that as a failure of this level alone, because the design level
    is advisory and must never decide the fate of the code review. An unverified
    candidate is never returned: a design note nobody checked would carry the
    reviewer's name without the reviewer's evidence.
    """

    reviewer = _AdvisoryReviewer("design review", settings, engine, fallback)
    design_material = replace(
        material,
        level=DESIGN_LEVEL,
        max_design_findings=settings.max_design_findings,
        verification_candidates=(),
        validation_error=None,
    )
    print(
        f"      -> Reviewing solution design with {_engine_label(reviewer.settings)} "
        f"(timeout: {_format_duration(settings.review_timeout_seconds)})...",
        flush=True,
    )
    attempt_material = design_material
    for attempt in range(1, MAX_OUTPUT_VALIDATION_ATTEMPTS + 1):
        structured_output = reviewer.review(
            repository,
            pull_request,
            attempt_material,
        )
        try:
            understanding, verdict, findings = validate_design_findings(
                structured_output,
                material,
                repository.path,
                settings.max_design_findings,
            )
        except ReviewError as error:
            if attempt == MAX_OUTPUT_VALIDATION_ATTEMPTS:
                raise
            print(f"      -> Design output rejected: {error}")
            print(
                "      -> Retrying the design review once with controller "
                "validation feedback...",
                flush=True,
            )
            attempt_material = replace(
                design_material, validation_error=str(error)
            )
            continue
        break

    if not findings:
        return DesignAssessment(
            understanding=understanding,
            verdict=verdict,
            reviewer_engine=reviewer.settings.review_engine,
            reviewer_model=reviewer.settings.review_model,
        )

    # With no separate verifier the engine that reviewed verifies, which is the
    # fallback when the primary is the one that just failed.
    verification_settings = (
        verification.settings if verification is not None else reviewer.settings
    )
    verification_engine = (
        verification.engine if verification is not None else reviewer.engine
    )
    verification_timeout = _verification_timeout_seconds(settings, len(findings))
    print(
        f"      -> Adversarially verifying {len(findings)} design candidate "
        f"finding{'s' if len(findings) != 1 else ''} with "
        f"{_engine_label(verification_settings)} "
        f"(timeout: {_format_duration(verification_timeout)})...",
        flush=True,
    )
    candidates = tuple(findings)
    # The design review already retries once when validation rejects its output.
    # This pass needs the same allowance: without it a single over-long reason
    # discards a design review and its verification, minutes of work for a
    # formatting slip the engine can correct when told what was wrong.
    judgment_material = replace(design_material, design_candidates=candidates)
    for attempt in range(1, MAX_OUTPUT_VALIDATION_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            with _ElapsedProgressLine("Design verification", started):
                judgment_output = verification_engine.review(
                    repository,
                    pull_request,
                    judgment_material,
                    timeout_seconds=verification_timeout,
                )
        except Exception:
            elapsed = _format_duration(time.monotonic() - started)
            print(f"      -> Design verification failed after {elapsed}", flush=True)
            raise
        print(
            f"      -> Design verification completed in "
            f"{_format_duration(time.monotonic() - started)}",
            flush=True,
        )
        if verification_activity is not None:
            verification_activity.record()
        try:
            confirmed, rejected = validate_design_judgments(
                judgment_output,
                candidates,
            )
        except ReviewError as error:
            if attempt == MAX_OUTPUT_VALIDATION_ATTEMPTS:
                print(
                    "      -> Design verification correction retry rejected: "
                    f"{error}",
                    flush=True,
                )
                raise
            print(f"      -> Design verification output rejected: {error}")
            print(
                "      -> Retrying the design verification once with controller "
                "validation feedback...",
                flush=True,
            )
            judgment_material = replace(
                judgment_material, validation_error=str(error)
            )
            continue
        break
    verifier_engine, verifier_model = _handed_over_verifier(verification_engine)
    return DesignAssessment(
        understanding=understanding,
        verdict=verdict,
        findings=confirmed,
        rejected=rejected,
        reviewer_engine=reviewer.settings.review_engine,
        reviewer_model=reviewer.settings.review_model,
        verifier_engine=verifier_engine,
        verifier_model=verifier_model,
    )


def _docs_level(
    settings: Settings,
    engine: ReviewEngine,
    verification: Optional[VerificationPass],
    repository: Repository,
    pull_request: PullRequest,
    material: ReviewMaterial,
    verification_activity: Optional[_VerificationActivity] = None,
    fallback: Optional[FallbackReviewer] = None,
) -> DocsAssessment:
    """Check the change against this repository's documentation, then verify it.

    Raises ReviewError when this level cannot produce a verified judgment, which the
    caller reports as a failure of this level alone: proposing a documentation edit
    is advisory work and must never decide the fate of the code review.
    """

    reviewer = _AdvisoryReviewer("documentation review", settings, engine, fallback)
    docs_material = replace(
        material,
        level=DOCS_LEVEL,
        max_docs_findings=settings.max_docs_findings,
        documentation_files=discover_documentation(repository.path),
        verification_candidates=(),
        design_candidates=(),
        validation_error=None,
    )
    print(
        f"      -> Checking {len(docs_material.documentation_files)} documentation "
        f"files with {_engine_label(reviewer.settings)} "
        f"(timeout: {_format_duration(settings.review_timeout_seconds)})...",
        flush=True,
    )
    attempt_material = docs_material
    for attempt in range(1, MAX_OUTPUT_VALIDATION_ATTEMPTS + 1):
        structured_output = reviewer.review(
            repository,
            pull_request,
            attempt_material,
        )
        try:
            understanding, verdict, findings = validate_docs_findings(
                structured_output,
                docs_material,
                repository.path,
                settings.max_docs_findings,
            )
        except ReviewError as error:
            if attempt == MAX_OUTPUT_VALIDATION_ATTEMPTS:
                raise
            print(f"      -> Documentation output rejected: {error}")
            print(
                "      -> Retrying the documentation review once with controller "
                "validation feedback...",
                flush=True,
            )
            attempt_material = replace(docs_material, validation_error=str(error))
            continue
        break

    if not findings:
        return DocsAssessment(
            understanding=understanding,
            verdict=verdict,
            reviewer_engine=reviewer.settings.review_engine,
            reviewer_model=reviewer.settings.review_model,
        )

    verification_settings = (
        verification.settings if verification is not None else reviewer.settings
    )
    verification_engine = (
        verification.engine if verification is not None else reviewer.engine
    )
    verification_timeout = _verification_timeout_seconds(settings, len(findings))
    print(
        f"      -> Adversarially verifying {len(findings)} documentation "
        f"proposal{'s' if len(findings) != 1 else ''} with "
        f"{_engine_label(verification_settings)} "
        f"(timeout: {_format_duration(verification_timeout)})...",
        flush=True,
    )
    candidates = tuple(findings)
    judgment_material = replace(docs_material, docs_candidates=candidates)
    for attempt in range(1, MAX_OUTPUT_VALIDATION_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            with _ElapsedProgressLine("Documentation verification", started):
                judgment_output = verification_engine.review(
                    repository,
                    pull_request,
                    judgment_material,
                    timeout_seconds=verification_timeout,
                )
        except Exception:
            elapsed = _format_duration(time.monotonic() - started)
            print(
                f"      -> Documentation verification failed after {elapsed}",
                flush=True,
            )
            raise
        print(
            f"      -> Documentation verification completed in "
            f"{_format_duration(time.monotonic() - started)}",
            flush=True,
        )
        if verification_activity is not None:
            verification_activity.record()
        try:
            confirmed, rejected = validate_docs_judgments(
                judgment_output,
                candidates,
            )
        except ReviewError as error:
            if attempt == MAX_OUTPUT_VALIDATION_ATTEMPTS:
                print(
                    "      -> Documentation verification correction retry "
                    f"rejected: {error}",
                    flush=True,
                )
                raise
            print(f"      -> Documentation verification output rejected: {error}")
            print(
                "      -> Retrying the documentation verification once with "
                "controller validation feedback...",
                flush=True,
            )
            judgment_material = replace(
                judgment_material, validation_error=str(error)
            )
            continue
        break
    verifier_engine, verifier_model = _handed_over_verifier(verification_engine)
    return DocsAssessment(
        understanding=understanding,
        verdict=verdict,
        findings=confirmed,
        rejected=rejected,
        reviewer_engine=reviewer.settings.review_engine,
        reviewer_model=reviewer.settings.review_model,
        verifier_engine=verifier_engine,
        verifier_model=verifier_model,
    )


def _report_docs_assessment(assessment: DocsAssessment, candidates: int) -> None:
    """Print what the documentation level concluded, rejections included."""

    if candidates:
        print(
            f"      -> Confirmed {len(assessment.findings)} of {candidates} "
            f"documentation proposal{'s' if candidates != 1 else ''}"
        )
    for rejection in assessment.rejected:
        print(
            f"      -> Rejected documentation proposal "
            f"[{rejection.finding.severity}] ({rejection.finding.category}) "
            f"{rejection.finding.title}"
        )
        print(f"         Rejected because: {rejection.reason}")
    for finding in assessment.findings:
        print(
            f"      -> [{finding.severity}] {finding.documentation.path}:"
            f"{finding.documentation.line} needs an update ({finding.category}): "
            f"{finding.title}"
        )
    if not assessment.findings:
        print("      -> Documentation still matches the change")


def _deliver_docs_notes(
    settings: Settings,
    repository: Repository,
    pull_request: PullRequest,
    assessment: DocsAssessment,
    destination: str,
    store: JsonStateStore,
    state_key: str,
    reviewer_engine: str,
    reviewer_model: Optional[str],
    verifier: "tuple[Optional[str], Optional[str]]",
    *,
    live: bool,
    docs_fingerprint: Optional[str],
) -> None:
    """Post proposed documentation updates, revalidating the pull request first.

    Every record written here carries the documentation edits it was judged on, so
    only a later change to them can earn this pull request another check.
    """

    _validate_pull_request_snapshot(
        get_pull_request_snapshot(repository, pull_request),
        pull_request,
        settings.excluded_pr_labels,
    )
    if not live:
        print(
            f"      -> Would propose {len(assessment.findings)} documentation "
            f"update{'s' if len(assessment.findings) != 1 else ''} (dry run)"
        )
        return

    store.set_status(
        state_key, "delivery_started", docs_fingerprint=docs_fingerprint
    )
    try:
        if destination == "slack":
            if settings.slack_webhook_url is None:
                raise DeliveryError(
                    "Documentation updates for your own pull request need "
                    "SLACK_WEBHOOK_URL"
                )
            receipt_id = send_slack_message(
                settings.slack_webhook_url,
                format_docs_slack_message(
                    repository,
                    pull_request,
                    assessment.findings,
                    reviewer_engine,
                    reviewer_model,
                    *verifier,
                ),
            )
        else:
            receipt_id = submit_docs_notes(
                repository,
                pull_request,
                format_docs_comment(
                    assessment.findings,
                    reviewer_engine,
                    reviewer_model,
                    *verifier,
                ),
            )
    except (DeliveryUncertainError, CommandError) as error:
        store.set_status(
            state_key,
            "delivery_uncertain",
            error=str(error),
            docs_fingerprint=docs_fingerprint,
        )
        print(
            f"      -> Documentation updates may or may not have posted: {error}"
        )
        return
    except DeliveryError as error:
        store.set_status(
            state_key,
            "failed",
            error=str(error),
            docs_fingerprint=docs_fingerprint,
        )
        print(f"      -> Documentation updates were not posted: {error}")
        return
    store.set_status(
        state_key,
        "delivered",
        receipt_id=receipt_id,
        findings=len(assessment.findings),
        docs_fingerprint=docs_fingerprint,
    )
    print(
        f"      -> Proposed {len(assessment.findings)} documentation update"
        f"{'s' if len(assessment.findings) != 1 else ''} (receipt: {receipt_id})"
    )


def _run_docs_level(
    settings: Settings,
    repository: Repository,
    exact_repository: Repository,
    pull_request: PullRequest,
    material: ReviewMaterial,
    engine: ReviewEngine,
    verification: Optional[VerificationPass],
    store: JsonStateStore,
    destination: str,
    verification_activity: Optional[_VerificationActivity] = None,
    *,
    live: bool,
    force: bool,
    docs_fingerprint: Optional[str],
    fallback: Optional[FallbackReviewer] = None,
) -> None:
    """Run the advisory documentation level and deliver what it confirmed.

    Contained exactly like the design level: this level informs, so a fault here
    must not stop the code review, withhold an approval, or change the exit code.

    Keyed by pull request rather than by head, for the same reason the design level
    is: the documentation a change outdates is a property of the change, and asking
    again on every push would post a second comment proposing much the same edit.
    The one push that earns a second look is one that changes what this pull request
    does to its documentation, recorded as `docs_fingerprint`: that is new text the
    level has never read, and in a repository that is mostly documentation it is
    nearly every push.
    """

    if _is_release_promotion(settings, pull_request):
        print(
            f"      -> Documentation level skipped: {pull_request.head_ref} -> "
            f"{pull_request.base_ref} promotes reviewed work whose documentation "
            "was already checked"
        )
        return

    state_key = store.pull_request_key(
        repository.slug,
        pull_request.number,
        "docs",
    )
    # Any record at all means this level may have posted here before: one that is
    # not terminal is a delivery that started and never recorded its outcome.
    record = store.get(state_key)
    again = force or record is not None
    if not force and store.is_settled_for_pull_request(
        repository.slug,
        pull_request.number,
        "docs",
    ):
        record = record or {}
        settled = record.get("status") or "settled at an earlier head"
        # A record written before fingerprints existed reads as a check of no
        # documentation edits: a pull request that edits none stays settled, and
        # one that does is checked once more and then remembered.
        if record.get("docs_fingerprint") == docs_fingerprint:
            print(
                "      -> Documentation level already settled for this pull "
                f"request ({settled}); its documentation edits are unchanged, so "
                "not checking the documentation again"
            )
            return
        print(
            "      -> Documentation edits in this pull request changed since the "
            f"last documentation check ({settled}); checking the documentation "
            "again"
        )
        again = True

    if not discover_documentation(exact_repository.path):
        # No invocation to spend: nothing here can be contradicted.
        print(
            "      -> Documentation level skipped: this repository carries no "
            "documentation to check"
        )
        return

    try:
        material = _with_own_notes(
            settings,
            repository,
            pull_request,
            material,
            DOCS_COMMENT_MARKER,
            "documentation",
            again=again,
        )
        assessment = _docs_level(
            settings,
            engine,
            verification,
            exact_repository,
            pull_request,
            material,
            verification_activity,
            fallback,
        )
    except PullRequestHeadChangedError:
        raise
    except (ReviewError, EngineError, CommandError) as error:
        # The level has already recorded a refusal from the primary. This error
        # may be the verifier's or the fallback's, and recording it would
        # send the code review past a primary that can still answer.
        print(f"      -> Documentation level failed: {error}")
        return
    except Exception as error:
        # A defect in this level is still this level's failure: it informs the
        # review, so it may not take the code review or the run down with it.
        print(
            "      -> Documentation level failed unexpectedly: "
            f"{type(error).__name__}: {error}"
        )
        return

    candidates = len(assessment.findings) + len(assessment.rejected)
    print(f"      -> Documentation judged {assessment.verdict}")
    _report_docs_assessment(assessment, candidates)
    if not assessment.findings:
        if live:
            store.set_status(
                state_key,
                "no_findings",
                candidates=candidates,
                docs_fingerprint=docs_fingerprint,
            )
        return

    # Named for the engine that reviewed, which is the fallback when the primary
    # failed, so a note never credits an engine that did not write it.
    reviewer_engine, reviewer_model = _advisory_reviewer_identity(
        settings, assessment
    )
    verifier = _advisory_verifier_identity(settings, assessment)
    if verifier == (reviewer_engine, reviewer_model):
        verifier = (None, None)
    _deliver_docs_notes(
        settings,
        repository,
        pull_request,
        assessment,
        destination,
        store,
        state_key,
        reviewer_engine,
        reviewer_model,
        verifier,
        live=live,
        docs_fingerprint=docs_fingerprint,
    )


def _is_release_promotion(settings: Settings, pull_request: PullRequest) -> bool:
    """Return whether this pull request promotes a branch rather than a change.

    A develop-to-main release carries no new design: every change in it already
    had its design reviewed on the feature pull request that put it on develop.
    Reviewing the promotion spends a full design pass, and a verification pass,
    to restate that work, and posts a second advisory comment about it. Matched on
    the head branch, which is what makes a pull request a promotion; the base is
    reported only so the skip says which promotion it recognised.
    """

    head_ref = pull_request.head_ref.strip().casefold()
    if not head_ref:
        return False
    return any(
        fnmatch.fnmatchcase(head_ref, pattern.strip().casefold())
        for pattern in settings.design_excluded_head_branches
    )


def _with_own_notes(
    settings: Settings,
    repository: Repository,
    pull_request: PullRequest,
    material: ReviewMaterial,
    marker: str,
    label: str,
    *,
    again: bool,
) -> ReviewMaterial:
    """Add this level's own earlier notes to the feedback it must not repeat.

    Looked up only when the level may already have posted here: it is running again,
    after a documentation edit or under --force, or an earlier delivery never
    recorded its outcome. Otherwise the lookup would be a GitHub call for nothing.
    """

    if not again:
        return material
    notes = list_own_advisory_notes(
        repository,
        pull_request,
        reviewer_account(repository, settings.reviewer_username),
        marker,
    )
    if not notes:
        return material
    combined = material.prior_comments + notes
    total_bytes = sum(len(item.body.encode("utf-8")) for item in combined)
    if total_bytes > MAX_PRIOR_REVIEW_BYTES:
        raise CommandError(
            f"Existing feedback and earlier {label} notes exceed the safe "
            f"review-context limit ({MAX_PRIOR_REVIEW_BYTES} bytes)"
        )
    print(
        f"      -> Considering {len(notes)} earlier {label} "
        f"note{'s' if len(notes) != 1 else ''} of this reviewer for duplicates"
    )
    return replace(material, prior_comments=combined)


def _report_design_assessment(assessment: DesignAssessment, candidates: int) -> None:
    """Print what the design level concluded, rejections included.

    Rejected candidates are unconfirmed by definition, so they appear here and in
    the transcript only, never on GitHub or in Slack.
    """

    if candidates:
        print(
            f"      -> Confirmed {len(assessment.findings)} of {candidates} "
            f"design candidate finding"
            f"{'s' if candidates != 1 else ''}"
        )
    for rejection in assessment.rejected:
        print(
            f"      -> Rejected design candidate [{rejection.finding.severity}] "
            f"({rejection.finding.category}) {rejection.finding.title}"
        )
        print(f"         Rejected because: {rejection.reason}")
    for finding in assessment.findings:
        print(f"      -> [{finding.severity}] ({finding.category}) {finding.title}")
        print(f"         {finding.consequence}")
    if not assessment.findings:
        print("      -> No confirmed solution-design findings")


def _deliver_design_notes(
    settings: Settings,
    repository: Repository,
    pull_request: PullRequest,
    assessment: DesignAssessment,
    destination: str,
    store: JsonStateStore,
    state_key: str,
    reviewer_engine: str,
    reviewer_model: Optional[str],
    verifier: "tuple[Optional[str], Optional[str]]",
    *,
    live: bool,
) -> None:
    """Post advisory design notes, after revalidating the mutable pull request.

    Delivered here rather than with the code-level result so notes survive a later
    code-level failure: the design level has already spent its quota, and its
    findings do not depend on anything the code level concludes.
    """

    _validate_pull_request_snapshot(
        get_pull_request_snapshot(repository, pull_request),
        pull_request,
        settings.excluded_pr_labels,
    )
    if not live:
        print(
            f"      -> Would post {len(assessment.findings)} advisory design "
            f"note{'s' if len(assessment.findings) != 1 else ''} (dry run)"
        )
        return

    store.set_status(state_key, "delivery_started")
    try:
        if destination == "slack":
            if settings.slack_webhook_url is None:
                raise DeliveryError(
                    "Design notes for your own pull request need SLACK_WEBHOOK_URL"
                )
            receipt_id = send_slack_message(
                settings.slack_webhook_url,
                format_design_slack_message(
                    repository,
                    pull_request,
                    assessment.findings,
                    reviewer_engine,
                    reviewer_model,
                    *verifier,
                ),
            )
        else:
            receipt_id = submit_design_notes(
                repository,
                pull_request,
                format_design_comment(
                    assessment.findings,
                    reviewer_engine,
                    reviewer_model,
                    *verifier,
                ),
            )
    except (DeliveryUncertainError, CommandError) as error:
        store.set_status(state_key, "delivery_uncertain", error=str(error))
        print(f"      -> Design notes may or may not have posted: {error}")
        return
    except DeliveryError as error:
        store.set_status(state_key, "failed", error=str(error))
        print(f"      -> Design notes were not posted: {error}")
        return
    store.set_status(
        state_key,
        "delivered",
        receipt_id=receipt_id,
        findings=len(assessment.findings),
    )
    print(f"      -> Posted advisory design notes (receipt: {receipt_id})")


def _run_design_level(
    settings: Settings,
    repository: Repository,
    exact_repository: Repository,
    pull_request: PullRequest,
    material: ReviewMaterial,
    engine: ReviewEngine,
    verification: Optional[VerificationPass],
    store: JsonStateStore,
    destination: str,
    verification_activity: Optional[_VerificationActivity] = None,
    *,
    live: bool,
    force: bool,
    fallback: Optional[FallbackReviewer] = None,
) -> None:
    """Run the advisory design level and deliver what it confirmed.

    Every failure here is contained: this level informs, it does not decide, so a
    design-level fault must not stop the code review, withhold an approval, or
    change the run's exit code. It is still reported as a failure of this level,
    because an error that became "no design issues found" would be a lie the
    reader could not detect.
    """

    if _is_release_promotion(settings, pull_request):
        print(
            f"      -> Design level skipped: {pull_request.head_ref} -> "
            f"{pull_request.base_ref} promotes reviewed work rather than "
            "proposing a design"
        )
        return

    # Keyed by pull request, not by head: the design level judges the shape of the
    # whole change, and re-running it on every push produced a second advisory
    # comment on the same pull request saying much the same thing at full cost.
    state_key = store.pull_request_key(
        repository.slug,
        pull_request.number,
        "design",
    )
    if not force and store.is_settled_for_pull_request(
        repository.slug,
        pull_request.number,
        "design",
    ):
        record = store.get(state_key) or {}
        settled = record.get("status") or "settled at an earlier head"
        print(
            "      -> Design level already settled for this pull request "
            f"({settled}); not reviewing the design again"
        )
        return

    try:
        material = _with_own_notes(
            settings,
            repository,
            pull_request,
            material,
            DESIGN_COMMENT_MARKER,
            "design",
            again=force,
        )
        assessment = _design_level(
            settings,
            engine,
            verification,
            exact_repository,
            pull_request,
            material,
            verification_activity,
            fallback,
        )
    except PullRequestHeadChangedError:
        raise
    except (ReviewError, EngineError, CommandError) as error:
        # The level has already recorded a refusal from the primary. This error
        # may be the verifier's or the fallback's, and recording it would
        # send the code review past a primary that can still answer.
        print(f"      -> Design level failed: {error}")
        return
    except Exception as error:
        # A defect in this level is still this level's failure: it informs the
        # review, so it may not take the code review or the run down with it.
        print(
            "      -> Design level failed unexpectedly: "
            f"{type(error).__name__}: {error}"
        )
        return

    candidates = len(assessment.findings) + len(assessment.rejected)
    print(f"      -> Solution design judged {assessment.verdict}")
    _report_design_assessment(assessment, candidates)
    if not assessment.findings:
        if live:
            store.set_status(state_key, "no_findings", candidates=candidates)
        return

    # Named for the engine that reviewed, which is the fallback when the primary
    # failed, so a note never credits an engine that did not write it.
    reviewer_engine, reviewer_model = _advisory_reviewer_identity(
        settings, assessment
    )
    verifier = _advisory_verifier_identity(settings, assessment)
    if verifier == (reviewer_engine, reviewer_model):
        verifier = (None, None)
    _deliver_design_notes(
        settings,
        repository,
        pull_request,
        assessment,
        destination,
        store,
        state_key,
        reviewer_engine,
        reviewer_model,
        verifier,
        live=live,
    )


def _recorded_conflict_base(store: JsonStateStore, state_key: str) -> Optional[str]:
    """Return the base commit a recorded merge conflict was measured against."""

    record = store.get(state_key) or {}
    if record.get("status") != "merge_conflict":
        return None
    recorded_base = record.get("base_sha")
    if not isinstance(recorded_base, str) or not recorded_base:
        return None
    return recorded_base


def _settled_conflict(
    repository: Repository,
    pull_request: PullRequest,
    excluded_labels: "frozenset[str]",
    snapshot: PullRequestSnapshot,
    recorded_base: str,
) -> "Optional[tuple[str, str]]":
    """Return why a recorded conflict needs no review, with the base to record.

    A conflict is recorded only for a head the review found nothing in, and the state
    key carries that head, so the code under review cannot have changed. A moving
    base does not change it either: the diff is taken from the merge base, which the
    branch advancing leaves where it was. The only thing a new review could learn is
    whether the conflict is gone, and GitHub answers that without a model.

    So an unmoved base is skipped outright, and a moved one is asked about first.
    Reviewing instead spent a full review on every move of a busy base branch: one
    unchanged conflicted head was reviewed eight times in a week as its base moved,
    each review ending at the same conflict.

    A pull request GitHub still reports conflicting is recorded against the new tip.
    One whose mergeability GitHub has not settled keeps the old record, so the next
    run asks again; reviewing it would end at the same unanswered question with a
    review spent. Only a cleared conflict, or a question this cannot answer, such as
    an unreadable branch, returns None and is reviewed.
    """

    if not snapshot.base_ref:
        return None
    try:
        base_tip = get_branch_head_sha(repository, snapshot.base_ref)
    except CommandError as error:
        print(f"      -> Could not read the base branch tip: {error}")
        return None
    if base_tip == recorded_base:
        return (
            "merge conflict already recorded for this head against "
            f"{snapshot.base_ref}, which has not moved",
            recorded_base,
        )
    snapshot = _await_mergeability(repository, pull_request, excluded_labels, snapshot)
    if snapshot.mergeable is None:
        return (
            f"{snapshot.base_ref} moved since this head's merge conflict was "
            "recorded, and GitHub has not finished calculating whether it still "
            "conflicts; asking again next run",
            recorded_base,
        )
    if not snapshot.mergeable:
        return (
            f"this head still conflicts with {snapshot.base_ref}, which moved to "
            f"{base_tip[:12]}; the code is unchanged since the conflict was recorded",
            base_tip,
        )
    print(
        f"      -> {snapshot.base_ref} moved and the recorded merge conflict is "
        "gone; reviewing again"
    )
    return None


def _conflict_base_sha(repository: Repository, snapshot: PullRequestSnapshot) -> str:
    """Return the base tip to record with a conflict, or an empty string.

    A failed lookup must not fail the review that is otherwise finished. Recording
    no base leaves the conflict retryable, which is the behavior this had before.
    """

    if not snapshot.base_ref:
        return ""
    try:
        return get_branch_head_sha(repository, snapshot.base_ref)
    except CommandError as error:
        print(f"      -> Could not read the base branch tip: {error}")
        return ""


def _verification_timeout_seconds(settings: Settings, candidates: int) -> int:
    """Return the budget for verifying ``candidates`` findings in one pass.

    A verification pass checks each candidate against the workspace in turn, so
    its work grows with the number of candidates while a flat budget does not.
    Every recorded pass carrying one or two candidates finished within about two
    minutes, and the only passes that ever exhausted a flat ten minutes carried
    three and four, losing their entire review. The budget is therefore a base
    plus an allowance for each candidate in the pass.
    """

    return settings.review_verify_timeout_seconds + (
        settings.review_verify_timeout_per_candidate_seconds * max(candidates, 0)
    )


def _outcome(recorder: progress.Recorder) -> str:
    """Summarize a finished review from the last step it reported."""
    return recorder.last_step() or "finished"


def run(args: argparse.Namespace) -> int:
    if getattr(args, "no_fallback", False) and (
        getattr(args, "fallback_engine", None)
        or getattr(args, "fallback_model", None)
    ):
        raise ConfigError(
            "--no-fallback cannot be combined with --fallback-engine or "
            "--fallback-model"
        )
    settings = load_settings(
        args.env_file,
        args.repositories_file,
        args.state_file,
        getattr(args, "engine", None),
        review_model_override=getattr(args, "model", None),
        verify_engine_override=getattr(args, "verify_engine", None),
        verify_model_override=getattr(args, "verify_model", None),
        fallback_engine_override=getattr(args, "fallback_engine", None),
        fallback_model_override=getattr(args, "fallback_model", None),
        fallback_disabled=getattr(args, "no_fallback", False),
    )
    if args.pr is not None and not args.repo:
        raise ConfigError(
            "--pr requires --repo to avoid matching multiple repositories"
        )
    if args.live and settings.dry_run:
        raise ConfigError(
            "Live delivery requires AUTO_REVIEW_DRY_RUN=0 in addition to --live"
        )
    # Before anything reads GitHub, so a run never reads or posts as an account
    # other than the reviewer just because it was the active one.
    pin_github_account(settings.reviewer_username, settings.repositories)

    concurrency = _selected_concurrency(settings, getattr(args, "concurrency", None))
    live = bool(args.live and not settings.dry_run)
    # A spent quota is remembered for one run and no longer, so every run finds out
    # for itself rather than inheriting a guess about a window that may have reset.
    _PRIMARY_ENGINE.reset()
    log_directory = settings.state_file.parent / RUN_LOG_DIRECTORY_NAME
    started = datetime.now().strftime("%Y%m%d-%H%M%S")
    # The process ID distinguishes runs that start in the same second, and ties
    # a transcript to the run still holding the terminal.
    name = f"run-{started}-{os.getpid()}.log"
    with progress.transcript(log_directory / name) as transcript_path:
        progress.prune_transcripts(log_directory, settings.run_log_retention_days)
        return _run_reviews(
            args,
            settings,
            concurrency=concurrency,
            live=live,
            transcript_path=transcript_path,
        )


def _run_reviews(
    args: argparse.Namespace,
    settings: Settings,
    *,
    concurrency: int,
    live: bool,
    transcript_path: Optional[Path],
) -> int:
    mode = "LIVE" if live else "DRY RUN"
    print(f"=== Automated Code Review ({mode}) ===")
    standby_engine, standby_model, standby_effort = _verify_fallback_identity(settings)
    for line in format_review_plan(
        settings.review_engine,
        settings.review_model,
        *_verifier_identity(settings),
        *_fallback_identity(settings),
        review_effort=settings.review_effort,
        code_effort=settings.review_code_effort,
        verify_effort=settings.verify_effort,
        fallback_effort=settings.fallback_effort,
        verify_fallback_engine=standby_engine,
        verify_fallback_model=standby_model,
        verify_fallback_effort=standby_effort,
    ):
        print(line)
    if not live and not settings.dry_run:
        print(
            "Note: AUTO_REVIEW_DRY_RUN=0, but --live was not supplied; delivery is disabled."
        )
    if transcript_path is not None:
        print(f"Run log: {transcript_path}")

    repositories = _selected_repositories(settings.repositories, args.repo)
    engine = create_review_engine(settings)
    verification = _create_verification_pass(settings, engine)
    fallback = _create_fallback_reviewer(settings)
    errors: list[str] = []
    incident_notice = _GithubIncidentNotice()
    verification_activity = _VerificationActivity()
    usage = _ModelUsage()
    metrics = ReviewMetrics()

    with JsonStateStore(settings.state_file, lock_enabled=live) as store:
        tasks = _discover_review_tasks(
            settings,
            repositories,
            requested_pr=args.pr,
            errors=errors,
            incident_notice=incident_notice,
        )
        _print_review_plan(tasks, concurrency, settings.reviewer_username)
        errors.extend(
            _execute_review_tasks(
                tasks,
                settings,
                store,
                engine,
                concurrency=concurrency,
                live=live,
                force=args.force,
                verification=verification,
                verification_activity=verification_activity,
                fallback=fallback,
                incident_notice=incident_notice,
                usage=usage,
                metrics=metrics,
            )
        )
    run_spend = usage.describe()
    if run_spend:
        print(f"\nSpent {run_spend}")
    metrics_path = getattr(args, "metrics", None)
    if metrics_path:
        _write_metrics(metrics_path, metrics, settings)
    if live:
        performed_verifier = (
            _verifier_identity(settings)
            if verification_activity.performed
            else (None, None)
        )
        slack_errors = _errors_for_slack(errors, settings.repositories)
        if len(slack_errors) < len(errors):
            print(
                f"Kept {len(errors) - len(slack_errors)} run error(s) out of the "
                "Slack notification: their repositories are kept out of Slack",
                file=sys.stderr,
            )
        _notify_run_failure(
            settings.slack_webhook_url,
            slack_errors,
            incident_notice.note,
            format_review_attribution(
                settings.review_engine,
                settings.review_model,
                *performed_verifier,
            ),
        )
    return 1 if errors else 0


def _admin_command(name: str) -> "Callable[[Sequence[str]], int]":
    """Return the entry point of a command that manages this installation."""

    if name == "schedule":
        from . import schedule

        return schedule.main
    if name == "doctor":
        from . import doctor

        return doctor.main
    if name == "setup":
        from . import setup_wizard

        return setup_wizard.main
    raise AssertionError(f"unknown command {name}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # A review run takes no positional argument, so a first word naming one of
    # these is never a review.
    if arguments and arguments[0] in ADMIN_COMMANDS:
        return _admin_command(arguments[0])(arguments[1:])
    args = build_parser().parse_args(arguments)
    try:
        return run(args)
    except KeyboardInterrupt:
        # Ctrl+C reaches the review commands as well, because they run in this
        # process group, so the run is already stopping. Report it as the shell
        # convention for a signalled exit rather than as a fault.
        print(
            "\nInterrupted; stopped the run and any review still in progress.",
            file=sys.stderr,
        )
        return INTERRUPTED_EXIT_CODE
    except StateLockedError as error:
        # An overlapping run is an expected outcome, not a fault: a manual run
        # and a scheduled one can meet, and the lock exists to let the second
        # one stop. Reporting it as a failure would page the operator about
        # their own manual run, so it is a safe skip with no notification.
        print(f"Skipped: {error}; that run is reviewing the same state")
        return 0
    except (CommandError, ConfigError, EngineError, StateError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if _startup_failure_notifications_enabled(args):
            _notify_run_failure(
                os.getenv("SLACK_WEBHOOK_URL", "").strip() or None,
                [f"run: {error}"],
            )
        return 2 if isinstance(error, ConfigError) else 1
    except Exception as error:
        # A defect in this tool that no single review owns. The traceback stays
        # here, where it can be found; the message carries only the defect's type
        # and place, because its text could name a repository kept out of Slack.
        traceback.print_exception(type(error), error, error.__traceback__)
        summary = f"crashed with {type(error).__name__} in {_crash_location(error)}"
        print(f"ERROR: {summary}", file=sys.stderr)
        if _startup_failure_notifications_enabled(args):
            _notify_run_failure(
                os.getenv("SLACK_WEBHOOK_URL", "").strip() or None,
                [f"run: {summary}; the traceback is in the terminal output"],
            )
        return 1
