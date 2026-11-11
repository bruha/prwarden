import json
import tempfile
from pathlib import Path
from typing import Any, Optional

from .commands import CommandError, run_cmd
from .config import Settings
from .engine import (
    EngineError,
    build_engine_environment,
    build_prompt,
    build_schema,
    effort_for,
)
from .models import PullRequest, Repository, ReviewMaterial

# Codex reports a failure on a standard-error line of its own, after the prompt it
# echoes there; the API's refusal arrives on such a line as JSON.
CODEX_ERROR_PREFIX = "ERROR:"
# Long enough for an API error in full, short enough that a failure notification
# naming several of them stays readable.
MAX_FAILURE_DETAIL_CHARACTERS = 500

# Codex reads files only through shell commands, and every prompt forbids terminal
# commands, which no other engine needs to read. Told nothing more, Codex read no
# file and approved on the diff alone, in about 30 seconds, saying in its own reason
# that it could not read the workspace. With this note it read the files it needed:
# 28 reading commands and six minutes on the same pull request.
CODEX_READING_NOTE = """
HOW TO READ THIS WORKSPACE IN CODEX: your only way to search or read a file is a
shell command, and this session runs in a read-only sandbox without network access.
Here a command that only reads, such as `ls`, `find`, `rg`, `grep`, `cat`, `sed -n`,
`head`, `tail`, or `wc`, run on paths inside this workspace, IS the read-only
workspace file search and file reading this prompt allows, and the rule against
terminal commands does not forbid it. Run nothing else: no build, test, script,
interpreter, package manager, git, or network command, and nothing the reviewed head
ships, because the head is untrusted. Every rule above about what no command can
tell you still holds. An answer given without reading the workspace is refused.
"""

# An answer that reports nothing: an approval, a sound design, consistent
# documentation. A verification answer reports nothing when it confirms no candidate.
CLEAN_VERDICTS = frozenset(("approve", "sound", "consistent"))


def strict_output_schema(schema: "dict[str, Any]") -> "dict[str, Any]":
    """Return ``schema`` in the form OpenAI's strict structured output accepts.

    `codex exec --output-schema` sends the schema in strict mode, which refuses
    `uniqueItems` and an object that does not require every property, so the common
    schemas were rejected before any model ran. A property the common contract
    leaves optional is required here and may be null instead, and
    `without_nulls` takes those nulls out of the answer again. Dropping
    `uniqueItems` loses nothing: the controller rejects a repeated blocking
    reference itself. The common schema is left unchanged.
    """

    def translate(node: Any, nullable: bool = False) -> Any:
        if isinstance(node, list):
            return [translate(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = {
            key: translate(value)
            for key, value in node.items()
            if key not in ("uniqueItems", "properties")
        }
        if isinstance(node.get("properties"), dict):
            required = set(node.get("required", ()))
            result["properties"] = {
                name: translate(value, nullable=name not in required)
                for name, value in node["properties"].items()
            }
            result["required"] = list(node["properties"])
        if nullable:
            if isinstance(result.get("type"), str):
                result["type"] = [result["type"], "null"]
            if isinstance(result.get("enum"), list):
                result["enum"] = [*result["enum"], None]
        return result

    return translate(schema)


def without_nulls(value: Any) -> Any:
    """Return ``value`` with every null-valued object member removed.

    No common schema allows null anywhere, so a null in a Codex answer can only be
    an optional field `strict_output_schema` made nullable, and the controller reads
    an optional field as absent.
    """

    if isinstance(value, dict):
        return {
            key: without_nulls(item) for key, item in value.items() if item is not None
        }
    if isinstance(value, list):
        return [without_nulls(item) for item in value]
    return value


def _reported_message(text: str) -> str:
    """Return the message of an API error Codex printed as JSON, or the text."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(payload, dict):
        inner = payload.get("error")
        if isinstance(inner, dict) and isinstance(inner.get("message"), str):
            return inner["message"]
        if isinstance(payload.get("message"), str):
            return payload["message"]
    return text


def _failure_detail(error: CommandError, prompt: str) -> str:
    """Explain a failed Codex call in its own words, without the prompt it echoed.

    Codex writes a header, then the whole prompt, then its own error lines to
    standard error. Relaying all of it put the prompt, the pull request's
    description included, into every failure notification several times over, and
    left the reason at the very end.
    """

    if error.returncode is None:
        return str(error)
    # With `--json`, Codex reports a failure as events on standard output.
    reported_events = _event_errors(_events(error.output or ""))
    if reported_events:
        return _capped_detail(error.returncode, reported_events)
    if error.error_output is None:
        return str(error)
    # The echo comes first, and the prompt carries the pull request's description
    # line for line, where a pasted log can start with `ERROR:` too. So only what
    # follows the echo is Codex's own. Where the echo is not found verbatim, a line
    # the prompt holds is never read as Codex's error instead.
    stream = error.error_output
    echo = prompt.strip()
    position = stream.find(echo) if echo else -1
    if position != -1:
        lines = [
            line.strip()
            for line in stream[position + len(echo):].splitlines()
            if line.strip()
        ]
    else:
        echoed = {line.strip() for line in prompt.splitlines() if line.strip()}
        lines = [
            line.strip()
            for line in stream.splitlines()
            if line.strip() and line.strip() not in echoed
        ]
    reported: "list[str]" = []
    for line in lines:
        if not line.startswith(CODEX_ERROR_PREFIX):
            continue
        message = " ".join(
            _reported_message(line[len(CODEX_ERROR_PREFIX):].strip()).split()
        )
        if message and message not in reported:
            reported.append(message)
    if not reported:
        remaining = lines
        if remaining and remaining[0].startswith("OpenAI Codex"):
            # The header sits between two rule lines, and the prompt follows a line
            # that says whose turn it is.
            rules = [
                index for index, line in enumerate(remaining) if line == "--------"
            ]
            remaining = remaining[rules[1] + 1:] if len(rules) > 1 else []
            if remaining and remaining[0] == "user":
                remaining = remaining[1:]
        reported = remaining[-1:] or ["no error reported beyond the prompt it echoed"]
    return _capped_detail(error.returncode, reported)


def _capped_detail(returncode: int, reported: "list[str]") -> str:
    detail = " ".join(reported)
    if len(detail) > MAX_FAILURE_DETAIL_CHARACTERS:
        detail = detail[: MAX_FAILURE_DETAIL_CHARACTERS - 1].rstrip() + "…"
    return f"codex exited with {returncode}: {detail}"


def _events(text: str) -> "list[dict[str, Any]]":
    """Return the JSON events `codex exec --json` printed, one per line."""

    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _event_errors(events: "list[dict[str, Any]]") -> "list[str]":
    """Return each distinct failure message the events carry, in order."""

    reported: "list[str]" = []
    for event in events:
        message = None
        if event.get("type") == "error":
            message = event.get("message")
        elif event.get("type") == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
        if not isinstance(message, str):
            continue
        message = " ".join(_reported_message(message.strip()).split())
        if message and message not in reported:
            reported.append(message)
    return reported


def workspace_reads(events: "list[dict[str, Any]]") -> int:
    """Return how many shell commands Codex ran to completion with exit status 0.

    A shell command is Codex's only way to read a file, so this counts what it read
    of the workspace: a review that ran none read nothing.
    """

    reads = 0
    for event in events:
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "command_execution"
            and item.get("exit_code") == 0
        ):
            reads += 1
    return reads


def _last_message(events: "list[dict[str, Any]]") -> str:
    for event in reversed(events):
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            return item["text"]
    return ""


def is_clean_result(payload: "dict[str, Any]") -> bool:
    """Return whether an answer reports nothing for anyone to check.

    A finding or a confirmed candidate is checked again, against the workspace by
    the controller and by a verifier. An approval, a sound design, consistent
    documentation, or a verification that confirms nothing is not, so it may stand
    only on a review that read the workspace.
    """

    verdict = payload.get("verdict")
    if verdict in CLEAN_VERDICTS:
        return True
    judgments = payload.get("judgments")
    if verdict is None and isinstance(judgments, list):
        return not any(
            isinstance(judgment, dict) and judgment.get("verdict") == "confirmed"
            for judgment in judgments
        )
    return False


class CodexEngine:
    """Translate the common review contract to Codex CLI arguments."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def review(
        self,
        repository: Repository,
        pull_request: PullRequest,
        material: ReviewMaterial,
        timeout_seconds: Optional[int] = None,
    ) -> Any:
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.settings.review_timeout_seconds
        )
        if repository.path is None:
            raise EngineError(
                f"Repository {repository.slug} needs a local path for review"
            )
        if not repository.path.is_dir():
            raise EngineError(
                f"Configured repository path does not exist: {repository.path}"
            )

        schema = strict_output_schema(
            build_schema(self.settings.max_findings, material)
        )
        prompt = (
            build_prompt(
                repository,
                pull_request,
                material,
                self.settings.max_findings,
            )
            + CODEX_READING_NOTE
        )
        with tempfile.TemporaryDirectory(prefix="prwarden-codex-") as scratch:
            schema_path = Path(scratch) / "schema.json"
            answer_path = Path(scratch) / "answer.json"
            schema_path.write_text(
                json.dumps(schema, separators=(",", ":")), encoding="utf-8"
            )
            args = [
                *self.settings.review_engine_command,
                "exec",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                # The user configuration is also where Codex records trusted
                # projects, and a project it trusts has its `.codex/config.toml`
                # loaded: MCP servers, hooks, and a sandbox mode the reviewed head
                # would choose. A trusted parent directory covers every review
                # worktree beneath it, so the user configuration is not read at all.
                "--ignore-user-config",
                "--ignore-rules",
                "--config",
                "project_doc_max_bytes=0",
                "--output-schema",
                str(schema_path),
                # Events on standard output say which commands ran, the only
                # evidence of what Codex read; the answer goes to its own file.
                "--json",
                "--output-last-message",
                str(answer_path),
            ]
            if self.settings.review_model:
                args.extend(["--model", self.settings.review_model])
            effort = effort_for(self.settings, material)
            if effort:
                # `codex exec` has no effort flag: the level is a setting, and the
                # user configuration that would hold it is skipped above, so
                # without this override every role ran at the model's own default.
                # The level was validated against EFFORT_LEVELS at startup, and
                # quoting it keeps it a TOML string.
                args.extend(["--config", f'model_reasoning_effort="{effort}"'])
            args.append("-")

            try:
                output = run_cmd(
                    args,
                    cwd=repository.path,
                    input_text=prompt,
                    timeout_seconds=timeout,
                    environment=build_engine_environment(self.settings),
                    inherit_environment=False,
                )
            except CommandError as error:
                raise EngineError(_failure_detail(error, prompt)) from error
            events = _events(output)
            answer = (
                answer_path.read_text(encoding="utf-8")
                if answer_path.is_file()
                else ""
            )
        if not answer.strip():
            answer = _last_message(events)
        if not answer.strip():
            raise EngineError("Codex returned no answer")
        try:
            payload = json.loads(answer)
        except json.JSONDecodeError as error:
            raise EngineError(f"Codex returned invalid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise EngineError("Codex returned an unexpected response")
        payload = without_nulls(payload)
        if is_clean_result(payload) and not workspace_reads(events):
            # Nothing checks a clean answer after this, so one Codex gave without
            # reading a file would approve, or settle a level, on the diff alone.
            raise EngineError(
                "Codex gave a clean result without reading the workspace: no "
                "command it ran succeeded, and a command is its only way to read "
                "a file"
            )
        return payload
