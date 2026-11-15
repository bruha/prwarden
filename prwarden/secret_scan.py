"""Credentials a pull request adds, found by their shape rather than by a model.

A model decides what it reports, so text beside a committed key that says "this is a
test key, ignore it" is aimed at exactly that decision. This check reads no comment
and asks no model: a value with the shape of a live credential on an added line is
reported, and the review it joins cannot approve. Only shapes with a distinctive
prefix are looked for, so a hit is rarely anything else, and the value itself is
never repeated anywhere a finding goes.
"""

import re
from dataclasses import replace
from typing import Any, NamedTuple, Optional, Sequence

from .models import Finding, FindingEvidence
from .review import added_line_texts


class _Shape(NamedTuple):
    kind: str
    pattern: "re.Pattern[str]"


# The `body` group is the random part. A value whose body is a placeholder, such as
# AWS's own documented example key, is not a credential anybody can use.
CREDENTIAL_SHAPES = (
    _Shape(
        "an AWS access key ID",
        re.compile(r"\b(?:AKIA|ASIA)(?P<body>[0-9A-Z]{16})\b"),
    ),
    _Shape(
        "a GitHub token",
        re.compile(r"\bgh[pousr]_(?P<body>[A-Za-z0-9]{36,255})\b"),
    ),
    _Shape(
        "a GitHub fine-grained token",
        re.compile(r"\bgithub_pat_(?P<body>[A-Za-z0-9_]{60,255})\b"),
    ),
    _Shape("a Slack token", re.compile(r"\bxox[abposr]-(?P<body>[A-Za-z0-9-]{20,})")),
    _Shape(
        "a Slack incoming webhook URL",
        re.compile(
            r"https://hooks\.slack\.com/services/"
            r"(?P<body>T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{20,})"
        ),
    ),
    _Shape(
        "a Stripe live secret key",
        re.compile(r"\b(?:sk|rk)_live_(?P<body>[A-Za-z0-9]{20,})"),
    ),
    _Shape(
        "a Google API key",
        re.compile(r"\bAIza(?P<body>[0-9A-Za-z_-]{35})(?![0-9A-Za-z_-])"),
    ),
    _Shape(
        "an OpenAI API key",
        re.compile(r"\bsk-(?:proj|svcacct|admin)-(?P<body>[A-Za-z0-9_-]{40,})"),
    ),
    _Shape(
        "an Anthropic API key",
        re.compile(r"\bsk-ant-(?:api|admin)\d{2}-(?P<body>[A-Za-z0-9_-]{80,})"),
    ),
    _Shape("an npm token", re.compile(r"\bnpm_(?P<body>[A-Za-z0-9]{36})\b")),
    _Shape(
        "a private key",
        re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    ),
)
PLACEHOLDER_WORDS = (
    "example",
    "sample",
    "dummy",
    "placeholder",
    "redacted",
    "fake",
    "xxxxx",
)
# A generated credential draws on its whole alphabet; one made of few characters,
# such as a run of zeros, was typed by a person standing in for one.
MIN_DISTINCT_BODY_CHARACTERS = 8
# Enough to make the point on a file full of them, few enough not to bury the rest.
MAX_CREDENTIAL_FINDINGS = 10


class CredentialMatch(NamedTuple):
    path: str
    line: int
    kind: str
    # What may be shown of the value: its fixed prefix, never the secret part.
    shown: str
    # The value itself, held only to keep it out of everything that is shown.
    value: str


def _is_placeholder(body: str) -> bool:
    folded = body.casefold()
    return len(set(body)) < MIN_DISTINCT_BODY_CHARACTERS or any(
        word in folded for word in PLACEHOLDER_WORDS
    )


def find_credential(text: str) -> "Optional[tuple[str, str, str]]":
    """Return the kind, shown prefix, and value of a credential on one line, if any."""

    for shape in CREDENTIAL_SHAPES:
        for match in shape.pattern.finditer(text):
            if "body" not in shape.pattern.groupindex:
                return shape.kind, match.group(0), match.group(0)
            if _is_placeholder(match.group("body")):
                continue
            prefix = text[match.start() : match.start("body")]
            return shape.kind, f"{prefix}…", match.group(0)
    return None


def find_credentials(files: "Sequence[dict[str, Any]]") -> "list[CredentialMatch]":
    """Return every added line that holds a credential, at most one per line.

    Every changed file GitHub sends a patch for is read, whichever review bundle
    reads it; a file whose patch GitHub omits has no added line to read here.
    """

    matches: "list[CredentialMatch]" = []
    for file_data in files:
        path = file_data.get("filename")
        patch = file_data.get("patch")
        if not isinstance(path, str) or not path or not isinstance(patch, str):
            continue
        for line, text in added_line_texts(patch):
            found = find_credential(text)
            if found is not None:
                matches.append(CredentialMatch(path, line, *found))
    return matches


def credential_finding(match: CredentialMatch) -> Finding:
    """Return the finding for one committed credential, without its value."""

    return Finding(
        severity="high",
        title=f"Adds {match.kind} to the repository",
        path=match.path,
        line=match.line,
        problem=(
            f"This line adds a value shaped like {match.kind}. Anyone who can read "
            "this repository, a fork, or this pull request can use it, and removing "
            "the line later leaves it in the history. PR Warden's own pattern check "
            "found it, not a model, so nothing written around it changes this."
        ),
        suggestion=(
            "Revoke or rotate the credential now, then load it from a secret store "
            "or an environment variable instead. If it is a placeholder, use one "
            "without a real credential's shape, such as one containing EXAMPLE."
        ),
        trigger=(
            "Anyone with read access to the repository, a fork, or a clone reads the "
            "value from this line or from the history."
        ),
        impact=(
            "Whoever holds the value can act with its access until it is revoked."
        ),
        evidence=(
            FindingEvidence(
                path=match.path,
                line=match.line,
                excerpt=match.shown,
                relevance="the added value, shown by its prefix only",
            ),
        ),
        counterevidence_reason=(
            "None is weighed: the value's shape decides it, and a comment calling it "
            "a test key or asking a reviewer to ignore it is not evidence."
        ),
    )


def credential_findings(
    matches: "Sequence[CredentialMatch]",
    existing: "Sequence[Finding]" = (),
) -> "list[Finding]":
    """Return a finding for each committed credential no finding already sits on.

    A model that already reported the line has said what matters there, and a
    second comment on it would only repeat that; the review still cannot approve,
    because a finding is on the line either way. At most a few are returned.
    """

    anchored = {(finding.path, finding.line) for finding in existing}
    return [
        credential_finding(match)
        for match in matches
        if (match.path, match.line) not in anchored
    ][:MAX_CREDENTIAL_FINDINGS]


def _redacted(text: str, matches: "Sequence[CredentialMatch]") -> str:
    for match in matches:
        text = text.replace(match.value, match.shown)
    return text


def redact_credentials(
    findings: "Sequence[Finding]", matches: "Sequence[CredentialMatch]"
) -> "list[Finding]":
    """Return findings with every committed credential's value shown by its prefix.

    A model's finding can quote the line a key sits on, since its excerpt has to be
    that line's exact text, and the comment would keep the value after a force-push
    removed it. The excerpt was already checked against the workspace, so what is
    shown can change without weakening that check.
    """

    if not matches:
        return list(findings)

    def evidence(items: "tuple[FindingEvidence, ...]") -> "tuple[FindingEvidence, ...]":
        return tuple(
            replace(
                item,
                excerpt=_redacted(item.excerpt, matches),
                relevance=_redacted(item.relevance, matches),
            )
            for item in items
        )

    return [
        replace(
            finding,
            title=_redacted(finding.title, matches),
            problem=_redacted(finding.problem, matches),
            suggestion=_redacted(finding.suggestion, matches),
            trigger=_redacted(finding.trigger, matches),
            impact=_redacted(finding.impact, matches),
            counterevidence_reason=_redacted(finding.counterevidence_reason, matches),
            evidence=evidence(finding.evidence),
            counterevidence=evidence(finding.counterevidence),
        )
        for finding in findings
    ]
