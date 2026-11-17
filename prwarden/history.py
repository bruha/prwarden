"""What each run did with each pull request, and `prwarden history` to read it.

The state file answers one question, whether a head still needs work, and keeps one
record per key that the next write replaces. It cannot say what happened to a pull
request over its life or what a review found, and the run logs that can say so are
transcripts written for a person, kept a week. So every run also appends one line per
pull request it looked at: when, at which head, what each level decided and why, the
findings it delivered, and where on GitHub they are. Another tool, or an agent asked
whether PR Warden reviewed a pull request, reads that through `prwarden history`
rather than parsing a transcript.

The history is a report and never an input: no decision reads it, so it can be
deleted at any time, and a line that cannot be written costs the history that line
and never a review. Each run writes its own file, which only that run appends to,
so overlapping runs, dry runs included, never share one. Files older than
`REVIEW_HISTORY_RETENTION_DAYS` are deleted at the start of a run.

A line holds pull-request titles and finding text: text the reviewed project and a
model wrote. It is created readable only by its owner, printed with control
characters neutralised, and is data for whoever reads it, never instructions.
"""

import argparse
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from . import progress
from .config import (
    ConfigError,
    default_locations,
    load_env_file,
    resolve_state_file,
    review_history_retention_days,
)
from .models import (
    DesignAssessment,
    DesignFinding,
    DocsAssessment,
    DocsFinding,
    Finding,
    PullRequest,
    Repository,
)


SCHEMA_VERSION = 1
HISTORY_DIRECTORY_NAME = "history"
HISTORY_FILE_PATTERN = "run-*.jsonl"
HISTORY_FILE_MODE = 0o600
LEVELS = ("code", "design", "docs")
# Asked a human to review a change too large to review whole; kept by pull request.
MANUAL_REVIEW = "manual_review"
# A level's reason or error is the controller's own sentence or a command's message,
# and one pathological message must not make a line unreadably long.
MAX_TEXT_CHARACTERS = 2000
OVERVIEW_TITLE_WIDTH = 60
EARLIEST = datetime.min.replace(tzinfo=timezone.utc)

# Statuses this module writes itself, for outcomes the state file never records.
SKIPPED = "skipped"
FAILED = "failed"
NO_FINDINGS = "no_findings"
DRY_RUN = "dry_run"
# A result a live review reached that the state file has not confirmed delivering.
UNDELIVERED = "undelivered"

# Where a review goes, as `prwarden.cli` names the routes.
SLACK_DESTINATION = "slack"
OWN_GITHUB_DESTINATION = "github-own"
# The receipt the controller records for a head GitHub already holds a review of.
EXISTING_REVIEW_RECEIPT = "existing-github-review"

# What a state record says that a reader needs, under the name the history uses.
STATE_FIELDS = (
    ("receipt_id", "receipt"),
    ("notification_receipt_id", "receipt"),
    ("reason", "reason"),
    ("error", "error"),
    ("closed_during_review", "closed_during_review"),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded(text: str, limit: int = MAX_TEXT_CHARACTERS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _code_finding(finding: Finding) -> "dict[str, Any]":
    entry: "dict[str, Any]" = {
        "severity": finding.severity,
        "title": finding.title,
        "path": finding.path,
        "line": finding.line,
        "problem": finding.problem,
        "suggestion": finding.suggestion,
    }
    if finding.rule:
        entry["rule"] = finding.rule
    if finding.original_severity:
        # What the reviewer said before the verifier corrected it, and why.
        entry["original_severity"] = finding.original_severity
        entry["severity_reason"] = finding.severity_reason
    if finding.found_by_pattern:
        entry["found_by"] = "pattern"
    return entry


def _design_finding(finding: DesignFinding) -> "dict[str, Any]":
    return {
        "severity": finding.severity,
        "title": finding.title,
        "category": finding.category,
        "problem": finding.problem,
        "alternative": finding.alternative,
    }


def _docs_finding(finding: DocsFinding) -> "dict[str, Any]":
    return {
        "severity": finding.severity,
        "title": finding.title,
        "category": finding.category,
        "path": finding.documentation.path,
        "line": finding.documentation.line,
        "proposed_update": finding.proposed_update,
    }


class ReviewRecord:
    """What one run did with one pull request, assembled while it is reviewed.

    Two sources, because neither holds everything. Every outcome a live review
    settles is written to the state file, so the record takes those from there and
    cannot miss one a later change adds. The state file never holds finding text,
    and a skip, a dry run, or a failed advisory level writes nothing there, so the
    controller notes those here as they happen.
    """

    def __init__(
        self,
        repository: Repository,
        pull_request: PullRequest,
        *,
        run: str,
        live: bool,
        destination: str,
    ) -> None:
        self._repository = repository
        self._pull_request = pull_request
        self._run = run
        self._live = live
        self._destination = destination
        self._started_at = _now()
        self._head = pull_request.head_sha
        self._superseded: "list[str]" = []
        self._levels: "dict[str, dict[str, Any]]" = {}
        self._state: "dict[str, dict[str, Any]]" = {}
        self._error: Optional[str] = None
        self._interrupted = False
        self._spent: Optional["dict[str, Any]"] = None

    @property
    def repository(self) -> str:
        return self._repository.slug

    @property
    def number(self) -> int:
        return self._pull_request.number

    def attempt(self, head_sha: str, destination: str) -> None:
        """Start reviewing ``head_sha``, which replaced the head before it.

        The code level judges one head, so a head that moved starts its outcome
        afresh; the advisory levels judge the pull request and keep theirs.
        """

        self._destination = destination
        if head_sha == self._head:
            return
        self._superseded.append(self._head)
        self._head = head_sha
        self._levels.pop("code", None)

    def skip(
        self,
        level: str,
        reason: str,
        earlier: Optional["Mapping[str, Any]"] = None,
    ) -> None:
        """Note a level that did not run, and the state record that settled it."""

        # A level an earlier attempt in this run already did is skipped by the next
        # attempt because it is done, and the work outranks the skip.
        current = self._levels.get(level)
        if current is not None and current.get("status") != SKIPPED:
            return
        skipped: "dict[str, Any]" = {"status": SKIPPED, "reason": _bounded(reason)}
        if earlier:
            # The record a run before this one wrote: what an agent asking about
            # this pull request most wants, and what the skip alone does not say.
            settled = {
                name: earlier[name]
                for name in ("status", "updated_at", "receipt_id")
                if isinstance(earlier.get(name), str) and earlier.get(name)
            }
            if settled:
                skipped["earlier"] = settled
        self._levels[level] = skipped

    def failed(self, level: str, error: str) -> None:
        self._levels[level] = {"status": FAILED, "error": _bounded(error)}

    def code_result(
        self,
        findings: "Sequence[Finding]",
        *,
        approve: bool,
        reason: str,
        withheld: int = 0,
    ) -> None:
        """Note what the code level concluded, before anything is delivered.

        ``withheld`` counts the confirmed findings over the limit a review posts,
        which are not shown and whose files the next review reads again.
        """

        verdict = "findings" if findings else "approve" if approve else "withhold"
        result: "dict[str, Any]" = {
            "status": UNDELIVERED if self._live else DRY_RUN,
            "verdict": verdict,
            "findings": [_code_finding(finding) for finding in findings],
        }
        if withheld > 0:
            result["withheld"] = withheld
        if verdict == "withhold" and reason:
            result["reason"] = _bounded(reason)
        self._levels["code"] = result

    def advisory_result(
        self, level: str, assessment: Union[DesignAssessment, DocsAssessment]
    ) -> None:
        """Note what an advisory level confirmed, before anything is delivered."""

        if isinstance(assessment, DesignAssessment):
            findings = [_design_finding(finding) for finding in assessment.findings]
        else:
            findings = [_docs_finding(finding) for finding in assessment.findings]
        if not findings:
            status = NO_FINDINGS
        else:
            status = UNDELIVERED if self._live else DRY_RUN
        self._levels[level] = {
            "status": status,
            "verdict": assessment.verdict,
            "findings": findings,
            "rejected": len(assessment.rejected),
        }

    def fail(self, error: BaseException) -> None:
        self._error = _bounded(str(error) or type(error).__name__)

    def interrupt(self) -> None:
        self._interrupted = True

    def spend(
        self, seconds: float, invocations: int, credits: Optional[float]
    ) -> None:
        # A review that called no model spent nothing, and says nothing about it,
        # as its report on the terminal does.
        if not invocations:
            return
        self._spent = {
            "seconds": round(seconds, 1),
            "invocations": invocations,
            "credits": None if credits is None else round(credits, 2),
        }

    def observe_state(
        self,
        before: "Mapping[str, Mapping[str, Any]]",
        after: "Mapping[str, Mapping[str, Any]]",
    ) -> None:
        """Take each outcome the state file recorded for this pull request since
        ``before`` was read."""

        prefix = f"{self._repository.slug}#{self._pull_request.number}#"
        head_key = f"{self._head}#{self._destination}"
        for key, record in after.items():
            if before.get(key) == record or not key.startswith(prefix):
                continue
            rest = key[len(prefix) :]
            if rest in LEVELS[1:]:
                level = rest
            elif rest == "oversized":
                level = MANUAL_REVIEW
            elif rest == head_key:
                level = "code"
            else:
                # A head this run superseded, which `superseded_heads` names.
                continue
            self._state[level] = dict(record)

    def entry(self) -> "dict[str, Any]":
        """Return the line this record writes."""

        levels = {name: dict(data) for name, data in self._levels.items()}
        for level, record in self._state.items():
            merged = levels.setdefault(level, {})
            merged["status"] = record.get("status")
            for source, target in STATE_FIELDS:
                value = record.get(source)
                if isinstance(value, bool):
                    if value:
                        merged[target] = True
                elif isinstance(value, str) and value:
                    merged[target] = _bounded(value)
        if self._error is not None and "code" not in self._state:
            code = levels.setdefault("code", {})
            # A review that raised has no result to report, whatever it had noted.
            if code.get("status") in (None, UNDELIVERED, DRY_RUN):
                code["status"] = FAILED
                code["error"] = self._error
        pull_request = self._pull_request
        entry: "dict[str, Any]" = {
            "schema": SCHEMA_VERSION,
            "run": self._run,
            "started_at": self._started_at,
            "recorded_at": _now(),
            "mode": "live" if self._live else DRY_RUN,
            "repository": self._repository.slug,
            "name": self._repository.name,
            "pull_request": pull_request.number,
            "url": pull_request.url,
            "title": pull_request.title,
            "author": pull_request.author,
            "head": self._head,
            "destination": self._destination,
        }
        if self._superseded:
            entry["superseded_heads"] = list(self._superseded)
        for level in (*LEVELS, MANUAL_REVIEW):
            if level in levels:
                entry[level] = levels[level]
        if self._error is not None:
            entry["error"] = self._error
        if self._interrupted:
            entry["interrupted"] = True
        if self._spent is not None:
            entry["spent"] = self._spent
        return entry


class ReviewHistory:
    """Appends this run's lines to its own history file.

    Never raises: the history reports reviews and must not be able to fail one. A
    line that cannot be written is said once on standard error.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._reported = False

    @property
    def run(self) -> str:
        """The run's name, which its transcript shares."""

        return self.path.stem

    def append(self, entry: "Mapping[str, Any]") -> None:
        try:
            data = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
        except (TypeError, ValueError) as error:
            self._report(error)
            return
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                # Created readable only by its owner whatever umask the run
                # inherited, because a line carries titles and finding text.
                descriptor = os.open(
                    str(self.path),
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                    HISTORY_FILE_MODE,
                )
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                finally:
                    os.close(descriptor)
            except OSError as error:
                self._report(error)

    def _report(self, error: BaseException) -> None:
        if self._reported:
            return
        self._reported = True
        print(
            f"Note: review history not recorded in {self.path}: {error}",
            file=sys.stderr,
        )


def prune(directory: Path, retention_days: int) -> None:
    """Delete history files last written over ``retention_days`` ago."""

    progress.prune_older_files(directory, HISTORY_FILE_PATTERN, retention_days)


class HistoryError(RuntimeError):
    """Raised when the history directory itself cannot be read."""


def _recorded_at(entry: "Mapping[str, Any]") -> Optional[datetime]:
    value = entry.get("recorded_at")
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _is_entry(value: Any) -> bool:
    number = value.get("pull_request") if isinstance(value, dict) else None
    return (
        isinstance(value, dict)
        and value.get("schema") == SCHEMA_VERSION
        and isinstance(value.get("repository"), str)
        and isinstance(number, int)
        and not isinstance(number, bool)
        and _recorded_at(value) is not None
    )


def read_entries(directory: Path) -> "tuple[list[dict[str, Any]], int]":
    """Return every entry kept in ``directory``, oldest first, and how many lines
    could not be read.

    A line that cannot be read is counted rather than dropped in silence, so a
    caller can tell an empty history from one it could not read.
    """

    if not directory.exists():
        return [], 0
    try:
        paths = sorted(directory.glob(HISTORY_FILE_PATTERN))
    except OSError as error:
        raise HistoryError(
            f"Cannot read review history in {directory}: {error}"
        ) from error
    entries: "list[dict[str, Any]]" = []
    unreadable = 0
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable += 1
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError:
                unreadable += 1
                continue
            if not _is_entry(value):
                unreadable += 1
                continue
            entries.append(value)
    entries.sort(key=lambda entry: _recorded_at(entry) or EARLIEST)
    return entries, unreadable


def select(
    entries: "Sequence[Mapping[str, Any]]",
    *,
    repository: Optional[str] = None,
    number: Optional[int] = None,
    days: Optional[int] = None,
    now: Optional[datetime] = None,
) -> "list[Mapping[str, Any]]":
    """Return the entries of one repository, pull request, or window, in order.

    A repository is named by its configured name or its slug, without regard to
    case, as GitHub compares slugs.
    """

    wanted = repository.casefold() if repository else None
    cutoff = None
    if days is not None:
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    selected = []
    for entry in entries:
        names = (
            str(entry.get("repository", "")).casefold(),
            str(entry.get("name", "")).casefold(),
        )
        if wanted is not None and wanted not in names:
            continue
        if number is not None and entry.get("pull_request") != number:
            continue
        moment = _recorded_at(entry)
        if cutoff is not None and (moment is None or moment < cutoff):
            continue
        selected.append(entry)
    return selected


def _clean(value: Any) -> str:
    """Return untrusted text as one printable line.

    A title or a finding can carry a terminal escape sequence, which printed as it
    is would let the reviewed project redraw the reader's terminal.
    """

    text = " ".join(str(value).split())
    return "".join(character if character.isprintable() else "?" for character in text)


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _with(text: str, detail: str) -> str:
    return f"{text}: {detail}" if detail else text


def link(entry: "Mapping[str, Any]", level: str) -> Optional[str]:
    """Return where on GitHub a level's delivered result is, when it is there."""

    data = entry.get(level)
    url = entry.get("url")
    if not isinstance(data, Mapping) or not isinstance(url, str):
        return None
    receipt = data.get("receipt")
    if data.get("status") != "delivered" or not isinstance(receipt, str):
        return None
    destination = entry.get("destination")
    if not receipt.isdigit() or destination == SLACK_DESTINATION:
        return None
    if not url.startswith("https://"):
        return None
    if level == "code":
        # Approvals and findings are reviews; the LGTM on the reviewer's own pull
        # request is a conversation comment, as every advisory note is.
        own_lgtm = destination == OWN_GITHUB_DESTINATION and not data.get("findings")
        anchor = "issuecomment" if own_lgtm else "pullrequestreview"
    elif level in LEVELS:
        anchor = "issuecomment"
    else:
        return None
    return f"{url}#{anchor}-{receipt}"


LEVEL_NOUNS = {
    "code": "finding",
    "design": "design note",
    "docs": "documentation update",
}
MANUAL_REVIEW_OUTCOMES = {
    "oversized_notified": "manual review requested in Slack",
    "oversized_not_notified": (
        "manual review needed; this repository is kept out of Slack"
    ),
    "oversized_notification_pending": "manual review request not sent yet",
}
STATUS_OUTCOMES = {
    NO_FINDINGS: "no findings",
    "approval_withheld": "approval withheld",
    FAILED: "failed",
    "delivery_uncertain": "delivery uncertain, not retried",
    "delivery_started": "delivery started; its outcome was not recorded",
    "in_progress": "stopped before an outcome was recorded",
    "superseded": "superseded by a newer head",
    "skipped_ineligible": "skipped",
    "feedback_changed": "not delivered",
    "skipped_oversized": "too large to review",
    "oversized_notification_pending": "too large to review",
    SKIPPED: "skipped",
}


def describe(entry: "Mapping[str, Any]", level: str) -> str:
    """Return one level's outcome in a few words."""

    data = entry.get(level)
    if not isinstance(data, Mapping):
        return "not recorded"
    status = data.get("status")
    findings = data.get("findings")
    count = len(findings) if isinstance(findings, list) else 0
    detail = _clean(data.get("error") or data.get("reason") or "")
    destination = entry.get("destination")
    noun = LEVEL_NOUNS.get(level, "finding")
    if level == MANUAL_REVIEW:
        return MANUAL_REVIEW_OUTCOMES.get(str(status), _with("manual review", detail))
    withheld = data.get("withheld")
    over_limit = (
        f"; {withheld} more confirmed, over the limit"
        if isinstance(withheld, int) and not isinstance(withheld, bool) and withheld > 0
        else ""
    )
    if status == "delivered":
        if data.get("receipt") == EXISTING_REVIEW_RECEIPT:
            return "skipped: this head already had a review on GitHub"
        if level != "code" or count:
            where = "sent to Slack" if destination == SLACK_DESTINATION else "posted"
            text = f"{_plural(count, noun)} {where}"
            if data.get("closed_during_review"):
                text += " after the pull request closed"
            return text + over_limit
        if destination == SLACK_DESTINATION:
            return "LGTM sent to Slack"
        if destination == OWN_GITHUB_DESTINATION:
            return "LGTM comment posted"
        return "approved"
    if status == DRY_RUN:
        verdict = data.get("verdict")
        if level == "code" and verdict == "approve":
            return "dry run: would approve"
        if level == "code" and verdict == "withhold":
            return _with("dry run: would withhold approval", detail)
        return f"dry run: would post {_plural(count, noun)}{over_limit}"
    if status == UNDELIVERED:
        found = f"found {_plural(count, noun)}, not delivered" if count else ""
        return _with(found or "not delivered", detail) + over_limit
    if status == "merge_conflict":
        return "approval withheld: merge conflict"
    text = STATUS_OUTCOMES.get(str(status))
    if text is None:
        text = str(status).replace("_", " ") if status else "not recorded"
    text = _with(text, detail)
    earlier = data.get("earlier")
    if status == SKIPPED and isinstance(earlier, Mapping):
        when = _local_moment(earlier.get("updated_at"))
        if when:
            text += f", recorded {when}"
        receipt = earlier.get("receipt_id")
        if isinstance(receipt, str) and receipt:
            text += f", receipt {_clean(receipt)}"
    return text


def _local_moment(value: Any) -> Optional[str]:
    moment = _recorded_at({"recorded_at": value})
    return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else None


def _local_time(entry: "Mapping[str, Any]") -> str:
    return _local_moment(entry.get("recorded_at")) or "?"


def _finding_line(finding: Any) -> Optional[str]:
    if not isinstance(finding, Mapping):
        return None
    severity = _clean(finding.get("severity") or "")
    was = finding.get("original_severity")
    if isinstance(was, str) and was:
        severity += f" (was {_clean(was)})"
    title = _clean(finding.get("title") or "")
    path = finding.get("path")
    line = finding.get("line")
    where = (
        f"{_clean(path)}:{line}"
        if isinstance(path, str) and path and isinstance(line, int)
        else ""
    )
    return "  ".join(part for part in (f"{severity:<8}", where, title) if part.strip())


def _pull_request_name(entry: "Mapping[str, Any]") -> str:
    return f"{_clean(entry.get('repository'))} #{entry.get('pull_request')}"


def _entry_lines(entry: "Mapping[str, Any]") -> "list[str]":
    mode = "live" if entry.get("mode") == "live" else "dry run"
    head = _clean(entry.get("head") or "")[:12]
    lines = [f"{_local_time(entry)}  {mode}  head {head}"]
    for level in (*LEVELS, MANUAL_REVIEW):
        data = entry.get(level)
        if not isinstance(data, Mapping):
            continue
        label = "manual" if level == MANUAL_REVIEW else level
        lines.append(f"  {label:<7} {describe(entry, level)}")
        where = link(entry, level)
        if where:
            lines.append(f"          {where}")
        findings = data.get("findings")
        for finding in findings if isinstance(findings, list) else ():
            text = _finding_line(finding)
            if text:
                lines.append(f"            {text}")
    code = entry.get("code")
    error = entry.get("error")
    if error and not (isinstance(code, Mapping) and code.get("error") == error):
        lines.append(f"  error   {_clean(error)}")
    superseded = entry.get("superseded_heads")
    if isinstance(superseded, list) and superseded:
        moved = ", ".join(_clean(head)[:12] for head in superseded)
        lines.append(f"  note    restarted after the head moved on from {moved}")
    if entry.get("interrupted"):
        lines.append("  note    the run was interrupted")
    spent = entry.get("spent")
    if isinstance(spent, Mapping) and spent.get("invocations"):
        seconds = spent.get("seconds")
        invocations = spent.get("invocations")
        credits = spent.get("credits")
        text = (
            f"{progress.format_duration(float(seconds or 0))} of model time across "
            f"{_plural(int(invocations), 'invocation')}"
        )
        if isinstance(credits, (int, float)):
            text += f", {credits:.2f} credits"
        lines.append(f"  spent   {text}")
    return lines


def _is_skip(data: "Mapping[str, Any]") -> bool:
    return (
        data.get("status") == SKIPPED
        or data.get("receipt") == EXISTING_REVIEW_RECEIPT
    )


def _decisive(
    entries: "Sequence[Mapping[str, Any]]", level: str
) -> Optional["Mapping[str, Any]"]:
    """Return the newest entry that did this level's work, else the newest that
    skipped it: a later run skips a level because an earlier one settled it."""

    newest_skip = None
    for entry in reversed(entries):
        data = entry.get(level)
        if not isinstance(data, Mapping):
            continue
        if not _is_skip(data):
            return entry
        if newest_skip is None:
            newest_skip = entry
    return newest_skip


def render_pull_request(
    entries: "Sequence[Mapping[str, Any]]", window: str
) -> "list[str]":
    """Return every recorded review of one pull request, newest first."""

    latest = entries[-1]
    lines = [
        f"{_pull_request_name(latest)} {_clean(latest.get('title') or '')}",
        _clean(latest.get("url") or ""),
        f"{_plural(len(entries), 'review')} recorded {window}, newest first:",
    ]
    for entry in reversed(entries):
        lines.append("")
        lines.extend(_entry_lines(entry))
    return lines


def render_overview(
    entries: "Sequence[Mapping[str, Any]]", window: str
) -> "list[str]":
    """Return one line pair per pull request: when it was last looked at, and what
    each level last decided."""

    groups: "dict[tuple[str, int], list[Mapping[str, Any]]]" = {}
    for entry in entries:
        key = (str(entry["repository"]).casefold(), int(entry["pull_request"]))
        groups.setdefault(key, []).append(entry)
    ordered = sorted(
        groups.values(),
        key=lambda group: _recorded_at(group[-1]) or EARLIEST,
        reverse=True,
    )
    lines = [
        f"{_plural(len(ordered), 'pull request')} reviewed {window}, most recent "
        "first. One in full: prwarden history --repo OWNER/REPO --pr NUMBER",
    ]
    for group in ordered:
        latest = group[-1]
        title = _shorten(_clean(latest.get("title") or ""), OVERVIEW_TITLE_WIDTH)
        lines.append("")
        lines.append(f"{_local_time(latest)}  {_pull_request_name(latest)}  {title}")
        outcomes = []
        for level in LEVELS:
            decisive = _decisive(group, level)
            if decisive is not None:
                outcomes.append(f"{level}: {describe(decisive, level)}")
        if outcomes:
            lines.append("    " + "; ".join(outcomes))
    return lines


def annotated(entry: "Mapping[str, Any]") -> "dict[str, Any]":
    """Return an entry with each level's outcome in words and its GitHub link."""

    result = dict(entry)
    for level in (*LEVELS, MANUAL_REVIEW):
        data = entry.get(level)
        if not isinstance(data, Mapping):
            continue
        level_data = dict(data)
        level_data["summary"] = describe(entry, level)
        where = link(entry, level)
        if where:
            level_data["link"] = where
        result[level] = level_data
    return result


def _history_location(
    env_file: Optional[Path], state_file: Optional[Path]
) -> "tuple[Path, int]":
    """Return the history directory runs write to, and how long it is kept.

    Resolved as a run resolves its state file, without the rest of what a run
    needs: reading the history asks GitHub for nothing.
    """

    locations = default_locations()
    load_env_file(env_file if env_file is not None else locations.env_file)
    state = resolve_state_file(state_file, locations)
    return state.parent / HISTORY_DIRECTORY_NAME, review_history_retention_days()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prwarden history",
        description=(
            "Show what PR Warden's runs on this host did with each pull request. "
            "Reads local files only: no GitHub call, no model."
        ),
    )
    parser.add_argument("--repo", help="repository name or owner/repository slug")
    parser.add_argument(
        "--pr", type=int, help="every recorded review of one pull request"
    )
    parser.add_argument(
        "--days", type=int, help="only reviews recorded in the last DAYS days"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the matching records as JSON, newest first",
    )
    parser.add_argument("--env-file", type=Path, help="dotenv file to load")
    parser.add_argument(
        "--state-file",
        type=Path,
        help="read the history kept beside this state file; overrides STATE_FILE",
    )
    return parser


def main(argv: Sequence[str], print_line: Callable[[str], None] = print) -> int:
    """Print the recorded reviews; exit 1 when part of the history was unreadable."""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.pr is not None and not args.repo:
        parser.error("--pr requires --repo, as a number alone can match several")
    if args.days is not None and args.days < 1:
        parser.error("--days must be at least 1")
    try:
        directory, retention = _history_location(args.env_file, args.state_file)
        entries, unreadable = read_entries(directory)
    except (ConfigError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except HistoryError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    matching = select(entries, repository=args.repo, number=args.pr, days=args.days)
    window = (
        f"in the last {_plural(args.days, 'day')}"
        if args.days is not None
        else f"on this host (kept {_plural(retention, 'day')})"
    )
    if args.json:
        document = {
            "schema": SCHEMA_VERSION,
            "history_directory": str(directory),
            "retention_days": retention,
            "unreadable_lines": unreadable,
            "entries": [annotated(entry) for entry in reversed(matching)],
        }
        print_line(json.dumps(document, indent=2, sort_keys=True))
    elif not matching:
        subject = (
            f"{_clean(args.repo)} #{args.pr}"
            if args.pr is not None
            else _clean(args.repo)
            if args.repo
            else "any pull request"
        )
        print_line(f"No review of {subject} recorded {window}.")
        print_line(
            "Drafts and pull requests with an excluded label are left out before "
            "any review; what a review posted is on the pull request itself."
        )
    elif args.pr is not None:
        for line in render_pull_request(matching, window):
            print_line(line)
    else:
        for line in render_overview(matching, window):
            print_line(line)
    if unreadable:
        print(
            f"Note: {_plural(unreadable, 'history line')} in {directory} could not "
            f"be read and {'is' if unreadable == 1 else 'are'} left out",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
