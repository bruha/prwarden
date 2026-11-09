"""The Jira work items a pull request names, read through Atlassian's own CLI.

A diff shows what changed and not why. The pull request's description says some of
it, and the work item it names often says the rest: what was asked for and what
done looks like. Reading it lets a review judge the change against that intent.

Only the project keys a repository lists are looked for, because the bare pattern
of a work item key also matches `UTF-8`, `ISO-8601`, and `SHA-256`. The CLI is
`acli`, signed in by the operator with `acli jira auth login`; PR Warden never sees
or stores the credential. Everything read here is untrusted, like a pull request's
own description: anyone who can edit a work item wrote it.
"""

import json
import re
import shutil
from typing import Any, Callable, Iterable, Optional, Sequence

from .commands import CommandError, run_cmd
from .config import JIRA_PROJECT_KEY
from .engine import scrubbed_environment
from .models import LinkedIssue
from .tools import search_path

JIRA_COMMAND = "acli"
JIRA_FIELDS = "key,issuetype,summary,status,description"
# A review reads a few work items, not a backlog: a pull request naming more is
# naming context, and each one costs a call and prompt room.
MAX_JIRA_ITEMS = 3
MAX_ISSUE_BODY_CHARACTERS = 3000
MAX_FAILURE_CHARACTERS = 300
JIRA_TIMEOUT_SECONDS = 60

Runner = Callable[..., str]


def find_jira_keys(
    texts: Iterable[str],
    projects: Sequence[str],
    limit: int = MAX_JIRA_ITEMS,
) -> "tuple[str, ...]":
    """Return the work item keys of ``projects`` that ``texts`` name, first first.

    Matched without regard to case, because a branch is often `app-123-fix`, and
    returned in Jira's own upper case, each once.
    """

    if not projects:
        return ()
    pattern = re.compile(
        r"(?<![A-Za-z0-9])("
        + "|".join(re.escape(project) for project in projects)
        + r")-([1-9][0-9]{0,8})(?![0-9])",
        re.IGNORECASE,
    )
    keys: "list[str]" = []
    for text in texts:
        for match in pattern.finditer(text or ""):
            key = f"{match.group(1).upper()}-{match.group(2)}"
            if key not in keys:
                keys.append(key)
            if len(keys) >= limit:
                return tuple(keys)
    return tuple(keys)


def document_text(node: Any) -> str:
    """Return the plain text of an Atlassian Document Format value.

    Jira Cloud returns a description as a tree of nodes rather than as text.
    Paragraphs, headings, list items, and table cells each become a line, a hard
    break a line break, and every other node contributes only its text, so nothing
    the tree holds is lost to an unfamiliar node type. A plain string passes through.
    """

    if isinstance(node, str):
        return node
    lines: "list[str]" = []
    current: "list[str]" = []
    blocks = {"paragraph", "heading", "listItem", "tableCell", "codeBlock", "panel"}

    def flush() -> None:
        text = "".join(current).strip()
        if text:
            lines.append(text)
        current.clear()

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        kind = value.get("type")
        if kind == "text" and isinstance(value.get("text"), str):
            current.append(value["text"])
        elif kind == "hardBreak":
            flush()
        elif kind in ("mention", "emoji", "status", "inlineCard"):
            attrs = value.get("attrs") if isinstance(value.get("attrs"), dict) else {}
            label = attrs.get("text") or attrs.get("shortName") or attrs.get("url")
            if isinstance(label, str):
                current.append(label)
        if kind in blocks:
            flush()
        walk(value.get("content"))
        if kind in blocks:
            flush()

    walk(node)
    flush()
    return "\n".join(lines)


def _name_of(value: Any) -> str:
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        return value["name"]
    return value if isinstance(value, str) else ""


def parse_work_item(payload: Any, key: str) -> LinkedIssue:
    """Return the work item ``acli jira workitem view --json`` described.

    Jira's REST shape puts the fields under `fields`; a flatter shape is read too,
    since the CLI's own output is not documented. A work item without a summary is
    not one this review can use, and says so.
    """

    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError(f"acli returned no work item for {key}")
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        fields = payload
    summary = fields.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError(f"acli returned no summary for {key}")
    reported = payload.get("key")
    if isinstance(reported, str) and reported.strip().upper() != key:
        raise ValueError(f"acli returned {reported!r} when asked for {key}")
    body = document_text(fields.get("description") or "").strip()
    if len(body) > MAX_ISSUE_BODY_CHARACTERS:
        body = body[: MAX_ISSUE_BODY_CHARACTERS - 1].rstrip() + "…"
    return LinkedIssue(
        source="jira",
        reference=key,
        title=" ".join(summary.split()),
        state=_name_of(fields.get("status")),
        kind=_name_of(fields.get("issuetype")),
        body=body,
    )


def jira_command() -> Optional[str]:
    """Return where `acli` is, looking where installers put it as well."""

    return shutil.which(JIRA_COMMAND, path=search_path())


def read_work_item(
    key: str,
    command: Optional[str] = None,
    run: Runner = run_cmd,
) -> LinkedIssue:
    """Return one Jira work item, read with the operator's `acli` sign-in.

    The key is checked against Jira's own shape before it becomes an argument, so
    nothing a pull request says can reach `acli` as an option. The CLI runs in the
    scrubbed environment every agent CLI gets, which holds no token of this tool's.
    """

    project, _, number = key.partition("-")
    if not JIRA_PROJECT_KEY.fullmatch(project) or not number.isdigit():
        raise ValueError(f"{key!r} is not a Jira work item key")
    executable = command or jira_command()
    if executable is None:
        raise CommandError(
            "acli is not installed; install it with: brew install atlassian/acli/acli",
            command=JIRA_COMMAND,
        )
    environment = scrubbed_environment()
    environment["PATH"] = search_path()
    try:
        output = run(
            [
                executable,
                "jira",
                "workitem",
                "view",
                key,
                "--json",
                "--fields",
                JIRA_FIELDS,
            ],
            timeout_seconds=JIRA_TIMEOUT_SECONDS,
            environment=environment,
            inherit_environment=False,
        )
    except CommandError as error:
        detail = " ".join(str(error).split())
        if len(detail) > MAX_FAILURE_CHARACTERS:
            detail = detail[: MAX_FAILURE_CHARACTERS - 1] + "…"
        raise CommandError(detail, command=JIRA_COMMAND) from error
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError(f"acli returned invalid JSON for {key}: {error}") from error
    return parse_work_item(payload, key)
