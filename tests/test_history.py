"""The review history each run keeps, and `prwarden history`, which reads it."""

import json
import os
import stat
import tempfile
import time
import unittest
from argparse import Namespace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prwarden import cli, history
from prwarden.config import Settings
from prwarden.engine import EngineError
from prwarden.history import ReviewHistory, ReviewRecord
from prwarden.models import (
    DesignAssessment,
    DesignFinding,
    Finding,
    PullRequest,
    Repository,
)
from prwarden.state import JsonStateStore
from tests.test_cli import CONFIRMED_JUDGMENT, READY_SNAPSHOT, finding_response
from tests.test_review_rules import without_review_rules


REPOSITORY = Repository("app", "owner/app")
PULL_REQUEST = PullRequest(
    7, "Add a feature", "", "author", "a" * 40, "https://github.com/owner/app/pull/7"
)
FINDING = Finding(
    severity="high",
    title="Unchecked value",
    path="app.py",
    line=3,
    problem="The value is used before it is validated.",
    suggestion="Validate it first.",
)


def head_key(head: str = "a" * 40, destination: str = "github") -> str:
    return f"owner/app#7#{head}#{destination}"


def record_for(*, live: bool = True, destination: str = "github") -> ReviewRecord:
    return ReviewRecord(
        REPOSITORY, PULL_REQUEST, run="run-1", live=live, destination=destination
    )


class ReviewRecordTests(unittest.TestCase):
    def test_a_delivered_approval_is_read_from_the_state_file(self) -> None:
        """The state file is where every live outcome is settled, receipt included."""

        record = record_for()
        record.code_result((), approve=True, reason="")
        record.observe_state(
            {},
            {
                head_key(): {
                    "status": "delivered",
                    "receipt_id": "123",
                    "updated_at": "t",
                }
            },
        )

        entry = record.entry()

        self.assertEqual(entry["code"]["status"], "delivered")
        self.assertEqual(entry["code"]["receipt"], "123")
        self.assertEqual(history.describe(entry, "code"), "approved")
        self.assertEqual(
            history.link(entry, "code"),
            "https://github.com/owner/app/pull/7#pullrequestreview-123",
        )

    def test_what_earlier_runs_recorded_is_not_this_runs_outcome(self) -> None:
        """A record this run did not write says nothing about what this run did."""

        settled = {"status": "delivered", "receipt_id": "9", "updated_at": "t"}
        record = record_for()
        record.skip("code", "state is delivered")
        record.observe_state({head_key(): settled}, {head_key(): dict(settled)})

        entry = record.entry()

        self.assertEqual(
            entry["code"], {"status": "skipped", "reason": "state is delivered"}
        )
        self.assertIsNone(history.link(entry, "code"))

    def test_a_dry_run_keeps_what_it_found_and_delivers_nothing(self) -> None:
        record = record_for(live=False)
        record.code_result((FINDING,), approve=False, reason="")

        entry = record.entry()

        self.assertEqual(entry["mode"], "dry_run")
        self.assertEqual(entry["code"]["status"], "dry_run")
        self.assertEqual(entry["code"]["findings"][0]["title"], "Unchecked value")
        self.assertEqual(entry["code"]["findings"][0]["line"], 3)
        self.assertEqual(
            history.describe(entry, "code"), "dry run: would post 1 finding"
        )

    def test_a_review_that_raised_is_recorded_failed_never_as_its_noted_result(
        self,
    ) -> None:
        """An error must not become an approval: the noted verdict was never given."""

        record = record_for()
        record.code_result((), approve=True, reason="")
        record.fail(RuntimeError("GitHub refused the approval"))

        entry = record.entry()

        self.assertEqual(entry["code"]["status"], "failed")
        self.assertEqual(entry["code"]["error"], "GitHub refused the approval")
        self.assertEqual(
            history.describe(entry, "code"), "failed: GitHub refused the approval"
        )

    def test_a_later_attempt_skipping_a_level_keeps_the_work_an_earlier_one_did(
        self,
    ) -> None:
        """A moved head restarts the review, which skips a level already settled."""

        record = record_for(live=False)
        record.advisory_result(
            "design",
            DesignAssessment(
                understanding="u",
                verdict="concerns",
                findings=(
                    DesignFinding(
                        severity="medium",
                        title="Wrong layer",
                        category="responsibility",
                        problem="p",
                        consequence="c",
                        why_not_preference="w",
                        alternative="a",
                    ),
                ),
            ),
        )
        record.attempt("b" * 40, "github")
        record.skip("design", "already settled for this pull request (delivered)")

        entry = record.entry()

        self.assertEqual(entry["design"]["status"], "dry_run")
        self.assertEqual(entry["design"]["findings"][0]["title"], "Wrong layer")
        self.assertEqual(entry["head"], "b" * 40)
        self.assertEqual(entry["superseded_heads"], ["a" * 40])

    def test_a_moved_head_starts_the_code_outcome_afresh(self) -> None:
        record = record_for()
        record.code_result((FINDING,), approve=False, reason="")
        record.attempt("b" * 40, "github")
        record.observe_state(
            {},
            {
                head_key(): {"status": "superseded"},
                head_key("b" * 40): {"status": "delivered", "receipt_id": "5"},
            },
        )

        entry = record.entry()

        self.assertEqual(entry["code"], {"status": "delivered", "receipt": "5"})

    def test_links_follow_where_each_result_was_posted(self) -> None:
        def entry_for(destination: str, receipt: str, findings=()) -> dict:
            record = record_for(destination=destination)
            record.code_result(findings, approve=not findings, reason="")
            record.observe_state(
                {},
                {
                    head_key(destination=destination): {
                        "status": "delivered",
                        "receipt_id": receipt,
                    }
                },
            )
            return record.entry()

        own = entry_for("github-own", "41")
        self.assertEqual(history.describe(own, "code"), "LGTM comment posted")
        self.assertTrue(history.link(own, "code").endswith("#issuecomment-41"))
        own_findings = entry_for("github-own", "42", (FINDING,))
        self.assertTrue(
            history.link(own_findings, "code").endswith("#pullrequestreview-42")
        )
        slack = entry_for("slack", "http-200")
        self.assertEqual(history.describe(slack, "code"), "LGTM sent to Slack")
        self.assertIsNone(history.link(slack, "code"))
        existing = entry_for("github", "existing-github-review")
        self.assertIsNone(history.link(existing, "code"))
        self.assertIn("already had a review", history.describe(existing, "code"))


class ReviewHistoryFileTests(unittest.TestCase):
    def test_each_entry_is_one_line_readable_only_by_its_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history" / "run-1.jsonl"
            writer = ReviewHistory(path)
            writer.append(record_for().entry())
            writer.append(record_for(live=False).entry())

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1])["mode"], "dry_run")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(writer.run, "run-1")

    def test_a_line_that_cannot_be_written_never_fails_the_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "history"
            blocker.write_text("a file where the directory should be")
            writer = ReviewHistory(blocker / "run-1.jsonl")

            with patch("sys.stderr", new_callable=StringIO) as errors:
                writer.append(record_for().entry())
                writer.append(record_for().entry())

            self.assertEqual(errors.getvalue().count("review history not recorded"), 1)

    def test_unreadable_lines_are_counted_rather_than_dropped_in_silence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            later = record_for().entry()
            earlier = dict(later, recorded_at="2026-01-01T00:00:00+00:00")
            future = dict(later, schema=history.SCHEMA_VERSION + 1)
            (root / "run-2.jsonl").write_text(
                json.dumps(later) + "\n{not json\n" + json.dumps(future) + "\n"
            )
            (root / "run-1.jsonl").write_text(json.dumps(earlier) + "\n\n")
            (root / "notes.txt").write_text("not history")

            entries, unreadable = history.read_entries(root)

            self.assertEqual(unreadable, 2)
            self.assertEqual(
                [entry["recorded_at"] for entry in entries],
                [earlier["recorded_at"], later["recorded_at"]],
            )

    def test_prune_deletes_only_history_older_than_the_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "run-old.jsonl"
            new = root / "run-new.jsonl"
            other = root / "keep.json"
            for path in (old, new, other):
                path.write_text("{}\n")
            stale = time.time() - 15 * 86400
            os.utime(old, (stale, stale))
            os.utime(other, (stale, stale))

            history.prune(root, 14)

            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(other.exists())
            with self.assertRaises(ValueError):
                history.prune(root, 0)


class HistoryCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.history_directory = self.home / ".state" / "history"
        self.history_directory.mkdir(parents=True)
        environment = patch.dict(
            os.environ, {"PRWARDEN_HOME": str(self.home)}, clear=True
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, *entries: dict, name: str = "run-1.jsonl") -> None:
        with (self.history_directory / name).open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def run_main(self, *argv: str) -> "tuple[int, str, str]":
        lines: "list[str]" = []
        with patch("sys.stderr", new_callable=StringIO) as errors, patch(
            "prwarden.config.run_cmd",
            side_effect=AssertionError("history asked GitHub for something"),
        ):
            code = history.main(list(argv), print_line=lines.append)
        return code, "\n".join(lines), errors.getvalue()

    def delivered(self, *, title: str = "Add a feature") -> dict:
        record = ReviewRecord(
            REPOSITORY,
            PullRequest(7, title, "", "author", "a" * 40, PULL_REQUEST.url),
            run="run-1",
            live=True,
            destination="github",
        )
        record.code_result((FINDING,), approve=False, reason="")
        record.observe_state(
            {}, {head_key(): {"status": "delivered", "receipt_id": "77"}}
        )
        record.spend(125.0, 2, None)
        return record.entry()

    def test_one_pull_request_shows_each_review_with_its_findings_and_link(
        self,
    ) -> None:
        later = self.delivered()
        earlier = dict(
            later,
            recorded_at="2026-01-01T00:00:00+00:00",
            code={"status": "skipped", "reason": "state is delivered"},
        )
        self.write(earlier, later)

        code, output, errors = self.run_main("--repo", "app", "--pr", "7")

        self.assertEqual(code, 0, errors)
        self.assertIn("owner/app #7 Add a feature", output)
        self.assertIn("2 reviews recorded on this host (kept 14 days)", output)
        self.assertIn("code    1 finding posted", output)
        self.assertIn("#pullrequestreview-77", output)
        self.assertIn("high      app.py:3  Unchecked value", output)
        self.assertIn("skipped: state is delivered", output)
        self.assertIn("spent   2m05s of model time across 2 invocations", output)
        # Newest first.
        self.assertLess(output.index("1 finding posted"), output.index("skipped"))

    def test_json_carries_every_entry_newest_first_with_words_and_links(self) -> None:
        older = dict(self.delivered(), recorded_at="2026-01-01T00:00:00+00:00")
        self.write(older, self.delivered())

        code, output, _ = self.run_main("--repo", "OWNER/APP", "--pr", "7", "--json")

        document = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(document["retention_days"], 14)
        self.assertEqual(document["unreadable_lines"], 0)
        self.assertEqual(len(document["entries"]), 2)
        newest = document["entries"][0]
        self.assertNotEqual(newest["recorded_at"], older["recorded_at"])
        self.assertEqual(newest["code"]["summary"], "1 finding posted")
        self.assertTrue(newest["code"]["link"].endswith("#pullrequestreview-77"))
        self.assertEqual(newest["code"]["findings"][0]["path"], "app.py")

    def test_the_overview_reports_what_each_level_last_decided(self) -> None:
        """A later run skips a settled head, which must not hide the decision."""

        decided = dict(self.delivered(), recorded_at="2026-01-01T00:00:00+00:00")
        skipped = dict(
            self.delivered(),
            code={"status": "skipped", "reason": "state is delivered"},
        )
        self.write(decided, skipped)

        code, output, _ = self.run_main()

        self.assertEqual(code, 0)
        self.assertIn("1 pull request reviewed", output)
        self.assertIn("code: 1 finding posted", output)
        self.assertNotIn("code: skipped", output)

    def test_text_from_the_reviewed_project_cannot_drive_the_terminal(self) -> None:
        self.write(self.delivered(title="Fix \x1b]0;owned\x07 the title\nnow"))

        _, output, _ = self.run_main("--repo", "owner/app", "--pr", "7")

        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x07", output)
        self.assertIn("Fix ?]0;owned? the title now", output)

    def test_nothing_recorded_says_so_and_succeeds(self) -> None:
        code, output, _ = self.run_main("--repo", "owner/app", "--pr", "8")

        self.assertEqual(code, 0)
        self.assertIn("No review of owner/app #8 recorded on this host", output)

    def test_an_unreadable_history_is_not_reported_as_complete(self) -> None:
        self.write(self.delivered())
        (self.history_directory / "run-2.jsonl").write_text("{broken\n")

        code, output, errors = self.run_main("--repo", "owner/app", "--pr", "7")

        self.assertEqual(code, 1)
        self.assertIn("1 finding posted", output)
        self.assertIn("1 history line", errors)

    def test_a_number_alone_is_refused_as_ambiguous(self) -> None:
        with patch("sys.stderr", new_callable=StringIO), self.assertRaises(
            SystemExit
        ) as exit_:
            history.main(["--pr", "7"], print_line=lambda line: None)
        self.assertEqual(exit_.exception.code, 2)

    def test_the_history_beside_another_state_file_can_be_read(self) -> None:
        other = self.home / "elsewhere"
        (other / "history").mkdir(parents=True)
        (other / "history" / "run-1.jsonl").write_text(
            json.dumps(self.delivered()) + "\n"
        )

        code, output, _ = self.run_main(
            "--state-file", str(other / "review-state.json"), "--json"
        )

        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(output)["entries"]), 1)

    def test_the_command_is_dispatched_before_the_review_parser(self) -> None:
        with patch("prwarden.history.main", return_value=0) as entry:
            self.assertEqual(cli.main(["history", "--json"]), 0)
        entry.assert_called_once_with(["--json"])


class RecordedReviewTests(unittest.TestCase):
    """What a review writes to the history, end to end through the controller."""

    def setUp(self) -> None:
        without_review_rules(self)
        for target, value in (
            ("prwarden.cli.list_pull_request_checks", ()),
            ("prwarden.cli._read_linked_issues", ()),
            ("prwarden.cli._read_file_history", ()),
            ("prwarden.cli.list_own_advisory_notes", ()),
        ):
            stub = patch(target, return_value=value)
            stub.start()
            self.addCleanup(stub.stop)
        self.addCleanup(cli._PRIMARY_ENGINE.reset)

    def review(
        self,
        directory: str,
        engine: Mock,
        *,
        live: bool = True,
        design_level: bool = False,
        state: "dict[str, str]" = None,
    ) -> "tuple[list[dict], Mock, Path]":
        root = Path(directory)
        (root / "app.py").write_text("value = 1\n", encoding="utf-8")
        repository = Repository("app", "owner/app", root)
        settings = Settings(
            reviewer_username="reviewer",
            repositories=[repository],
            dry_run=not live,
            slack_webhook_url=None,
            state_file=root / "state.json",
            review_engine="antigravity",
            review_engine_command=("agy",),
            review_engine_env_allowlist=(),
            review_model=None,
            review_timeout_seconds=300,
            max_diff_bytes=100000,
            max_findings=10,
            design_level=design_level,
            docs_level=False,
        )
        path = root / "history" / "run-1.jsonl"
        pull_request = PullRequest(
            1, "Title", "", "author", "head", "https://github.com/owner/app/pull/1"
        )
        with patch("prwarden.cli.has_current_review", return_value=False), patch(
            "prwarden.cli.list_prior_review_comments", return_value=()
        ), patch(
            "prwarden.cli.list_changed_files",
            return_value=[{"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}],
        ), patch(
            "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
        ), patch("prwarden.cli.review_workspace") as workspace, patch(
            "prwarden.cli.submit_approval", return_value="501"
        ) as approval, patch(
            "prwarden.cli.submit_review", return_value="502"
        ), patch("sys.stdout", new_callable=StringIO), patch(
            "sys.stderr", new_callable=StringIO
        ):
            workspace.return_value.__enter__.return_value = repository
            workspace.return_value.dependency_links = ()
            workspace.return_value.dependency_notes = ()
            with JsonStateStore(settings.state_file) as store:
                for key, status in (state or {}).items():
                    store.set_status(key, status)
                try:
                    cli._process_pull_request_with_head_restarts(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=live,
                        force=False,
                        history=ReviewHistory(path),
                    )
                except Exception:
                    pass
        entries = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        ]
        return entries, approval, path

    def test_a_live_approval_records_the_receipt_github_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.return_value = {
                "verdict": "approve",
                "approval_reason": "The change is correct.",
                "findings": [],
            }

            entries, approval, _ = self.review(directory, engine)

            approval.assert_called_once()
            (entry,) = entries
            self.assertEqual(entry["mode"], "live")
            self.assertEqual(entry["code"]["status"], "delivered")
            self.assertEqual(entry["code"]["receipt"], "501")
            self.assertEqual(entry["spent"]["invocations"], 1)

    def test_a_dry_run_records_the_findings_it_would_have_posted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                finding_response("app.py", 1),
                CONFIRMED_JUDGMENT,
            ]

            entries, _, _ = self.review(directory, engine, live=False)

            (entry,) = entries
            self.assertEqual(entry["mode"], "dry_run")
            self.assertEqual(entry["code"]["status"], "dry_run")
            self.assertEqual(entry["code"]["findings"][0]["title"], "Incorrect value")

    def test_a_settled_head_records_why_it_was_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()

            entries, _, _ = self.review(
                directory,
                engine,
                state={"owner/app#1#head#github": "delivered"},
            )

            engine.review.assert_not_called()
            (entry,) = entries
            self.assertEqual(entry["code"]["status"], "skipped")
            self.assertEqual(entry["code"]["reason"], "state is delivered")
            # What the earlier run recorded, which the skip alone does not say.
            self.assertEqual(entry["code"]["earlier"]["status"], "delivered")
            self.assertIn(
                ", recorded ", history.describe(entry, "code")
            )
            self.assertNotIn("spent", entry)

    def test_a_failed_review_is_recorded_with_its_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = EngineError("UNAVAILABLE: try again")

            entries, approval, _ = self.review(directory, engine)

            approval.assert_not_called()
            (entry,) = entries
            self.assertEqual(entry["code"]["status"], "failed")
            self.assertIn("UNAVAILABLE", entry["code"]["error"])

    def test_a_failed_design_level_is_recorded_and_the_approval_still_given(
        self,
    ) -> None:
        """The design level is advisory: its failure is its own, never the code's."""

        with tempfile.TemporaryDirectory() as directory:
            engine = Mock()
            engine.review.side_effect = [
                {"understanding": "u", "verdict": "not-a-verdict", "findings": []},
                {"understanding": "u", "verdict": "not-a-verdict", "findings": []},
                {"verdict": "approve", "approval_reason": "Fine.", "findings": []},
            ]

            entries, approval, _ = self.review(directory, engine, design_level=True)

            approval.assert_called_once()
            (entry,) = entries
            self.assertEqual(entry["design"]["status"], "failed")
            self.assertEqual(entry["code"]["status"], "delivered")

    def test_a_run_prunes_the_history_with_the_configured_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository("app", "owner/app", Path(directory))
            settings = Settings(
                reviewer_username="reviewer",
                repositories=[repository],
                dry_run=True,
                slack_webhook_url=None,
                state_file=Path(directory) / "state.json",
                review_engine="antigravity",
                review_engine_command=("agy",),
                review_engine_env_allowlist=(),
                review_model=None,
                review_timeout_seconds=300,
                max_diff_bytes=100000,
                max_findings=10,
                review_history_retention_days=5,
            )
            arguments = Namespace(
                env_file=Path(directory) / ".env",
                repositories_file=None,
                state_file=None,
                repo=[],
                pr=None,
                live=False,
                force=False,
                engine=None,
                concurrency=1,
            )

            with patch("prwarden.cli.load_settings", return_value=settings), patch(
                "prwarden.cli.create_review_engine"
            ), patch("prwarden.cli.list_open_prs", return_value=[]), patch(
                "prwarden.cli.pin_github_account"
            ), patch(
                "prwarden.cli.prune_history"
            ) as prune, patch("sys.stdout", new_callable=StringIO):
                cli.run(arguments)

            prune.assert_called_once_with(Path(directory) / "history", 5)

    def test_the_history_names_the_routes_the_controller_uses(self) -> None:
        self.assertEqual(history.OWN_GITHUB_DESTINATION, cli.OWN_GITHUB_DESTINATION)
        self.assertEqual(
            cli._destination(
                Repository("app", "owner/app", slack=True),
                PullRequest(1, "t", "", "reviewer", "h", "u"),
                "reviewer",
            ),
            history.SLACK_DESTINATION,
        )


if __name__ == "__main__":
    unittest.main()
