import json
import tempfile
import unittest
from pathlib import Path

from src.comment_template import (
    DEFAULT_GITHUB_COMMENT_TAGS_FILE,
    DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE,
    CommentTemplateError,
    load_comment_template,
)


class CommentTemplateTests(unittest.TestCase):
    def write_files(self, template: str, tags: dict[str, str]):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        template_file = root / "comment.md"
        tags_file = root / "tags.json"
        template_file.write_text(template, encoding="utf-8")
        tags_file.write_text(json.dumps(tags), encoding="utf-8")
        return template_file, tags_file

    def values(self) -> dict[str, str]:
        return {
            "severity": "HIGH",
            "title": "Data loss",
            "path": "src/app.py",
            "line": "42",
            "problem": "Data is discarded.",
            "trigger": "Save a replacement.",
            "impact": "Stored data is lost.",
            "evidence": "- `src/app.py:42` — replacement",
            "counterevidence_reason": "No recovery path exists.",
            "suggested_change": "Preserve the prior value.",
            "reviewer_attribution": "Reviewed by Model @ Engine",
            "reviewer_identity": "Model @ Engine",
            "engine": "Engine",
            "model": "Model",
            "verifier_identity": "Verify Model @ Verify Engine",
            "verify_engine": "Verify Engine",
            "verify_model": "Verify Model",
            "marker": "<!-- prwarden -->",
        }

    def test_default_files_load_and_render_all_current_sections(self):
        template = load_comment_template(
            DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE,
            DEFAULT_GITHUB_COMMENT_TAGS_FILE,
        )

        rendered = template.render(self.values())

        self.assertTrue(rendered.startswith("**[HIGH] Data loss**"))
        self.assertIn("**Trigger:** Save a replacement.", rendered)
        self.assertIn("**Evidence:**\n- `src/app.py:42`", rendered)
        self.assertIn("_Reviewed by Model @ Engine_", rendered)
        self.assertTrue(rendered.endswith("<!-- prwarden -->"))

    def test_custom_registry_can_add_aliases_and_rearrange_sections(self):
        template_file, tags_file = self.write_files(
            "[location]\n[priority]: [problem]\n[author]\n[identity]",
            {
                "location": "path",
                "priority": "severity",
                "problem": "problem",
                "author": "reviewer_attribution",
                "identity": "marker",
            },
        )
        template = load_comment_template(template_file, tags_file)

        rendered = template.render(self.values())

        self.assertEqual(
            rendered,
            "src/app.py\nHIGH: Data is discarded.\n"
            "Reviewed by Model @ Engine\n<!-- prwarden -->",
        )

    def test_unknown_template_tag_is_rejected(self):
        template_file, tags_file = self.write_files(
            "[problem]\n[unknown]\n[marker]",
            {"problem": "problem", "marker": "marker"},
        )

        with self.assertRaisesRegex(CommentTemplateError, "unknown tags"):
            load_comment_template(template_file, tags_file)

    def test_backslash_escaped_bracket_label_remains_literal(self):
        template_file, tags_file = self.write_files(
            "[problem]\n\\[details]\n[marker]",
            {"problem": "problem", "marker": "marker"},
        )
        template = load_comment_template(template_file, tags_file)

        rendered = template.render(self.values())

        self.assertIn("\\[details]", rendered)

    def test_unsupported_registry_field_is_rejected(self):
        template_file, tags_file = self.write_files(
            "[custom]\n[marker]",
            {"custom": "arbitrary_code", "marker": "marker"},
        )

        with self.assertRaisesRegex(CommentTemplateError, "unsupported field"):
            load_comment_template(template_file, tags_file)

    def test_exactly_one_marker_field_is_required(self):
        template_file, tags_file = self.write_files(
            "[problem]",
            {"problem": "problem", "marker": "marker"},
        )

        with self.assertRaisesRegex(CommentTemplateError, "exactly one"):
            load_comment_template(template_file, tags_file)


if __name__ == "__main__":
    unittest.main()
