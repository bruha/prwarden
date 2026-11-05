"""Validated templates for GitHub inline review comments."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


# The defaults ship inside the package, so an installed copy carries them as well.
TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / "templates"
DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE = TEMPLATE_DIRECTORY / "github-comment.md"
DEFAULT_GITHUB_COMMENT_TAGS_FILE = TEMPLATE_DIRECTORY / "github-comment-tags.json"
MAX_TEMPLATE_BYTES = 100_000
MAX_RENDERED_COMMENT_BYTES = 60_000
TAG_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
TEMPLATE_TAG_PATTERN = re.compile(r"(?<!\\)\[([a-z][a-z0-9-]*)\]")

SUPPORTED_COMMENT_FIELDS = frozenset(
    {
        "severity",
        "title",
        "path",
        "line",
        "problem",
        "rule",
        "trigger",
        "impact",
        "evidence",
        "counterevidence_reason",
        "suggested_change",
        "reviewer_attribution",
        "reviewer_identity",
        "engine",
        "model",
        "verifier_identity",
        "verify_engine",
        "verify_model",
        "marker",
    }
)


class CommentTemplateError(ValueError):
    """Raised when a comment template or tag registry is invalid."""


@dataclass(frozen=True)
class CommentTemplate:
    text: str
    tags: Mapping[str, str]
    source: Path
    tags_source: Path

    def render(self, values: Mapping[str, str]) -> str:
        missing_values = sorted(set(self.tags.values()) - set(values))
        if missing_values:
            raise CommentTemplateError(
                "Comment renderer is missing values for: "
                + ", ".join(missing_values)
            )

        def replace(match: re.Match[str]) -> str:
            tag_name = match.group(1)
            field_name = self.tags.get(tag_name)
            if field_name is None:
                raise CommentTemplateError(
                    f"Unknown comment template tag [{tag_name}] in {self.source}"
                )
            return values[field_name]

        rendered = TEMPLATE_TAG_PATTERN.sub(replace, self._without_empty_lines(values))
        rendered = rendered.strip()
        if not rendered:
            raise CommentTemplateError(
                f"Comment template {self.source} rendered an empty comment"
            )
        rendered_bytes = len(rendered.encode("utf-8"))
        if rendered_bytes > MAX_RENDERED_COMMENT_BYTES:
            raise CommentTemplateError(
                f"Rendered GitHub comment is {rendered_bytes} bytes; maximum is "
                f"{MAX_RENDERED_COMMENT_BYTES}"
            )
        return rendered

    def _without_empty_lines(self, values: Mapping[str, str]) -> str:
        """Return the template without the lines of tags that have nothing to say.

        A tag alone on its line whose value is empty, such as the rule of a finding
        that breaks none, takes its line and the blank line after it, so an optional
        section leaves no gap. Values are substituted afterwards, so text inside one
        is never touched.
        """

        kept: "list[str]" = []
        skip_blank = False
        for line in self.text.split("\n"):
            alone = TEMPLATE_TAG_PATTERN.fullmatch(line.strip())
            field_name = self.tags.get(alone.group(1)) if alone else None
            if field_name is not None and values.get(field_name) == "":
                skip_blank = True
                continue
            if skip_blank and not line.strip():
                skip_blank = False
                continue
            skip_blank = False
            kept.append(line)
        return "\n".join(kept)


def _read_bounded_text(path: Path, description: str) -> str:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise CommentTemplateError(
            f"Cannot inspect {description} {path}: {error}"
        ) from error
    if size > MAX_TEMPLATE_BYTES:
        raise CommentTemplateError(
            f"{description.capitalize()} {path} exceeds {MAX_TEMPLATE_BYTES} bytes"
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise CommentTemplateError(
            f"Cannot read {description} {path}: {error}"
        ) from error


def load_comment_template(template_path: Path, tags_path: Path) -> CommentTemplate:
    template_text = _read_bounded_text(template_path, "comment template")
    if not template_text.strip():
        raise CommentTemplateError(f"Comment template {template_path} is empty")

    tags_text = _read_bounded_text(tags_path, "comment tag registry")
    try:
        payload = json.loads(tags_text)
    except json.JSONDecodeError as error:
        raise CommentTemplateError(
            f"Invalid JSON in comment tag registry {tags_path}: {error}"
        ) from error
    if not isinstance(payload, dict) or not payload:
        raise CommentTemplateError(
            f"Comment tag registry {tags_path} must be a non-empty JSON object"
        )

    tags: dict[str, str] = {}
    for tag_name, field_name in payload.items():
        if not isinstance(tag_name, str) or not TAG_NAME_PATTERN.fullmatch(tag_name):
            raise CommentTemplateError(
                f"Invalid tag name {tag_name!r} in {tags_path}; use lowercase letters, "
                "digits, and hyphens"
            )
        if (
            not isinstance(field_name, str)
            or field_name not in SUPPORTED_COMMENT_FIELDS
        ):
            raise CommentTemplateError(
                f"Tag {tag_name!r} in {tags_path} maps to unsupported field "
                f"{field_name!r}"
            )
        tags[tag_name] = field_name

    referenced_tags = TEMPLATE_TAG_PATTERN.findall(template_text)
    unknown_tags = sorted(set(referenced_tags) - set(tags))
    if unknown_tags:
        raise CommentTemplateError(
            f"Comment template {template_path} uses unknown tags: "
            + ", ".join(f"[{name}]" for name in unknown_tags)
        )
    marker_uses = sum(tags[name] == "marker" for name in referenced_tags)
    if marker_uses != 1:
        raise CommentTemplateError(
            f"Comment template {template_path} must use exactly one tag mapped to marker"
        )

    return CommentTemplate(
        text=template_text,
        tags=tags,
        source=template_path,
        tags_source=tags_path,
    )
