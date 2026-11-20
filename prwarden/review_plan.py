"""Which types of review a run does, in what order, and on which engines.

A review type is one kind of review with its own prompt, answer schema, validation,
and delivery: the code review, which alone can approve, and the advisory
solution-design and documentation reviews. Which types exist is code, because each
needs all of those; which of them run, in what order, and on which engines is
configuration.

Each type reviews and verifies with engines of its own. A type's engines are carried
as a copy of the run's settings with that type's reviewer, verifier, and their
fallbacks in the role fields, because every adapter, every hand-over, and every line
that names an engine already reads it from there.

The code review always runs last. Its approval is what a person or an agent merges
on, so the advisory notes are on the pull request before it, never after a merge
they were meant to inform. The checks type is not ordered with the others: it reads
why the failed checks failed, so it runs only after a code review that found nothing
and would approve but for them, and its reading goes into the note that says the
review waits for them.
"""

from dataclasses import dataclass, replace
from typing import Optional

from .config import ConfigError, Settings
from .engine import CHECKS_LEVEL, CODE_LEVEL, DESIGN_LEVEL, DOCS_LEVEL

# Every type there is, in the order a run does them unless told otherwise.
REVIEW_TYPES = (DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL, CHECKS_LEVEL)
# The types that run where the plan puts them; the checks type runs when it is due.
ORDERED_TYPES = (DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL)


@dataclass(frozen=True)
class ReviewType:
    """One type of review, and the settings that name its engines."""

    name: str
    settings: Settings


@dataclass(frozen=True)
class ReviewPlan:
    """The types of review a run does, in the order it does them."""

    types: "tuple[ReviewType, ...]"

    def __post_init__(self) -> None:
        names = self.names
        unknown = [name for name in names if name not in REVIEW_TYPES]
        if unknown:
            raise ConfigError(
                f"Unknown review type {unknown[0]!r}; the types are "
                + ", ".join(REVIEW_TYPES)
            )
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ConfigError(f"Review type {repeated[0]!r} is listed twice")
        ordered = [name for name in names if name in ORDERED_TYPES]
        if CODE_LEVEL in ordered and ordered[-1] != CODE_LEVEL:
            raise ConfigError(
                "The code review must run last: its approval is what a pull request "
                "is merged on, so the advisory notes come before it"
            )

    @property
    def names(self) -> "tuple[str, ...]":
        return tuple(review_type.name for review_type in self.types)

    def get(self, name: str) -> Optional[ReviewType]:
        for review_type in self.types:
            if review_type.name == name:
                return review_type
        return None


def review_plan(settings: Settings) -> ReviewPlan:
    """Return the plan a run's settings describe.

    The design and documentation reviews run when enabled, then the code review. All
    three share the run's engines; only the code review's reasoning effort may
    differ, so its settings carry that effort as the reviewer's own, which is the
    effort every code-level call, its verification on the same engine included, has
    always run at.
    """

    advisory = replace(settings, review_code_effort=None)
    types = []
    if settings.design_level:
        types.append(ReviewType(DESIGN_LEVEL, advisory))
    if settings.docs_level:
        types.append(ReviewType(DOCS_LEVEL, advisory))
    types.append(
        ReviewType(
            CODE_LEVEL,
            replace(
                settings,
                review_effort=settings.review_code_effort or settings.review_effort,
                review_code_effort=None,
            ),
        )
    )
    if settings.checks_level:
        types.append(ReviewType(CHECKS_LEVEL, checks_settings(settings)))
    return ReviewPlan(tuple(types))


def checks_settings(settings: Settings) -> Settings:
    """Return the settings the checks type reads failed checks with.

    The review engine, at the checks type's own model and effort when set. No
    verifier and no fallback: the reading is checked by the controller, every quoted
    log line and cited workspace line found where it claims to be, and a reading
    that fails costs only itself, since the note it would join is posted without it.
    """

    return replace(
        settings,
        review_model=settings.checks_model or settings.review_model,
        review_effort=settings.checks_effort or settings.review_effort,
        review_code_effort=None,
        verify_engine=None,
        verify_engine_command=(),
        verify_model=None,
        verify_effort=None,
        fallback_engine=None,
        fallback_engine_command=(),
        fallback_model=None,
        fallback_effort=None,
        verify_fallback_engine=None,
        verify_fallback_engine_command=(),
        verify_fallback_model=None,
        verify_fallback_effort=None,
    )
