"""The reviews file: which types of review run, in what order, and on which engines.

`reviews.json` sits beside `.env` and names, for each type of review, a `review`
stage and a `verify` stage. Each stage lists the engines it may run on: the first
reviews, and each one after it takes over, in order, when the one before fails at the
engine level. A type also says how many findings one review of it may post. Beside
the types, the file holds what every review shares: how many pull requests run at
once, how much of a change one review reads, and which environment variables each
provider's CLI is given beyond the few every one gets.

Without the file a run takes the same settings from `.env`, as it always has.
`reviews_payload` writes those settings out as the file, so a run with the file it
returns does what the run without it did.

Only the built-in engines can be named: the controller supplies their commands. A
custom `command` engine stays in `.env`.
"""

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .config import (
    DEFAULT_ENGINE_COMMANDS,
    DEFAULT_IGNORED_MISSING_PATCH_PATTERNS,
    EFFORT_LEVELS,
    ENV_KEY_PATTERN,
    MAX_REVIEW_BUNDLE_LIMIT,
    MAX_REVIEW_CONCURRENCY,
    ConfigError,
    Settings,
    parse_command,
)
from .engine import CHECKS_LEVEL, CODE_LEVEL, DESIGN_LEVEL, DOCS_LEVEL

REVIEWS_FILE_NAME = "reviews.json"
# The `.env` keys a reviews file takes the place of: with the file, none is read.
FILE_KEYS = (
    "REVIEW_ENGINE",
    "REVIEW_ENGINE_COMMAND",
    "REVIEW_ENGINE_ENV_ALLOWLIST",
    "REVIEW_MODEL",
    "REVIEW_EFFORT",
    "REVIEW_CODE_EFFORT",
    "REVIEW_TIMEOUT_SECONDS",
    "REVIEW_VERIFY_ENGINE",
    "REVIEW_VERIFY_MODEL",
    "REVIEW_VERIFY_EFFORT",
    "REVIEW_VERIFY_TIMEOUT_SECONDS",
    "REVIEW_VERIFY_TIMEOUT_PER_CANDIDATE_SECONDS",
    "REVIEW_FALLBACK_ENGINE",
    "REVIEW_FALLBACK_MODEL",
    "REVIEW_FALLBACK_EFFORT",
    "REVIEW_VERIFY_FALLBACK_ENGINE",
    "REVIEW_VERIFY_FALLBACK_MODEL",
    "REVIEW_VERIFY_FALLBACK_EFFORT",
    "REVIEW_DESIGN_LEVEL",
    "REVIEW_DOCS_LEVEL",
    "REVIEW_CHECKS_LEVEL",
    "REVIEW_CHECKS_MODEL",
    "REVIEW_CHECKS_EFFORT",
    "MAX_FINDINGS",
    "MAX_DESIGN_FINDINGS",
    "MAX_DOCS_FINDINGS",
    "MAX_DIFF_BYTES",
    "MAX_REVIEW_BUNDLES",
    "IGNORED_MISSING_PATCH_PATTERNS",
    "REVIEW_CONCURRENCY",
)
# The main engine and up to three that take over from it.
MAX_STAGE_ENGINES = 4
ENGINES = tuple(sorted(DEFAULT_ENGINE_COMMANDS))
# The types a run orders; the checks type runs only when a code review waits for
# failed checks, so it has no place in the order.
ORDERED_TYPES = (DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL)
TYPES = (*ORDERED_TYPES, CHECKS_LEVEL)
TYPE_NOUNS = {
    DESIGN_LEVEL: "solution-design review",
    DOCS_LEVEL: "documentation review",
    CODE_LEVEL: "code review",
    CHECKS_LEVEL: "checks reading",
}
# What one review of each type may post, and the most it may be set to.
DEFAULT_MAX_FINDINGS = {CODE_LEVEL: 10, DESIGN_LEVEL: 4, DOCS_LEVEL: 3}
MAX_FINDINGS_LIMIT = 10
# The budgets `.env` has always allowed, in seconds.
REVIEW_TIMEOUT = (300, 30, 1800)
VERIFY_TIMEOUT = (900, 30, 3600)
VERIFY_TIMEOUT_PER_CANDIDATE = (180, 0, 600)
DEFAULT_CONCURRENCY = 2
MAX_DIFF_BYTES = (200000, 1000, 500000)
DEFAULT_REVIEW_BUNDLES = 4


@dataclass(frozen=True)
class EngineChoice:
    """One engine a stage may run on: the CLI, its model, and its effort.

    A model of None is the provider's default, and so is an effort of None.
    """

    engine: str
    model: Optional[str] = None
    effort: Optional[str] = None


@dataclass(frozen=True)
class Stage:
    """The engines one stage of a type runs on, in the order they are tried.

    Every engine of a stage has the stage's budget. A verification is given its
    budget plus ``timeout_per_candidate_seconds`` for each candidate it judges.
    """

    engines: "tuple[EngineChoice, ...]"
    timeout_seconds: int
    timeout_per_candidate_seconds: int = 0


@dataclass(frozen=True)
class ReviewTypeConfig:
    """One type of review as the file configures it.

    ``verify`` is None when the reviewer verifies its own candidates, and for the
    checks type, whose reading the controller checks instead. ``max_findings`` is
    None for the checks type, which posts no findings.
    """

    name: str
    review: Stage
    verify: Optional[Stage] = None
    max_findings: Optional[int] = None


@dataclass(frozen=True)
class ReviewsFile:
    """What a reviews file says, checked, with the file it came from."""

    source: Path
    types: "tuple[ReviewTypeConfig, ...]"
    concurrency: int = DEFAULT_CONCURRENCY
    max_diff_bytes: int = MAX_DIFF_BYTES[0]
    max_review_bundles: int = DEFAULT_REVIEW_BUNDLES
    ignored_missing_patch_patterns: "tuple[str, ...]" = (
        DEFAULT_IGNORED_MISSING_PATCH_PATTERNS
    )
    # Beyond the few variables every CLI gets, the ones each provider's is given.
    engine_env: "tuple[tuple[str, tuple[str, ...]], ...]" = ()

    def get(self, name: str) -> Optional[ReviewTypeConfig]:
        for review_type in self.types:
            if review_type.name == name:
                return review_type
        return None

    def env_for(self, engine: str) -> "tuple[str, ...]":
        return dict(self.engine_env).get(engine, ())


def _refuse(source: object, where: str, problem: str) -> ConfigError:
    return ConfigError(f"{source}: {where} {problem}")


def _object(
    value: Any, source: object, where: str, allowed: "Sequence[str]"
) -> "dict[str, Any]":
    """Return ``value`` as an object holding only ``allowed`` keys."""

    if not isinstance(value, dict):
        raise _refuse(source, where, "must be an object")
    unknown = sorted(key for key in value if key not in allowed)
    if unknown:
        raise _refuse(
            source,
            where,
            f"has an unknown key {unknown[0]!r}; it may hold "
            + ", ".join(allowed),
        )
    return value


def _integer(
    value: Any, source: object, where: str, limits: "tuple[int, int, int]"
) -> int:
    default, minimum, maximum = limits
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise _refuse(source, where, "must be a whole number")
    if not minimum <= value <= maximum:
        raise _refuse(source, where, f"must be between {minimum} and {maximum}")
    return value


def _names(value: Any, source: object, where: str) -> "tuple[str, ...]":
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise _refuse(source, where, "must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _engine_choice(value: Any, source: object, where: str) -> EngineChoice:
    payload = _object(value, source, where, ("engine", "model", "effort"))
    engine = payload.get("engine")
    if not isinstance(engine, str) or engine.strip().lower() not in ENGINES:
        raise _refuse(
            source, f"{where}.engine", "must be one of " + ", ".join(ENGINES)
        )
    model = payload.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise _refuse(source, f"{where}.model", "must be a model ID, or left out")
    effort = payload.get("effort")
    if effort is not None and (
        not isinstance(effort, str) or effort.strip().lower() not in EFFORT_LEVELS
    ):
        raise _refuse(
            source,
            f"{where}.effort",
            "must be one of " + ", ".join(EFFORT_LEVELS) + ", or left out",
        )
    return EngineChoice(
        engine.strip().lower(),
        model.strip() if model is not None else None,
        effort.strip().lower() if effort is not None else None,
    )


def _stage(
    value: Any,
    source: object,
    where: str,
    *,
    verify: bool,
    most: int = MAX_STAGE_ENGINES,
) -> Stage:
    keys = ("engines", "timeout_seconds") + (
        ("timeout_per_candidate_seconds",) if verify else ()
    )
    payload = _object(value, source, where, keys)
    engines = payload.get("engines")
    if not isinstance(engines, list) or not 1 <= len(engines) <= most:
        count = "one engine" if most == 1 else f"one to {most} engines"
        raise _refuse(source, f"{where}.engines", f"must list {count}")
    choices = tuple(
        _engine_choice(engine, source, f"{where}.engines[{index}]")
        for index, engine in enumerate(engines)
    )
    seen: "set[tuple[str, Optional[str]]]" = set()
    for index, choice in enumerate(choices):
        key = (choice.engine, choice.model)
        if key in seen:
            # Taking over from a failed engine with the same engine and model can
            # only fail the same way.
            raise _refuse(
                source,
                f"{where}.engines[{index}]",
                "repeats an engine and model the stage already lists",
            )
        seen.add(key)
    return Stage(
        choices,
        _integer(
            payload.get("timeout_seconds"),
            source,
            f"{where}.timeout_seconds",
            VERIFY_TIMEOUT if verify else REVIEW_TIMEOUT,
        ),
        _integer(
            payload.get("timeout_per_candidate_seconds"),
            source,
            f"{where}.timeout_per_candidate_seconds",
            VERIFY_TIMEOUT_PER_CANDIDATE,
        )
        if verify
        else 0,
    )


def _review_type(
    name: str, value: Any, source: object
) -> Optional[ReviewTypeConfig]:
    """Return one configured type, or None when the file turns it off."""

    where = f"types.{name}"
    checks = name == CHECKS_LEVEL
    keys = ("enabled", "review") + (() if checks else ("verify", "max_findings"))
    payload = _object(value, source, where, keys)
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _refuse(source, f"{where}.enabled", "must be true or false")
    if not enabled:
        if name == CODE_LEVEL:
            raise _refuse(
                source,
                f"{where}.enabled",
                "cannot be false: the code review is the one that approves",
            )
        return None
    if "review" not in payload:
        raise _refuse(source, f"{where}.review", "is required")
    # The checks reading has no fallback: a reading that fails costs only itself,
    # since the note it would join is posted without it.
    review = _stage(
        payload["review"],
        source,
        f"{where}.review",
        verify=False,
        most=1 if checks else MAX_STAGE_ENGINES,
    )
    if checks:
        return ReviewTypeConfig(name, review)
    verify = (
        _stage(payload["verify"], source, f"{where}.verify", verify=True)
        if payload.get("verify") is not None
        else None
    )
    return ReviewTypeConfig(
        name,
        review,
        verify,
        _integer(
            payload.get("max_findings"),
            source,
            f"{where}.max_findings",
            (DEFAULT_MAX_FINDINGS[name], 1, MAX_FINDINGS_LIMIT),
        ),
    )


def parse_reviews(payload: Any, source: Path) -> ReviewsFile:
    """Return what a reviews file says, or refuse it, naming the setting at fault."""

    top = _object(
        payload,
        source,
        "the file",
        ("concurrency", "diff", "engines", "order", "types"),
    )
    types_payload = top.get("types")
    if types_payload is None:
        raise _refuse(source, "types", "is required")
    types_payload = _object(types_payload, source, "types", TYPES)
    configured = {
        name: review_type
        for name in TYPES
        if name in types_payload
        for review_type in (_review_type(name, types_payload[name], source),)
        if review_type is not None
    }
    if CODE_LEVEL not in configured:
        raise _refuse(
            source,
            "types.code",
            "is required: the code review is the one that approves",
        )
    present = [name for name in ORDERED_TYPES if name in configured]
    order_payload = top.get("order")
    if order_payload is None:
        order = present
    else:
        listed = list(_names(order_payload, source, "order"))
        if sorted(listed) != sorted(present):
            raise _refuse(
                source,
                "order",
                "must list each enabled type of review once: " + ", ".join(present),
            )
        if listed[-1] != CODE_LEVEL:
            raise _refuse(
                source,
                "order",
                "must end with code: its approval is what a pull request is merged "
                "on, so the advisory notes come before it",
            )
        order = listed
    types = tuple(configured[name] for name in order) + tuple(
        configured[name] for name in (CHECKS_LEVEL,) if name in configured
    )
    diff = _object(
        top.get("diff", {}),
        source,
        "diff",
        ("max_bytes", "max_bundles", "ignored_missing_patch"),
    )
    ignored = diff.get("ignored_missing_patch")
    engines = _object(top.get("engines", {}), source, "engines", ENGINES)
    engine_env = []
    for engine in ENGINES:
        if engine not in engines:
            continue
        where = f"engines.{engine}"
        names = _names(
            _object(engines[engine], source, where, ("env",)).get("env", []),
            source,
            f"{where}.env",
        )
        bad = [name for name in names if not ENV_KEY_PATTERN.fullmatch(name)]
        if bad:
            raise _refuse(
                source, f"{where}.env", f"names {bad[0]!r}, which is not a variable"
            )
        engine_env.append((engine, names))
    return ReviewsFile(
        source=source,
        types=types,
        concurrency=_integer(
            top.get("concurrency"),
            source,
            "concurrency",
            (DEFAULT_CONCURRENCY, 1, MAX_REVIEW_CONCURRENCY),
        ),
        max_diff_bytes=_integer(
            diff.get("max_bytes"), source, "diff.max_bytes", MAX_DIFF_BYTES
        ),
        max_review_bundles=_integer(
            diff.get("max_bundles"),
            source,
            "diff.max_bundles",
            (DEFAULT_REVIEW_BUNDLES, 1, MAX_REVIEW_BUNDLE_LIMIT),
        ),
        ignored_missing_patch_patterns=(
            DEFAULT_IGNORED_MISSING_PATCH_PATTERNS
            if ignored is None
            else _names(ignored, source, "diff.ignored_missing_patch")
        ),
        engine_env=tuple(engine_env),
    )


def load_reviews_file(path: Path) -> Optional[ReviewsFile]:
    """Return what the reviews file at ``path`` says, or None when there is none."""

    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON: {error}") from error
    return parse_reviews(payload, path)


def apply_overrides(
    reviews: ReviewsFile,
    *,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
    fallback_engine: Optional[str] = None,
    fallback_model: Optional[str] = None,
    no_fallback: bool = False,
) -> ReviewsFile:
    """Return the file as this run's command line changes it.

    A command-line engine replaces the first engine of every type's stage and does
    not inherit its model or effort, which belong to the engine they were written
    for, unless it is that same engine. ``--engine`` also moves the checks reading,
    which reads on the code review's engine by default, and ``--model`` alone does
    not, since the checks reading has a model of its own.
    """

    for flag, named in (
        ("--engine", engine),
        ("--verify-engine", verify_engine),
        ("--fallback-engine", fallback_engine),
    ):
        selected = (named or "").strip().lower()
        if selected and selected not in ENGINES:
            raise ConfigError(
                f"{flag} {selected}: {reviews.source} names only the built-in "
                f"engines, {', '.join(ENGINES)}; a custom command engine is set in "
                ".env without a reviews file"
            )

    def chosen(
        first: EngineChoice, new_engine: Optional[str], new_model: Optional[str]
    ) -> EngineChoice:
        selected = (new_engine or "").strip().lower() or first.engine
        named = (new_model or "").strip() or None
        if selected != first.engine:
            return EngineChoice(selected, named)
        return EngineChoice(selected, named or first.model, first.effort)

    def staged(
        stage: Optional[Stage], new_engine: Optional[str], new_model: Optional[str]
    ) -> Optional[Stage]:
        if not new_engine and not new_model:
            return stage
        if stage is None:
            # A verifier named for a type that verified with its reviewer.
            return Stage(
                (EngineChoice((new_engine or "").strip().lower(), new_model or None),),
                VERIFY_TIMEOUT[0],
                VERIFY_TIMEOUT_PER_CANDIDATE[0],
            )
        first = chosen(stage.engines[0], new_engine, new_model)
        rest = tuple(
            choice
            for choice in stage.engines[1:]
            if (choice.engine, choice.model) != (first.engine, first.model)
        )
        return replace(stage, engines=(first, *rest))

    types = []
    for review_type in reviews.types:
        if review_type.name == CHECKS_LEVEL:
            first = review_type.review.engines[0]
            selected = (engine or "").strip().lower()
            if selected and selected != first.engine:
                review_type = replace(
                    review_type,
                    review=replace(
                        review_type.review, engines=(chosen(first, selected, model),)
                    ),
                )
            types.append(review_type)
            continue
        if verify_model and not verify_engine and review_type.verify is None:
            raise ConfigError(
                f"--verify-model needs --verify-engine: the {review_type.name} "
                f"review in {reviews.source} names no verifier"
            )
        review = staged(review_type.review, engine, model)
        assert review is not None
        engines = review.engines
        if no_fallback:
            engines = engines[:1]
        elif fallback_engine or fallback_model:
            if len(engines) < 2 and not fallback_engine:
                raise ConfigError(
                    f"--fallback-model needs --fallback-engine: the "
                    f"{review_type.name} review in {reviews.source} names no fallback"
                )
            current = (
                engines[1]
                if len(engines) > 1
                else EngineChoice((fallback_engine or "").strip().lower())
            )
            fallback = chosen(current, fallback_engine, fallback_model)
            if (fallback.engine, fallback.model) == (
                engines[0].engine,
                engines[0].model,
            ):
                engines = engines[:1] + engines[2:]
            else:
                engines = (
                    engines[0],
                    fallback,
                    *(
                        choice
                        for choice in engines[2:]
                        if (choice.engine, choice.model)
                        != (fallback.engine, fallback.model)
                    ),
                )
        types.append(
            replace(
                review_type,
                review=replace(review, engines=engines),
                verify=staged(review_type.verify, verify_engine, verify_model),
            )
        )
    return replace(reviews, types=tuple(types))


def engine_commands() -> "dict[str, tuple[str, ...]]":
    """Return the command of every built-in engine, resolved on this machine."""

    return {
        engine: parse_command(json.dumps(command))
        for engine, command in DEFAULT_ENGINE_COMMANDS.items()
    }


def engines_placeholder(reviews: ReviewsFile) -> "dict[str, Any]":
    """Return the reviewer fields settings need before the file's are put in."""

    code = reviews.get(CODE_LEVEL)
    assert code is not None
    first = code.review.engines[0]
    return {
        "review_engine": first.engine,
        "review_engine_command": engine_commands()[first.engine],
        "review_model": first.model,
    }


def _on_engine(
    settings: Settings,
    choice: EngineChoice,
    reviews: ReviewsFile,
    commands: "Mapping[str, tuple[str, ...]]",
) -> Settings:
    """Return ``settings`` with ``choice`` as the engine that reviews."""

    return replace(
        settings,
        review_engine=choice.engine,
        review_engine_command=commands[choice.engine],
        review_engine_env_allowlist=reviews.env_for(choice.engine),
        review_model=choice.model,
        review_effort=choice.effort,
        review_code_effort=None,
    )


@dataclass(frozen=True)
class TypeEngines:
    """The settings one type reviews with, and the engines that take over."""

    settings: Settings
    fallbacks: "tuple[Settings, ...]" = ()
    verifiers: "tuple[Settings, ...]" = ()


def type_engines(
    base: Settings,
    reviews: ReviewsFile,
    review_type: ReviewTypeConfig,
    commands: "Optional[Mapping[str, tuple[str, ...]]]" = None,
) -> TypeEngines:
    """Return the settings and engine chains one configured type runs on.

    The type's settings name the first fallback and the first two verifiers in the
    role fields too, so whatever reads the reviewer, verifier, or fallback from
    settings, as every report of who reviewed does, reads this type's own.
    """

    commands = commands if commands is not None else engine_commands()
    review = review_type.review.engines
    verify = review_type.verify.engines if review_type.verify is not None else ()

    def role(position: "Sequence[EngineChoice]", index: int) -> "dict[str, Any]":
        choice = position[index] if index < len(position) else None
        return {
            "engine": choice.engine if choice else None,
            "engine_command": commands[choice.engine] if choice else (),
            "model": choice.model if choice else None,
            "effort": choice.effort if choice else None,
        }

    roles: "dict[str, Any]" = {}
    for prefix, values in (
        ("fallback", role(review, 1)),
        ("verify", role(verify, 0)),
        ("verify_fallback", role(verify, 1)),
    ):
        for key, value in values.items():
            roles[f"{prefix}_{key}"] = value
    limits: "dict[str, Any]" = {
        "review_timeout_seconds": review_type.review.timeout_seconds,
        # A type with no verify stage verifies with its reviewer, within the
        # default verification budget.
        "review_verify_timeout_seconds": (
            review_type.verify.timeout_seconds
            if review_type.verify is not None
            else VERIFY_TIMEOUT[0]
        ),
        "review_verify_timeout_per_candidate_seconds": (
            review_type.verify.timeout_per_candidate_seconds
            if review_type.verify is not None
            else VERIFY_TIMEOUT_PER_CANDIDATE[0]
        ),
    }
    if review_type.name == CODE_LEVEL:
        limits["max_findings"] = review_type.max_findings
    elif review_type.name == DESIGN_LEVEL:
        limits["max_design_findings"] = review_type.max_findings
    elif review_type.name == DOCS_LEVEL:
        limits["max_docs_findings"] = review_type.max_findings
    settings = _on_engine(
        replace(base, **roles, **limits), review[0], reviews, commands
    )
    return TypeEngines(
        settings,
        tuple(
            _on_engine(settings, choice, reviews, commands) for choice in review[1:]
        ),
        tuple(_on_engine(settings, choice, reviews, commands) for choice in verify),
    )


def settings_from_reviews(base: Settings, reviews: ReviewsFile) -> Settings:
    """Return the run's settings with what the reviews file says in them.

    The settings that describe the whole run, the reviewer it names first, its
    concurrency, and how much of a change one review reads, are the code review's
    and the file's, so the run's own report of them stays true. Each type's
    engines come from the file itself, through ``reviews``.
    """

    checks = reviews.get(CHECKS_LEVEL)
    with_file = replace(
        base,
        reviews=reviews,
        review_concurrency=reviews.concurrency,
        max_diff_bytes=reviews.max_diff_bytes,
        max_review_bundles=reviews.max_review_bundles,
        ignored_missing_patch_patterns=reviews.ignored_missing_patch_patterns,
        design_level=reviews.get(DESIGN_LEVEL) is not None,
        docs_level=reviews.get(DOCS_LEVEL) is not None,
        checks_level=checks is not None,
        checks_model=checks.review.engines[0].model if checks else None,
        checks_effort=checks.review.engines[0].effort if checks else None,
    )
    code = reviews.get(CODE_LEVEL)
    assert code is not None
    return type_engines(with_file, reviews, code).settings


def _choice(
    engine: Optional[str], model: Optional[str], effort: Optional[str]
) -> "dict[str, Any]":
    choice: "dict[str, Any]" = {"engine": engine}
    if model:
        choice["model"] = model
    if effort:
        choice["effort"] = effort
    return choice


def reviews_payload(settings: Settings) -> "dict[str, Any]":
    """Return the reviews file that says what ``settings`` say, from `.env`.

    Every built-in engine named is given the one allowlist `.env` gives every engine
    today. A custom `command` engine cannot be written into the file, so it is
    refused.
    """

    from .review_plan import review_plan

    if settings.review_engine not in DEFAULT_ENGINE_COMMANDS:
        raise ConfigError(
            f"REVIEW_ENGINE={settings.review_engine} cannot move to "
            f"{REVIEWS_FILE_NAME}: it names only the built-in engines"
        )
    plan = review_plan(settings)
    types: "dict[str, Any]" = {}
    used: "set[str]" = set()
    for review_type in plan.types:
        type_settings = review_type.settings
        review = [
            _choice(
                type_settings.review_engine,
                type_settings.review_model,
                type_settings.review_effort,
            ),
            *(
                _choice(item.review_engine, item.review_model, item.review_effort)
                for item in review_type.fallbacks
            ),
        ]
        entry: "dict[str, Any]" = {
            "review": {
                "engines": review,
                "timeout_seconds": type_settings.review_timeout_seconds,
            }
        }
        used.update(choice["engine"] for choice in review)
        if review_type.name != CHECKS_LEVEL:
            if review_type.verifiers:
                verify = [
                    _choice(item.review_engine, item.review_model, item.review_effort)
                    for item in review_type.verifiers
                ]
                used.update(choice["engine"] for choice in verify)
                entry["verify"] = {
                    "engines": verify,
                    "timeout_seconds": type_settings.review_verify_timeout_seconds,
                    "timeout_per_candidate_seconds": (
                        type_settings.review_verify_timeout_per_candidate_seconds
                    ),
                }
            entry["max_findings"] = {
                CODE_LEVEL: type_settings.max_findings,
                DESIGN_LEVEL: type_settings.max_design_findings,
                DOCS_LEVEL: type_settings.max_docs_findings,
            }[review_type.name]
        types[review_type.name] = entry
    diff: "dict[str, Any]" = {
        "max_bytes": settings.max_diff_bytes,
        "max_bundles": settings.max_review_bundles,
    }
    # Left out when it is the built-in list, so a release that adds to that list
    # reaches the file too, as it reaches `.env` that sets none.
    defaults = DEFAULT_IGNORED_MISSING_PATCH_PATTERNS
    if settings.ignored_missing_patch_patterns != defaults:
        diff["ignored_missing_patch"] = list(settings.ignored_missing_patch_patterns)
    payload: "dict[str, Any]" = {
        "concurrency": settings.review_concurrency,
        "diff": diff,
    }
    if settings.review_engine_env_allowlist:
        payload["engines"] = {
            engine: {"env": list(settings.review_engine_env_allowlist)}
            for engine in sorted(used)
        }
    payload["order"] = [name for name in plan.names if name in ORDERED_TYPES]
    payload["types"] = types
    return payload


def plan_summary(settings: Settings) -> "list[Any]":
    """Return everything the reviews ``settings`` plan depend on, as plain values.

    Two settings whose summaries are equal review every pull request alike: the same
    types in the same order, on the same engines, models and efforts, within the
    same limits. Setup compares the two before it moves `.env` into a file.
    """

    from .review_plan import review_plan

    def chain(items: "Sequence[Settings]") -> "list[Any]":
        return [
            (
                item.review_engine,
                item.review_model,
                item.review_effort,
                item.review_engine_env_allowlist,
            )
            for item in items
        ]

    return [
        (
            settings.review_concurrency,
            settings.max_diff_bytes,
            settings.max_review_bundles,
            tuple(settings.ignored_missing_patch_patterns),
        ),
        *(
            (
                review_type.name,
                chain((review_type.settings, *review_type.fallbacks)),
                chain(review_type.verifiers),
                review_type.settings.review_timeout_seconds,
                review_type.settings.review_verify_timeout_seconds,
                review_type.settings.review_verify_timeout_per_candidate_seconds,
                review_type.settings.max_findings,
                review_type.settings.max_design_findings,
                review_type.settings.max_docs_findings,
            )
            for review_type in review_plan(settings).types
        ),
    ]


def render_reviews(payload: "Mapping[str, Any]") -> str:
    """Return ``payload`` as the text of a reviews file a person can read and edit.

    Indented as JSON usually is, except that an object or list holding no other, an
    engine or the order, stays on one line when it fits, as in the example file.
    """

    def render(value: Any, depth: int, before: int) -> str:
        # ``before`` is how much of the line precedes the value, so a value kept on
        # one line is kept there only when the whole line fits.
        if not isinstance(value, (dict, list)):
            return json.dumps(value)
        items = list(value.values()) if isinstance(value, dict) else value
        flat = json.dumps(value)
        if not any(isinstance(item, (dict, list)) and item for item in items) and (
            before + len(flat) + 1 <= 88
        ):
            return flat
        inner = "  " * (depth + 1)
        if isinstance(value, dict):
            lines = []
            for key, item in value.items():
                head = f"{inner}{json.dumps(key)}: "
                lines.append(head + render(item, depth + 1, len(head)))
            return "{\n" + ",\n".join(lines) + "\n" + "  " * depth + "}"
        lines = [f"{inner}{render(item, depth + 1, len(inner))}" for item in value]
        return "[\n" + ",\n".join(lines) + "\n" + "  " * depth + "]"

    return render(dict(payload), 0, 0) + "\n"
