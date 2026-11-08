"""A repository's own `linguist-generated` marks, read from its base branch."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prwarden import cli
from prwarden.commands import CommandError
from prwarden.models import PullRequest, Repository
from prwarden.review import build_review_material, omitted_patch_paths
from prwarden.workspace import (
    UnsupportedGitError,
    WorkspaceError,
    read_generated_marks,
)

PATCH = "@@ -0,0 +1 @@\n+value = 1"
OMITTED = {"status": "modified", "additions": 9000, "deletions": 10, "changes": 9010}


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def omitted(path: str) -> dict:
    return {"filename": path, **OMITTED}


class ReadGeneratedMarksTests(unittest.TestCase):
    """Git's own matching, against the base commit and nothing else."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.origin = root / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        git(self.origin, "config", "user.email", "test@example.com")
        git(self.origin, "config", "user.name", "Test")
        (self.origin / ".gitattributes").write_text(
            "src/api/generated/** linguist-generated=true\n"
            "*.pb.go linguist-generated\n"
            "package-lock.json -linguist-generated\n"
            "data/*.csv linguist-generated=false\n",
            encoding="utf-8",
        )
        nested = self.origin / "assets"
        nested.mkdir()
        (nested / ".gitattributes").write_text("icons.svg linguist-generated\n")
        (self.origin / "app.py").write_text("print('x')\n")
        git(self.origin, "add", "-A")
        git(self.origin, "commit", "-q", "-m", "base")
        self.base = git(self.origin, "rev-parse", "HEAD")
        # The head marks its own new file generated; only the base may say so.
        with (self.origin / ".gitattributes").open("a", encoding="utf-8") as marks:
            marks.write("src/payments.ts linguist-generated\n")
        git(self.origin, "commit", "-q", "-am", "head")
        self.checkout = root / "checkout"
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.checkout)],
            check=True,
        )
        # The operator's own clone may sit on any branch with any edits.
        (self.checkout / ".gitattributes").write_text("* linguist-generated\n")
        self.repository = Repository("app", "acme/app", self.checkout)

    def test_the_base_commit_decides_each_mark(self) -> None:
        marks = read_generated_marks(
            self.repository,
            self.base,
            (
                "src/api/generated/client.ts",
                "proto/user.pb.go",
                "assets/icons.svg",
                "package-lock.json",
                "data/prices.csv",
                "src/payments.ts",
                "src/app.ts",
            ),
        )

        self.assertEqual(
            marks,
            {
                "src/api/generated/client.ts": True,
                "proto/user.pb.go": True,
                "assets/icons.svg": True,
                "package-lock.json": False,
                "data/prices.csv": False,
                "src/payments.ts": None,
                "src/app.ts": None,
            },
        )

    def test_no_paths_reads_nothing(self) -> None:
        with patch("prwarden.workspace.run_cmd") as run:
            self.assertEqual(read_generated_marks(self.repository, self.base, ()), {})
        run.assert_not_called()

    def test_a_commit_that_cannot_be_read_is_an_error(self) -> None:
        with self.assertRaises(WorkspaceError):
            read_generated_marks(self.repository, "b" * 40, ("src/app.ts",))

    def test_a_git_that_cannot_read_a_commits_attributes_says_so(self) -> None:
        """Before 2.40 `check-attr` has no `--source`, and without it Git would
        read the checkout's working tree, which is not the base branch."""

        with patch("prwarden.workspace._git_version", return_value=(2, 39)):
            with self.assertRaisesRegex(UnsupportedGitError, "Git 2.39"):
                read_generated_marks(self.repository, self.base, ("src/app.ts",))


class MaterialTests(unittest.TestCase):
    """The marks decide only what GitHub would not show."""

    def test_a_file_marked_generated_is_ignored_when_its_patch_is_omitted(self):
        material = build_review_material(
            [
                omitted("src/api/generated/client.ts"),
                omitted("src/legacy.ts"),
                {"filename": "app.py", "patch": PATCH},
            ],
            10000,
            ignored_missing_patch_patterns=(),
            generated_marks={
                "src/api/generated/client.ts": True,
                "src/legacy.ts": None,
            },
        )

        self.assertEqual(
            material.ignored_missing_patch_files, ("src/api/generated/client.ts",)
        )
        self.assertEqual(
            [file.path for file in material.diff_budget.unreadable],
            ["src/legacy.ts"],
        )

    def test_a_file_marked_not_generated_stays_unread_whatever_the_patterns(self):
        """A repository can only add review by unmarking a file, never remove it."""

        material = build_review_material(
            [omitted("package-lock.json"), {"filename": "app.py", "patch": PATCH}],
            10000,
            ignored_missing_patch_patterns=("package-lock.json",),
            generated_marks={"package-lock.json": False},
        )

        self.assertEqual(material.ignored_missing_patch_files, ())
        self.assertEqual(
            [file.path for file in material.diff_budget.unreadable],
            ["package-lock.json"],
        )

    def test_marks_that_could_not_be_read_keep_every_omitted_file_unread(self):
        material = build_review_material(
            [omitted("package-lock.json"), {"filename": "app.py", "patch": PATCH}],
            10000,
            ignored_missing_patch_patterns=("package-lock.json",),
            marks_unreadable=True,
        )

        self.assertEqual(material.ignored_missing_patch_files, ())
        self.assertEqual(
            [file.path for file in material.diff_budget.unreadable],
            ["package-lock.json"],
        )

    def test_a_mark_never_hides_a_patch_github_showed(self):
        material = build_review_material(
            [{"filename": "src/api/generated/client.ts", "patch": PATCH}],
            10000,
            generated_marks={"src/api/generated/client.ts": True},
        )

        self.assertEqual(material.ignored_missing_patch_files, ())
        self.assertIn("src/api/generated/client.ts", material.diff)

    def test_only_files_github_would_not_show_are_asked_about(self):
        files = [
            omitted("src/legacy.ts"),
            {"filename": "app.py", "patch": PATCH},
            {"filename": "logo.png", "status": "added", "additions": 0,
             "deletions": 0, "changes": 0},
            {"filename": "new.ts", "previous_filename": "old.ts",
             "status": "renamed", "additions": 0, "deletions": 0},
            {"filename": "gone.ts", "status": "removed", "additions": 0,
             "deletions": 40},
        ]

        self.assertEqual(omitted_patch_paths(files), ("src/legacy.ts",))


class LookupTests(unittest.TestCase):
    """The controller reads the base branch's current tip, and survives not being
    able to: the omitted files then stay unread, which withholds the approval."""

    def setUp(self) -> None:
        self.repository = Repository("app", "acme/app", Path("."))
        self.pull_request = PullRequest(
            7, "Title", "", "author", "a" * 40, "url", base_ref="main"
        )

    def test_the_marks_come_from_the_base_branch_tip(self) -> None:
        with patch(
            "prwarden.cli.get_branch_head_sha", return_value="c" * 40
        ) as tip, patch(
            "prwarden.cli.read_generated_marks", return_value={"gen.ts": True}
        ) as read:
            marks = cli._read_generated_marks(
                self.repository, self.pull_request, ("gen.ts",)
            )

        tip.assert_called_once_with(self.repository, "main")
        read.assert_called_once_with(self.repository, "c" * 40, ("gen.ts",))
        self.assertEqual(marks, {"gen.ts": True})

    def test_nothing_omitted_asks_nothing(self) -> None:
        with patch("prwarden.cli.get_branch_head_sha") as tip:
            self.assertEqual(
                cli._read_generated_marks(self.repository, self.pull_request, ()), {}
            )
        tip.assert_not_called()

    def test_marks_that_cannot_be_read_count_as_none_and_say_so(self) -> None:
        for failure in (
            CommandError("gh exited with 1: HTTP 502"),
            WorkspaceError("Cannot read acme/app at cccccccccccc"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch(
                    "prwarden.cli.get_branch_head_sha", return_value="c" * 40
                ), patch(
                    "prwarden.cli.read_generated_marks", side_effect=failure
                ), patch("sys.stdout") as output:
                    marks = cli._read_generated_marks(
                        self.repository, self.pull_request, ("gen.ts",)
                    )
                self.assertIsNone(marks)
                printed = "".join(
                    call.args[0] for call in output.write.call_args_list
                )
                self.assertIn("Could not read .gitattributes on main", printed)

    def test_an_old_git_leaves_the_decision_to_the_ignore_list(self) -> None:
        """A lasting fact of the installation, not a failure: the review behaves
        as it did before marks were read, rather than withholding every approval
        over a lockfile on a machine that cannot read them."""

        with patch(
            "prwarden.cli.get_branch_head_sha", return_value="c" * 40
        ), patch(
            "prwarden.cli.read_generated_marks",
            side_effect=UnsupportedGitError("Git 2.34 cannot read attributes"),
        ), patch("sys.stdout") as output:
            marks = cli._read_generated_marks(
                self.repository, self.pull_request, ("gen.ts",)
            )

        self.assertEqual(marks, {})
        printed = "".join(call.args[0] for call in output.write.call_args_list)
        self.assertIn("Not reading .gitattributes on main", printed)

    def test_a_base_tip_that_is_not_an_object_name_is_refused(self) -> None:
        with patch(
            "prwarden.cli.get_branch_head_sha", return_value="--output=/tmp/x"
        ), patch("prwarden.cli.read_generated_marks") as read, patch("sys.stdout"):
            marks = cli._read_generated_marks(
                self.repository, self.pull_request, ("gen.ts",)
            )

        self.assertIsNone(marks)
        read.assert_not_called()

    def test_each_unread_file_records_what_the_base_said(self) -> None:
        self.assertEqual(
            cli._generated_mark_states(
                {"a.ts": True, "b.ts": False, "c.ts": None}, ("b.ts", "c.ts", "d.ts")
            ),
            {"b.ts": "not_generated", "c.ts": "unmarked", "d.ts": "unmarked"},
        )
        self.assertEqual(
            cli._generated_mark_states(None, ("b.ts",)), {"b.ts": "unavailable"}
        )


if __name__ == "__main__":
    unittest.main()
