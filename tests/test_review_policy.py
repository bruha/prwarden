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
from prwarden.delivery import OutgoingSecretError
from prwarden.github import APPROVAL_COMMENT_MARKER
from prwarden.models import (
    PriorReviewComment,
    PullRequest,
    PullRequestCheck,
    Repository,
)
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

    def test_the_head_is_read_as_later_reviews_will_read_it(self) -> None:
        """Rules are read stripped, so a first line indented past a fence's three
        spaces opens a fence there and hides every heading after it."""

        self.head("AGENTS.md", "\t```\n" + RULES)

        self.assertEqual(
            self.changes([changed("AGENTS.md")], {"AGENTS.md": RULES}),
            ("the code review rules in AGENTS.md",),
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

    def run_review(
        self,
        engine,
        files,
        *,
        base_documents=(),
        force=False,
        prior=(),
        signoff=None,
        checks=(),
        checks_level=False,
        check_log="",
        approval_error=None,
    ):
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
            checks_level=checks_level,
        )
        with patch("prwarden.cli.has_current_review", return_value=False), patch(
            "prwarden.cli.list_prior_review_comments", return_value=tuple(prior)
        ), patch("prwarden.cli._read_generated_marks", return_value={}), patch(
            "prwarden.cli.list_changed_files", return_value=files
        ), patch(
            "prwarden.cli.list_pull_request_checks", return_value=checks
        ), patch(
            "prwarden.cli.read_check_log", return_value=check_log
        ) as read_log, patch(
            "prwarden.cli.check_requirements", return_value=None
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
            "prwarden.cli.submit_approval",
            return_value="approval-1",
            side_effect=approval_error,
        ) as submit_approval, patch(
            "prwarden.cli.submit_waiting_comment", return_value="comment-1"
        ) as submit_waiting, patch(
            "prwarden.cli.submit_waiting_review", return_value="review-9"
        ) as submit_waiting_review, patch(
            "prwarden.cli.policy_signoff", return_value=signoff
        ) as read_signoff, patch(
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
            "submit_waiting": submit_waiting,
            "submit_waiting_review": submit_waiting_review,
            "read_signoff": read_signoff,
            "read_log": read_log,
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

    TWO_POLICY_FILES = WORKFLOW_CHANGE + [
        {"filename": "CODEOWNERS", "patch": "@@ -1 +1,2 @@\n* @team\n+docs/ @docs"},
    ]

    def test_a_person_is_asked_on_the_first_line_the_policy_changes(self) -> None:
        """The note sits where the person asked to sign it off reads the change,
        names every other policy change, and is said once for the head."""

        engine = self.approving_engine()

        first = self.run_review(engine, self.TWO_POLICY_FILES)
        again = self.run_review(engine, self.TWO_POLICY_FILES)

        first["submit_waiting"].assert_not_called()
        first["submit_waiting_review"].assert_called_once()
        _, _, path, line, body = first["submit_waiting_review"].call_args.args
        self.assertEqual((path, line), (".github/workflows/test.yml", 1))
        self.assertIn("## Waiting for a person", body)
        self.assertIn("This is the first such change in this pull request", body)
        self.assertIn("- `CODEOWNERS`", body)
        self.assertNotIn("- `.github/workflows/test.yml`", body)
        self.assertIn("replying **LGTM** here", body)
        self.assertEqual(
            first["record"]["policy_anchor"],
            [".github/workflows/test.yml", 1, ".github/workflows/test.yml"],
        )
        again["submit_waiting_review"].assert_not_called()

    def test_a_head_withheld_before_the_note_existed_gets_it_on_the_next_run(
        self,
    ) -> None:
        """Every pull request already waiting when this shipped sits in the skip
        path, which no review reaches again, so the skip path says it too, on the
        recorded line, or on the pull request when the record names none."""

        engine = self.approving_engine()
        with patch(
            "prwarden.cli._deliver_waiting_note"
        ):  # As an installation without the note recorded it.
            self.run_review(engine, self.WORKFLOW_CHANGE)

        again = self.run_review(engine, self.WORKFLOW_CHANGE)

        self.assertEqual(engine.review.call_count, 1)
        again["submit_waiting_review"].assert_called_once()

        with JsonStateStore(self.root / "state.json") as store:
            key = store.key("owner/app", 1, "head", "github")
            record = store.get(key)
            del record["policy_anchor"]
            store.set_status(key, record.pop("status"), **record)
            store.data["records"].pop(key + ":waiting")
            store._save()
        older = self.run_review(engine, self.WORKFLOW_CHANGE)

        older["submit_waiting"].assert_called_once()
        self.assertIn(
            "replying **LGTM** on this pull request",
            older["submit_waiting"].call_args.args[2],
        )

    def test_a_persons_sign_off_reopens_the_head_and_it_is_approved(self) -> None:
        """Until a person signs the policy change off the review waits; after, the
        head is reviewed again and approved like any other."""

        engine = self.approving_engine()
        self.run_review(engine, self.WORKFLOW_CHANGE)

        waiting = self.run_review(engine, self.WORKFLOW_CHANGE)
        signed = self.run_review(engine, self.WORKFLOW_CHANGE, signoff="maintainer")

        self.assertEqual(engine.review.call_count, 2)
        waiting["submit_approval"].assert_not_called()
        self.assertIn("a person's sign-off reopens it", waiting["terminal"])
        # Read since the head was withheld, by PR Warden's clock.
        since = waiting["read_signoff"].call_args.args[2]
        self.assertIsInstance(since, str)
        self.assertIn(
            "@maintainer signed off the review-policy change; reconsidering",
            signed["terminal"],
        )
        signed["submit_approval"].assert_called_once()
        # The approval names whose sign-off let it through, and for which head.
        body = signed["submit_approval"].call_args.args[2]
        self.assertIn("@maintainer signed off its review-policy change", body)
        self.assertIn("`head`", body)
        self.assertIn(APPROVAL_COMMENT_MARKER, body)
        self.assertEqual(signed["record"]["status"], "delivered")

    def test_a_delivery_refused_for_a_secret_is_settled_and_not_spent_again(
        self,
    ) -> None:
        """The same review would write the same text, so the head is not retried,
        and the refusal reaches the operator as the run's error."""

        engine = self.approving_engine()
        refusal = OutgoingSecretError("PR Warden did not post to GitHub: a secret")
        files = [{"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}]

        first = None
        with self.assertRaises(OutgoingSecretError) as raised:
            first = self.run_review(engine, files, approval_error=refusal)
        with JsonStateStore(self.root / "state.json") as store:
            record = store.get(store.key("owner/app", 1, "head", "github"))
        again = self.run_review(engine, files)

        self.assertIsNone(first)
        self.assertIs(raised.exception, refusal)
        self.assertEqual(record["status"], "delivery_blocked")
        self.assertEqual(engine.review.call_count, 1)
        again["submit_approval"].assert_not_called()

    def test_an_approval_of_the_head_signs_it_off_on_the_first_review(self) -> None:
        engine = self.approving_engine()

        result = self.run_review(engine, self.WORKFLOW_CHANGE, signoff="maintainer")

        # With no withheld review yet, only an approval of this head can count.
        self.assertIsNone(result["read_signoff"].call_args.args[2])
        self.assertIn(
            "@maintainer signed off this head's review-policy change",
            result["terminal"],
        )
        result["submit_approval"].assert_called_once()
        self.assertIn(
            "@maintainer signed off",
            result["submit_approval"].call_args.args[2],
        )
        result["submit_waiting_review"].assert_not_called()

    FAILED_CHECK = (
        PullRequestCheck(
            "compile-code", "completed", "failure", check_id=5, app="github-actions"
        ),
    )
    CHECK_LOG = "2026-11-20T10:00:01Z app.py:1: compile error: value is never read\n"
    DIAGNOSIS = {
        "diagnoses": [
            {
                "check": "compile-code",
                "category": "code_defect",
                "log_excerpt": "app.py:1: compile error: value is never read",
                "cause": "The compiler rejects a value the change never reads.",
                "fix": "Read `value` where it is needed, or remove it.",
                "citations": [],
            }
        ]
    }

    def test_a_signed_off_head_has_its_failed_checks_read(self) -> None:
        """Signed off, the head waits for its checks alone, so the note that says
        so carries the reading like any other; the sign-off is read once."""

        engine = Mock()
        engine.review.side_effect = [
            self.approving_engine().review.return_value,
            self.DIAGNOSIS,
        ]

        result = self.run_review(
            engine,
            self.WORKFLOW_CHANGE,
            signoff="maintainer",
            checks=self.FAILED_CHECK,
            checks_level=True,
            check_log=self.CHECK_LOG,
        )

        self.assertEqual(engine.review.call_count, 2)
        self.assertEqual(engine.review.call_args_list[1].args[2].level, "checks")
        result["read_signoff"].assert_called_once()
        result["submit_approval"].assert_not_called()
        body = result["submit_waiting"].call_args.args[2]
        self.assertIn("## Waiting for checks", body)
        self.assertIn("### Why they failed", body)

    def test_a_policy_change_nobody_signed_off_spends_no_reading(self) -> None:
        """It waits for a person first, and its note says so, not why checks fail."""

        engine = self.approving_engine()

        result = self.run_review(
            engine,
            self.WORKFLOW_CHANGE,
            checks=self.FAILED_CHECK,
            checks_level=True,
            check_log=self.CHECK_LOG,
        )

        self.assertEqual(engine.review.call_count, 1)
        result["read_log"].assert_not_called()
        result["read_signoff"].assert_called_once()
        self.assertEqual(result["record"]["status"], "approval_withheld")

    def test_a_signed_off_head_withheld_for_more_reopens_once_that_clears(
        self,
    ) -> None:
        """A head withheld over the policy and an unresolved comment still has the
        comment to answer; once it is resolved the sign-off lets it through."""

        engine = Mock()
        engine.review.side_effect = [
            {
                "verdict": "do_not_approve",
                "approval_reason": "The unresolved comment on app.py still applies.",
                "blocking_comment_ids": ["C1"],
                "findings": [],
            },
            {
                "verdict": "approve",
                "approval_reason": "No qualifying defects remain.",
                "findings": [],
            },
        ]
        comment = PriorReviewComment(
            "inline:5", "inline", "colleague", "This still fails.", "app.py", 1, False
        )

        first = self.run_review(engine, self.WORKFLOW_CHANGE, prior=(comment,))
        resolved = replace(comment, resolved=True)
        signed = self.run_review(
            engine, self.WORKFLOW_CHANGE, prior=(resolved,), signoff="maintainer"
        )

        self.assertEqual(
            first["record"]["policy_changes"], [".github/workflows/test.yml"]
        )
        first["submit_waiting_review"].assert_not_called()
        self.assertEqual(engine.review.call_count, 2)
        signed["submit_approval"].assert_called_once()

    def test_a_sign_off_never_outweighs_a_finding(self) -> None:
        engine = Mock()
        engine.review.side_effect = [finding_response("app.py", 1), CONFIRMED_JUDGMENT]

        result = self.run_review(engine, self.WORKFLOW_CHANGE, signoff="maintainer")

        result["submit_review"].assert_called_once()
        result["submit_approval"].assert_not_called()

    def test_a_policy_change_withheld_for_another_reason_gets_no_note(self) -> None:
        """Only a review that would have approved waits for a person alone."""

        engine = Mock()
        engine.review.return_value = {
            "verdict": "do_not_approve",
            "approval_reason": "The unresolved comment on app.py still applies.",
            "blocking_comment_ids": ["C1"],
            "findings": [],
        }
        unresolved = PriorReviewComment(
            "inline:5", "inline", "colleague", "This still fails.", "app.py", 1, False
        )

        result = self.run_review(engine, self.WORKFLOW_CHANGE, prior=(unresolved,))

        self.assertEqual(result["record"]["status"], "approval_withheld")
        result["submit_waiting"].assert_not_called()

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
