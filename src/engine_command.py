import json
from typing import Any, Optional

from .commands import CommandError, run_cmd
from .config import Settings
from .engine import EngineError, build_engine_environment, build_prompt, build_schema
from .models import PullRequest, Repository, ReviewMaterial


class CommandEngine:
    """Run a provider wrapper that implements the common command contract."""

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
        contract_environment = {
            "AI_CODE_REVIEW_SCHEMA": json.dumps(schema, separators=(",", ":")),
            "AI_CODE_REVIEW_MAX_FINDINGS": str(self.settings.max_findings),
            "AI_CODE_REVIEW_MODEL": self.settings.review_model or "",
        }
        environment = build_engine_environment(self.settings, contract_environment)

        try:
            output = run_cmd(
                self.settings.review_engine_command,
                cwd=repository.path,
                input_text=prompt,
                timeout_seconds=timeout,
                environment=environment,
                inherit_environment=False,
            )
        except CommandError as error:
            raise EngineError(str(error)) from error

        try:
            payload = json.loads(output)
        except json.JSONDecodeError as error:
            raise EngineError(f"Command review engine returned invalid JSON: {error}") from error
        if not isinstance(payload, dict):
            raise EngineError("Command review engine must return a JSON object")
        return payload
