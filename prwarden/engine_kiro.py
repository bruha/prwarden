import json
import math
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .commands import CommandError, run_cmd_streams
from .config import Settings
from .engine import (
    EngineError,
    build_engine_environment,
    build_prompt,
    build_schema,
    effort_for,
    host_loaded_instructions,
    refuse_host_configuration,
    report_invocation_credits,
    response_discriminator_keys,
)
from .models import PullRequest, Repository, ReviewMaterial


ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
JSON_FENCE = re.compile(r"\A```(?:json)?\s*\n(?P<payload>.*?)\n```\s*\Z", re.DOTALL)
DEFAULT_RESPONSE_KEYS = frozenset({"verdict", "findings"})
# Kiro CLI 3.1 reports what a headless call cost only in its event stream: the
# "▸ Credits: 0.45" line earlier versions printed on standard error is gone, and with
# it every credit figure a run recorded. So every call asks for the stream, one JSON
# event per line on standard output, and reads three things from it: the answer, the
# cost, and how the run ended. A failed run reports why there too, rather than on
# standard error, which then holds only Kiro's own log lines.
KIRO_STREAM_ARGS = ("--output-format", "stream-json")
# A failure message is read in notifications and the run log, so a long one is cut.
MAX_FAILURE_DETAIL_CHARACTERS = 500

# Kiro reads workspace configuration from the directory it runs in, and a review runs
# in the reviewed head. A `.kiro/settings/mcp.json` there, or a legacy
# `.amazonq/mcp.json`, names commands Kiro starts as MCP servers before the first
# turn, under this account and whatever tools the review trusts. The controller
# therefore owns that configuration: it refuses a head that ships any of its own, and
# writes the only configuration file Kiro finds there.
KIRO_WORKSPACE_DIRECTORY = ".kiro"
LEGACY_WORKSPACE_DIRECTORY = ".amazonq"
# The subtrees a head may still ship. Kiro only reads them as text, never runs them,
# and the documentation level reviews steering files as agent documentation.
KIRO_DOCUMENT_DIRECTORIES = {
    KIRO_WORKSPACE_DIRECTORY: ("steering", "specs"),
    LEGACY_WORKSPACE_DIRECTORY: ("rules",),
}
# Kiro renders its text output as Markdown, which dropped the backticks, asterisks,
# and underscores of an unfenced JSON answer, so an excerpt copied exactly from the
# file arrived changed and was rejected. The event stream the adapter reads carries
# the model's text as written; rendering stays off so no output Kiro renders can
# change an excerpt. Code intelligence is off so Kiro starts no language server,
# since a language server loads the project's own configuration.
KIRO_WORKSPACE_FILES = {
    "settings/cli.json": (
        '{"chat.disableMarkdownRendering": true, '
        '"chat.enableCodeIntelligence": false}\n'
    ),
}
# Kiro's default agent carries an IDE assistant's system prompt of some 20,000
# characters, resent on every turn of a review that takes thirty. A review needs none
# of it, since the controller's prompt says everything the model is to do, so every
# call runs as this agent instead: no system prompt, no MCP servers, and no tool but
# reading and searching files, which is all a review is trusted with anyway. The
# head's own instruction files still reach the model; see KIRO_LOADED_INSTRUCTIONS.
#
# It is written in the format Kiro CLI 3.1 upgrades agents to. Given the older format
# alone, Kiro rewrote the file on its first call, adding a permission for skills and
# a `.bak` copy beside it, and the next call in the worktree refused the head for
# configuration the controller had not written. An agent that carries the upgrade
# marker is not rewritten, and one whose fingerprint does not match its content is
# kept as the hand-edited file it then is, so the zero fingerprint below keeps Kiro's
# hands off this one.
#
# Skills are denied outright. Without that rule Kiro still offered the agent its skill
# tool, whose description lists every skill installed for the account, which was 184
# on the machine this was measured on: about 10,000 tokens resent on every turn of a
# review that must not activate one. Denying the capability removes the tool and its
# list, and took a trivial call from about 16,000 tokens of tools to 6,000.
KIRO_REVIEW_AGENT = "prwarden-review"
KIRO_REVIEW_TOOLS = ("glob", "grep", "read")
KIRO_REVIEW_PERMISSIONS = (
    {"capability": "fs_read", "effect": "allow"},
    {"capability": "skill", "effect": "deny"},
)
KIRO_WORKSPACE_FILES[f"agents/{KIRO_REVIEW_AGENT}.json"] = (
    json.dumps(
        {
            "name": KIRO_REVIEW_AGENT,
            "description": "PR Warden's read-only pull-request review",
            "tools": list(KIRO_REVIEW_TOOLS),
            "allowedTools": list(KIRO_REVIEW_TOOLS),
            "resources": [],
            "includeMcpJson": False,
            "mcpServers": {},
            "permissions": {"rules": [dict(rule) for rule in KIRO_REVIEW_PERMISSIONS]},
            "kiroUpgrade": {
                "revision": 2,
                "fingerprint": "sha256:" + "0" * 64,
                "source": "sha256:" + "0" * 64,
            },
        },
        indent=2,
    )
    + "\n"
)
# What Kiro writes into the controller's directory during a call. Its `code` tool still
# writes `.kiro/settings/lsp.json` with code intelligence off, and starts nothing from
# it, but the next call in the same worktree then found configuration the controller
# did not write and refused the head. A head that ships the file is refused before any
# call, so one found after a call is Kiro's own and is removed before the next. So is
# the copy Kiro keeps of an agent it upgrades, in case a later Kiro upgrades this one.
KIRO_GENERATED_FILES = ("settings/lsp.json", f"agents/{KIRO_REVIEW_AGENT}.json.bak")
# What Kiro adds to the model's context from the directory it starts in: its default
# agent's resources, which custom agents inherit, checked with a file in each place.
# Kiro CLI 3.1 still loads `AGENTS.md` under the controller's agent above.
KIRO_LOADED_INSTRUCTIONS = ("AGENTS.md", "README.md", ".kiro/steering/**/*.md")
_WORKSPACE_LOCK = threading.Lock()


def prepare_kiro_workspace(root: Path) -> None:
    """Give the review workspace the controller's Kiro configuration and no other.

    Raises EngineError, before Kiro starts, when the reviewed head carries Kiro
    configuration of its own. Calling it again on a prepared workspace is a no-op.
    """

    with _WORKSPACE_LOCK:
        refuse_host_configuration(
            root,
            "Kiro",
            KIRO_DOCUMENT_DIRECTORIES,
            {KIRO_WORKSPACE_DIRECTORY: KIRO_WORKSPACE_FILES},
        )
        workspace = root / KIRO_WORKSPACE_DIRECTORY
        for relative, content in KIRO_WORKSPACE_FILES.items():
            path = workspace / relative
            if path.is_file():
                continue
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            except OSError as error:
                raise EngineError(
                    f"Could not write Kiro workspace configuration {path}: {error}"
                ) from error


def remove_kiro_generated_files(root: Path) -> None:
    """Remove what Kiro wrote into the controller's directory during a call.

    Only the named files, only as files or links: a link is removed, never followed,
    and anything else is left for the next call's refusal to name. A controller file
    Kiro rewrote during the call, as it rewrites an agent it upgrades, is written back,
    since the call that began found it exactly as the controller wrote it and only
    Kiro could have changed it; a link in its place is left to be refused. The `agents`
    directory Kiro CLI 3.1 creates on every call needs no removal: it holds the
    controller's own agent, so the next call finds only what the controller wrote.
    """

    workspace = root / KIRO_WORKSPACE_DIRECTORY
    with _WORKSPACE_LOCK:
        for relative in KIRO_GENERATED_FILES:
            path = workspace / relative
            if path.is_symlink() or path.is_file():
                try:
                    path.unlink()
                except OSError as error:
                    raise EngineError(
                        f"Could not remove {path}, which Kiro wrote during the "
                        f"review: {error}"
                    ) from error
        for relative, content in KIRO_WORKSPACE_FILES.items():
            path = workspace / relative
            if path.is_symlink() or not path.is_file():
                continue
            try:
                if path.read_bytes() != content.encode("utf-8"):
                    path.write_text(content, encoding="utf-8")
            except OSError as error:
                raise EngineError(
                    f"Could not restore {path}, which Kiro rewrote during the "
                    f"review: {error}"
                ) from error


@dataclass(frozen=True)
class KiroRun:
    """What one `kiro-cli chat --output-format stream-json` call reported."""

    # The model's last message: the text after its last tool call, as Kiro's own
    # `finalText` is. Narration before a tool call is not the answer.
    answer: str
    # None when Kiro reported no cost, which says nothing rather than "free".
    credits: Optional[float]
    # Kiro said the run finished successfully. Anything else is an engine failure,
    # never an empty review.
    finished: bool
    status: Optional[str]
    errors: "tuple[str, ...]"


def _kiro_events(output: str) -> "list[dict[str, Any]]":
    """Return the JSON events Kiro printed, one per line, skipping anything else."""

    events = []
    for line in output.splitlines():
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


def _mapping(value: Any, key: str) -> "dict[str, Any]":
    inner = value.get(key) if isinstance(value, dict) else None
    return inner if isinstance(inner, dict) else {}


def _reported_credits(entries: Any) -> Optional[float]:
    """Return the total of the credit entries Kiro's usage report lists."""

    if not isinstance(entries, list):
        return None
    total: Optional[float] = None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("unit") != "credit":
            continue
        usage = entry.get("usage")
        if isinstance(usage, bool) or not isinstance(usage, (int, float)):
            continue
        if not math.isfinite(usage) or usage < 0:
            continue
        total = (total or 0.0) + float(usage)
    return total


def _add_message(messages: "list[str]", message: Any) -> None:
    if isinstance(message, str):
        message = " ".join(message.split())
        if message and message not in messages:
            messages.append(message)


def read_kiro_run(output: str) -> KiroRun:
    """Read the answer, the cost, and the outcome from Kiro's event stream.

    The cost is the session's usage report, the last one Kiro sent. The answer is
    the text of the agent's message chunks since its last tool call, and Kiro's
    `finalText` only when it streamed none, since that copy can be truncated.
    """

    chunks: "list[str]" = []
    final_text = ""
    credits: Optional[float] = None
    status: Optional[str] = None
    run_errors: "list[str]" = []
    displayed: "list[str]" = []
    for event in _kiro_events(output):
        kind = event.get("type")
        data = _mapping(event, "data")
        if kind == "runFinished":
            status = data.get("status") if isinstance(data.get("status"), str) else ""
            if isinstance(data.get("finalText"), str):
                final_text = data["finalText"]
            continue
        if kind == "runError":
            _add_message(run_errors, data.get("message"))
            continue
        if kind != "sessionUpdate":
            continue
        update = _mapping(data, "update")
        update_kind = update.get("sessionUpdate")
        if update_kind == "agent_message_chunk":
            content = _mapping(update, "content")
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                chunks.append(content["text"])
        elif update_kind in ("tool_call", "tool_call_update"):
            chunks = []
        elif update_kind == "session_info_update":
            meta = _mapping(_mapping(update, "_meta"), "kiro")
            if meta.get("kind") == "session_usage":
                credits = _reported_credits(meta.get("credits"))
            elif meta.get("kind") == "display_error":
                _add_message(displayed, meta.get("message"))
    answer = "".join(chunks)
    if not answer.strip():
        answer = final_text
    return KiroRun(
        answer=answer,
        credits=credits,
        finished=status == "success" and not run_errors,
        status=status,
        errors=tuple(run_errors or displayed),
    )


def _capped(detail: str) -> str:
    if len(detail) > MAX_FAILURE_DETAIL_CHARACTERS:
        return detail[: MAX_FAILURE_DETAIL_CHARACTERS - 1].rstrip() + "…"
    return detail


def _failure_detail(error: CommandError, run: KiroRun) -> str:
    """Explain a failed Kiro call in the words of the run's own error report."""

    if error.returncode is None or not run.errors:
        return str(error)
    command = error.command or "kiro-cli"
    detail = _capped(" ".join(run.errors))
    return f"{command} exited with {error.returncode}: {detail}"


def _unfinished_detail(run: KiroRun) -> str:
    """Explain a call that exited zero without a successfully finished run."""

    if run.errors:
        return f"Kiro reported an error: {_capped(' '.join(run.errors))}"
    if run.status is None:
        return "Kiro ended without reporting a finished run"
    return f"Kiro ended the run with status {run.status or 'unknown'!r}"


def parse_kiro_output(
    output: str,
    required_keys: "Optional[frozenset[str]]" = None,
) -> dict[str, Any]:
    expected = DEFAULT_RESPONSE_KEYS if required_keys is None else required_keys
    candidate = ANSI_ESCAPE.sub("", output).strip()
    if candidate.startswith("> "):
        candidate = candidate[2:].lstrip()
    if candidate.startswith("json\n"):
        candidate = candidate[5:].lstrip()
    fenced = JSON_FENCE.fullmatch(candidate)
    if fenced:
        candidate = fenced.group("payload")
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as direct_error:
        decoder = json.JSONDecoder()
        matches: list[dict[str, Any]] = []
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                possible, _ = decoder.raw_decode(candidate, index)
            except json.JSONDecodeError:
                continue
            if isinstance(possible, dict) and expected.issubset(possible):
                matches.append(possible)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise EngineError(
                "Kiro returned multiple findings JSON objects; response is ambiguous"
            )
        raise EngineError(f"Kiro returned invalid JSON: {direct_error}") from direct_error
    if not isinstance(payload, dict):
        raise EngineError("Kiro must return one JSON object")
    return payload


class KiroEngine:
    """Translate the common review contract to Kiro CLI headless arguments."""

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

        prompt = build_prompt(
            repository,
            pull_request,
            material,
            self.settings.max_findings,
        )
        if repository.path is not None:
            prompt += host_loaded_instructions(
                repository.path, "Kiro", KIRO_LOADED_INSTRUCTIONS
            )
        prompt += "\nReturn exactly one JSON object matching this schema:\n"
        prompt += json.dumps(
            build_schema(self.settings.max_findings, material), separators=(",", ":")
        )
        args = [
            *self.settings.review_engine_command,
            "chat",
            "--no-interactive",
            *KIRO_STREAM_ARGS,
            "--agent",
            KIRO_REVIEW_AGENT,
            "--trust-tools=read,grep,glob",
            "--wrap",
            "never",
        ]
        if self.settings.review_model:
            args.extend(["--model", self.settings.review_model])
        effort = effort_for(self.settings, material)
        if effort:
            args.extend(["--effort", effort])
        args.append(prompt)

        prepare_kiro_workspace(repository.path)
        try:
            output, _ = run_cmd_streams(
                args,
                cwd=repository.path,
                timeout_seconds=timeout,
                environment=build_engine_environment(self.settings),
                inherit_environment=False,
            )
        except CommandError as error:
            # A refused call can still have spent credits, and the stream it printed
            # says both what it cost and why it failed.
            run = read_kiro_run(error.output or "")
            if run.credits is not None:
                report_invocation_credits(run.credits)
            raise EngineError(_failure_detail(error, run)) from error
        finally:
            remove_kiro_generated_files(repository.path)
        run = read_kiro_run(output)
        if run.credits is not None:
            report_invocation_credits(run.credits)
        if not run.finished:
            raise EngineError(_unfinished_detail(run))
        return parse_kiro_output(run.answer, response_discriminator_keys(material))
