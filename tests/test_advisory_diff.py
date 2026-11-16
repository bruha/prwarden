import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prwarden import cli
from prwarden.config import Settings
from prwarden.engine import DESIGN_LEVEL, DOCS_LEVEL, EngineError, build_prompt
from prwarden.models import PullRequest, Repository, ReviewMaterial
from prwarden.review import without_generated_files


def section(path: str, *added: str) -> str:
    lines = "\n".join(f"+{line}" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(added)} @@\n{lines}\n"
    )


LOCKFILE = "web/package-lock.json"
DIFF = "\n".join(
    (
        section("src/app.ts", "export const ready = true;"),
        section(LOCKFILE, '"resolved": "https://registry.example/left-pad.tgz",'),
        section("README.md", "Run the app with `npm start`."),
    )
)
MATERIAL = ReviewMaterial(
    DIFF,
    {
        "src/app.ts": frozenset({1}),
        LOCKFILE: frozenset({1}),
        "README.md": frozenset({1}),
    },
)
REPOSITORY = Repository("app", "owner/app", Path("/tmp"))
PULL_REQUEST = PullRequest(1, "Title", "", "author", "sha", "url")


class WithoutGeneratedFilesTests(unittest.TestCase):
    def test_a_lockfile_section_is_left_out_and_named(self) -> None:
        trimmed = without_generated_files(MATERIAL)

        self.assertNotIn("left-pad", trimmed.diff)
        self.assertIn(section("src/app.ts", "export const ready = true;"), trimmed.diff)
        self.assertIn("Run the app with `npm start`.", trimmed.diff)
        self.assertEqual(trimmed.generated_files_left_out, (LOCKFILE,))
        # Only the text an advisory level reads changes; what a finding may cite
        # stays the controller's own map of the whole change.
        self.assertEqual(trimmed.added_lines, MATERIAL.added_lines)

    def test_a_diff_without_generated_files_is_returned_unchanged(self) -> None:
        material = ReviewMaterial(
            section("src/app.ts", "x"), {"src/app.ts": frozenset({1})}
        )

        self.assertIs(without_generated_files(material), material)

    def test_a_path_that_only_contains_a_lockfile_name_is_kept(self) -> None:
        material = ReviewMaterial(
            section("docs/package-lock.json.md", "Why the lockfile is committed."),
            {"docs/package-lock.json.md": frozenset({1})},
        )

        self.assertIs(without_generated_files(material), material)

    def test_the_advisory_prompts_name_the_lockfile_and_the_code_prompt_reads_it(self):
        """A lockfile's diff can outweigh the rest of a change, and every advisory
        call paid for it on each of its turns; the code level can still report a
        resolved source, so only the advisory levels leave it out."""

        trimmed = without_generated_files(MATERIAL)
        for level in (DESIGN_LEVEL, DOCS_LEVEL):
            with self.subTest(level):
                prompt = build_prompt(
                    REPOSITORY, PULL_REQUEST, replace(trimmed, level=level), 10
                )
                self.assertNotIn("left-pad", prompt)
                self.assertIn("whose diff\nis left out below", prompt)
                self.assertIn(f"\n{LOCKFILE}\n", prompt)

        code_prompt = build_prompt(REPOSITORY, PULL_REQUEST, MATERIAL, 10)
        self.assertIn("left-pad", code_prompt)
        self.assertNotIn("whose diff\nis left out below", code_prompt)


class AdvisoryLevelTests(unittest.TestCase):
    def settings(self) -> Settings:
        return Settings(
            reviewer_username="reviewer",
            repositories=[],
            dry_run=True,
            slack_webhook_url=None,
            state_file=Path("/tmp/state.json"),
            review_engine="antigravity",
            review_engine_command=("agy",),
            review_engine_env_allowlist=(),
            review_model=None,
            review_timeout_seconds=300,
            max_diff_bytes=100000,
            max_findings=10,
        )

    def test_both_advisory_levels_review_the_diff_without_the_lockfile(self) -> None:
        for name, level in (("design", cli._design_level), ("docs", cli._docs_level)):
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                repository = Repository("app", "owner/app", Path(directory))
                engine = Mock()
                engine.review.side_effect = EngineError("stop after the first call")
                with patch("sys.stdout", new_callable=StringIO):
                    with self.assertRaises(Exception):
                        level(
                            self.settings(), engine, None, repository, PULL_REQUEST,
                            MATERIAL,
                        )

                reviewed = engine.review.call_args.args[2]
                self.assertNotIn("left-pad", reviewed.diff)
                self.assertEqual(reviewed.generated_files_left_out, (LOCKFILE,))


if __name__ == "__main__":
    unittest.main()
