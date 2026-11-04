import json
import tempfile
from pathlib import Path
from typing import Any, Optional

from .commands import CommandError, run_cmd
from .config import Settings
from .engine import EngineError, build_engine_environment, build_prompt, build_schema
from .models import PullRequest, Repository, ReviewMaterial


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

        schema = build_schema(self.settings.max_findings, material)
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
            raise EngineError(str(error)) from error
        finally:
            schema_path.unlink(missing_ok=True)

        try:
            payload = json.loads(output)
        except json.JSONDecodeError as error:
            raise EngineError(f"Codex returned invalid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise EngineError("Codex returned an unexpected response")
        return payload
