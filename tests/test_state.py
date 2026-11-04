import json
import tempfile
import unittest
from pathlib import Path

from src.state import JsonStateStore, StateError


class JsonStateTests(unittest.TestCase):
    def test_persists_terminal_status_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with JsonStateStore(path) as store:
                key = store.key("owner/repo", 7, "abc123", "slack")
                store.set_status(key, "no_findings")
                self.assertTrue(store.is_terminal(key))

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["records"][key]["status"], "no_findings")

    def test_pull_request_scope_matches_any_head_and_nothing_else(self):
        """Per-pull-request work is settled by any head, and only its own.

        Prefix and suffix have to be exact: #4# must not match #47#, and a settled
        code review must not settle the design level.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with JsonStateStore(path) as store:
                self.assertFalse(
                    store.is_settled_for_pull_request("owner/repo", 47, "design")
                )
                store.set_status(
                    store.key("owner/repo", 47, "an-older-head", "design"),
                    "delivered",
                )
                self.assertTrue(
                    store.is_settled_for_pull_request("owner/repo", 47, "design")
                )

                # A neighbouring number, another destination, and another repository
                # are all separate scopes.
                self.assertFalse(
                    store.is_settled_for_pull_request("owner/repo", 4, "design")
                )
                self.assertFalse(
                    store.is_settled_for_pull_request("owner/repo", 47, "github")
                )
                self.assertFalse(
                    store.is_settled_for_pull_request("other/repo", 47, "design")
                )

    def test_a_failed_pull_request_scoped_attempt_is_reviewed_again(self):
        """A design level that failed has settled nothing."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with JsonStateStore(path) as store:
                store.set_status(
                    store.pull_request_key("owner/repo", 47, "design"),
                    "failed",
                    error="the verifier answered past the limit twice",
                )
                self.assertFalse(
                    store.is_settled_for_pull_request("owner/repo", 47, "design")
                )

    def test_failed_status_is_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with JsonStateStore(path) as store:
                key = store.key("owner/repo", 8, "def456", "github")
                store.set_status(key, "failed", error="temporary")
                self.assertFalse(store.is_terminal(key))

    def test_approval_withheld_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with JsonStateStore(path) as store:
                key = store.key("owner/repo", 9, "ghi789", "github")
                store.set_status(key, "approval_withheld")
                self.assertTrue(store.is_terminal(key))

    def test_skipped_oversized_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            with JsonStateStore(Path(directory) / "state.json") as store:
                key = store.key("owner/app", 1, "head", "github")
                store.set_status(key, "skipped_oversized")
                self.assertTrue(store.is_terminal(key))

    def test_second_process_lock_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with JsonStateStore(path):
                with self.assertRaises(StateError):
                    with JsonStateStore(path):
                        pass

    def test_lock_can_be_disabled_for_read_only_dry_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with JsonStateStore(path):
                with JsonStateStore(path, lock_enabled=False) as store:
                    self.assertEqual(store.data, {"version": 1, "records": {}})


if __name__ == "__main__":
    unittest.main()
