import re
from typing import Optional


ENGINE_DISPLAY_NAMES = {
    "antigravity": "Antigravity",
    "claude": "Claude",
    "codex": "Codex",
    "command": "Command",
    "kiro": "Kiro",
}
GEMINI_MODEL_PATTERN = re.compile(
    r"^gemini-(?P<version>\d+(?:\.\d+)*)-"
    r"(?P<family>flash|pro|argon)-(?P<tier>high|medium|low)$"
)
GPT_MODEL_PATTERN = re.compile(
    r"^gpt-(?P<version>\d+(?:\.\d+)*)(?:-(?P<variant>[a-z]+))?$"
)
CLAUDE_MODEL_PATTERN = re.compile(
    r"^claude-(?P<family>opus|sonnet|haiku)-(?P<version>\d+(?:\.\d+)*)$"
)
# Antigravity spells a Claude version with dashes and names its reasoning variant,
# or from agy 1.4.6 its effort tier.
CLAUDE_DASHED_MODEL_PATTERN = re.compile(
    r"^claude-(?P<family>opus|sonnet|haiku)-(?P<major>\d+)-(?P<minor>\d+)"
    r"(?:-(?P<variant>thinking|high|medium|low))?$"
)


def format_engine_display_name(review_engine: str) -> str:
    return ENGINE_DISPLAY_NAMES.get(review_engine.casefold(), review_engine)


def format_model_display_name(review_model: Optional[str]) -> str:
    if not review_model:
        return "provider default (model ID not configured)"
    normalized = review_model.casefold()
    gemini = GEMINI_MODEL_PATTERN.fullmatch(normalized)
    if gemini:
        return (
            f"Gemini {gemini.group('version')} "
            f"{gemini.group('family').title()} ({gemini.group('tier').title()})"
        )
    gpt = GPT_MODEL_PATTERN.fullmatch(normalized)
    if gpt:
        variant = gpt.group("variant")
        name = f"GPT-{gpt.group('version')}"
        return f"{name} {variant.title()}" if variant else name
    claude = CLAUDE_MODEL_PATTERN.fullmatch(normalized)
    if claude:
        return (
            f"Claude {claude.group('family').title()} {claude.group('version')}"
        )
    dashed = CLAUDE_DASHED_MODEL_PATTERN.fullmatch(normalized)
    if dashed:
        name = (
            f"Claude {dashed.group('family').title()} "
            f"{dashed.group('major')}.{dashed.group('minor')}"
        )
        variant = dashed.group("variant")
        return f"{name} ({variant.title()})" if variant else name
    return review_model


def format_reviewer_identity(
    review_engine: str,
    review_model: Optional[str],
) -> str:
    return (
        f"{format_model_display_name(review_model)} @ "
        f"{format_engine_display_name(review_engine)}"
    )


def _effort_phrase(effort: Optional[str], code_effort: Optional[str] = None) -> str:
    """Describe the effort a role runs at, or nothing when it is the model default."""

    if code_effort and code_effort != effort:
        base = f"{effort} effort" if effort else "the model's default effort"
        return f" at {base}, {code_effort} for the code review"
    return f" at {effort} effort" if effort else ""


def format_review_plan(
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
    fallback_engine: Optional[str] = None,
    fallback_model: Optional[str] = None,
    *,
    review_effort: Optional[str] = None,
    code_effort: Optional[str] = None,
    verify_effort: Optional[str] = None,
    fallback_effort: Optional[str] = None,
    verify_fallback_engine: Optional[str] = None,
    verify_fallback_model: Optional[str] = None,
    verify_fallback_effort: Optional[str] = None,
) -> tuple[str, ...]:
    """Describe the engines a run intends to use, before any review happens.

    Startup states configuration in the future tense, unlike delivered
    attribution, which reports completed work. Optional engines are named only
    when they are configured and differ from the primary engine and model.
    """

    reviewer = format_reviewer_identity(review_engine, review_model)
    lines = [
        f"Review will be done by {reviewer}"
        f"{_effort_phrase(review_effort, code_effort)}."
    ]
    if fallback_engine is not None:
        fallback = format_reviewer_identity(fallback_engine, fallback_model)
        lines.append(
            f"Falls back to {fallback}{_effort_phrase(fallback_effort)} "
            "on engine failure."
        )
    if verify_engine is not None:
        verifier = format_reviewer_identity(verify_engine, verify_model)
        lines.append(f"Verification by {verifier}{_effort_phrase(verify_effort)}.")
    if verify_engine is not None and verify_fallback_engine is not None:
        standby = format_reviewer_identity(
            verify_fallback_engine, verify_fallback_model
        )
        lines.append(
            f"Verification falls back to {standby}"
            f"{_effort_phrase(verify_fallback_effort)} on engine failure."
        )
    if (
        fallback_engine is not None
        and verify_engine is not None
        and fallback_engine == verify_engine
        and fallback_engine != review_engine
    ):
        # One provider carrying both roles means its limit takes out the fallback and
        # verification together, which reads as two independent safety nets and is
        # one. Worth saying at startup, where the configuration can still be changed.
        lines.append(
            f"Note: {fallback_engine} serves both fallback and verification, "
            "so one provider limit stops both."
        )
    elif (
        fallback_engine is not None
        and verify_engine is not None
        and verify_engine == review_engine
        and fallback_engine != review_engine
        and (verify_fallback_engine is None or verify_fallback_engine == review_engine)
    ):
        # The mirror image. With no verifier on another provider, when the reviewer's
        # provider is down the fallback can still review, and an approval needs no
        # verification, but any finding it raises goes to a verifier that is down too.
        lines.append(
            f"Note: {review_engine} serves both review and verification, so while "
            f"it is down the fallback's findings cannot be verified or delivered."
        )
    return tuple(lines)


def format_review_attribution(
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
) -> str:
    """Describe who reviewed, and who independently verified when they differ."""

    attribution = f"Reviewed by {format_reviewer_identity(review_engine, review_model)}"
    if verify_engine is None:
        return attribution
    verifier = format_reviewer_identity(verify_engine, verify_model)
    return f"{attribution}, verified by {verifier}"
