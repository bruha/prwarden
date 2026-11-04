import json
import re
import urllib.error
import urllib.request
from typing import Optional, Sequence

from .models import DesignFinding, DocsFinding, Finding, PullRequest, Repository
from .reviewer_identity import format_review_attribution, format_reviewer_identity


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
                f"• Problem: {finding.problem}",
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
    lines.extend(
        [
            f"_{reviewer}_",
        ]
    )
    return "\n".join(lines).rstrip()


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
