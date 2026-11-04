import json
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


def _failure_detail(error: CommandError) -> str:
    """Explain a failed Claude invocation using its own reported reason.

    Claude Code reports operational failures such as an expired login or a usage
    limit in the JSON result envelope on stdout, then exits non-zero with an empty
    stderr. That reason replaces the exit-status text, which adds only the
    executable path and a redundant "no error output" for an engine the caller
    already names.
    """

    if not error.output:
        return str(error)
    try:
        envelope = json.loads(error.output.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return str(error)
    if not isinstance(envelope, dict):
        return str(error)
    reported = envelope.get("result")
    if not isinstance(reported, str) or not reported.strip():
        return str(error)
    return " ".join(reported.split())[:200]


class ClaudeEngine:
    """Translate the common review contract to Claude Code CLI arguments."""

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

        schema = build_schema(self.settings.max_findings, material)
        prompt = build_prompt(
            repository,
            pull_request,
            material,
            self.settings.max_findings,
        )
        args = [
            *self.settings.review_engine_command,
            "--print",
            "--safe-mode",
            # Safe mode disables hooks, MCP servers, and CLAUDE.md, but project and
            # local settings still load: a reviewed head's `.claude/settings.json`
            # ran its `apiKeyHelper` command, and its `env` sent the API requests to
            # a host the head chose. Only the operator's own settings are read, and
            # no MCP configuration at all.
            "--setting-sources",
            "user",
            "--strict-mcp-config",
            "--permission-mode",
            "plan",
            "--tools",
            "Read,Grep,Glob",
            "--disallowedTools",
            "mcp__*",
            "--disable-slash-commands",
            "--no-chrome",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
        ]
        if self.settings.review_model:
            args.extend(["--model", self.settings.review_model])
        effort = effort_for(self.settings, material)
        if effort:
            args.extend(["--effort", effort])

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
            raise EngineError(_failure_detail(error)) from error

        try:
            envelope = json.loads(output)
        except json.JSONDecodeError as error:
            raise EngineError(f"Claude returned invalid JSON: {error}") from error
        if not isinstance(envelope, dict):
            raise EngineError("Claude returned an unexpected response")
        if envelope.get("is_error"):
            detail = envelope.get("result") or "unknown API error"
            raise EngineError(f"Claude review failed: {detail}")
        if envelope.get("subtype") != "success":
            raise EngineError(
                f"Claude review failed with subtype {envelope.get('subtype')!r}"
            )
        if "structured_output" not in envelope:
            raise EngineError("Claude response is missing structured_output")
        return envelope["structured_output"]
