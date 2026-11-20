import json
import re
import urllib.error
import urllib.request
from typing import Optional, Sequence

from .models import (
    FAILING_OPTIONAL_CHECKS,
    WAITING_FOR_CHECKS,
    CheckDiagnosis,
    DesignFinding,
    DocsFinding,
    Finding,
    PullRequest,
    Repository,
    WaitingReason,
)
from .reviewer_identity import (
    describe_checks,
    describe_severity_change,
    format_review_attribution,
    format_reviewer_identity,
)


class DeliveryError(RuntimeError):
    """Raised when delivery definitely did not begin."""


class DeliveryUncertainError(RuntimeError):
    """Raised when a delivery may have succeeded despite a client-side error."""


ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w:/])/(?:[^\s/\\|<>]+/)+([^\s/\\|<>]*)")


def redact_local_paths(text: str) -> str:
    """Reduce absolute local paths in outbound text to their final component.

    Terminal output is local and keeps full paths for debugging. A Slack channel
    is shared and archived, so a message must not disclose the reviewing host's
    directory layout, account name, or install locations. URLs are left intact
    because their slashes follow a scheme or host character.
    """

    def shorten(match: "re.Match[str]") -> str:
        return match.group(1) or match.group(0).rstrip("/").rsplit("/", 1)[-1]

    return ABSOLUTE_PATH_PATTERN.sub(shorten, text)


def format_slack_message(
    repository: Repository,
    pull_request: PullRequest,
    findings: list[Finding],
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
    withheld: int = 0,
) -> str:
    lines = [
        f"*PR #{pull_request.number} Code Review Feedback ({repository.name})*",
        _format_pr_link(repository, pull_request),
        f"*{pull_request.title}*",
        "",
    ]
    for index, finding in enumerate(findings, 1):
        lines.extend(
            [
                f"{index}. *[{finding.severity.upper()}] {finding.title}*",
                f"• File: `{finding.path}` (Line {finding.line})",
                "• Checked: "
                + describe_checks(
                    finding, review_engine, review_model, verify_engine, verify_model
                ),
                *(
                    [f"• Severity: {describe_severity_change(finding)}"]
                    if finding.original_severity
                    else []
                ),
                f"• Problem: {finding.problem}",
                *(
                    [f"• Repository rule (`{finding.rule_source}`): {finding.rule}"]
                    if finding.rule
                    else []
                ),
                f"• Trigger: {finding.trigger}",
                f"• Impact: {finding.impact}",
                "• Evidence: "
                + "; ".join(
                    f"`{item.path}:{item.line}` — {item.relevance}"
                    for item in finding.evidence
                ),
                f"• Counterevidence considered: {finding.counterevidence_reason}",
                f"• Change: {finding.suggestion}",
                "",
            ]
        )
    reviewer = _format_reviewer(
        review_engine,
        review_model,
        verify_engine,
        verify_model,
    )
    if withheld:
        lines.append(f"_{withheld_note(len(findings), withheld)}_")
        lines.append("")
    lines.extend(
        [
            f"_{reviewer}_",
        ]
    )
    return "\n".join(lines).rstrip()


def withheld_note(shown: int, withheld: int) -> str:
    """Say that a review confirmed more findings than it shows, and what follows.

    Inline comments alone read as everything the review found, so a review that
    left some for the next one says so where the author reads it.
    """

    most_severe = "the most severe" if shown == 1 else f"the {shown} most severe"
    return (
        f"This review confirmed {shown + withheld} defects and shows {most_severe}. "
        "The next review reads the files of the rest again."
    )


def format_docs_slack_message(
    repository: Repository,
    pull_request: PullRequest,
    findings: "Sequence[DocsFinding]",
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
) -> str:
    """Render proposed documentation updates for the reviewer's own pull request."""

    lines = [
        f"*PR #{pull_request.number} Documentation Updates ({repository.name})*",
        _format_pr_link(repository, pull_request),
        f"*{pull_request.title}*",
        "The code may be right and the document out of date.",
        "",
    ]
    for index, finding in enumerate(findings, 1):
        audience = "coding agents" if finding.audience == "ai" else "people"
        location = f"`{finding.documentation.path}:{finding.documentation.line}`"
        update_label = (
            f"Add after {location}" if finding.adds_list_entries else "Proposed update"
        )
        lines.extend(
            [
                f"{index}. *[{finding.severity.upper()}] {finding.title}*",
                f"• Document: {location} (for {audience})",
                f"• Says today: {finding.documented}",
                f"• Change makes true: {finding.behavior}",
                f"• {update_label}: {finding.proposed_update}",
                "• Changed lines: "
                + "; ".join(
                    f"`{item.path}:{item.line}` — {item.relevance}"
                    for item in finding.citations
                ),
                "",
            ]
        )
    reviewer = _format_reviewer(
        review_engine,
        review_model,
        verify_engine,
        verify_model,
    )
    lines.append(f"_{reviewer}_")
    return redact_local_paths("\n".join(lines).rstrip())


def format_design_slack_message(
    repository: Repository,
    pull_request: PullRequest,
    findings: "Sequence[DesignFinding]",
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
) -> str:
    """Render advisory design notes for the reviewer's own pull request."""

    lines = [
        f"*PR #{pull_request.number} Solution Design Notes ({repository.name})*",
        _format_pr_link(repository, pull_request),
        f"*{pull_request.title}*",
        "These describe the approach, not individual lines.",
        "",
    ]
    for index, finding in enumerate(findings, 1):
        lines.extend(
            [
                f"{index}. *[{finding.severity.upper()}] {finding.title}*",
                f"• Category: {finding.category.replace('_', ' ')}",
                f"• Problem: {finding.problem}",
                f"• Consequence: {finding.consequence}",
                f"• Not a preference because: {finding.why_not_preference}",
                f"• Cheaper structure: {finding.alternative}",
                "• Citations: "
                + "; ".join(
                    f"`{item.path}:{item.line}` — {item.relevance}"
                    for item in finding.citations
                ),
            ]
        )
        if finding.searches:
            lines.append(
                "• Searches: "
                + "; ".join(
                    f"`{search.pattern}` in `{search.scope}` — "
                    + (
                        "matched only "
                        + ", ".join(f"`{path}`" for path in search.matched_files)
                        if search.matched_files
                        else "no match"
                    )
                    for search in finding.searches
                )
            )
        lines.append("")
    reviewer = _format_reviewer(
        review_engine,
        review_model,
        verify_engine,
        verify_model,
    )
    lines.append(f"_{reviewer}_")
    return redact_local_paths("\n".join(lines).rstrip())


def format_manual_review_message(
    repository: Repository,
    pull_request: PullRequest,
    reason: str,
    review_engine: str,
    review_model: Optional[str],
    coverage: Optional[str] = None,
    not_reviewed: Optional[str] = None,
) -> str:
    """Ask for a human review, saying how much of the change was read.

    ``coverage`` distinguishes the two ways a change can be too large: one where
    no review ran at all, and one where the review read what fitted, which still
    needs a human for the rest but not for the whole.
    """

    lines = [
        "*MANUAL REVIEW NEEDED*",
        "PR Warden could not review this change completely.",
        _format_pr_link(repository, pull_request),
        f"*{pull_request.title}*",
        (
            f"> Automated review: partial, {coverage}"
            if coverage
            else "> Automated review: not run"
        ),
        f"> Reason: {reason}",
    ]
    if not_reviewed:
        lines.append(f"> Not reviewed: {not_reviewed}")
    lines.extend(
        ["", f"_{_format_reviewer(review_engine, review_model, configured=True)}_"]
    )
    return "\n".join(lines)


def format_stale_review_message(
    repository: Repository,
    pull_request: PullRequest,
    reason: str,
    review_engine: str,
    review_model: Optional[str],
) -> str:
    """Report a result a human's newer feedback overtook before delivery.

    Nothing is wrong with the review; somebody was reviewing the same pull
    request at the same time and said something this one had not read. The
    message names where that feedback landed and never quotes it, because the
    body is the author's text and this leaves the machine.
    """

    return "\n".join(
        [
            "*PR Warden: review not delivered*",
            _format_pr_link(repository, pull_request),
            f"*{pull_request.title}*",
            f"> Reason: {reason}",
            "> Run the reviewer again to judge the current feedback.",
            "",
            f"_{_format_reviewer(review_engine, review_model, configured=True)}_",
        ]
    )


def format_slack_lgtm_message(
    repository: Repository,
    pull_request: PullRequest,
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
    rejected_candidates: int = 0,
) -> str:
    """Describe a clean result, including candidates verification threw out.

    An LGTM reached by rejecting candidates is a different event from a review
    that saw nothing to raise, and only the first says the verification pass did
    any work. Reporting the count distinguishes them without implying the
    rejected candidates were real defects.
    """
    if rejected_candidates < 0:
        raise ValueError("Rejected candidate count must not be negative")
    reviewer = _format_reviewer(
        review_engine,
        review_model,
        verify_engine,
        verify_model,
    )
    lines = [
        "*PR Warden: LGTM*",
        _format_pr_link(repository, pull_request),
        f"*{pull_request.title}*",
        "> LGTM — AI review found no qualifying issues and GitHub reports "
        "no merge conflicts. This PR can be merged once required checks "
        "and branch protections pass.",
    ]
    if rejected_candidates:
        issue_word = "issue" if rejected_candidates == 1 else "issues"
        lines.append(
            f"> Verification confirmed 0 of {rejected_candidates} candidate "
            f"{issue_word} the review raised."
        )
    lines.extend(["", f"_{reviewer}_"])
    return "\n".join(lines)


def _format_pr_link(repository: Repository, pull_request: PullRequest) -> str:
    return f"<{pull_request.url}|{repository.slug} PR #{pull_request.number}>"


# Check names come from the reviewed head, so a list of them is bounded like the
# note's own list.
MAX_SLACK_CHECK_NAMES = 10
MAX_SLACK_CHECK_NAME_CHARACTERS = 120


def _slack_check_names(names: "Sequence[str]") -> str:
    """Return check names as inline code, each on one line and cut, the first few."""

    shown = ", ".join(
        "`" + " ".join(name.split()).replace("`", "")[:MAX_SLACK_CHECK_NAME_CHARACTERS]
        + "`"
        for name in names[:MAX_SLACK_CHECK_NAMES]
    )
    more = len(names) - MAX_SLACK_CHECK_NAMES
    return shown + (f" and {more} more" if more > 0 else "")


def format_waiting_slack_message(
    repository: Repository,
    pull_request: PullRequest,
    reason: WaitingReason,
    shown: "Sequence[str]",
    more: int = 0,
    *,
    diagnoses: "Sequence[CheckDiagnosis]" = (),
    reader: str = "",
) -> str:
    """Say what a clean review of the reviewer's own pull request waits for.

    ``shown`` are the items to name, already bounded, and ``more`` how many others
    there are. ``diagnoses`` are the checks level's readings, written by ``reader``.
    """

    names = ", ".join(f"`{item.replace('`', '')}`" for item in shown)
    if more:
        names += f" and {more} more"
    head = pull_request.head_sha[:12]
    if reason.kind == WAITING_FOR_CHECKS:
        heading = "Waiting for checks"
        text = (
            f"PR Warden found nothing to report at `{head}`, but holds its LGTM "
            f"while these checks fail: {names}. It reviews again once they pass."
        )
        if reason.optional:
            text += (
                " These fail too, but GitHub does not require them to merge, so "
                f"they do not hold it back: {_slack_check_names(reason.optional)}."
            )
    elif reason.kind == FAILING_OPTIONAL_CHECKS:
        heading = "Checks to fix"
        text = (
            f"PR Warden found nothing to report at `{head}` and sent its LGTM, but "
            f"these checks fail: {names}. GitHub does not require them to merge, so "
            "they do not hold the LGTM back; fix them, or re-run them."
        )
    else:
        heading = "Waiting for a person"
        text = (
            f"PR Warden found nothing to report at `{head}`, but holds its LGTM on "
            f"a change to review policy until a person signs it off: {names}. A "
            "person with write access other than you approves the pull request or "
            "replies LGTM on it."
        )
    lines = [
        f"*PR #{pull_request.number} {heading} ({repository.name})*",
        _format_pr_link(repository, pull_request),
        f"*{pull_request.title}*",
        f"> {text}",
    ]
    for diagnosis in diagnoses:
        name = " ".join(diagnosis.check.split()).replace("`", "")[:120]
        lines.extend(
            [
                "",
                f"• `{name}`: {diagnosis.cause}",
                f"  Possible fix: {diagnosis.fix}",
            ]
        )
    if diagnoses:
        lines.extend(["", f"_Read from the checks' output by {reader or 'a model'}._"])
    return redact_local_paths("\n".join(lines))


def _format_reviewer(
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
    *,
    configured: bool = False,
) -> str:
    if configured:
        return f"Configured reviewer: {format_reviewer_identity(review_engine, review_model)}"
    return format_review_attribution(
        review_engine,
        review_model,
        verify_engine,
        verify_model,
    )


def send_slack_message(webhook_url: str, text: str, timeout_seconds: int = 20) -> str:
    """Post one message to Slack, without local filesystem paths."""

    text = redact_local_paths(text)
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace").strip()
            if response.status != 200 or body != "ok":
                raise DeliveryUncertainError(
                    f"Slack returned HTTP {response.status} with body {body!r}"
                )
            return "http-200"
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace").strip()
        raise DeliveryError(f"Slack rejected the message: HTTP {error.code} {body}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DeliveryUncertainError(f"Slack delivery outcome is uncertain: {error}") from error
