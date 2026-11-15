"""A pull request that changes review policy gets findings, never an approval."""

import os
import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prwarden import cli
from prwarden.config import Settings
from prwarden.models import PullRequest, Repository
from prwarden.review import is_review_policy_path, review_policy_changes
from prwarden.state import JsonStateStore
from prwarden.workspace import MAX_BASE_FILE_BYTES, WorkspaceError, merge_base_commit
from tests.test_cli import CONFIRMED_JUDGMENT, READY_SNAPSHOT, finding_response
from tests.test_review_rules import without_review_rules


RULES = """# Agents

Run the tests.

## Code review

- Every query is parameterized.
"""
LOOSER_RULES = RULES.replace("Every query is parameterized.", "Anything goes.")
BASE_SHA = "b" * 40


def changed(path: str, **fields) -> dict:
    return {
        "filename": path,
        "status": "modified",
        "patch": "@@ -1 +1 @@\n+x",
        **fields,
    }


class PolicyPathTests(unittest.TestCase):
    def test_policy_files_are_named_whatever_they_say(self) -> None:
        for path in (
            ".prwarden/background.md",
            ".github/workflows/test.yml",
            "CODEOWNERS",
            ".github/CODEOWNERS",
            "docs/CODEOWNERS",
            ".gitattributes",
            "packages/app/.gitattributes",
        ):
            with self.subTest(path=path):
                self.assertTrue(is_review_policy_path(path))
        for path in (
            "src/app.py",
            ".github/dependabot.yml",
            "docs/README.md",
            "src/CODEOWNERS.md",
            "workflows/test.yml",
        ):
            with self.subTest(path=path):
                self.assertFalse(is_review_policy_path(path))


class RuleChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def head(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def changes(self, files, base=None):
        return review_policy_changes(files, base or {}, self.root)

    def test_an_edit_outside_the_code_review_section_is_not_policy(self) -> None:
        """The rest of an instruction file is documentation anyone may edit."""

        self.head("AGENTS.md", RULES.replace("Run the tests.", "Run every test."))

        self.assertEqual(
            self.changes([changed("AGENTS.md")], {"AGENTS.md": RULES}), ()
        )

    def test_an_edit_to_the_rules_themselves_is_policy(self) -> None:
        self.head("AGENTS.md", LOOSER_RULES)

        self.assertEqual(
            self.changes([changed("AGENTS.md")], {"AGENTS.md": RULES}),
            ("the code review rules in AGENTS.md",),
        )

    def test_new_and_removed_rules_are_policy(self) -> None:
        self.head(".kiro/steering/review.md", RULES)

        self.assertEqual(
            self.changes([changed(".kiro/steering/review.md", status="added")]),
            ("the code review rules in .kiro/steering/review.md",),
        )
        self.assertEqual(
            self.changes(
                [changed("CLAUDE.md", status="removed")], {"CLAUDE.md": RULES}
            ),
            ("the code review rules in CLAUDE.md",),
        )

    def test_a_rename_that_keeps_the_rules_is_not_policy(self) -> None:
        self.head(".github/copilot-instructions.md", RULES)

        self.assertEqual(
            self.changes(
                [
                    changed(
                        ".github/copilot-instructions.md",
                        status="renamed",
                        previous_filename="AGENTS.md",
                    )
                ],
                {"AGENTS.md": RULES},
            ),
            (),
        )

    def test_a_rename_off_or_onto_the_instruction_paths_moves_the_rules(
        self,
    ) -> None:
        """Later reviews read rules only from instruction files, so moving one away
        removes its rules even though its text is unchanged, and moving a file onto
        those paths adds its rules."""

        self.head("docs/AGENTS.md", RULES)
        self.head(".kiro/steering/review.md", RULES)

        self.assertEqual(
            self.changes(
                [
                    changed(
                        "docs/AGENTS.md",
                        status="renamed",
                        previous_filename="AGENTS.md",
                    )
                ],
                {"AGENTS.md": RULES},
            ),
            ("the code review rules in docs/AGENTS.md",),
        )
        self.assertEqual(
            self.changes(
                [
                    changed(
                        ".kiro/steering/review.md",
                        status="renamed",
                        previous_filename="docs/review.md",
                    )
                ],
                {"docs/review.md": RULES},
            ),
            ("the code review rules in .kiro/steering/review.md",),
        )

    def test_a_head_file_too_large_to_be_read_as_rules_holds_none(self) -> None:
        """Later reviews skip such a file, so its rules stop applying."""

        grown = RULES.replace("Run the tests.", "x" * MAX_BASE_FILE_BYTES)
        self.head("AGENTS.md", grown)

        self.assertEqual(
            self.changes([changed("AGENTS.md")], {"AGENTS.md": RULES}),
            ("the code review rules in AGENTS.md",),
        )

    def test_a_head_file_that_cannot_be_read_counts_as_changed(self) -> None:
        """Not knowing whether the rules moved must cost the approval."""

        (self.root / "AGENTS.md").write_bytes(b"\xff\xfe rules")

        self.assertEqual(
            self.changes([changed("AGENTS.md")], {"AGENTS.md": RULES}),
            ("the code review rules in AGENTS.md",),
        )

    def test_a_linked_instruction_file_holds_no_rule_either_side(self) -> None:
        """The base branch's rules come from regular files only."""

        self.head("AGENTS.md", RULES)
        os.symlink(self.root / "AGENTS.md", self.root / "CLAUDE.md")

        self.assertEqual(self.changes([changed("CLAUDE.md", status="added")]), ())

    def test_a_policy_file_is_named_under_its_old_name_too(self) -> None:
        self.assertEqual(
            self.changes(
                [
                    changed(
                        "ci/test.yml",
                        status="renamed",
                        previous_filename=".github/workflows/test.yml",
                    ),
                    changed("src/app.py"),
                ]
            ),
            (".github/workflows/test.yml",),
        )


class BaseReadTests(unittest.TestCase):
    REPOSITORY = Repository("app", "owner/app", Path("/tmp"))
    PULL_REQUEST = PullRequest(1, "T", "", "a", "h" * 40, "u", base_ref="main")
    MERGE_BASE = "a" * 40

    def test_the_base_side_is_read_only_for_a_changed_instruction_file(self) -> None:
        with patch("prwarden.cli.read_base_files") as read, patch(
            "prwarden.cli.merge_base_commit"
        ) as merge_base:
            changes = cli._review_policy_changes(
                self.REPOSITORY,
                self.PULL_REQUEST,
                [changed("src/app.py"), changed(".github/workflows/test.yml")],
                None,
                BASE_SHA,
            )

        read.assert_not_called()
        merge_base.assert_not_called()
        self.assertEqual(changes, (".github/workflows/test.yml",))

    def test_the_rules_are_compared_where_the_head_left_the_base(self) -> None:
        """A rule change merged since must not be blamed on every open pull request."""

        with patch(
            "prwarden.cli.read_base_files", return_value=[("AGENTS.md", RULES)]
        ) as read, patch(
            "prwarden.cli.merge_base_commit", return_value=self.MERGE_BASE
        ) as merge_base:
            cli._review_policy_changes(
                self.REPOSITORY,
                self.PULL_REQUEST,
                [changed("AGENTS.md")],
                None,
                BASE_SHA,
            )

        merge_base.assert_called_once_with(
            self.REPOSITORY, self.PULL_REQUEST.head_sha, BASE_SHA
        )
        self.assertEqual(read.call_args.args[1], self.MERGE_BASE)

    def test_a_replay_compares_with_its_pinned_base(self) -> None:
        with patch(
            "prwarden.cli.read_base_files", return_value=[("AGENTS.md", RULES)]
        ) as read, patch("prwarden.cli.merge_base_commit") as merge_base:
            cli._review_policy_changes(
                self.REPOSITORY,
                self.PULL_REQUEST,
                [changed("AGENTS.md")],
                None,
                diff_base=self.MERGE_BASE,
            )

        merge_base.assert_not_called()
        self.assertEqual(read.call_args.args[1], self.MERGE_BASE)

    def test_an_unreadable_base_counts_every_instruction_file_as_changed(
        self,
    ) -> None:
        with patch(
            "prwarden.cli.merge_base_commit", return_value=self.MERGE_BASE
        ), patch(
            "prwarden.cli.read_base_files", side_effect=WorkspaceError("gone")
        ), patch("sys.stdout", new_callable=StringIO) as output:
            changes = cli._review_policy_changes(
                self.REPOSITORY,
                self.PULL_REQUEST,
                [changed("AGENTS.md"), changed("CODEOWNERS")],
                None,
                BASE_SHA,
            )

        self.assertEqual(
            changes, ("CODEOWNERS", "the code review rules in AGENTS.md")
        )
        self.assertIn("so each counts as changed", output.getvalue())


class MergeBaseTests(unittest.TestCase):
    def test_the_merge_base_is_where_the_head_left_the_moving_tip(self) -> None:
        import subprocess

        def git(root: Path, *args: str) -> str:
            return subprocess.run(
                ["git", "-C", str(root), *args],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        with tempfile.TemporaryDirectory() as directory:
            origin = Path(directory) / "origin"
            origin.mkdir()
            git(origin, "init", "-q", "-b", "main")
            git(origin, "config", "user.email", "test@example.com")
            git(origin, "config", "user.name", "Test")
            (origin / "AGENTS.md").write_text(RULES, encoding="utf-8")
            git(origin, "add", "-A")
            git(origin, "commit", "-q", "-m", "base")
            fork = git(origin, "rev-parse", "HEAD")
            git(origin, "switch", "-q", "-c", "feature")
            (origin / "app.py").write_text("x = 1\n", encoding="utf-8")
            git(origin, "add", "-A")
            git(origin, "commit", "-q", "-m", "head")
            head = git(origin, "rev-parse", "HEAD")
            git(origin, "switch", "-q", "main")
            (origin / "AGENTS.md").write_text(LOOSER_RULES, encoding="utf-8")
            git(origin, "commit", "-q", "-am", "rules moved on")
            tip = git(origin, "rev-parse", "HEAD")
            checkout = Path(directory) / "checkout"
            subprocess.run(
                ["git", "clone", "-q", "--no-checkout", str(origin), str(checkout)],
                check=True,
            )

            found = merge_base_commit(
                Repository("app", "owner/app", checkout), head, tip
            )

        self.assertEqual(found, fork)


class OrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        without_review_rules(self)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "app.py").write_text("value = 1\n", encoding="utf-8")

    def approving_engine(self) -> Mock:
        engine = Mock()
        engine.review.return_value = {
            "verdict": "approve",
            "approval_reason": "No qualifying defects remain.",
            "findings": [],
        }
        return engine

    def run_review(self, engine, files, *, base_documents=(), force=False):
        repository = Repository("app", "owner/app", self.root)
        pull_request = PullRequest(
            1, "Title", "", "author", "head", "url", base_ref="main"
        )
        settings = Settings(
            reviewer_username="reviewer",
            repositories=[repository],
            dry_run=False,
            slack_webhook_url="https://example.invalid/webhook",
            state_file=self.root / "state.json",
            review_engine="antigravity",
            review_engine_command=("agy",),
            review_engine_env_allowlist=(),
            review_model=None,
            review_timeout_seconds=300,
            max_diff_bytes=100000,
            max_findings=10,
            design_level=False,
            docs_level=False,
        )
        with patch("prwarden.cli.has_current_review", return_value=False), patch(
            "prwarden.cli.list_prior_review_comments", return_value=()
        ), patch("prwarden.cli._read_generated_marks", return_value={}), patch(
            "prwarden.cli.list_changed_files", return_value=files
        ), patch(
            "prwarden.cli.list_pull_request_checks", return_value=()
        ), patch(
            "prwarden.cli._read_file_history", return_value=()
        ), patch(
            "prwarden.cli._read_linked_issues", return_value=()
        ), patch(
            "prwarden.cli.get_pull_request_snapshot",
            return_value=replace(READY_SNAPSHOT, changed_files=len(files)),
        ), patch(
            "prwarden.cli.read_base_files", return_value=list(base_documents)
        ), patch(
            "prwarden.cli.merge_base_commit", return_value="a" * 40
        ), patch(
            "prwarden.cli.review_workspace"
        ) as workspace, patch(
            "prwarden.cli.submit_review", return_value="review-1"
        ) as submit_review, patch(
            "prwarden.cli.submit_approval", return_value="approval-1"
        ) as submit_approval, patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            workspace.return_value.__enter__.return_value = repository
            workspace.return_value.dependency_links = ()
            workspace.return_value.dependency_notes = ()
            with JsonStateStore(settings.state_file) as store:
                cli._process_pull_request(
                    settings,
                    repository,
                    pull_request,
                    store,
                    engine,
                    live=True,
                    force=force,
                )
                record = store.get(store.key(repository.slug, 1, "head", "github"))
        return {
            "record": record or {},
            "terminal": output.getvalue(),
            "submit_review": submit_review,
            "submit_approval": submit_approval,
        }

    WORKFLOW_CHANGE = [
        {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"},
        {"filename": ".github/workflows/test.yml", "patch": "@@ -1 +1 @@\n+on: push"},
    ]

    def test_a_clean_policy_change_is_withheld_and_not_reviewed_again(self) -> None:
        engine = self.approving_engine()

        first = self.run_review(engine, self.WORKFLOW_CHANGE)

        first["submit_approval"].assert_not_called()
        self.assertEqual(first["record"]["status"], "approval_withheld")
        self.assertEqual(
            first["record"]["policy_changes"], [".github/workflows/test.yml"]
        )
        self.assertIn(
            "changes review policy, so a person must approve it: "
            ".github/workflows/test.yml",
            first["terminal"],
        )

        again = self.run_review(engine, self.WORKFLOW_CHANGE)

        self.assertEqual(engine.review.call_count, 1)
        self.assertIn(
            "approval stays withheld while this head changes review policy",
            again["terminal"],
        )

    def test_force_reviews_a_policy_change_again_and_still_withholds(self) -> None:
        engine = self.approving_engine()
        self.run_review(engine, self.WORKFLOW_CHANGE)

        forced = self.run_review(engine, self.WORKFLOW_CHANGE, force=True)

        self.assertEqual(engine.review.call_count, 2)
        forced["submit_approval"].assert_not_called()

    def test_findings_on_a_policy_change_are_still_delivered(self) -> None:
        engine = Mock()
        engine.review.side_effect = [finding_response("app.py", 1), CONFIRMED_JUDGMENT]

        result = self.run_review(engine, self.WORKFLOW_CHANGE)

        result["submit_review"].assert_called_once()
        result["submit_approval"].assert_not_called()
        self.assertEqual(result["record"]["status"], "delivered")

    def test_an_instruction_edit_that_keeps_the_rules_is_approved(self) -> None:
        (self.root / "AGENTS.md").write_text(
            RULES.replace("Run the tests.", "Run every test."), encoding="utf-8"
        )
        files = [
            {"filename": "AGENTS.md", "patch": "@@ -3 +3 @@\n+Run every test."},
        ]

        result = self.run_review(
            self.approving_engine(), files, base_documents=[("AGENTS.md", RULES)]
        )

        result["submit_approval"].assert_called_once()

    def test_an_edit_that_loosens_the_rules_is_never_approved(self) -> None:
        """Approving it would let the reviewer sign off its own new rules."""

        (self.root / "AGENTS.md").write_text(LOOSER_RULES, encoding="utf-8")
        files = [
            {"filename": "AGENTS.md", "patch": "@@ -7 +7 @@\n+- Anything goes."},
        ]

        result = self.run_review(
            self.approving_engine(), files, base_documents=[("AGENTS.md", RULES)]
        )

        result["submit_approval"].assert_not_called()
        self.assertEqual(
            result["record"]["policy_changes"], ["the code review rules in AGENTS.md"]
        )


if __name__ == "__main__":
    unittest.main()
