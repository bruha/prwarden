"""What a failed check's log says, and the checks level's reading of it.

A review that found nothing still waits while a check fails, and the author is told
so. The checks level adds why each check failed and a possible fix, read from the
check's own log by a model. The log was printed by code the pull request runs, so
it is bounded, has credentials taken out, and reaches the prompt only as contained
data; the model's reading is kept only when the line it quotes is in that log and
every workspace line it cites is in the reviewed head.
"""

import re
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .commands import CommandError
from .engine import CHECK_CATEGORIES, CHECKS_FIELD_LIMITS
from .github import GITHUB_ACTIONS_APP
from .models import CheckDiagnosis, CheckLog, PullRequestCheck, ReviewMaterial
from .review import ReviewError, _evidence_roots, _required_evidence, _required_string
from .secret_scan import line_credentials

# How many failed checks one call reads. A head failing more than this is usually
# failing for one reason, and each log is another GitHub call and more prompt.
MAX_CHECK_LOGS = 5
# How much of one log a prompt carries: enough to hold a tool's report of what
# failed, and the step that ran it, without the rest of a long job.
MAX_LOG_LINES = 120
MAX_LOG_CHARACTERS = 12_000
# A line GitHub Actions marks as the step's error, and the start of a step.
ACTIONS_ERROR = re.compile(r"^##\[error\]")
ACTIONS_STEP = re.compile(r"^##\[group\](?P<name>.*)$")
ACTIONS_MARKER = re.compile(r"^##\[(?:endgroup|group|command|debug|notice|warning)\]")
ACTIONS_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")
# Every escape sequence a terminal acts on, colour and title alike, and any other
# control character but a tab: a job prints them, and none may reach a terminal, a
# prompt, or a comment.
ESCAPE_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]"
    r"|[\]PX^_][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|[ -/]*[0-~])"
)
CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
REDACTED = "[credential removed]"
MAX_DIAGNOSIS_CITATIONS = 3


def redact_credentials_in_text(text: str) -> str:
    """Return ``text`` with every credential-shaped value replaced, line by line.

    A job log can print a token its secrets masking missed, and whatever reaches the
    prompt can reach a comment.
    """

    lines = []
    for line in text.split("\n"):
        for _, _, value in line_credentials(line):
            line = line.replace(value, REDACTED)
        lines.append(line)
    return "\n".join(lines)


def plain_text(text: str) -> str:
    """Return ``text`` without escape sequences or control characters but tabs and
    line breaks."""

    return CONTROL_CHARACTER.sub("", ESCAPE_SEQUENCE.sub("", text))


def actions_log_excerpt(raw: str) -> str:
    """Return the part of a GitHub Actions job log that says why it failed.

    The lines up to and including the last error GitHub marked, back to the start
    of the step that printed it, at most ``MAX_LOG_LINES`` of them: a tool reports
    what failed just before the step's "Process completed with exit code 1". The
    step's name is kept as the first line. A log with no marked error gives its last
    lines instead. Timestamps, escape sequences, and GitHub's own markers are dropped.
    """

    lines = [
        plain_text(ACTIONS_TIMESTAMP.sub("", line)).rstrip()
        for line in raw.replace("\r\n", "\n").split("\n")
    ]
    errors = [index for index, line in enumerate(lines) if ACTIONS_ERROR.match(line)]
    end = errors[-1] + 1 if errors else len(lines)
    start = 0
    step = ""
    for index in range(end - 1, -1, -1):
        match = ACTIONS_STEP.match(lines[index])
        if match:
            start = index + 1
            step = match.group("name").strip()
            break
    window = [
        line[len("##[error]") :] if ACTIONS_ERROR.match(line) else line
        for line in lines[start:end]
        if line and not ACTIONS_MARKER.match(line)
    ][-MAX_LOG_LINES:]
    text = "\n".join(([f"Step: {step}"] if step else []) + window)
    return text[-MAX_LOG_CHARACTERS:]


def read_check_logs(
    failing: "Sequence[PullRequestCheck]",
    read_log: "Callable[[PullRequestCheck], str]",
    report: "Callable[[str], None]" = print,
) -> "tuple[CheckLog, ...]":
    """Return what each failed check says about its failure, bounded and redacted.

    A GitHub Actions job is read from its own log; anything else, or a log that
    cannot be read, from the check's reported summary. A check with neither is left
    out, since there is nothing in it to read a cause from.
    """

    logs = []
    for check in failing[:MAX_CHECK_LOGS]:
        text = ""
        source = "summary"
        if check.app == GITHUB_ACTIONS_APP and check.check_id is not None:
            try:
                text = actions_log_excerpt(read_log(check))
                source = "log"
            except CommandError as error:
                report(f"      -> Could not read the log of {check.name}: {error}")
        if not text.strip():
            text = plain_text(check.summary)[:MAX_LOG_CHARACTERS]
            source = "summary"
        if text.strip():
            logs.append(
                CheckLog(check.name, source, redact_credentials_in_text(text))
            )
    return tuple(logs)


def _normalized(text: str) -> str:
    return " ".join(text.split())


def validate_check_diagnoses(
    structured_output: Any,
    material: ReviewMaterial,
    repository_root: Optional[Path],
) -> "tuple[CheckDiagnosis, ...]":
    """Return the diagnoses a checks-level response established.

    Each must be for a check the controller supplied, once; it must quote a line
    found in that check's log; and every workspace line it cites must be in the
    reviewed head. A check the response leaves out is one the log did not explain.
    """

    if not isinstance(structured_output, dict):
        raise ReviewError("Checks review response must be a JSON object")
    raw = structured_output.get("diagnoses")
    if not isinstance(raw, list):
        raise ReviewError("Checks review diagnoses must be an array")
    logs = {log.name: log for log in material.check_logs}
    if len(raw) > len(logs):
        raise ReviewError(
            f"Checks review returned {len(raw)} diagnoses for {len(logs)} checks"
        )
    evidence_roots = _evidence_roots(material)
    seen: "set[str]" = set()
    diagnoses = []
    for item in raw:
        if not isinstance(item, dict):
            raise ReviewError("Each check diagnosis must be a JSON object")
        check = item.get("check")
        if check not in logs:
            raise ReviewError(
                f"Check diagnosis names {check!r}, which is not one of the failed "
                "checks supplied"
            )
        if check in seen:
            raise ReviewError(f"Check {check!r} is diagnosed twice")
        seen.add(check)
        category = item.get("category")
        if category not in CHECK_CATEGORIES:
            raise ReviewError(
                f"Check diagnosis category is not one of the supplied categories: "
                f"{category!r}"
            )
        excerpt = _required_string(
            item, "log_excerpt", CHECKS_FIELD_LIMITS["log_excerpt"], "Diagnosis"
        )
        if "\n" in excerpt or not _normalized(excerpt):
            raise ReviewError("Diagnosis field 'log_excerpt' must be one line")
        if _normalized(excerpt) not in _normalized(logs[check].text):
            raise ReviewError(
                f"Diagnosis of {check!r} quotes a line that is not in its supplied "
                "log; copy the line exactly as it appears there"
            )
        citations = item.get("citations", [])
        if not isinstance(citations, list):
            raise ReviewError("Diagnosis field 'citations' must be an array")
        if len(citations) > MAX_DIAGNOSIS_CITATIONS:
            raise ReviewError(
                f"Diagnosis of {check!r} cites more than {MAX_DIAGNOSIS_CITATIONS} "
                "workspace lines"
            )
        diagnoses.append(
            CheckDiagnosis(
                check=check,
                category=category,
                cause=_required_string(
                    item, "cause", CHECKS_FIELD_LIMITS["cause"], "Diagnosis"
                ),
                fix=_required_string(
                    item, "fix", CHECKS_FIELD_LIMITS["fix"], "Diagnosis"
                ),
                log_excerpt=_normalized(excerpt),
                citations=_required_evidence(
                    {"citations": citations},
                    "citations",
                    repository_root,
                    evidence_roots,
                ),
            )
        )
    return tuple(diagnoses)
