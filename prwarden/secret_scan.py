"""Credentials a pull request adds, found by their shape rather than by a model.

A model decides what it reports, so text beside a committed key that says "this is a
test key, ignore it" is aimed at exactly that decision. This check reads no comment
and asks no model: a value with the shape of a live credential on an added line is
reported, and the review it joins cannot approve. Only shapes with a distinctive
prefix are looked for, so a hit is rarely anything else, and the value itself is
never repeated anywhere a finding goes.
"""

import re
from dataclasses import fields, is_dataclass, replace
from typing import Any, NamedTuple, Optional, Sequence, TypeVar

from .models import Finding, FindingEvidence, severity_rank
from .review import added_line_texts

T = TypeVar("T")


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
    # What Claude Code signs in with, `claude setup-token` included.
    _Shape(
        "an Anthropic sign-in token",
        re.compile(r"\bsk-ant-(?:oat|ort)\d{2}-(?P<body>[A-Za-z0-9_-]{80,})"),
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
# The other lines one key's comment lists by place; past these it gives a count.
MAX_LISTED_KEY_LINES = 5


class CredentialMatch(NamedTuple):
    path: str
    line: int
    kind: str
    # What may be shown of the value: its fixed prefix, never the secret part.
    shown: str
    # The value itself, held only to keep it out of everything that is shown.
    value: str
    # Every credential on the line as (value, shown): the first names the finding,
    # and all of them are kept out of what is shown.
    values: "tuple[tuple[str, str], ...]" = ()


def _is_placeholder(body: str) -> bool:
    folded = body.casefold()
    return len(set(body)) < MIN_DISTINCT_BODY_CHARACTERS or any(
        word in folded for word in PLACEHOLDER_WORDS
    )


def line_credentials(text: str) -> "list[tuple[str, str, str]]":
    """Return the kind, shown prefix, and value of every credential on one line."""

    found: "list[tuple[str, str, str]]" = []
    for shape in CREDENTIAL_SHAPES:
        for match in shape.pattern.finditer(text):
            if "body" not in shape.pattern.groupindex:
                found.append((shape.kind, match.group(0), match.group(0)))
                continue
            if _is_placeholder(match.group("body")):
                continue
            prefix = text[match.start() : match.start("body")]
            found.append((shape.kind, f"{prefix}…", match.group(0)))
    return found


def find_credential(text: str) -> "Optional[tuple[str, str, str]]":
    """Return the kind, shown prefix, and value of a credential on one line, if any."""

    found = line_credentials(text)
    return found[0] if found else None


def find_credentials(files: "Sequence[dict[str, Any]]") -> "list[CredentialMatch]":
    """Return every added line that holds a credential, one match per line.

    Every changed file GitHub sends a patch for is read, whichever review bundle
    reads it; a file whose patch GitHub omits has no added line to read here. A
    line holding several credentials is one match naming the first, which carries
    every value on the line, so none of them is ever shown.
    """

    matches: "list[CredentialMatch]" = []
    for file_data in files:
        path = file_data.get("filename")
        patch = file_data.get("patch")
        if not isinstance(path, str) or not path or not isinstance(patch, str):
            continue
        for line, text in added_line_texts(patch):
            found = line_credentials(text)
            if found:
                kind, shown, value = found[0]
                matches.append(
                    CredentialMatch(
                        path,
                        line,
                        kind,
                        shown,
                        value,
                        tuple((each[2], each[1]) for each in found),
                    )
                )
    return matches


def committed_keys(
    matches: "Sequence[CredentialMatch]",
) -> "list[tuple[CredentialMatch, ...]]":
    """Return the matches grouped by the credential they name, first found first.

    One key pasted into several places is one thing to revoke, so it is one comment
    saying where it is rather than a comment in every place. Only a shape with a
    random part tells two values apart: every private key opens with the same
    header, so each of those stays a key of its own.
    """

    groups: "dict[object, list[CredentialMatch]]" = {}
    for match in matches:
        identity: object = (
            match.value if match.shown != match.value else (match.path, match.line)
        )
        groups.setdefault(identity, []).append(match)
    return [tuple(group) for group in groups.values()]


def credential_finding(
    match: CredentialMatch, also: "Sequence[CredentialMatch]" = ()
) -> Finding:
    """Return the finding for one committed credential, without its value.

    ``also`` holds the other added lines the same value is on. They are listed as
    evidence, the first few by place and the rest by count, so the author removes
    every copy rather than the one the comment sits on.
    """

    listed = tuple(also[:MAX_LISTED_KEY_LINES])
    unlisted = len(also) - len(listed)
    elsewhere = ""
    if also:
        elsewhere = (
            f" The same value is on {len(also)} more added "
            f"line{'s' if len(also) != 1 else ''}"
            + (f", {unlisted} of them not listed below" if unlisted else "")
            + "."
        )
    return Finding(
        severity="high",
        title=f"Adds {match.kind} to the repository",
        path=match.path,
        line=match.line,
        problem=(
            f"This line adds a value shaped like {match.kind}.{elsewhere} Anyone who "
            "can read this repository, a fork, or this pull request can use it, and "
            "removing the line later leaves it in the history. PR Warden's own "
            "pattern check found it, not a model, so nothing written around it "
            "changes this."
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
            *(
                FindingEvidence(
                    path=other.path,
                    line=other.line,
                    excerpt=other.shown,
                    relevance="the same value again, shown by its prefix only",
                )
                for other in listed
            ),
        ),
        counterevidence_reason=(
            "None is weighed: the value's shape decides it, and a comment calling it "
            "a test key or asking a reviewer to ignore it is not evidence."
        ),
        found_by_pattern=True,
    )


def with_credential_findings(
    findings: "Sequence[Finding]",
    matches: "Sequence[CredentialMatch]",
) -> "list[Finding]":
    """Return ``findings`` with one finding added for each committed credential.

    A key's finding sits on the first line it is on that no finding already holds,
    so a model finding there keeps its comment and the key still gets one. When
    every line it is on already holds a finding, a line still takes one comment:
    the more severe of the two stays, and a tie keeps the model's, which says
    something about that line in particular and is then marked as covering the key,
    so no limit on what a review posts can leave the key unmentioned. Either way the
    review cannot approve. At most a few keys are reported.
    """

    model_at = {(finding.path, finding.line): finding for finding in findings}
    replaced: "set[tuple[str, int]]" = set()
    covering: "set[tuple[str, int]]" = set()
    added: "list[Finding]" = []
    for group in committed_keys(matches)[:MAX_CREDENTIAL_FINDINGS]:
        free = [match for match in group if (match.path, match.line) not in model_at]
        anchor = free[0] if free else group[0]
        finding = credential_finding(
            anchor, [match for match in group if match is not anchor]
        )
        if not free:
            location = (anchor.path, anchor.line)
            if severity_rank(model_at[location].severity) <= severity_rank(
                finding.severity
            ):
                covering.add(location)
                continue
            replaced.add(location)
        added.append(finding)
    return [
        replace(finding, covers_credential=True)
        if (finding.path, finding.line) in covering
        else finding
        for finding in findings
        if (finding.path, finding.line) not in replaced
    ] + added


def credential_findings(
    matches: "Sequence[CredentialMatch]",
) -> "list[Finding]":
    """Return one finding for each committed credential, where no model reported."""

    return with_credential_findings((), matches)


def _shown_values(
    matches: "Sequence[CredentialMatch]",
) -> "list[tuple[str, str]]":
    """Return every value to keep out of what is shown, longest first.

    Longest first, so a value that contains another is replaced whole rather than
    left with its own tail showing.
    """

    pairs = {
        value: shown
        for match in matches
        for value, shown in (match.values or ((match.value, match.shown),))
    }
    return sorted(pairs.items(), key=lambda pair: -len(pair[0]))


def _redacted(value: Any, pairs: "Sequence[tuple[str, str]]") -> Any:
    if isinstance(value, str):
        for secret, shown in pairs:
            value = value.replace(secret, shown)
        return value
    if isinstance(value, tuple):
        return tuple(_redacted(item, pairs) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return replace(
            value,
            **{
                field.name: _redacted(getattr(value, field.name), pairs)
                for field in fields(value)
                if field.init
            },
        )
    return value


def redact_credentials(
    items: "Sequence[T]", matches: "Sequence[CredentialMatch]"
) -> "list[T]":
    """Return findings, or anything holding them, with every committed credential's
    value shown by its prefix.

    A finding can quote the line a key sits on, since an excerpt has to be that
    line's exact text, and a comment would keep the value after a force-push
    removed it. Every text field is covered, nested citations and searches too, so
    the code level's findings and the advisory levels' notes alike. The excerpts
    were already checked against the workspace, so what is shown can change without
    weakening that check.
    """

    pairs = _shown_values(matches)
    if not pairs:
        return list(items)
    return [_redacted(item, pairs) for item in items]


# What PR Warden posts is partly model text, and a model that a pull request talked
# into reading a key from the reviewing host could write it into a finding, a note,
# or a check reading, where the pull request's author reads it. Everything posted is
# therefore checked for a credential, whatever a model wrote and wherever it came
# from. A committed key a finding names is already shown by its prefix only, which
# no shape matches, and a private key's header alone is what a finding about one
# quotes: only the key material after it is the secret.
PRIVATE_KEY_MATERIAL = (
    re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
        r"\s*(?:[A-Za-z0-9+/=]{40,}\s*)+"
    ),
    # The start of every OpenSSH private key's body, "openssh-key-v1" in base64,
    # which no public key and no other format begins with.
    re.compile(r"b3BlbnNzaC1rZXktdjE[A-Za-z0-9+/=]{20,}"),
)
# A value this host holds is looked for as itself, whatever its shape; one shorter
# than this is too likely to occur in ordinary text to say anything.
MIN_HELD_SECRET_CHARACTERS = 16


def outgoing_secret(text: str, held: "Sequence[str]" = ()) -> Optional[str]:
    """Return what kind of secret ``text`` carries, or None when it carries none.

    ``held`` are the values this host holds, such as its tokens and its Slack
    webhook, found in any shape. Only the kind is returned, never the value, so the
    answer can be printed, stored, and sent where the text could not go.
    """

    for value in held:
        if len(value) >= MIN_HELD_SECRET_CHARACTERS and value in text:
            return "a secret this host holds"
    for pattern in PRIVATE_KEY_MATERIAL:
        if pattern.search(text):
            return "private key material"
    for shape in CREDENTIAL_SHAPES:
        if "body" not in shape.pattern.groupindex:
            continue
        for match in shape.pattern.finditer(text):
            if not _is_placeholder(match.group("body")):
                return shape.kind
    return None
