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
they were meant to inform.
"""

from dataclasses import dataclass, replace
from typing import Optional

from .config import ConfigError, Settings
from .engine import CODE_LEVEL, DESIGN_LEVEL, DOCS_LEVEL

# Every type there is, in the order a run does them unless told otherwise.
REVIEW_TYPES = (DESIGN_LEVEL, DOCS_LEVEL, CODE_LEVEL)


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
        if CODE_LEVEL in names and names[-1] != CODE_LEVEL:
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
    return ReviewPlan(tuple(types))
