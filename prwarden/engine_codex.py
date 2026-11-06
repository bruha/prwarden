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

    if error.returncode is None or error.error_output is None:
        return str(error)
    reported: "list[str]" = []
    for line in error.error_output.splitlines():
        if not line.startswith(CODEX_ERROR_PREFIX):
            continue
        message = " ".join(
            _reported_message(line[len(CODEX_ERROR_PREFIX):].strip()).split()
        )
        if message and message not in reported:
            reported.append(message)
    if not reported:
        echoed = {line.strip() for line in prompt.splitlines() if line.strip()}
        remaining = [
            line.strip()
            for line in error.error_output.splitlines()
            if line.strip() and line.strip() not in echoed
        ]
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
    detail = " ".join(reported)
    if len(detail) > MAX_FAILURE_DETAIL_CHARACTERS:
        detail = detail[: MAX_FAILURE_DETAIL_CHARACTERS - 1].rstrip() + "…"
    return f"codex exited with {error.returncode}: {detail}"


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
        prompt = build_prompt(
            repository,
            pull_request,
            material,
            self.settings.max_findings,
        )
        schema_path: Path
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        ) as schema_file:
            json.dump(schema, schema_file, separators=(",", ":"))
            schema_path = Path(schema_file.name)

        args = [
            *self.settings.review_engine_command,
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            # The user configuration is also where Codex records trusted projects,
            # and a project it trusts has its `.codex/config.toml` loaded: MCP
            # servers, hooks, and a sandbox mode the reviewed head would choose. A
            # trusted parent directory covers every review worktree beneath it, so
            # the user configuration is not read at all.
            "--ignore-user-config",
            "--ignore-rules",
            "--config",
            "project_doc_max_bytes=0",
            "--output-schema",
            str(schema_path),
        ]
        if self.settings.review_model:
            args.extend(["--model", self.settings.review_model])
        effort = effort_for(self.settings, material)
        if effort:
            # `codex exec` has no effort flag: the level is a setting, and the user
            # configuration that would hold it is skipped above, so without this
            # override every role ran at the model's own default. The level was
            # validated against EFFORT_LEVELS at startup, and quoting it keeps it a
            # TOML string.
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
        finally:
            schema_path.unlink(missing_ok=True)

        try:
            payload = json.loads(output)
        except json.JSONDecodeError as error:
            raise EngineError(f"Codex returned invalid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise EngineError("Codex returned an unexpected response")
        return without_nulls(payload)
