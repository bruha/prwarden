import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from prwarden.commands import CommandError
from prwarden.models import DependencyLink, PullRequest, Repository
from prwarden.workspace import WorkspaceError, review_workspace


class ReviewWorkspaceTests(unittest.TestCase):
    def input(self, directory: str):
        checkout = Path(directory) / "checkout"
        checkout.mkdir()
        repository = Repository("app", "owner/app", checkout)
        pull_request = PullRequest(
            7,
            "Title",
            "",
            "author",
            "a" * 40,
            "url",
        )
        return repository, pull_request

    def test_fetches_exact_head_and_removes_detached_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, pull_request = self.input(directory)
            workspace_root = Path(directory) / "review-worktrees"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                command = list(args)
                calls.append(command)
                if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
                    return "true"
                if command[-3:] == ["remote", "get-url", "origin"]:
                    return "git@github.com:owner/app.git"
                if "worktree" in command and "add" in command:
                    Path(command[-2]).mkdir(parents=True)
                if "worktree" in command and "remove" in command:
                    shutil.rmtree(command[-1])
                return ""

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with review_workspace(
                    repository,
                    pull_request,
                    workspace_root,
                ) as exact_repository:
                    exact_path = exact_repository.path
                    self.assertIsNotNone(exact_path)
                    self.assertTrue(exact_path.is_dir())
                    self.assertEqual(exact_repository.slug, repository.slug)

            self.assertFalse(exact_path.exists())
            fetch = next(command for command in calls if "fetch" in command)
            self.assertIn("--no-write-fetch-head", fetch)
            self.assertEqual(fetch[-1], pull_request.head_sha)
            add = next(
                command
                for command in calls
                if "worktree" in command and "add" in command
            )
            self.assertIn("--detach", add)
            self.assertEqual(add[-1], pull_request.head_sha)

    def test_removes_detached_worktree_when_review_body_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, pull_request = self.input(directory)
            workspace_root = Path(directory) / "review-worktrees"
            removed: list[Path] = []

            def run(args, **kwargs):
                command = list(args)
                if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
                    return "true"
                if command[-3:] == ["remote", "get-url", "origin"]:
                    return "git@github.com:owner/app.git"
                if "worktree" in command and "add" in command:
                    Path(command[-2]).mkdir(parents=True)
                if "worktree" in command and "remove" in command:
                    removed.append(Path(command[-1]))
                    shutil.rmtree(command[-1])
                return ""

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "review failed"):
                    with review_workspace(
                        repository,
                        pull_request,
                        workspace_root,
                    ):
                        raise RuntimeError("review failed")

            self.assertEqual(len(removed), 1)
            self.assertFalse(removed[0].exists())

    def test_removes_orphaned_directory_after_git_cleanup_race(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, pull_request = self.input(directory)
            workspace_root = Path(directory) / "review-worktrees"
            calls: list[list[str]] = []

            def run(args, **kwargs):
                command = list(args)
                calls.append(command)
                if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
                    return "true"
                if command[-3:] == ["remote", "get-url", "origin"]:
                    return "git@github.com:owner/app.git"
                if "worktree" in command and "add" in command:
                    path = Path(command[-2])
                    path.mkdir(parents=True)
                    (path / ".DS_Store").write_text("metadata", encoding="utf-8")
                if "worktree" in command and "remove" in command:
                    raise CommandError("Directory not empty")
                return ""

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with review_workspace(
                    repository,
                    pull_request,
                    workspace_root,
                ) as exact_repository:
                    exact_path = exact_repository.path

            self.assertFalse(exact_path.exists())
            self.assertTrue(
                any(command[-2:] == ["worktree", "prune"] for command in calls)
            )

    def test_rejects_checkout_with_wrong_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, pull_request = self.input(directory)

            def run(args, **kwargs):
                command = list(args)
                if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
                    return "true"
                return "git@github.com:different/repository.git"

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with self.assertRaisesRegex(WorkspaceError, "does not match"):
                    with review_workspace(
                        repository,
                        pull_request,
                        Path(directory) / "worktrees",
                    ):
                        pass

    def test_fetch_failure_is_reported_without_entering_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            repository, pull_request = self.input(directory)

            def run(args, **kwargs):
                command = list(args)
                if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
                    return "true"
                if command[-3:] == ["remote", "get-url", "origin"]:
                    return "git@github.com:owner/app.git"
                if "fetch" in command:
                    raise CommandError("fetch failed")
                return ""

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with self.assertRaisesRegex(WorkspaceError, "Cannot prepare PR head"):
                    with review_workspace(
                        repository,
                        pull_request,
                        Path(directory) / "worktrees",
                    ):
                        pass


class DependencyLinkTests(unittest.TestCase):
    """Installed dependencies are readable only when both trees resolve alike."""

    def scenario(
        self,
        directory: str,
        *,
        head_lockfile: Optional[str] = "pinned\n",
        checkout_lockfile: Optional[str] = "pinned\n",
        install_dependencies: bool = True,
        lockfile_name: str = "bun.lock",
    ):
        checkout = Path(directory) / "checkout"
        checkout.mkdir()
        if checkout_lockfile is not None:
            (checkout / lockfile_name).write_text(checkout_lockfile)
        if install_dependencies:
            typings = checkout / "node_modules" / "@types" / "react"
            typings.mkdir(parents=True)
            (typings / "index.d.ts").write_text("interface SubmitEvent {}\n")
        repository = Repository("app", "owner/app", checkout)
        pull_request = PullRequest(7, "Title", "", "author", "b" * 40, "url")

        def run(args, **kwargs):
            command = list(args)
            if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
                return "true"
            if command[-3:] == ["remote", "get-url", "origin"]:
                return "git@github.com:owner/app.git"
            if "worktree" in command and "add" in command:
                worktree = Path(command[-2])
                worktree.mkdir(parents=True)
                if head_lockfile is not None:
                    (worktree / lockfile_name).write_text(head_lockfile)
            if "worktree" in command and "remove" in command:
                shutil.rmtree(command[-1])
            return ""

        return checkout, repository, pull_request, run

    def bun_lockfile(self, packages: "dict[str, str]") -> str:
        """Write a bun lockfile, trailing commas and all."""

        entries = ",\n".join(
            f'    "{name}": ["{name}@{version}", "", {{}}, "sha512-x"]'
            for name, version in packages.items()
        )
        return (
            '{\n  "lockfileVersion": 1,\n  "workspaces": {\n    "": {\n'
            '      "name": "app",\n    },\n  },\n'
            '  "packages": {\n' + entries + ",\n  },\n}\n"
        )

    def npm_lockfile(self, packages: "dict[str, str]") -> str:
        return json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "app"},
                    **{
                        f"node_modules/{name}": {"version": version}
                        for name, version in packages.items()
                    },
                    # A dependency's private copy is not the resolved version.
                    "node_modules/left-pad/node_modules/tiny": {"version": "9.9.9"},
                },
            }
        )

    def test_a_changed_lockfile_still_links_unchanged_packages(self):
        """A lockfile bump must not withhold every dependency's source."""

        with tempfile.TemporaryDirectory() as directory:
            head = self.bun_lockfile({"react": "19.2.0", "zod": "4.1.0"})
            local = self.bun_lockfile({"react": "19.2.0", "zod": "3.24.1"})
            checkout, repository, pull_request, run = self.scenario(
                directory,
                head_lockfile=head,
                checkout_lockfile=local,
            )
            for package in ("react", "zod"):
                (checkout / "node_modules" / package).mkdir(parents=True)
                (checkout / "node_modules" / package / "index.js").write_text("x")

            workspace = review_workspace(
                repository, pull_request, Path(directory) / "worktrees"
            )
            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with workspace as exact_repository:
                    modules = exact_repository.path / "node_modules"
                    self.assertFalse(modules.is_symlink())
                    # react resolves alike, zod was bumped by this pull request,
                    # and @types/react is installed but named by no lockfile.
                    self.assertTrue((modules / "react").is_symlink())
                    self.assertEqual(
                        (modules / "react" / "index.js").read_text(), "x"
                    )
                    self.assertFalse((modules / "zod").exists())
                    self.assertFalse((modules / "@types" / "react").exists())

                    self.assertEqual(len(workspace.dependency_links), 1)
                    link = workspace.dependency_links[0]
                    self.assertEqual(link.lockfiles, ())
                    self.assertEqual(link.packages, 1)
                    self.assertEqual(
                        workspace.dependency_notes,
                        (
                            "bun.lock differs from the local checkout, so only the "
                            "1 node_modules package resolved to the same version "
                            "is linked",
                        ),
                    )

            # Cleanup unlinks; a developer's installed packages are untouched.
            self.assertEqual(
                (checkout / "node_modules" / "react" / "index.js").read_text(), "x"
            )
            self.assertTrue((checkout / "node_modules" / "zod").is_dir())

    def test_an_npm_lockfile_is_compared_per_package(self):
        with tempfile.TemporaryDirectory() as directory:
            head = self.npm_lockfile({"react": "19.2.0", "zod": "4.1.0"})
            local = self.npm_lockfile({"react": "19.2.0", "zod": "3.24.1"})
            checkout, repository, pull_request, run = self.scenario(
                directory,
                head_lockfile=head,
                checkout_lockfile=local,
                lockfile_name="package-lock.json",
            )
            for package in ("react", "zod"):
                (checkout / "node_modules" / package).mkdir(parents=True)

            workspace = review_workspace(
                repository, pull_request, Path(directory) / "worktrees"
            )
            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with workspace as exact_repository:
                    modules = exact_repository.path / "node_modules"
                    self.assertTrue((modules / "react").is_symlink())
                    self.assertFalse((modules / "zod").exists())
                    self.assertEqual(workspace.dependency_links[0].packages, 1)

    def test_a_drifted_checkout_is_named_as_the_reason(self):
        """A stale local checkout and a dependency-changing PR look alike."""

        with tempfile.TemporaryDirectory() as directory:
            checkout, repository, pull_request, run = self.scenario(
                directory,
                head_lockfile="unreadable format\n",
                checkout_lockfile="another unreadable format\n",
            )
            workspace = review_workspace(
                repository, pull_request, Path(directory) / "worktrees"
            )
            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with workspace:
                    self.assertEqual(workspace.dependency_links, ())
                    self.assertEqual(
                        workspace.dependency_notes,
                        ("no node_modules linked because bun.lock differs from the "
                         "local checkout",),
                    )

    def test_a_missing_install_is_named_as_the_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout, repository, pull_request, run = self.scenario(
                directory, install_dependencies=False
            )
            workspace = review_workspace(
                repository, pull_request, Path(directory) / "worktrees"
            )
            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with workspace:
                    self.assertEqual(
                        workspace.dependency_notes,
                        ("node_modules is not installed in the local checkout",),
                    )

    def test_links_installed_dependencies_when_lockfiles_match(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout, repository, pull_request, run = self.scenario(directory)
            workspace = review_workspace(
                repository,
                pull_request,
                Path(directory) / "worktrees",
            )

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with workspace as exact_repository:
                    link = exact_repository.path / "node_modules"
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(
                        link.resolve(),
                        (checkout / "node_modules").resolve(),
                    )
                    self.assertEqual(
                        (link / "@types" / "react" / "index.d.ts").read_text(),
                        "interface SubmitEvent {}\n",
                    )
                    self.assertEqual(
                        workspace.dependency_links,
                        (
                            DependencyLink(
                                name="node_modules",
                                target=str((checkout / "node_modules").resolve()),
                                lockfiles=("bun.lock",),
                            ),
                        ),
                    )

            self.assertTrue(
                (checkout / "node_modules" / "@types" / "react" / "index.d.ts").is_file(),
                "cleanup must never delete the configured checkout's dependencies",
            )

    def test_does_not_link_when_head_lockfile_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            _, repository, pull_request, run = self.scenario(
                directory,
                head_lockfile="different\n",
            )
            workspace = review_workspace(
                repository,
                pull_request,
                Path(directory) / "worktrees",
            )

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with workspace as exact_repository:
                    self.assertFalse((exact_repository.path / "node_modules").exists())
                    self.assertEqual(workspace.dependency_links, ())

    def test_does_not_link_without_a_lockfile_at_the_reviewed_head(self):
        with tempfile.TemporaryDirectory() as directory:
            _, repository, pull_request, run = self.scenario(
                directory,
                head_lockfile=None,
            )
            workspace = review_workspace(
                repository,
                pull_request,
                Path(directory) / "worktrees",
            )

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with workspace as exact_repository:
                    self.assertFalse((exact_repository.path / "node_modules").exists())
                    self.assertEqual(workspace.dependency_links, ())

    def test_does_not_link_when_the_checkout_lockfile_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            _, repository, pull_request, run = self.scenario(
                directory,
                checkout_lockfile=None,
            )
            workspace = review_workspace(
                repository,
                pull_request,
                Path(directory) / "worktrees",
            )

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with workspace as exact_repository:
                    self.assertFalse((exact_repository.path / "node_modules").exists())
                    self.assertEqual(workspace.dependency_links, ())

    def test_does_not_link_when_dependencies_are_not_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            _, repository, pull_request, run = self.scenario(
                directory,
                install_dependencies=False,
            )
            workspace = review_workspace(
                repository,
                pull_request,
                Path(directory) / "worktrees",
            )

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with workspace as exact_repository:
                    self.assertFalse((exact_repository.path / "node_modules").exists())
                    self.assertEqual(workspace.dependency_links, ())


if __name__ == "__main__":
    unittest.main()
