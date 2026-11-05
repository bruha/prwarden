import json
import re
import threading
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
# Kiro closes every headless call with "▸ Credits: 0.45 • Time: 1s" on standard
# error. It is the only report of what a call cost, so it is read rather than dropped.
KIRO_CREDITS = re.compile(r"Credits:\s*(?P<credits>[0-9]+(?:\.[0-9]+)?)")

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
# Kiro otherwise renders the answer as Markdown before printing it, which drops the
# backticks, asterisks, and underscores of an unfenced JSON answer, so an excerpt
# copied exactly from the file arrives changed and is rejected. Code intelligence is
# off so Kiro starts no language server, since a language server loads the project's
# own configuration.
KIRO_WORKSPACE_FILES = {
    "settings/cli.json": (
        '{"chat.disableMarkdownRendering": true, '
        '"chat.enableCodeIntelligence": false}\n'
    ),
}
# What Kiro writes into the controller's directory during a call. Its `code` tool still
# writes `.kiro/settings/lsp.json` with code intelligence off, and starts nothing from
# it, but the next call in the same worktree then found configuration the controller
# did not write and refused the head. A head that ships the file is refused before any
# call, so one found after a call is Kiro's own and is removed before the next.
KIRO_GENERATED_FILES = ("settings/lsp.json",)
# What Kiro adds to the model's context from the directory it starts in: its default
# agent's resources, which custom agents inherit, checked with a file in each place.
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
    and anything else is left for the next call's refusal to name.
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


def parse_kiro_credits(text: str) -> Optional[float]:
    """Return the credits Kiro reported spending, or None when it reported none."""

    matches = list(KIRO_CREDITS.finditer(ANSI_ESCAPE.sub("", text)))
    if not matches:
        return None
    return float(matches[-1].group("credits"))


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
            output, error_output = run_cmd_streams(
                args,
                cwd=repository.path,
                timeout_seconds=timeout,
                environment=build_engine_environment(self.settings),
                inherit_environment=False,
            )
        except CommandError as error:
            # A refused call can still have spent credits, and its standard error is
            # the detail of the error.
            credits = parse_kiro_credits(str(error))
            if credits is not None:
                report_invocation_credits(credits)
            raise EngineError(str(error)) from error
        finally:
            remove_kiro_generated_files(repository.path)
        credits = parse_kiro_credits(error_output)
        if credits is not None:
            report_invocation_credits(credits)
        return parse_kiro_output(output, response_discriminator_keys(material))
