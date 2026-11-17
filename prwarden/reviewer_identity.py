import re
from typing import Optional

from .models import Finding, severity_rank


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


def same_model(first: Optional[str], second: Optional[str]) -> bool:
    """Return whether two model IDs could name one model, as far as they say.

    Hosts spell one model differently, `claude-opus-5.5` on one and
    `claude-opus-5-5-high` on another, so the IDs are compared by the model they
    display as, without the effort tier or variant a host appends. A provider
    default names no model, so it could be any one.
    """

    if not first or not second:
        return True

    def family(model: str) -> str:
        shown = format_model_display_name(model)
        return re.sub(r"\s*\([^)]*\)$", "", shown).casefold()

    return family(first) == family(second)


def describe_confirmation(
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
) -> str:
    """Say who confirmed a finding, and whether that was another model.

    ``verify_engine`` is None when the reviewer verified its own candidates. A
    different model is claimed only when the two IDs name different models.
    """

    if verify_engine is None:
        return "re-checked by the same model in a separate pass"
    verifier = format_reviewer_identity(verify_engine, verify_model)
    if not same_model(review_model, verify_model):
        return (
            f"confirmed by {verifier}, a different model from the one that found it"
        )
    return f"confirmed by {verifier} in a separate pass"


def describe_checks(
    finding: Finding,
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
) -> str:
    """Say what was checked before a confirmed finding was posted, from facts alone.

    Never a confidence a model states about itself: the controller counts the
    citations it matched against the reviewed commit and names the verifier that
    confirmed the finding. That is the finding's own, recorded when it was
    confirmed, because a review read in bundles can hand its verification to a
    fallback part way; the review's verifier stands in only for a finding that
    carries none.
    """

    if finding.found_by_pattern:
        return "found by PR Warden's own pattern check, not by a model"
    cited = len(finding.evidence) + len(finding.counterevidence)
    matched = f"{cited} citation{'s' if cited != 1 else ''} matched this commit"
    confirmation = finding.confirmation or describe_confirmation(
        review_engine, review_model, verify_engine, verify_model
    )
    return f"{matched}; {confirmation}"


def describe_severity_change(finding: Finding) -> str:
    """Say how the verifier corrected a finding's severity and why, or nothing."""

    if not finding.original_severity:
        return ""
    raised = severity_rank(finding.severity) < severity_rank(finding.original_severity)
    return (
        f"the verifier {'raised' if raised else 'lowered'} this from "
        f"{finding.original_severity.upper()} to {finding.severity.upper()}: "
        f"{finding.severity_reason}"
    )
