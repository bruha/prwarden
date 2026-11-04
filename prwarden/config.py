import json
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .comment_template import (
    DEFAULT_GITHUB_COMMENT_TAGS_FILE,
    DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE,
    CommentTemplate,
    CommentTemplateError,
    load_comment_template,
)
from .commands import CommandError, run_cmd
from .models import Repository
from .paths import LocationError, Locations, resolve_locations


MAX_REVIEW_BUNDLE_LIMIT = 8
MAX_REVIEW_CONCURRENCY = 8
MAX_RUN_LOG_RETENTION_DAYS = 365
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_ENGINE_COMMANDS = {
    "antigravity": ("agy",),
    "codex": ("codex",),
    "claude": ("claude",),
    "kiro": ("kiro-cli",),
}
SUPPORTED_REVIEW_ENGINES = frozenset((*DEFAULT_ENGINE_COMMANDS, "command"))
SUPPORTED_VERIFY_ENGINES = frozenset(DEFAULT_ENGINE_COMMANDS)
SUPPORTED_FALLBACK_ENGINES = frozenset(DEFAULT_ENGINE_COMMANDS)
# Reasoning effort as both effort-aware CLIs spell it. Kiro accepts any string and
# exits 0, so a misspelt level would quietly run at the model's default; checking it
# here is the only place that mistake can be caught.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# The adapters that pass effort to their CLI. Effort named for any other engine
# would be dropped without a word, so it is refused instead.
EFFORT_ENGINES = frozenset(("antigravity", "claude", "kiro"))
GITHUB_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
GITHUB_USERNAME_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gh api user", ("gh", "api", "user", "--jq", ".login")),
    (
        "gh api graphql viewer",
        (
            "gh",
            "api",
            "graphql",
            "-f",
            "query={viewer{login}}",
            "--jq",
            ".data.viewer.login",
        ),
    ),
)


class ConfigError(ValueError):
    """Raised when local configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    reviewer_username: str
    repositories: list[Repository]
    dry_run: bool
    slack_webhook_url: Optional[str]
    state_file: Path
    review_engine: str
    review_engine_command: tuple[str, ...]
    review_engine_env_allowlist: tuple[str, ...]
    review_model: Optional[str]
    review_timeout_seconds: int
    max_diff_bytes: int
    max_findings: int
    # One bundle is the single review this tool has always done, so a caller that
    # says nothing gets exactly that.
    max_review_bundles: int = 1
    design_level: bool = True
    max_design_findings: int = 4
    docs_level: bool = True
    max_docs_findings: int = 3
    review_verify_timeout_seconds: int = 900
    review_verify_timeout_per_candidate_seconds: int = 180
    review_concurrency: int = 2
    run_log_retention_days: int = 7
    verify_engine: Optional[str] = None
    verify_engine_command: tuple[str, ...] = ()
    verify_model: Optional[str] = None
    fallback_engine: Optional[str] = None
    fallback_engine_command: tuple[str, ...] = ()
    fallback_model: Optional[str] = None
    # The verifier's own fallback, used only when the verification engine fails at
    # the engine level. Without it a verifier on the reviewer's provider meant that
    # while that provider was down, nothing the review fallback found was delivered.
    verify_fallback_engine: Optional[str] = None
    verify_fallback_engine_command: tuple[str, ...] = ()
    verify_fallback_model: Optional[str] = None
    # Reasoning effort per role, or None for the model's own default. The code level
    # may run harder than the advisory levels the same reviewer also does, because
    # it alone can approve, so it takes an override of its own.
    review_effort: Optional[str] = None
    review_code_effort: Optional[str] = None
    verify_effort: Optional[str] = None
    fallback_effort: Optional[str] = None
    verify_fallback_effort: Optional[str] = None
    github_comment_template: CommentTemplate = field(
        default_factory=lambda: load_comment_template(
            DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE,
            DEFAULT_GITHUB_COMMENT_TAGS_FILE,
        )
    )
    excluded_pr_labels: frozenset[str] = frozenset()
    ignored_missing_patch_patterns: tuple[str, ...] = ()
    design_excluded_head_branches: tuple[str, ...] = (
        "develop",
        "release/*",
    )


def load_env_file(path: Path) -> "dict[str, str]":
    """Load a dependency-free subset of dotenv syntax, returning what it declared.

    Existing process environment values take precedence. Blank lines and
    full-line comments are ignored. Values may be wrapped in matching quotes.

    The returned mapping is what the file itself said, whether or not the process
    environment overrode it, because a setting's meaning can depend on which of the
    two it came from: an engine command written beside one engine does not belong to
    a different engine chosen later.
    """

    if not path.exists():
        return {}

    if os.name != "nt":
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as error:
            raise ConfigError(f"Cannot inspect private configuration {path}: {error}") from error
        if mode != 0o600:
            raise ConfigError(
                f"{path} contains private configuration and must have mode 0600; "
                f"current mode is {mode:04o}. Run: chmod 600 {path}"
            )

    declared: "dict[str, str]" = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()

        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not ENV_KEY_PATTERN.fullmatch(key):
            raise ConfigError(f"Invalid entry in {path} at line {line_number}")

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        declared[key] = value
        os.environ.setdefault(key, value)
    return declared


def _engine_switched_from(declared: "dict[str, str]", review_engine: str) -> bool:
    """Return whether the running engine is not the one the file was written for."""

    file_engine = declared.get("REVIEW_ENGINE", "").strip().lower()
    return bool(file_engine) and file_engine != review_engine


def parse_bool(value: str, variable_name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{variable_name} must be true/false, yes/no, on/off, or 1/0")


def parse_integer(
    value: str,
    variable_name: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ConfigError(f"{variable_name} must be an integer") from error
    if parsed < minimum or parsed > maximum:
        raise ConfigError(
            f"{variable_name} must be between {minimum} and {maximum}"
        )
    return parsed


def resolve_path(value: str, base_directory: Path) -> Path:
    expanded = Path(os.path.expandvars(value)).expanduser()
    if not expanded.is_absolute():
        expanded = base_directory / expanded
    return expanded.resolve()


def load_repositories(path: Path) -> list[Repository]:
    if not path.exists():
        raise ConfigError(
            f"Repository configuration not found: {path}. Copy "
            "repositories.example.json from the PR Warden repository there and "
            "edit it."
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"Invalid JSON in {path}: {error}") from error

    if not isinstance(payload, list) or not payload:
        raise ConfigError(f"{path} must contain a non-empty JSON array")

    repositories: list[Repository] = []
    seen_slugs: set[str] = set()
    for index, item in enumerate(payload):
        label = f"{path} entry {index + 1}"
        if not isinstance(item, dict):
            raise ConfigError(f"{label} must be an object")

        slug = item.get("slug")
        if not isinstance(slug, str) or slug.count("/") != 1 or not all(slug.split("/")):
            raise ConfigError(f"{label}.slug must use the owner/repository format")
        if slug in seen_slugs:
            raise ConfigError(f"Duplicate repository slug in {path}: {slug}")
        seen_slugs.add(slug)

        name = item.get("name", slug.rsplit("/", 1)[1])
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{label}.name must be a non-empty string")

        configured_path = item.get("path")
        if configured_path is not None and not isinstance(configured_path, str):
            raise ConfigError(f"{label}.path must be a string when provided")

        slack = item.get("slack", True)
        if not isinstance(slack, bool):
            raise ConfigError(f"{label}.slack must be true or false when provided")

        github_account = item.get("github_account")
        if github_account is not None:
            if not isinstance(github_account, str):
                raise ConfigError(
                    f"{label}.github_account must be a GitHub login when provided"
                )
            try:
                github_account = _normalize_github_username(github_account)
            except ValueError as error:
                raise ConfigError(
                    f"{label}.github_account is invalid: {error}"
                ) from error

        repositories.append(
            Repository(
                name=name.strip(),
                slug=slug,
                path=resolve_path(configured_path, path.parent) if configured_path else None,
                slack=slack,
                github_account=github_account,
            )
        )
    return repositories


def parse_command(value: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ConfigError("REVIEW_ENGINE_COMMAND must be a JSON array of strings") from error
    if (
        not isinstance(payload, list)
        or not payload
        or any(not isinstance(item, str) or not item.strip() for item in payload)
    ):
        raise ConfigError("REVIEW_ENGINE_COMMAND must be a non-empty JSON array of strings")
    command = [item.strip() for item in payload]
    executable = command[0]
    if os.sep in executable:
        command[0] = str(Path(executable).expanduser().resolve())
    else:
        resolved = shutil.which(executable)
        user_install = Path.home() / ".local" / "bin" / executable
        if resolved:
            command[0] = resolved
        elif user_install.exists():
            command[0] = str(user_install)
    return tuple(command)


def parse_env_allowlist(value: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ConfigError("REVIEW_ENGINE_ENV_ALLOWLIST must be a JSON array of names") from error
    if not isinstance(payload, list) or any(
        not isinstance(item, str) or not ENV_KEY_PATTERN.fullmatch(item)
        for item in payload
    ):
        raise ConfigError("REVIEW_ENGINE_ENV_ALLOWLIST must be a JSON array of environment variable names")
    return tuple(payload)


def parse_excluded_pr_labels(value: str) -> frozenset[str]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ConfigError(
            "EXCLUDED_PR_LABELS must be a JSON array of non-empty strings"
        ) from error
    if not isinstance(payload, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in payload
    ):
        raise ConfigError(
            "EXCLUDED_PR_LABELS must be a JSON array of non-empty strings"
        )
    return frozenset(item.strip().casefold() for item in payload)


def parse_ignored_missing_patch_patterns(value: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ConfigError(
            "IGNORED_MISSING_PATCH_PATTERNS must be a JSON array of non-empty strings"
        ) from error
    if not isinstance(payload, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in payload
    ):
        raise ConfigError(
            "IGNORED_MISSING_PATCH_PATTERNS must be a JSON array of non-empty strings"
        )
    return tuple(item.strip() for item in payload)


DEFAULT_DESIGN_EXCLUDED_HEAD_BRANCHES = '["develop", "release/*"]'


def parse_design_excluded_head_branches(value: str) -> tuple[str, ...]:
    """Return the head-branch globs whose pull requests skip the design level.

    A promotion of one long-lived branch into another carries no new design: its
    content was already designed, and reviewed, on the feature pull requests that
    reached the promoted branch. Reviewing it again spends a full design pass to
    restate what was said there, so these are named by head branch and skipped.
    """

    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ConfigError(
            "DESIGN_EXCLUDED_HEAD_BRANCHES must be a JSON array of non-empty strings"
        ) from error
    if not isinstance(payload, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in payload
    ):
        raise ConfigError(
            "DESIGN_EXCLUDED_HEAD_BRANCHES must be a JSON array of non-empty strings"
        )
    return tuple(item.strip() for item in payload)


def _normalize_github_username(value: str) -> str:
    """Return a trimmed, syntactically valid GitHub login.

    Raises ``ValueError`` with a caller-friendly reason so each identity source
    can be reported without leaking control characters into messages.
    """

    username = value.strip()
    if not username:
        raise ValueError("the login is empty")
    if not GITHUB_USERNAME_PATTERN.fullmatch(username):
        raise ValueError(f"{username!r} is not a valid GitHub login")
    return username


def resolve_github_username() -> str:
    """Resolve the reviewer identity from configuration or GitHub CLI.

    An explicit ``GITHUB_REVIEWER_USERNAME`` wins and needs no network call.
    Otherwise the REST current-user endpoint is tried first and the GraphQL
    ``viewer`` query second, so a single degraded GitHub endpoint cannot stop a
    whole run. Both report the same authenticated login, so only the last source
    spends transient-failure retries; an earlier endpoint failing hands over
    immediately instead of waiting out its own backoff.
    """

    configured = os.getenv("GITHUB_REVIEWER_USERNAME", "").strip()
    if configured:
        try:
            return _normalize_github_username(configured)
        except ValueError as error:
            raise ConfigError(f"GITHUB_REVIEWER_USERNAME is invalid: {error}") from error

    failures: list[str] = []
    last_index = len(GITHUB_USERNAME_COMMANDS) - 1
    for index, (label, command) in enumerate(GITHUB_USERNAME_COMMANDS):
        try:
            return _normalize_github_username(
                run_cmd(list(command), retry_transient=index == last_index)
            )
        except (CommandError, ValueError) as error:
            failures.append(f"{label}: {error}")
    raise ConfigError(
        "Cannot resolve the current GitHub CLI user; set GITHUB_REVIEWER_USERNAME "
        f"in .env to bypass the lookup. Attempts: {'; '.join(failures)}"
    )


def parse_effort(
    value: Optional[str], name: str, engine: Optional[str], engine_name: str
) -> Optional[str]:
    """Return one validated reasoning effort, or None when none was named.

    Effort belongs to the engine beside it, so it is refused without one and for an
    adapter that cannot pass it on. Which levels a given model honours is the
    provider's to say: Antigravity rejects an unsupported pairing itself.
    """

    effort = (value or "").strip().lower() or None
    if effort is None:
        return None
    if effort not in EFFORT_LEVELS:
        raise ConfigError(
            f"{name} must be one of {', '.join(EFFORT_LEVELS)}, not {value!r}"
        )
    if engine is None:
        raise ConfigError(f"{name} requires {engine_name}")
    if engine not in EFFORT_ENGINES:
        supported = sorted(EFFORT_ENGINES)
        raise ConfigError(
            f"{name} is not supported by the {engine} adapter; only "
            f"{', '.join(supported[:-1])} and {supported[-1]} pass a reasoning effort"
        )
    return effort


def _optional_engine(
    role: str,
    supported: frozenset,
    noun: str,
    engine_override: Optional[str] = None,
    model_override: Optional[str] = None,
    disabled: bool = False,
) -> tuple[Optional[str], tuple[str, ...], Optional[str], Optional[str]]:
    """Read one optional secondary engine, its model and effort, command line first.

    The verification and fallback engines follow the same rule: only a built-in
    adapter is allowed, because the controller supplies its command, and naming a
    model without an engine is a configuration mistake rather than a default.

    A command-line engine does not inherit the configured model or effort, because a
    model ID belongs to the provider that publishes it and each provider decides
    which levels its models take; naming one alongside is how a different model is
    chosen. `disabled` switches the role off entirely, which is what asking for no
    fallback means.
    """

    if disabled:
        return None, (), None, None
    environment_engine = os.getenv(f"REVIEW_{role}_ENGINE", "").strip().lower() or None
    environment_model = os.getenv(f"REVIEW_{role}_MODEL", "").strip() or None
    environment_effort = os.getenv(f"REVIEW_{role}_EFFORT", "").strip() or None
    selected = (engine_override or "").strip().lower() or None
    engine = selected or environment_engine
    model = (model_override or "").strip() or None
    inherits = selected is None or selected == environment_engine
    if model is None and inherits:
        model = environment_model
    command: tuple[str, ...] = ()
    if engine is not None:
        if engine not in supported:
            raise ConfigError(
                f"Unsupported REVIEW_{role}_ENGINE {engine!r}; supported "
                f"{noun} engines are antigravity, codex, claude, and kiro"
            )
        command = parse_command(json.dumps(DEFAULT_ENGINE_COMMANDS[engine]))
    elif model is not None:
        raise ConfigError(f"REVIEW_{role}_MODEL requires REVIEW_{role}_ENGINE")
    effort = parse_effort(
        environment_effort if inherits else None,
        f"REVIEW_{role}_EFFORT",
        engine,
        f"REVIEW_{role}_ENGINE",
    )
    return engine, command, model, effort


def default_locations() -> Locations:
    """Return this run's configuration and state locations, as a ConfigError."""

    try:
        return resolve_locations()
    except LocationError as error:
        raise ConfigError(str(error)) from error


# Until 2.1.0 the default comment files sat in a `templates/` directory beside the
# package, and `.env.example` named them there. They now ship inside the package so
# an installed copy has them too; a setting that still names the old place means the
# default it always meant.
LEGACY_TEMPLATE_DIRECTORY = Path("templates")


def _comment_file(variable: str, default: Path, base_directory: Path) -> Path:
    configured = os.getenv(variable, "").strip()
    if not configured:
        return default
    resolved = resolve_path(configured, base_directory)
    if not resolved.exists() and Path(configured) == (
        LEGACY_TEMPLATE_DIRECTORY / default.name
    ):
        return default
    return resolved


def load_settings(
    env_file: Optional[Path] = None,
    repositories_file: Optional[Path] = None,
    state_file: Optional[Path] = None,
    review_engine_override: Optional[str] = None,
    review_model_override: Optional[str] = None,
    verify_engine_override: Optional[str] = None,
    verify_model_override: Optional[str] = None,
    fallback_engine_override: Optional[str] = None,
    fallback_model_override: Optional[str] = None,
    fallback_disabled: bool = False,
) -> Settings:
    locations = default_locations()
    # Relative settings resolve against the configuration directory, which in a
    # source checkout is the checkout root they always resolved against.
    base_directory = locations.config_directory
    declared = load_env_file(env_file if env_file is not None else locations.env_file)

    selected_repositories_file = repositories_file
    if selected_repositories_file is None:
        configured_value = os.getenv("REPOSITORIES_FILE")
        selected_repositories_file = (
            resolve_path(configured_value, base_directory)
            if configured_value
            else locations.repositories_file
        )
    elif not selected_repositories_file.is_absolute():
        selected_repositories_file = (Path.cwd() / selected_repositories_file).resolve()

    selected_state_file = state_file
    if selected_state_file is None:
        configured_state = os.getenv("STATE_FILE")
        selected_state_file = (
            resolve_path(configured_state, base_directory)
            if configured_state
            else locations.state_file
        )
    elif not selected_state_file.is_absolute():
        selected_state_file = (Path.cwd() / selected_state_file).resolve()

    comment_template_file = _comment_file(
        "GITHUB_COMMENT_TEMPLATE_FILE",
        DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE,
        base_directory,
    )
    comment_tags_file = _comment_file(
        "GITHUB_COMMENT_TAGS_FILE",
        DEFAULT_GITHUB_COMMENT_TAGS_FILE,
        base_directory,
    )
    try:
        github_comment_template = load_comment_template(
            comment_template_file,
            comment_tags_file,
        )
    except CommentTemplateError as error:
        raise ConfigError(str(error)) from error

    environment_engine = os.getenv("REVIEW_ENGINE", "antigravity").strip().lower()
    review_engine = (review_engine_override or environment_engine).strip().lower()
    if review_engine not in SUPPORTED_REVIEW_ENGINES:
        raise ConfigError(
            f"Unsupported REVIEW_ENGINE {review_engine!r}; supported engines are "
            "antigravity, codex, claude, kiro, and command"
        )
    configured_command = os.getenv("REVIEW_ENGINE_COMMAND", "").strip()
    configured_model = os.getenv("REVIEW_MODEL", "").strip() or None
    configured_effort = os.getenv("REVIEW_EFFORT", "").strip() or None
    configured_code_effort = os.getenv("REVIEW_CODE_EFFORT", "").strip() or None
    if review_engine_override and review_engine != environment_engine:
        # A model ID belongs to the provider that publishes it. Carrying
        # REVIEW_MODEL onto a different engine sent Antigravity's
        # gemini-3.1-pro-high to the Claude CLI as --model, which that provider
        # can only reject, so --engine silently could not work for anybody who had
        # set a model. The command is cleared for the same reason, and so is the
        # effort: Antigravity refuses one its default model does not take.
        configured_command = ""
        configured_model = None
        configured_effort = None
        configured_code_effort = None
    elif _engine_switched_from(declared, review_engine):
        # The same mistake reached the other way. A command and a model written in
        # the configuration file belong to the engine named in that same file, so
        # selecting another engine through the environment must not keep them: the
        # Claude adapter built Claude arguments and handed them to Antigravity's
        # binary, which answered with its own help text and no review at all.
        # Values the environment supplied for the new engine are kept, because those
        # were chosen with that engine in view.
        if configured_command == declared.get("REVIEW_ENGINE_COMMAND", "").strip():
            configured_command = ""
        if configured_model == (declared.get("REVIEW_MODEL", "").strip() or None):
            configured_model = None
        if configured_effort == (declared.get("REVIEW_EFFORT", "").strip() or None):
            configured_effort = None
        if configured_code_effort == (
            declared.get("REVIEW_CODE_EFFORT", "").strip() or None
        ):
            configured_code_effort = None
    if review_model_override:
        # Named alongside the engine, so it was chosen for the engine that will run.
        configured_model = review_model_override.strip() or None
    if configured_command:
        review_engine_command = parse_command(configured_command)
    elif review_engine in DEFAULT_ENGINE_COMMANDS:
        review_engine_command = parse_command(
            json.dumps(DEFAULT_ENGINE_COMMANDS[review_engine])
        )
    else:
        raise ConfigError(
            "REVIEW_ENGINE_COMMAND is required when REVIEW_ENGINE=command"
        )

    review_effort = parse_effort(
        configured_effort, "REVIEW_EFFORT", review_engine, "REVIEW_ENGINE"
    )
    review_code_effort = parse_effort(
        configured_code_effort, "REVIEW_CODE_EFFORT", review_engine, "REVIEW_ENGINE"
    )

    verify_engine, verify_engine_command, verify_model, verify_effort = (
        _optional_engine(
            "VERIFY",
            SUPPORTED_VERIFY_ENGINES,
            "verification",
            engine_override=verify_engine_override,
            model_override=verify_model_override,
        )
    )
    fallback_engine, fallback_engine_command, fallback_model, fallback_effort = (
        _optional_engine(
            "FALLBACK",
            SUPPORTED_FALLBACK_ENGINES,
            "fallback",
            engine_override=fallback_engine_override,
            model_override=fallback_model_override,
            disabled=fallback_disabled,
        )
    )
    (
        verify_fallback_engine,
        verify_fallback_engine_command,
        verify_fallback_model,
        verify_fallback_effort,
    ) = _optional_engine(
        "VERIFY_FALLBACK", SUPPORTED_VERIFY_ENGINES, "verification fallback"
    )
    if verify_fallback_engine is not None and verify_engine is None:
        # With no separate verifier the reviewer verifies its own findings, and the
        # reviewer's fallback already takes over when that engine fails.
        raise ConfigError(
            "REVIEW_VERIFY_FALLBACK_ENGINE requires REVIEW_VERIFY_ENGINE"
        )

    return Settings(
        reviewer_username=resolve_github_username(),
        repositories=load_repositories(selected_repositories_file),
        dry_run=parse_bool(os.getenv("AUTO_REVIEW_DRY_RUN", "1"), "AUTO_REVIEW_DRY_RUN"),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL", "").strip() or None,
        state_file=selected_state_file,
        review_engine=review_engine,
        review_engine_command=review_engine_command,
        review_engine_env_allowlist=parse_env_allowlist(
            os.getenv("REVIEW_ENGINE_ENV_ALLOWLIST", "[]")
        ),
        review_model=configured_model,
        review_timeout_seconds=parse_integer(
            os.getenv("REVIEW_TIMEOUT_SECONDS", "300"),
            "REVIEW_TIMEOUT_SECONDS",
            30,
            1800,
        ),
        design_level=parse_bool(
            os.getenv("REVIEW_DESIGN_LEVEL", "1"), "REVIEW_DESIGN_LEVEL"
        ),
        max_design_findings=parse_integer(
            os.getenv("MAX_DESIGN_FINDINGS", "4"),
            "MAX_DESIGN_FINDINGS",
            1,
            10,
        ),
        docs_level=parse_bool(
            os.getenv("REVIEW_DOCS_LEVEL", "1"), "REVIEW_DOCS_LEVEL"
        ),
        max_docs_findings=parse_integer(
            os.getenv("MAX_DOCS_FINDINGS", "3"),
            "MAX_DOCS_FINDINGS",
            1,
            10,
        ),
        review_verify_timeout_seconds=parse_integer(
            os.getenv("REVIEW_VERIFY_TIMEOUT_SECONDS", "900"),
            "REVIEW_VERIFY_TIMEOUT_SECONDS",
            30,
            3600,
        ),
        review_verify_timeout_per_candidate_seconds=parse_integer(
            os.getenv("REVIEW_VERIFY_TIMEOUT_PER_CANDIDATE_SECONDS", "180"),
            "REVIEW_VERIFY_TIMEOUT_PER_CANDIDATE_SECONDS",
            0,
            600,
        ),
        max_diff_bytes=parse_integer(
            os.getenv("MAX_DIFF_BYTES", "200000"),
            "MAX_DIFF_BYTES",
            1000,
            500000,
        ),
        max_findings=parse_integer(
            os.getenv("MAX_FINDINGS", "10"),
            "MAX_FINDINGS",
            1,
            10,
        ),
        # Only a change that does not fit one review is bundled, and `1` keeps the
        # single review this tool has always done. Each bundle is another invocation,
        # so the cap is what stops one enormous pull request spending a whole slot.
        max_review_bundles=parse_integer(
            os.getenv("MAX_REVIEW_BUNDLES", "4"),
            "MAX_REVIEW_BUNDLES",
            1,
            MAX_REVIEW_BUNDLE_LIMIT,
        ),
        # Reviews are bound by provider capacity rather than local CPU, and
        # every extra slot is another CLI invocation against one account.
        review_concurrency=parse_integer(
            os.getenv("REVIEW_CONCURRENCY", "2"),
            "REVIEW_CONCURRENCY",
            1,
            MAX_REVIEW_CONCURRENCY,
        ),
        # Transcripts are small text files, so a window wide enough to cover
        # last week's scheduled runs costs little and answers most questions.
        run_log_retention_days=parse_integer(
            os.getenv("RUN_LOG_RETENTION_DAYS", "7"),
            "RUN_LOG_RETENTION_DAYS",
            1,
            MAX_RUN_LOG_RETENTION_DAYS,
        ),
        verify_engine=verify_engine,
        verify_engine_command=verify_engine_command,
        verify_model=verify_model,
        fallback_engine=fallback_engine,
        fallback_engine_command=fallback_engine_command,
        fallback_model=fallback_model,
        review_effort=review_effort,
        review_code_effort=review_code_effort,
        verify_effort=verify_effort,
        fallback_effort=fallback_effort,
        verify_fallback_engine=verify_fallback_engine,
        verify_fallback_engine_command=verify_fallback_engine_command,
        verify_fallback_model=verify_fallback_model,
        verify_fallback_effort=verify_fallback_effort,
        github_comment_template=github_comment_template,
        excluded_pr_labels=parse_excluded_pr_labels(
            os.getenv("EXCLUDED_PR_LABELS", "[]")
        ),
        ignored_missing_patch_patterns=parse_ignored_missing_patch_patterns(
            os.getenv("IGNORED_MISSING_PATCH_PATTERNS", "[]")
        ),
        design_excluded_head_branches=parse_design_excluded_head_branches(
            os.getenv(
                "DESIGN_EXCLUDED_HEAD_BRANCHES",
                DEFAULT_DESIGN_EXCLUDED_HEAD_BRANCHES,
            )
        ),
    )
