import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

from prwarden.commands import CommandError
from prwarden.models import DependencyLink, PullRequest, Repository
from prwarden.workspace import (
    WorkspaceError,
    read_base_files,
    remove_leftover_workspaces,
    review_workspace,
)


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


class FetchRetryTests(unittest.TestCase):
    """A fetch only reads, so one that fails for a reason that passes is tried again.

    Driven below `run_cmd`, at `subprocess.run`, so the retry that runs is the real
    one rather than a stand-in.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        checkout = self.root / "checkout"
        checkout.mkdir()
        self.repository = Repository("app", "owner/app", checkout)
        self.pull_request = PullRequest(7, "Title", "", "author", "a" * 40, "url")
        self.fetches: "list[list[str]]" = []
        self.results: "list[object]" = []

    def run_process(self, args, **kwargs):
        command = list(args)
        if "fetch" in command:
            self.fetches.append(command)
            result = self.results.pop(0)
            if result == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            code, error = result
            return subprocess.CompletedProcess(command, code, "", error)
        output = ""
        if command[-2:] == ["rev-parse", "--is-inside-work-tree"]:
            output = "true\n"
        elif command[-3:] == ["remote", "get-url", "origin"]:
            output = "git@github.com:owner/app.git\n"
        elif "worktree" in command and "add" in command:
            Path(command[-2]).mkdir(parents=True)
        elif "worktree" in command and "remove" in command:
            shutil.rmtree(command[-1], ignore_errors=True)
        return subprocess.CompletedProcess(command, 0, output, "")

    def review(self, *results) -> "tuple[Optional[Path], Mock]":
        self.results = list(results)
        with patch(
            "prwarden.commands.subprocess.run", side_effect=self.run_process
        ), patch("prwarden.commands.time.sleep") as sleep, patch(
            "prwarden.commands.progress.notify"
        ):
            with review_workspace(
                self.repository, self.pull_request, self.root / "worktrees"
            ) as exact:
                return exact.path, sleep

    def test_a_fetch_that_times_out_once_is_tried_again(self) -> None:
        path, sleep = self.review("timeout", (0, ""))

        self.assertIsNotNone(path)
        self.assertEqual(len(self.fetches), 2)
        self.assertEqual(self.fetches[0], self.fetches[1])
        sleep.assert_called_once()

    def test_a_dropped_ssh_connection_is_tried_again(self) -> None:
        for message in (
            "kex_exchange_identification: Connection closed by remote host\n"
            "fatal: Could not read from remote repository.",
            "fatal: the remote end hung up unexpectedly",
            "fatal: early EOF\nfatal: index-pack failed",
        ):
            with self.subTest(message=message.splitlines()[0]):
                self.fetches = []

                path, _ = self.review((128, message), (0, ""))

                self.assertIsNotNone(path)
                self.assertEqual(len(self.fetches), 2)

    def test_a_fetch_that_keeps_failing_is_an_error_after_two_attempts(self) -> None:
        with self.assertRaisesRegex(WorkspaceError, "Cannot prepare PR head"):
            self.review("timeout", "timeout")

        self.assertEqual(len(self.fetches), 2)

    def test_a_commit_github_does_not_have_is_not_asked_for_twice(self) -> None:
        missing = (128, "fatal: remote error: upload-pack: not our ref " + "a" * 40)

        with patch("prwarden.commands.time.sleep") as sleep:
            with self.assertRaisesRegex(WorkspaceError, "not our ref"):
                self.review(missing)

        self.assertEqual(len(self.fetches), 1)
        sleep.assert_not_called()

    def test_a_base_branch_fetch_is_tried_again_too(self) -> None:
        self.results = ["timeout", (0, "")]
        with patch(
            "prwarden.commands.subprocess.run", side_effect=self.run_process
        ), patch("prwarden.commands.time.sleep"), patch(
            "prwarden.commands.progress.notify"
        ):
            read_base_files(self.repository, "b" * 40, ("AGENTS.md",), bool)

        self.assertEqual(len(self.fetches), 2)


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


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


@unittest.skipIf(shutil.which("git") is None, "needs Git")
class LeftoverWorkspaceTests(unittest.TestCase):
    """Folders earlier runs left behind, against a real Git repository."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name).resolve()
        self.checkout = base / "checkout"
        _git("init", "-q", str(self.checkout))
        _git(
            "-C", str(self.checkout),
            "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-q", "--allow-empty", "-m", "init",
        )
        self.root = base / "worktrees"
        self.root.mkdir()

    def add(self, name: str, *, locked_by_git: bool = False) -> Path:
        path = self.root / name
        _git("-C", str(self.checkout), "worktree", "add", "-q", "--detach", str(path))
        if locked_by_git:
            # What a `worktree add` stopped partway leaves.
            _git(
                "-C", str(self.checkout), "worktree", "lock",
                "--reason", "initializing", str(path),
            )
        return path

    def registered(self) -> list[str]:
        listing = _git("-C", str(self.checkout), "worktree", "list", "--porcelain")
        return [
            Path(line.split(" ", 1)[1]).name
            for line in listing.splitlines()
            if line.startswith("worktree ") and str(self.root) in line
        ]

    def test_a_folder_no_review_holds_is_removed_with_its_locked_record(self) -> None:
        path = self.add("owner-app-pr-7-aaaa-x1", locked_by_git=True)
        (self.root / "owner-app-pr-7-aaaa-x1.lock").write_text("")

        removed, problems = remove_leftover_workspaces(self.root, [self.checkout])

        self.assertEqual((removed, problems), (["owner-app-pr-7-aaaa-x1"], []))
        self.assertFalse(path.exists())
        self.assertFalse((self.root / "owner-app-pr-7-aaaa-x1.lock").exists())
        self.assertEqual(self.registered(), [])

    def test_a_record_whose_folder_is_gone_is_dropped(self) -> None:
        path = self.add("owner-app-pr-8-bbbb-x2", locked_by_git=True)
        shutil.rmtree(path)

        removed, problems = remove_leftover_workspaces(self.root, [self.checkout])

        self.assertEqual((removed, problems), ([], []))
        self.assertEqual(self.registered(), [])

    @unittest.skipIf(fcntl is None, "needs file locks")
    def test_a_folder_a_running_review_holds_is_kept(self) -> None:
        path = self.add("owner-app-pr-9-cccc-x3")
        lock = self.root / "owner-app-pr-9-cccc-x3.lock"
        with lock.open("w") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            removed, problems = remove_leftover_workspaces(self.root, [self.checkout])

        self.assertEqual((removed, problems), ([], []))
        self.assertTrue(path.is_dir())
        self.assertTrue(lock.exists())
        self.assertEqual(self.registered(), ["owner-app-pr-9-cccc-x3"])

    def test_a_folder_without_a_lock_file_is_removed_only_once_a_day_old(self) -> None:
        path = self.add("owner-app-pr-10-dddd-x4")

        young = remove_leftover_workspaces(self.root, [self.checkout])
        self.assertEqual(young, ([], []))
        self.assertTrue(path.is_dir())

        later = time.time() + 2 * 24 * 60 * 60
        old = remove_leftover_workspaces(self.root, [self.checkout], now=later)
        self.assertEqual(old, (["owner-app-pr-10-dddd-x4"], []))
        self.assertFalse(path.exists())
        self.assertEqual(self.registered(), [])

    def test_a_half_made_folder_git_does_not_know_is_removed(self) -> None:
        path = self.root / "owner-app-pr-11-eeee-x5"
        (path / "src").mkdir(parents=True)
        (path / "src" / "index.ts").write_text("partial")
        (self.root / "owner-app-pr-11-eeee-x5.lock").write_text("")
        outside = self.root.parent / "kept"
        outside.mkdir()
        (self.root / "not-a-folder.txt").write_text("kept")

        removed, problems = remove_leftover_workspaces(self.root, [self.checkout])

        self.assertEqual((removed, problems), (["owner-app-pr-11-eeee-x5"], []))
        self.assertFalse(path.exists())
        self.assertTrue(outside.is_dir())
        self.assertTrue((self.root / "not-a-folder.txt").exists())

    def test_a_missing_folder_and_checkout_are_nothing_to_do(self) -> None:
        self.assertEqual(
            remove_leftover_workspaces(
                self.root / "absent", [self.root.parent / "no-checkout"]
            ),
            ([], []),
        )


class WorkspaceLockTests(unittest.TestCase):
    def input(self, directory: str):
        checkout = Path(directory) / "checkout"
        checkout.mkdir()
        repository = Repository("app", "owner/app", checkout)
        pull_request = PullRequest(7, "Title", "", "author", "a" * 40, "url")
        return repository, pull_request

    def run_git(self, *, add_fails: bool = False):
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
                if add_fails:
                    raise CommandError("Command timed out after 180s: git")
            if "worktree" in command and "remove" in command:
                shutil.rmtree(command[-1], ignore_errors=True)
            return ""

        return calls, run

    @unittest.skipIf(fcntl is None, "needs file locks")
    def test_a_review_holds_its_folder_locked_until_it_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, pull_request = self.input(directory)
            root = Path(directory) / "worktrees"
            calls, run = self.run_git()

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with review_workspace(repository, pull_request, root) as exact:
                    lock = exact.path.with_name(exact.path.name + ".lock")
                    with lock.open("r+") as other:
                        with self.assertRaises(OSError):
                            fcntl.flock(
                                other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                            )

            self.assertFalse(lock.exists())
            self.assertFalse(exact.path.exists())
            removal = next(command for command in calls if "remove" in command)
            self.assertEqual(removal[-4:-1], ["remove", "--force", "--force"])

    def test_a_stopped_worktree_add_is_removed_with_double_force(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository, pull_request = self.input(directory)
            root = Path(directory) / "worktrees"
            calls, run = self.run_git(add_fails=True)

            with patch("prwarden.workspace.run_cmd", side_effect=run):
                with self.assertRaisesRegex(WorkspaceError, "timed out"):
                    with review_workspace(repository, pull_request, root):
                        pass

            removal = next(command for command in calls if "remove" in command)
            self.assertEqual(removal[-4:-1], ["remove", "--force", "--force"])
            self.assertEqual(list(root.iterdir()) if root.exists() else [], [])


if __name__ == "__main__":
    unittest.main()
