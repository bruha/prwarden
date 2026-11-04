import errno
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import call, patch

import prwarden.release as release
from prwarden.release import ReleaseError, Version, apply_plan, create_plan


def git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=str(root),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def remove_tree_with_retry(
    root: Path,
    *,
    attempts: int = 5,
    initial_delay_seconds: float = 0.05,
) -> None:
    """Remove a test tree, retrying only a transient non-empty directory."""
    if attempts < 1:
        raise ValueError("Cleanup attempts must be positive")

    for attempt in range(attempts):
        try:
            shutil.rmtree(root)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            if error.errno != errno.ENOTEMPTY or attempt == attempts - 1:
                raise
            time.sleep(initial_delay_seconds * (2**attempt))


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="prwarden-release-test-"))
        self.addCleanup(remove_tree_with_retry, self.root)
        (self.root / "prwarden").mkdir()
        (self.root / "VERSION").write_text("1.2.3\n", encoding="utf-8")
        (self.root / "prwarden" / "__init__.py").write_text(
            '"""Package."""\n\n__version__ = "1.2.3"\n',
            encoding="utf-8",
        )
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.name", "Release Test")
        git(self.root, "config", "user.email", "release@example.invalid")
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n<!-- release-baseline: 1.2.3 PLACEHOLDER -->\n"
            "## [Unreleased]\n\n## [1.2.3] - 2026-01-01\n",
            encoding="utf-8",
        )
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "chore: establish baseline")
        self.baseline = git(self.root, "rev-parse", "HEAD")
        changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")
        (self.root / "CHANGELOG.md").write_text(
            changelog.replace("PLACEHOLDER", self.baseline),
            encoding="utf-8",
        )
        git(self.root, "add", "CHANGELOG.md")
        git(self.root, "commit", "-m", "chore(release): v1.2.3")
        git(self.root, "tag", "v1.2.3")

    def commit(self, subject: str, body: str = "") -> None:
        marker = self.root / "change.txt"
        previous = marker.read_text(encoding="utf-8") if marker.exists() else ""
        marker.write_text(previous + subject + "\n", encoding="utf-8")
        git(self.root, "add", "change.txt")
        command = ["commit", "-m", subject]
        if body:
            command.extend(("-m", body))
        git(self.root, *command)

    def test_a_merge_commit_neither_blocks_nor_counts(self) -> None:
        """A merge records that a branch arrived, not what changed. Its subject is not
        a Conventional Commit and never could be, and the commits it brought are in
        the range already, so reading it as one refused to release at all."""

        self.commit("fix: correct a boundary")
        git(self.root, "checkout", "-b", "feature")
        self.commit("feat: add a capability")
        git(self.root, "checkout", "main")
        git(self.root, "merge", "--no-ff", "feature", "-m", "merge: feature")

        commits = release._parse_commits(self.root, self.baseline)

        subjects = [commit.subject for commit in commits]
        self.assertIn("fix: correct a boundary", subjects)
        self.assertIn("feat: add a capability", subjects)
        self.assertNotIn("merge: feature", subjects)
        # The merged feat still decides the bump; the merge itself adds nothing.
        self.assertEqual(release._required_bump(commits), "minor")

    def test_version_bumps_reset_lower_components(self) -> None:
        version = Version.parse("1.2.3")
        self.assertEqual(str(version.bump("patch")), "1.2.4")
        self.assertEqual(str(version.bump("minor")), "1.3.0")
        self.assertEqual(str(version.bump("major")), "2.0.0")

    @patch("time.sleep")
    @patch("shutil.rmtree")
    def test_temporary_repository_cleanup_retries_directory_not_empty(
        self,
        rmtree_mock,
        sleep_mock,
    ) -> None:
        cleanup_error = OSError(errno.ENOTEMPTY, "Directory not empty")
        rmtree_mock.side_effect = (cleanup_error, None)
        root = Path("/tmp/release-test")

        remove_tree_with_retry(
            root,
            attempts=3,
            initial_delay_seconds=0.01,
        )

        self.assertEqual(rmtree_mock.call_args_list, [call(root), call(root)])
        sleep_mock.assert_called_once_with(0.01)

    @patch("time.sleep")
    @patch("shutil.rmtree")
    def test_temporary_repository_cleanup_reraises_after_retry_limit(
        self,
        rmtree_mock,
        sleep_mock,
    ) -> None:
        cleanup_error = OSError(errno.ENOTEMPTY, "Directory not empty")
        rmtree_mock.side_effect = cleanup_error
        root = Path("/tmp/release-test")

        with self.assertRaises(OSError) as caught:
            remove_tree_with_retry(
                root,
                attempts=3,
                initial_delay_seconds=0.01,
            )

        self.assertIs(caught.exception, cleanup_error)
        self.assertEqual(rmtree_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_args_list, [call(0.01), call(0.02)])

    @patch("time.sleep")
    @patch("shutil.rmtree")
    def test_temporary_repository_cleanup_does_not_retry_other_errors(
        self,
        rmtree_mock,
        sleep_mock,
    ) -> None:
        cleanup_error = OSError(errno.EACCES, "Permission denied")
        rmtree_mock.side_effect = cleanup_error
        root = Path("/tmp/release-test")

        with self.assertRaises(OSError) as caught:
            remove_tree_with_retry(root)

        self.assertIs(caught.exception, cleanup_error)
        rmtree_mock.assert_called_once_with(root)
        sleep_mock.assert_not_called()

    def test_feature_requires_minor_and_updates_release_files(self) -> None:
        self.commit("feat(review): add chunked analysis")

        plan = create_plan(self.root, released=date(2026, 8, 13))

        self.assertEqual(plan.bump, "minor")
        self.assertEqual(str(plan.next_version), "1.3.0")
        self.assertIn("## [1.3.0] - 2026-08-13", plan.changelog)
        self.assertIn(
            "- **review:** add chunked analysis", plan.changelog
        )
        apply_plan(plan, self.root)
        self.assertEqual(
            (self.root / "VERSION").read_text(encoding="utf-8"), "1.3.0\n"
        )
        self.assertIn(
            '__version__ = "1.3.0"',
            (self.root / "prwarden" / "__init__.py").read_text(encoding="utf-8"),
        )

    def test_changelog_baseline_is_used_before_first_version_tag(self) -> None:
        git(self.root, "tag", "--delete", "v1.2.3")
        self.commit("fix(review): preserve validation detail")

        plan = create_plan(self.root)

        self.assertEqual(plan.boundary, self.baseline)
        self.assertEqual(plan.bump, "patch")
        self.assertEqual(str(plan.next_version), "1.2.4")
        self.assertEqual(len(plan.commits), 1)

    def test_breaking_footer_requires_major(self) -> None:
        self.commit(
            "refactor(state): change record layout",
            "BREAKING CHANGE: existing state files require migration",
        )

        plan = create_plan(self.root)

        self.assertEqual(plan.bump, "major")
        self.assertEqual(str(plan.next_version), "2.0.0")
        self.assertIn("### Breaking Changes", plan.changelog)

    def test_lower_override_is_rejected(self) -> None:
        self.commit("feat: add an adapter")

        with self.assertRaisesRegex(ReleaseError, "lower than required minor"):
            create_plan(self.root, requested_bump="patch")

    def test_non_conventional_commit_is_rejected(self) -> None:
        self.commit("add an adapter")

        with self.assertRaisesRegex(ReleaseError, "must use Conventional Commits"):
            create_plan(self.root)

    def test_dirty_worktree_is_rejected(self) -> None:
        self.commit("fix: correct parsing")
        (self.root / "untracked.txt").write_text("dirty\n", encoding="utf-8")

        with self.assertRaisesRegex(ReleaseError, "must be clean"):
            create_plan(self.root)

    def test_release_must_run_on_main(self) -> None:
        self.commit("fix: correct parsing")
        git(self.root, "switch", "-c", "fix/parsing")

        with self.assertRaisesRegex(ReleaseError, "must be prepared on main"):
            create_plan(self.root)


if __name__ == "__main__":
    unittest.main()
