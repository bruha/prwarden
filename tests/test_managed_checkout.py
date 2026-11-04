"""Repositories PR Warden clones and keeps itself, because no local clone was named."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prwarden.github as github
from prwarden.commands import CommandError
from prwarden.config import ConfigError, load_repositories
from prwarden.models import PullRequest, Repository
from prwarden.workspace import (
    WorkspaceError,
    ensure_managed_checkout,
    review_workspace,
)
from tests.test_repository_account import tokens


SHA = "a" * 40
PULL_REQUEST = PullRequest(7, "Title", "", "author", SHA, "url")


class ManagedCheckoutConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkouts = self.root / "checkouts"

    def load(self, entries: str, checkouts=True):
        path = self.root / "repositories.json"
        path.write_text(entries, encoding="utf-8")
        return load_repositories(path, self.checkouts if checkouts else None)

    def test_an_entry_without_a_path_is_cloned_under_the_checkouts_directory(
        self,
    ) -> None:
        own, managed = self.load(
            '[{"slug": "owner/own", "path": "."}, {"slug": "owner/app"}]'
        )

        self.assertEqual(own.path, self.root)
        self.assertFalse(own.managed_checkout)
        self.assertEqual(managed.path, self.checkouts / "owner" / "app")
        self.assertTrue(managed.managed_checkout)

    def test_a_reader_that_names_no_checkouts_directory_gets_no_path(self) -> None:
        (repository,) = self.load('[{"slug": "owner/app"}]', checkouts=False)

        self.assertIsNone(repository.path)
        self.assertFalse(repository.managed_checkout)

    def test_a_slug_that_could_leave_the_checkouts_directory_is_refused(
        self,
    ) -> None:
        for slug in ("../app", "owner/..", "own er/app", "-owner/app"):
            with self.subTest(slug=slug):
                with self.assertRaisesRegex(ConfigError, "cannot clone|can clone"):
                    self.load(f'[{{"slug": "{slug}"}}]')


class EnsureManagedCheckoutTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkouts = Path(temporary.name).resolve() / "checkouts"
        self.repository = Repository(
            "app",
            "owner/app",
            self.checkouts / "owner" / "app",
            managed_checkout=True,
        )
        self.calls: "list[list[str]]" = []

    def clone(self, repository, args, **_options):
        command = list(args)
        self.calls.append(command)
        if command[0] == "clone":
            (Path(command[-1]) / ".git").mkdir()
        return ""

    def test_the_first_use_clones_objects_only_and_later_uses_do_not(self) -> None:
        with patch("prwarden.workspace.run_git_for", side_effect=self.clone):
            first = ensure_managed_checkout(self.repository)
            second = ensure_managed_checkout(self.repository)

        self.assertEqual(first, self.repository.path)
        self.assertEqual(second, first)
        self.assertTrue((first / ".git").is_dir())
        (clone,) = self.calls
        self.assertEqual(clone[:3], ["clone", "--quiet", "--no-checkout"])
        self.assertEqual(clone[3], "https://github.com/owner/app.git")
        # Made beside its final place and renamed, never cloned in place.
        self.assertEqual(Path(clone[4]).parent, first.parent)
        self.assertNotEqual(Path(clone[4]), first)
        self.assertEqual(sorted(p.name for p in first.parent.iterdir()), ["app"])

    def test_a_failed_clone_leaves_nothing_that_looks_finished(self) -> None:
        def fail(repository, args, **options):
            self.clone(repository, args, **options)
            raise CommandError("Repository not found")

        with patch("prwarden.workspace.run_git_for", side_effect=fail):
            with self.assertRaisesRegex(WorkspaceError, "Cannot clone owner/app"):
                ensure_managed_checkout(self.repository)

        self.assertFalse(self.repository.path.exists())
        self.assertEqual(list(self.repository.path.parent.iterdir()), [])


class ManagedReviewWorkspaceTests(unittest.TestCase):
    def test_a_managed_clone_is_made_then_fetched_through_github_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repository = Repository(
                "app", "owner/app", root / "checkouts" / "owner" / "app",
                managed_checkout=True,
            )
            through_github: "list[list[str]]" = []
            plain: "list[list[str]]" = []

            def git_for(repository, args, **_options):
                command = list(args)
                through_github.append(command)
                if command[0] == "clone":
                    (Path(command[-1]) / ".git").mkdir()
                return ""

            def run(args, **_options):
                command = list(args)
                plain.append(command)
                if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
                    return "true"
                if command[-3:] == ["remote", "get-url", "origin"]:
                    return "https://github.com/owner/app.git"
                if "worktree" in command and "add" in command:
                    Path(command[-2]).mkdir(parents=True)
                if "worktree" in command and "remove" in command:
                    shutil.rmtree(command[-1])
                return ""

            with patch("prwarden.workspace.run_git_for", side_effect=git_for), patch(
                "prwarden.workspace.run_cmd", side_effect=run
            ):
                with review_workspace(repository, PULL_REQUEST, root / "worktrees"):
                    pass

        self.assertEqual([command[0] for command in through_github][0], "clone")
        fetch = through_github[1]
        self.assertIn("fetch", fetch)
        self.assertEqual(fetch[-1], SHA)
        self.assertFalse(any("fetch" in command for command in plain))

    def test_an_operators_clone_fetches_with_its_own_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checkout = root / "checkout"
            checkout.mkdir()
            repository = Repository("app", "owner/app", checkout)

            def run(args, **_options):
                command = list(args)
                if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
                    return "true"
                if command[-3:] == ["remote", "get-url", "origin"]:
                    return "git@github.com:owner/app.git"
                if "worktree" in command and "add" in command:
                    Path(command[-2]).mkdir(parents=True)
                if "worktree" in command and "remove" in command:
                    shutil.rmtree(command[-1])
                return ""

            with patch("prwarden.workspace.run_git_for") as git_for, patch(
                "prwarden.workspace.run_cmd", side_effect=run
            ):
                with review_workspace(repository, PULL_REQUEST, root / "worktrees"):
                    pass

        git_for.assert_not_called()


class RunGitForTests(unittest.TestCase):
    """A managed clone has no credentials, so GitHub CLI lends it the right ones."""

    def setUp(self) -> None:
        for state in (
            github._pinned_account,
            github._repository_accounts,
            github._repository_account_failures,
        ):
            self.addCleanup(state.clear)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in github.GITHUB_TOKEN_VARIABLES:
            os.environ.pop(name, None)

    def test_git_is_given_the_repositorys_token_and_no_other_helper(self) -> None:
        personal = Repository(
            "own", "owner/own", Path("."), github_account="owner",
            managed_checkout=True,
        )
        run_patch = patch("prwarden.github.commands.run_cmd", side_effect=tokens)
        which_patch = patch("prwarden.github.shutil.which", return_value="/opt/bin/gh")
        with run_patch as run, which_patch:
            github.pin_github_account("reviewer", [personal])
            github.run_git_for(personal, ["fetch", "origin", SHA])

        args = list(run.call_args.args[0])
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(
            args[:5],
            [
                "git",
                "-c",
                "credential.helper=",
                "-c",
                "credential.https://github.com.helper=!/opt/bin/gh auth git-credential",
            ],
        )
        self.assertEqual(args[5:], ["fetch", "origin", SHA])
        self.assertEqual(environment["GH_TOKEN"], "token-owner")
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")


if __name__ == "__main__":
    unittest.main()
