"""Tests for the frozen replay harness."""

import json
import src.evaluate as evaluate
import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from src.commands import CommandError
from src.config import ConfigError, Settings
from src.evaluate import (
    ACTED_ON,
    ACTED_ON_AFTER_REBASE,
    BASELINE_ARM,
    CHANGED_ELSEWHERE,
    NOT_ACTED_ON,
    OVERRIDABLE_SETTINGS,
    ReplayCase,
    advisory_case,
    baseline_engine,
    capture,
    case_for_pull_request,
    cases_fingerprint,
    changed_files,
    changed_later,
    classify_outcome,
    control_for,
    current_attribution,
    expected_for,
    final_head_for,
    load_arm,
    load_cases,
    load_labels,
    main,
    measured_engine,
    merge_base,
    mine_controls,
    mine_labels,
    parse_pull_request_spec,
    replay,
    replay_case,
    reviewer_provenance,
    describe_stability,
    candidates_for,
    load_candidates,
    save_candidates,
    score_against_labels,
    stability,
    transcript_name,
    verification_prompts_from,
)
from src.cli import VerificationPass
from src.metrics import compare
from src.engine import DESIGN_LEVEL, DOCS_LEVEL, EngineError, build_prompt
from src.metrics import ReviewMetrics
from src.models import (
    DocsAssessment,
    DocsFinding,
    Finding,
    FindingEvidence,
    PullRequest,
    RejectedDocsCandidate,
    Repository,
    ReviewMaterial,
)
from src.review import ReviewError


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


class LocalDiffTests(unittest.TestCase):
    """The diff a replay reviews comes from git, not from GitHub's files API."""

    def repository(self, directory: str) -> Path:
        root = Path(directory)
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "Replay Test")
        git(root, "config", "user.email", "replay@example.invalid")
        (root / "app.py").write_text("value = 1\n", encoding="utf-8")
        git(root, "add", "app.py")
        git(root, "commit", "-m", "base")
        return root

    def commit(self, root: Path, message: str) -> str:
        git(root, "add", "-A")
        git(root, "commit", "-m", message)
        return git(root, "rev-parse", "HEAD").strip()

    def test_a_changed_file_carries_its_hunks_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            base = git(root, "rev-parse", "HEAD").strip()
            (root / "app.py").write_text("value = 2\nextra = 3\n", encoding="utf-8")
            head = self.commit(root, "change")

            files = changed_files(root, base, head)

            self.assertEqual(len(files), 1)
            record = files[0]
            self.assertEqual(record["filename"], "app.py")
            self.assertEqual(record["additions"], 2)
            self.assertEqual(record["deletions"], 1)
            self.assertEqual(record["changes"], 3)
            self.assertTrue(record["patch"].startswith("@@"))
            self.assertIn("+extra = 3", record["patch"])

    def test_the_patch_never_carries_the_file_header(self) -> None:
        """`+++ b/app.py` would otherwise be read as an added line, and anchorable."""

        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            base = git(root, "rev-parse", "HEAD").strip()
            (root / "app.py").write_text("value = 2\n", encoding="utf-8")
            head = self.commit(root, "change")

            patch = changed_files(root, base, head)[0]["patch"]

            self.assertNotIn("+++", patch)
            self.assertNotIn("diff --git", patch)
            self.assertNotIn("index ", patch)

    def test_a_new_file_is_reported_as_added_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            base = git(root, "rev-parse", "HEAD").strip()
            (root / "new.py").write_text("added = 1\n", encoding="utf-8")
            head = self.commit(root, "add a file")

            record = changed_files(root, base, head)[0]

            self.assertEqual(record["filename"], "new.py")
            self.assertEqual((record["additions"], record["deletions"]), (1, 0))
            self.assertIn("+added = 1", record["patch"])

    def test_a_path_with_a_space_survives_parsing(self) -> None:
        """Parsed from the NUL-separated form precisely so this works."""

        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            base = git(root, "rev-parse", "HEAD").strip()
            (root / "two words.py").write_text("spaced = 1\n", encoding="utf-8")
            head = self.commit(root, "spaced path")

            names = [record["filename"] for record in changed_files(root, base, head)]

            self.assertIn("two words.py", names)

    def test_a_rename_reports_where_it_came_from(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            base = git(root, "rev-parse", "HEAD").strip()
            git(root, "mv", "app.py", "renamed.py")
            head = self.commit(root, "rename")

            record = changed_files(root, base, head)[0]

            self.assertEqual(record["filename"], "renamed.py")
            self.assertEqual(record["previous_filename"], "app.py")
            self.assertEqual(record["status"], "renamed")
            # Nothing changed inside it, so there is no hunk and nothing to review.
            self.assertNotIn("patch", record)

    def test_a_binary_file_reports_no_changed_line(self) -> None:
        """Which is what GitHub reports, and what the textless-file rule reads."""

        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            base = git(root, "rev-parse", "HEAD").strip()
            (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02")
            head = self.commit(root, "add a binary")

            record = [
                entry
                for entry in changed_files(root, base, head)
                if entry["filename"] == "logo.png"
            ][0]

            self.assertEqual(
                (record["additions"], record["deletions"], record["changes"]),
                (0, 0, 0),
            )
            self.assertNotIn("patch", record)

    def test_an_unchanged_range_yields_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            head = git(root, "rev-parse", "HEAD").strip()

            self.assertEqual(changed_files(root, head, head), [])

    def test_the_merge_base_is_where_the_branch_left_the_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory)
            forked_at = git(root, "rev-parse", "HEAD").strip()
            git(root, "checkout", "-q", "-b", "feature")
            (root / "app.py").write_text("value = 2\n", encoding="utf-8")
            head = self.commit(root, "feature work")
            git(root, "checkout", "-q", "main")
            (root / "other.py").write_text("moved = 1\n", encoding="utf-8")
            self.commit(root, "base moved on")

            # merge_base fetches the base branch from origin, which a local
            # repository does not have, so the underlying computation is exercised
            # directly against the branch tip.
            computed = git(root, "merge-base", head, "main").strip()

            self.assertEqual(computed, forked_at)
            self.assertNotIn(
                "other.py",
                [record["filename"] for record in changed_files(root, computed, head)],
            )


class CaseSetTests(unittest.TestCase):
    def write_cases(self, directory: str, payload) -> Path:
        path = Path(directory) / "cases.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def case(self, **overrides) -> dict:
        record = {
            "slug": "owner/app",
            "number": 123,
            "head": "b" * 40,
            "base": "a" * 40,
            "title": "feat: add export button",
            "head_ref": "feat/add-export-button",
            "base_ref": "develop",
            "captured": "2026-09-14",
        }
        record.update(overrides)
        return record

    def test_a_pinned_case_is_read_whole(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cases(directory, {"cases": [self.case()]})

            cases = load_cases(path)

            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0].label, "owner/app#123")
            self.assertEqual(cases[0].base_ref, "develop")

    def test_a_case_missing_what_replay_needs_is_refused(self) -> None:
        for field in ("slug", "number", "head", "base"):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as directory:
                    record = self.case()
                    del record[field]
                    path = self.write_cases(directory, {"cases": [record]})

                    with self.assertRaisesRegex(ConfigError, f"missing '{field}'"):
                        load_cases(path)

    def test_a_commit_that_is_not_an_object_name_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_cases(
                directory, {"cases": [self.case(head="not-a-commit")]}
            )

            with self.assertRaisesRegex(ConfigError, "unusable head commit"):
                load_cases(path)

    def test_an_empty_or_unreadable_set_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            empty = self.write_cases(directory, {"cases": []})
            with self.assertRaisesRegex(ConfigError, "holds no cases"):
                load_cases(empty)
            with self.assertRaisesRegex(ConfigError, "Cannot read"):
                load_cases(Path(directory) / "absent.json")
            broken = Path(directory) / "broken.json"
            broken.write_text("{oops", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "not valid JSON"):
                load_cases(broken)

    def test_the_fingerprint_follows_the_work_not_the_order(self) -> None:
        first = ReplayCase("owner/app", 1, "a" * 40, "b" * 40)
        second = ReplayCase("owner/app", 2, "c" * 40, "d" * 40)

        self.assertEqual(
            cases_fingerprint([first, second]), cases_fingerprint([second, first])
        )
        self.assertNotEqual(
            cases_fingerprint([first, second]), cases_fingerprint([first])
        )
        moved = ReplayCase("owner/app", 2, "e" * 40, "d" * 40)
        self.assertNotEqual(
            cases_fingerprint([first, second]), cases_fingerprint([first, moved])
        )


class ArmTests(unittest.TestCase):
    def write_arms(self, directory: str, arms) -> Path:
        path = Path(directory) / "arms.json"
        path.write_text(json.dumps({"arms": arms}), encoding="utf-8")
        return path

    def settings(self, directory: str) -> Settings:
        repository = Repository("app", "owner/app", Path(directory))
        return Settings(
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
            max_diff_bytes=200000,
            max_findings=10,
        )

    def test_the_baseline_needs_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            arm = load_arm(Path(directory) / "absent.json", "baseline")

            self.assertEqual(arm, BASELINE_ARM)
            self.assertEqual(arm.describe(), {"name": "baseline"})

    def test_an_arm_applies_only_what_it_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_arms(
                directory,
                [{"name": "quick", "design_level": False, "max_findings": 3}],
            )

            arm = load_arm(path, "quick")
            applied = arm.applied_to(self.settings(directory))

            self.assertFalse(applied.design_level)
            self.assertEqual(applied.max_findings, 3)
            self.assertTrue(applied.docs_level)
            self.assertEqual(arm.describe()["max_findings"], 3)

    def test_an_engine_arm_is_reported_for_the_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_arms(directory, [{"name": "claude", "engine": "claude"}])

            arm = load_arm(path, "claude")

            self.assertEqual(arm.engine, "claude")
            self.assertEqual(arm.overrides, {})

    def test_an_arm_may_not_override_what_it_cannot_describe(self) -> None:
        """An arm that could set a verification engine could not set its command."""

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_arms(
                directory, [{"name": "bad", "verify_engine": "kiro", "dry_run": False}]
            )

            with self.assertRaisesRegex(ConfigError, "may not override"):
                load_arm(path, "bad")

    def test_an_unknown_arm_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_arms(directory, [{"name": "baseline"}])

            with self.assertRaisesRegex(ConfigError, "no arm named 'ghost'"):
                load_arm(path, "ghost")

    def test_every_overridable_setting_exists_on_settings(self) -> None:
        """A typo in the allowlist would fail only when someone used that arm."""

        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            for name in OVERRIDABLE_SETTINGS:
                self.assertTrue(hasattr(settings, name), name)

    def test_an_arm_may_vary_the_verifier_effort(self) -> None:
        """The cheap verifier question verify mode exists to ask."""

        with tempfile.TemporaryDirectory() as directory:
            path = self.write_arms(
                directory, [{"name": "harder", "verify_effort": "MAX"}]
            )
            settings = replace(
                self.settings(directory),
                verify_engine="kiro",
                verify_engine_command=("kiro-cli",),
                verify_model="gpt-5.6-terra",
            )

            applied = load_arm(path, "harder").applied_to(settings)

            self.assertEqual(applied.verify_effort, "max")

    def test_an_arm_effort_is_held_to_the_loader_rules(self) -> None:
        """Kiro runs a misspelt level at the default without a word, so an arm that
        named one would measure the default under another name."""

        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            for arm, pattern in (
                ({"name": "typo", "review_effort": "x-high"}, "must be one of"),
                ({"name": "nonstr", "review_effort": 3}, "sets review_effort to 3"),
                (
                    {"name": "orphan", "verify_effort": "high"},
                    "requires a verification engine",
                ),
            ):
                with self.subTest(arm=arm["name"]):
                    path = self.write_arms(directory, [arm])
                    with self.assertRaisesRegex(ConfigError, pattern):
                        load_arm(path, arm["name"]).applied_to(settings)


class CaptureTests(unittest.TestCase):
    def test_capture_pins_the_head_and_the_merge_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = Repository("app", "owner/app", root)
            settings = Settings(
                reviewer_username="reviewer",
                repositories=[repository],
                dry_run=True,
                slack_webhook_url=None,
                state_file=root / "state.json",
                review_engine="antigravity",
                review_engine_command=("agy",),
                review_engine_env_allowlist=(),
                review_model=None,
                review_timeout_seconds=300,
                max_diff_bytes=200000,
                max_findings=10,
            )
            pull_request = PullRequest(
                7,
                "feat: something",
                "Stops trimming trailing spaces on purpose.",
                "author",
                "d" * 40,
                "url",
                head_ref="feat/x",
                base_ref="develop",
            )
            path = root / "evals" / "cases.json"

            with patch(
                "src.evaluate.list_open_prs", return_value=[pull_request]
            ), patch(
                "src.evaluate.merge_base", return_value="e" * 40
            ), patch("builtins.print"):
                cases = capture(settings, path)

            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0].head, "d" * 40)
            self.assertEqual(cases[0].base, "e" * 40)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["cases"][0]["number"], 7)
            self.assertEqual(written["cases"][0]["base_ref"], "develop")
            # The description states intent, and a live review reads it.
            self.assertEqual(
                load_cases(path)[0].body,
                "Stops trimming trailing spaces on purpose.",
            )

    def test_a_repository_whose_base_cannot_be_resolved_is_skipped(self) -> None:
        from src.commands import CommandError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = Repository("app", "owner/app", root)
            settings = Settings(
                reviewer_username="reviewer",
                repositories=[repository],
                dry_run=True,
                slack_webhook_url=None,
                state_file=root / "state.json",
                review_engine="antigravity",
                review_engine_command=("agy",),
                review_engine_env_allowlist=(),
                review_model=None,
                review_timeout_seconds=300,
                max_diff_bytes=200000,
                max_findings=10,
            )
            pull_request = PullRequest(
                7, "t", "", "a", "d" * 40, "url", head_ref="f", base_ref="develop"
            )

            with patch(
                "src.evaluate.list_open_prs", return_value=[pull_request]
            ), patch(
                "src.evaluate.merge_base",
                side_effect=CommandError("no such ref", command="git"),
            ), patch("builtins.print"), patch("sys.stderr"):
                with self.assertRaisesRegex(ConfigError, "no open pull request"):
                    capture(settings, root / "cases.json")


class ProvenanceTests(unittest.TestCase):
    def test_the_measurement_names_the_build_that_produced_it(self) -> None:
        """A prompt is code, so only the commit can identify what was measured."""

        provenance = reviewer_provenance()

        self.assertIn("version", provenance)
        self.assertIn("commit", provenance)
        self.assertIn("dirty", provenance)


class OutcomeClassificationTests(unittest.TestCase):
    """What a later push did to the lines one comment pointed at."""

    def diff(self, *hunks: str) -> str:
        return "\n".join(hunks)

    def test_a_push_that_edited_the_anchor_counts_as_acted_on(self) -> None:
        diff = self.diff("@@ -60,3 +60,4 @@ context", "-  old", "+  new")

        self.assertEqual(classify_outcome(diff, 61), ACTED_ON)

    def test_a_fix_one_line_off_still_counts(self) -> None:
        """A correction often lands beside the line the comment named."""

        diff = self.diff("@@ -60,1 +60,1 @@", "-  old", "+  new")

        self.assertEqual(classify_outcome(diff, 62), ACTED_ON)
        self.assertEqual(classify_outcome(diff, 58), ACTED_ON)

    def test_a_push_elsewhere_in_the_file_is_not_credited(self) -> None:
        diff = self.diff("@@ -200,2 +200,2 @@", "-  far", "+  away")

        self.assertEqual(classify_outcome(diff, 61), CHANGED_ELSEWHERE)

    def test_a_file_nobody_touched_again_is_not_acted_on(self) -> None:
        self.assertEqual(classify_outcome("", 61), NOT_ACTED_ON)
        self.assertEqual(classify_outcome("   \n", 61), NOT_ACTED_ON)

    def test_a_rebase_is_reported_apart_rather_than_credited(self) -> None:
        """A rebase rewrites everything, so it proves nothing about one comment."""

        diff = self.diff("@@ -60,3 +60,4 @@", "-  old", "+  new")

        self.assertEqual(
            classify_outcome(diff, 61, rebased=True), ACTED_ON_AFTER_REBASE
        )

    def test_a_hunk_header_that_cannot_be_read_is_skipped(self) -> None:
        diff = self.diff("@@ nonsense @@", "@@ -60,1 +60,1 @@", "-a", "+b")

        self.assertEqual(classify_outcome(diff, 60), ACTED_ON)

    def test_a_single_line_hunk_without_a_count_is_understood(self) -> None:
        diff = self.diff("@@ -60 +60 @@", "-a", "+b")

        self.assertEqual(classify_outcome(diff, 60), ACTED_ON)


class LabelScoringTests(unittest.TestCase):
    def finding(self, path: str, line: int):
        return Mock(path=path, line=line)

    def expected(self, path: str, line: int, outcome: str = ACTED_ON) -> dict:
        return {"path": path, "line": line, "outcome": outcome, "title": "t"}

    def test_a_confirmed_finding_at_the_anchor_is_reproduced(self) -> None:
        score = score_against_labels(
            [self.expected("app.py", 10)],
            [self.finding("app.py", 10)],
            [self.finding("app.py", 10)],
        )

        self.assertEqual((score["acted_on"], score["raised"]), (1, 1))
        self.assertEqual(score["reproduced"], 1)
        self.assertEqual(score["missed"], [])

    def test_raised_and_confirmed_are_counted_apart(self) -> None:
        """The gap says the verifier changed, not the review, and they differ."""

        score = score_against_labels(
            [self.expected("app.py", 10)],
            [self.finding("app.py", 10)],
            [],
        )

        self.assertEqual(score["raised"], 1)
        self.assertEqual(score["reproduced"], 0)
        self.assertEqual(score["missed"], ["app.py:10"])

    def test_only_what_authors_acted_on_is_scored(self) -> None:
        score = score_against_labels(
            [
                self.expected("app.py", 10, NOT_ACTED_ON),
                self.expected("other.py", 3, CHANGED_ELSEWHERE),
                self.expected("third.py", 7, ACTED_ON_AFTER_REBASE),
            ],
            [self.finding("app.py", 10)],
            [self.finding("app.py", 10)],
        )

        self.assertEqual(score["acted_on"], 0)
        self.assertEqual(score["reproduced"], 0)

    def test_a_finding_on_another_file_does_not_count(self) -> None:
        score = score_against_labels(
            [self.expected("app.py", 10)],
            [self.finding("other.py", 10)],
            [self.finding("other.py", 10)],
        )

        self.assertEqual(score["reproduced"], 0)
        self.assertEqual(score["missed"], ["app.py:10"])

    def test_nothing_expected_scores_nothing(self) -> None:
        score = score_against_labels([], [self.finding("app.py", 1)], [])

        self.assertEqual(score["acted_on"], 0)


class PipelineComparabilityTests(unittest.TestCase):
    """A defect an author fixed is real whoever found it, but who verified it decides
    how a rejection reads."""

    VERIFIED = "Reviewed by X @ E, verified by V @ K"
    UNVERIFIED = "Reviewed by Older @ E"

    def finding(self, path: str, line: int):
        return Mock(path=path, line=line)

    def expected(self, line: int, attribution: str) -> dict:
        return {
            "path": "app.py",
            "line": line,
            "outcome": ACTED_ON,
            "attribution": attribution,
        }

    def test_a_finding_another_pipeline_delivered_is_scored(self) -> None:
        """Scoring only the current pipeline's own findings scored nothing at all
        once the reviewer model changed."""

        score = score_against_labels(
            [self.expected(10, self.VERIFIED), self.expected(50, self.UNVERIFIED)],
            [self.finding("app.py", 10)],
            [self.finding("app.py", 10)],
        )

        self.assertEqual(score["acted_on"], 2)
        self.assertEqual((score["raised"], score["reproduced"]), (1, 1))
        self.assertEqual(score["missed"], ["app.py:50"])

    def test_an_unverified_origin_is_counted(self) -> None:
        score = score_against_labels(
            [
                self.expected(10, self.VERIFIED),
                self.expected(50, self.UNVERIFIED),
                self.expected(90, ""),
            ],
            [],
            [],
        )

        self.assertEqual(score["unverified_origin"], 2)

    def test_throwing_out_an_unverified_finding_is_named_not_blamed(self) -> None:
        """One such finding read as a verifier regression until its comment was
        checked: the verifier had never seen it, because none was configured then."""

        score = score_against_labels(
            [self.expected(10, self.UNVERIFIED), self.expected(50, self.VERIFIED)],
            [self.finding("app.py", 10), self.finding("app.py", 50)],
            [],
        )

        self.assertEqual(score["first_judged"], ["app.py:10"])
        self.assertEqual(score["missed"], ["app.py:10", "app.py:50"])

    def test_an_unverified_finding_never_raised_is_not_first_judged(self) -> None:
        """The verifier cannot be blamed or excused for a claim it was never shown."""

        score = score_against_labels([self.expected(10, self.UNVERIFIED)], [], [])

        self.assertEqual(score["first_judged"], [])

    def test_an_unverified_finding_kept_is_not_first_judged(self) -> None:
        score = score_against_labels(
            [self.expected(10, self.UNVERIFIED)],
            [self.finding("app.py", 10)],
            [self.finding("app.py", 10)],
        )

        self.assertEqual(score["first_judged"], [])
        self.assertEqual(score["reproduced"], 1)

    def test_the_current_pipeline_is_described_by_the_delivery_formatter(self) -> None:
        """The recorded and the current string must be produced the same way, or
        every comparison turns into parsing rules that drift."""

        settings = Mock(
            review_engine="antigravity",
            review_model="gemini-3.1-pro-high",
            review_verify_engine="kiro",
            review_verify_model="gpt-5.6-terra",
        )
        identity = ("kiro", "gpt-5.6-terra")
        with patch("src.cli._verifier_identity", return_value=identity):
            described = current_attribution(settings)

        self.assertIn("Reviewed by", described)
        self.assertIn("verified by", described)


class LabelledOnlySelectionTests(unittest.TestCase):
    """Replaying a case with nothing scoreable spends an invocation to learn
    nothing."""

    def setUp(self) -> None:
        # Pinning asks the real GitHub CLI for a stored login, which no test has.
        pinning = patch("src.evaluate.pin_github_account")
        pinning.start()
        self.addCleanup(pinning.stop)

    def write_corpus(
        self, root: Path, outcomes: "tuple[str, str]"
    ) -> "tuple[Path, Path]":
        cases_path = root / "cases.json"
        labels_path = root / "labels.json"
        cases_path.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "slug": "owner/app",
                            "number": 1,
                            "head": "a" * 40,
                            "base": "b" * 40,
                        },
                        {
                            "slug": "owner/app",
                            "number": 2,
                            "head": "c" * 40,
                            "base": "d" * 40,
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        labels_path.write_text(
            json.dumps(
                {
                    "labels": {
                        f"owner/app#{number}@{head * 12}": {
                            "slug": "owner/app",
                            "number": number,
                            "expected": [
                                {
                                    "path": "app.py",
                                    "line": number,
                                    "outcome": outcome,
                                    # Another pipeline's finding, which must still
                                    # make its case scoreable.
                                    "attribution": "Reviewed by Older @ E",
                                }
                            ],
                        }
                        for number, head, outcome in zip(
                            (1, 2), ("a", "c"), outcomes
                        )
                    }
                }
            ),
            encoding="utf-8",
        )
        return cases_path, labels_path

    def run_replay(self, root: Path, outcomes: "tuple[str, str]", *flags: str):
        cases_path, labels_path = self.write_corpus(root, outcomes)
        settings = Mock(
            state_file=root / ".state" / "state.json",
            run_log_retention_days=7,
            repositories=(),
        )
        with patch("src.evaluate.load_settings", return_value=settings), patch(
            "src.evaluate.replay", return_value=0
        ) as replay_call, patch("src.evaluate.load_arm") as load_arm:
            load_arm.return_value.applied_to.return_value = settings
            exit_code = main(
                [
                    "--cases",
                    str(cases_path),
                    "--labels",
                    str(labels_path),
                    "replay",
                    *flags,
                ]
            )
        return exit_code, replay_call

    def test_only_cases_carrying_an_acted_on_finding_are_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, replay_call = self.run_replay(
                Path(directory), (ACTED_ON, NOT_ACTED_ON), "--labelled-only"
            )

            self.assertEqual(exit_code, 0)
            replayed = replay_call.call_args.args[1]
            self.assertEqual([case.number for case in replayed], [1])

    def test_a_finding_another_pipeline_posted_makes_its_case_scoreable(self) -> None:
        """Selecting by the current pipeline's own findings selected nothing once
        the reviewer model changed."""

        with tempfile.TemporaryDirectory() as directory:
            _, replay_call = self.run_replay(
                Path(directory), (ACTED_ON, ACTED_ON), "--labelled-only"
            )

            replayed = replay_call.call_args.args[1]
            self.assertEqual([case.number for case in replayed], [1, 2])

    def test_without_the_flag_every_pinned_case_is_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, replay_call = self.run_replay(
                Path(directory), (ACTED_ON, NOT_ACTED_ON)
            )

            replayed = replay_call.call_args.args[1]
            self.assertEqual([case.number for case in replayed], [1, 2])

    def test_nothing_scoreable_is_refused_not_silently_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, replay_call = self.run_replay(
                Path(directory), (NOT_ACTED_ON, CHANGED_ELSEWHERE), "--labelled-only"
            )

            self.assertEqual(exit_code, 2)
            self.assertFalse(replay_call.called)

    def test_concurrency_reaches_the_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, replay_call = self.run_replay(
                Path(directory), (ACTED_ON, ACTED_ON), "--concurrency", "3"
            )

            self.assertEqual(replay_call.call_args.kwargs["concurrency"], 3)

    def test_concurrency_outside_the_bound_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exit_code, replay_call = self.run_replay(
                Path(directory), (ACTED_ON, ACTED_ON), "--concurrency", "9"
            )

            self.assertEqual(exit_code, 2)
            self.assertFalse(replay_call.called)


class ReplayBundlingTests(unittest.TestCase):
    """The harness must bundle exactly as a live review does.

    A measurement that cannot see a change cannot measure it, and bundling was the
    change this harness was built to judge.
    """

    def test_a_replay_reviews_every_bundle(self) -> None:
        case = ReplayCase("owner/app", 1, "a" * 40, "b" * 40)
        settings = Mock(
            max_diff_bytes=600,
            max_review_bundles=4,
            ignored_missing_patch_patterns=(),
            state_file=Path("/tmp/state.json"),
            repositories=(Repository("app", "owner/app", Path("/tmp")),),
        )
        primary = Mock(diff="one", added_lines={"a.py": frozenset({1})})
        second = Mock(
            diff="two",
            added_lines={"b.py": frozenset({1})},
            diff_budget=Mock(reviewed_files=1),
        )
        with patch("src.evaluate._repository_for") as repository_for, patch(
            "src.evaluate.changed_files", return_value=[{"filename": "a.py"}]
        ), patch("src.cli._bundled_material", return_value=(primary, (second,))), patch(
            "src.cli._bundle_material_from", return_value=second
        ), patch("src.cli.create_review_engine"), patch(
            "src.cli._create_verification_pass"
        ), patch("src.cli._create_fallback_reviewer", return_value=None), patch(
            "src.cli.review_workspace"
        ) as workspace, patch(
            "src.evaluate.read_background", return_value=""
        ), patch("src.cli._design_level"), patch("src.cli._docs_level"), patch(
            "src.cli._report_diff_budget"
        ), patch(
            "src.cli._review_with_engine_fallback"
        ) as review, patch(
            "src.evaluate.replace", side_effect=lambda material, **kw: material
        ):
            repository_for.return_value = Repository("app", "owner/app", Path("/tmp"))
            workspace.return_value.__enter__.return_value = repository_for.return_value
            workspace.return_value.dependency_links = ()
            workspace.return_value.dependency_notes = ()
            review.return_value = ({}, [], True, "", [], settings, Mock())

            replay_case(settings, case, ReviewMetrics())

        reviewed = [call.args[5] for call in review.call_args_list]
        self.assertEqual(len(reviewed), 2)
        self.assertIs(reviewed[1], second)


class ReplayResilienceTests(unittest.TestCase):
    def test_one_engine_failure_does_not_cost_the_cases_behind_it(self) -> None:
        """A quota refusal ended a measurement two cases into seventeen."""

        cases = (
            ReplayCase("owner/app", 1, "a" * 40, "b" * 40),
            ReplayCase("owner/app", 2, "c" * 40, "d" * 40),
        )
        settings = Mock(
            review_engine="antigravity",
            review_model="gemini-3.1-pro-high",
            max_review_bundles=1,
        )
        spent = EngineError(
            "RESOURCE_EXHAUSTED (code 429): Individual quota reached. "
            "Resets in 17h59m45s."
        )
        with patch(
            "src.evaluate.replay_case",
            side_effect=[spent, {}],
        ) as replay_case_mock, patch(
            "src.evaluate.reviewer_provenance", return_value={}
        ), patch(
            "src.evaluate.cases_fingerprint", return_value="f"
        ), patch(
            "sys.stdout", new_callable=StringIO
        ):
            exit_code = replay(settings, cases, BASELINE_ARM, None)

        # A spent quota will refuse the retry too, so the case is not re-attempted.
        self.assertEqual(replay_case_mock.call_count, 2)
        self.assertEqual(exit_code, 1)

    def test_a_workspace_that_cannot_be_prepared_fails_one_case(self) -> None:
        """One git fetch that timed out ended a two-pass run nine cases in."""

        from src.workspace import WorkspaceError

        cases = (
            ReplayCase("owner/app", 1, "a" * 40, "b" * 40),
            ReplayCase("owner/app", 2, "c" * 40, "d" * 40),
        )
        settings = Mock(review_engine="kiro", review_model="m", max_review_bundles=1)
        document: dict = {"summary": {}}
        metrics = Mock()
        metrics.document.return_value = document
        lost = WorkspaceError("Cannot prepare PR head: Command timed out after 180s")
        with patch(
            "src.evaluate.replay_case", side_effect=[lost, {}, lost]
        ) as replay_case_mock, patch(
            "src.evaluate.ReviewMetrics", return_value=metrics
        ), patch(
            "src.evaluate.reviewer_provenance", return_value={}
        ), patch(
            "src.evaluate.cases_fingerprint", return_value="f"
        ), patch(
            "sys.stdout", new_callable=StringIO
        ), patch(
            "sys.stderr", new_callable=StringIO
        ):
            exit_code = replay(settings, cases, BASELINE_ARM, None)

        # Both cases ran, and the lost one was given its one retry.
        self.assertEqual(replay_case_mock.call_count, 3)
        self.assertEqual(exit_code, 1)
        self.assertEqual(document["cases"]["scored"], ["owner/app#2@cccccccccccc"])
        self.assertEqual(document["cases"]["failed"], ["owner/app#1@aaaaaaaaaaaa"])

    def test_a_case_lost_mid_stream_is_re_attempted(self) -> None:
        """A cut response took the case with it and shrank a pass from seventeen to
        thirteen, which made two arms look different when they measured different
        corpora. Two of five such failures recovered on retry."""

        cases = (
            ReplayCase("owner/app", 1, "a" * 40, "b" * 40),
            ReplayCase("owner/app", 2, "c" * 40, "d" * 40),
        )
        settings = Mock(
            review_engine="antigravity",
            review_model="gemini-3.1-pro-high",
            max_review_bundles=1,
        )
        interrupted = EngineError(
            "Antigravity review failed with status 'ERROR': The stream was "
            "interrupted. Please continue the task you were working on."
        )
        document: dict = {"summary": {}}
        metrics = Mock()
        metrics.document.return_value = document
        with patch(
            "src.evaluate.replay_case",
            side_effect=[interrupted, {}, {}],
        ) as replay_case_mock, patch(
            "src.evaluate.ReviewMetrics", return_value=metrics
        ), patch(
            "src.evaluate.reviewer_provenance", return_value={}
        ), patch(
            "src.evaluate.cases_fingerprint", return_value="f"
        ), patch(
            "sys.stdout", new_callable=StringIO
        ):
            exit_code = replay(settings, cases, BASELINE_ARM, None)

        # Three calls: both cases, then the interrupted one again.
        self.assertEqual(replay_case_mock.call_count, 3)
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["cases"]["failed"], [])
        self.assertEqual(len(document["cases"]["scored"]), 2)

    def test_a_pass_names_the_cases_it_measured(self) -> None:
        """Comparing two runs over different subsets as though they covered the same
        corpus read a smaller sample as a regression."""

        cases = (
            ReplayCase("owner/app", 1, "a" * 40, "b" * 40),
            ReplayCase("owner/app", 2, "c" * 40, "d" * 40),
        )
        settings = Mock(
            review_engine="antigravity",
            review_model="gemini-3.1-pro-high",
            max_review_bundles=1,
        )
        spent = EngineError("Individual quota reached. Resets in 17h59m45s.")
        document: dict = {"summary": {}}
        metrics = Mock()
        metrics.document.return_value = document
        with patch(
            "src.evaluate.replay_case", side_effect=[{}, spent]
        ), patch(
            "src.evaluate.ReviewMetrics", return_value=metrics
        ), patch(
            "src.evaluate.reviewer_provenance", return_value={}
        ), patch(
            "src.evaluate.cases_fingerprint", return_value="f"
        ), patch(
            "sys.stdout", new_callable=StringIO
        ):
            replay(settings, cases, BASELINE_ARM, None)

        # Anchored, because a pull request reviewed at two heads is two cases.
        self.assertEqual(document["cases"]["scored"], ["owner/app#1@aaaaaaaaaaaa"])
        self.assertEqual(document["cases"]["failed"], ["owner/app#2@cccccccccccc"])


class VerifySelectionTests(unittest.TestCase):
    """Verify mode judges recordings, so it should go to them directly."""

    def test_a_case_is_named_with_the_head_it_is_pinned_to(self) -> None:
        case = ReplayCase("owner/app", 7, "a" * 40, "b" * 40)

        self.assertEqual(case.pinned, "owner/app#7@aaaaaaaaaaaa")

    def test_two_heads_of_one_pull_request_are_two_identities(self) -> None:
        """Rendered by label alone they were indistinguishable, and a set of them
        silently held one."""

        first = ReplayCase("owner/app", 7, "a" * 40, "c" * 40)
        second = ReplayCase("owner/app", 7, "b" * 40, "c" * 40)

        self.assertEqual(first.label, second.label)
        self.assertNotEqual(first.pinned, second.pinned)
        self.assertEqual(len({first.pinned, second.pinned}), 2)


class VerifyNarrowingTests(unittest.TestCase):
    """Verify mode judges recordings. Sweeping the corpus printed a skip line for
    thirty-four cases and buried the thirteen that were the point."""

    def _args(self, directory):
        return argparse.Namespace(
            command="verify",
            cases=Path(directory) / "cases.json",
            labels=Path(directory) / "labels.json",
            candidates=Path(directory) / "candidates.json",
            arm="baseline",
            arms=Path(directory) / "arms.json",
            metrics=None,
            case=(),
            passes=1,
            repo=(),
        )

    def _corpus(self, directory):
        cases = [
            {"slug": "owner/app", "number": n, "head": c * 40, "base": "b" * 40}
            for n, c in ((1, "a"), (2, "d"), (3, "e"))
        ]
        (Path(directory) / "cases.json").write_text(
            json.dumps({"cases": cases}), encoding="utf-8"
        )
        (Path(directory) / "labels.json").write_text(
            json.dumps({"labels": {}}), encoding="utf-8"
        )
        (Path(directory) / "arms.json").write_text(
            json.dumps({"arms": []}), encoding="utf-8"
        )
        # Only the middle case has anything recorded.
        (Path(directory) / "candidates.json").write_text(
            json.dumps(
                {
                    "candidates": {
                        "owner/app#2@dddddddddddd": {
                            "slug": "owner/app",
                            "number": 2,
                            "head": "d" * 40,
                            "base": "b" * 40,
                            "reviewer": "Reviewed by X",
                            "recorded": "2026-09-24T00:00:00",
                            "candidates": [],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_only_cases_with_recordings_are_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._corpus(directory)
            args = self._args(directory)
            with patch("src.evaluate.replay", return_value=0) as replay_mock, patch(
                "sys.stdout", new_callable=StringIO
            ) as out:
                evaluate._run_command(args, Mock(review_engine="kiro"))

        replayed = replay_mock.call_args[0][1]
        self.assertEqual(
            [case.pinned for case in replayed], ["owner/app#2@dddddddddddd"]
        )
        written = out.getvalue()
        self.assertIn("Re-judging the 1 case(s)", written)
        self.assertIn("leaving out 2 with nothing recorded", written)
        self.assertNotIn("no recorded candidates; skipped", written)

    def test_nothing_recorded_for_the_selection_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._corpus(directory)
            args = self._args(directory)
            args.case = ("owner/app#1",)
            with patch("sys.stdout", new_callable=StringIO):
                with self.assertRaisesRegex(ConfigError, "No recorded candidates"):
                    evaluate._run_command(args, Mock(review_engine="kiro"))


class MeasuredProvenanceTests(unittest.TestCase):
    """A measurement has to name what produced it. Two passes archived on 2026-09-24
    named an engine and a model neither of them called, and the same arm."""

    def _settings(self, **overrides):
        base = dict(
            reviewer_username="reviewer",
            repositories=[],
            dry_run=True,
            slack_webhook_url=None,
            state_file=Path("/tmp/state.json"),
            review_engine="antigravity",
            review_engine_command=("agy",),
            review_engine_env_allowlist=(),
            review_model="gemini-3.1-pro-high",
            review_timeout_seconds=300,
            max_diff_bytes=200000,
            max_findings=10,
            verify_engine="kiro",
            verify_engine_command=("kiro",),
            verify_model="gpt-5.6-terra",
        )
        base.update(overrides)
        return Settings(**base)

    def test_a_replay_names_the_review_engine(self) -> None:
        engine, model = measured_engine(self._settings(), verifying=False)

        self.assertEqual((engine, model), ("antigravity", "gemini-3.1-pro-high"))

    def test_a_verification_pass_names_the_engine_it_called(self) -> None:
        """Every invocation of both archived passes went to kiro."""

        engine, model = measured_engine(self._settings(), verifying=True)

        self.assertEqual((engine, model), ("kiro", "gpt-5.6-terra"))

    def test_without_a_separate_verifier_the_primary_engine_really_ran(self) -> None:
        settings = self._settings(verify_engine=None, verify_model=None)

        engine, model = measured_engine(settings, verifying=True)

        self.assertEqual((engine, model), ("antigravity", "gemini-3.1-pro-high"))

    def test_a_verifier_matching_the_primary_is_the_primary(self) -> None:
        """A second pass on the same engine and model is not an independent one."""

        settings = self._settings(
            verify_engine="antigravity", verify_model="gemini-3.1-pro-high"
        )

        engine, model = measured_engine(settings, verifying=True)

        self.assertEqual((engine, model), ("antigravity", "gemini-3.1-pro-high"))

    def _document(self, *, recorded):
        cases = (ReplayCase("owner/app", 1, "a" * 40, "b" * 40),)
        document: dict = {"summary": {}}
        metrics = Mock()
        metrics.document.return_value = document
        with patch("src.evaluate.replay_case", return_value={}), patch(
            "src.evaluate.ReviewMetrics", return_value=metrics
        ), patch("src.evaluate.reviewer_provenance", return_value={}), patch(
            "src.evaluate.cases_fingerprint", return_value="f"
        ), patch(
            "src.evaluate.candidates_for", return_value=([], "Reviewed by X")
        ), patch(
            "sys.stdout", new_callable=StringIO
        ):
            replay(
                self._settings(),
                cases,
                BASELINE_ARM,
                None,
                recorded=recorded,
            )
        return document, metrics

    def test_a_verification_document_records_the_verifier(self) -> None:
        document, metrics = self._document(recorded={"candidates": {}})

        metrics.document.assert_called_once_with(
            engine="kiro", model="gpt-5.6-terra"
        )

    def test_a_verification_document_claims_no_arm(self) -> None:
        """No arm may describe the verifier, so stamping "baseline" on a verification
        pass claimed the baseline for whichever tree was under measurement."""

        document, _ = self._document(recorded={"candidates": {}})

        self.assertNotIn("arm", document)

    def test_a_replay_document_records_its_arm(self) -> None:
        document, _ = self._document(recorded=None)

        self.assertEqual(document["arm"], {"name": "baseline"})


class ReplayEvidenceTests(unittest.TestCase):
    def test_a_confirmed_finding_is_named_in_the_transcript(self) -> None:
        """A replay delivers nothing, so its transcript is the only record."""

        case = ReplayCase("owner/app", 1, "a" * 40, "b" * 40)
        settings = Mock(
            max_diff_bytes=600,
            max_review_bundles=1,
            ignored_missing_patch_patterns=(),
            state_file=Path("/tmp/state.json"),
        )
        finding = Mock(
            severity="medium", path="app.py", line=7, title="Stale value kept"
        )
        with patch("src.evaluate._repository_for") as repository_for, patch(
            "src.evaluate.changed_files", return_value=[{"filename": "app.py"}]
        ), patch(
            "src.cli._bundled_material", return_value=(Mock(), ())
        ), patch("src.cli.create_review_engine"), patch(
            "src.cli._create_verification_pass"
        ), patch("src.cli._create_fallback_reviewer", return_value=None), patch(
            "src.cli.review_workspace"
        ) as workspace, patch(
            "src.evaluate.read_background", return_value=""
        ), patch("src.cli._design_level"), patch("src.cli._docs_level"), patch(
            "src.cli._report_diff_budget"
        ), patch(
            "src.cli._review_with_engine_fallback"
        ) as review, patch(
            "src.cli._verify_code_candidates"
        ) as verify, patch(
            "src.evaluate.replace", side_effect=lambda material, **kw: material
        ), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            repository_for.return_value = Repository("app", "owner/app", Path("/tmp"))
            workspace.return_value.__enter__.return_value = repository_for.return_value
            workspace.return_value.dependency_links = ()
            workspace.return_value.dependency_notes = ()
            review.return_value = ({}, [finding], False, "", [], settings, Mock())
            verify.return_value = ({}, [finding], False, "", [])

            replay_case(settings, case, ReviewMetrics())

        reported = output.getvalue()
        self.assertIn("Confirmed finding [MEDIUM] app.py:7 Stale value kept", reported)


class PassStabilityTests(unittest.TestCase):
    """One pass reports one draw from what the reviewer might have said."""

    def test_the_same_rate_twice_can_still_be_a_different_set(self) -> None:
        """This is the trap: a steady rate over a moving set measures nothing."""

        known = {f"f{index}" for index in range(1, 22)}
        hits = {f"f{index}": 2 for index in range(1, 6)}
        hits.update({f"f{index}": 1 for index in range(6, 12)})

        summary = stability(hits, known, [0.381, 0.381])

        self.assertEqual(summary["spread"], 0.0)
        self.assertEqual(summary["moved"], 6)
        self.assertEqual(len(summary["always"]), 5)
        self.assertEqual(len(summary["never"]), 10)
        self.assertEqual(summary["stable_rate"], round(5 / 21, 3))

    def test_the_report_says_findings_moved_even_when_the_rate_did_not(self) -> None:
        known = {"a.py:1", "b.py:2"}
        lines = describe_stability(
            stability({"a.py:1": 2, "b.py:2": 1}, known, [0.5, 0.5])
        )

        self.assertIn("spread of 0.0", lines[0])
        self.assertTrue(any("1 findings moved between passes" in l for l in lines))

    def test_a_reviewer_that_agrees_with_itself_reports_no_movement(self) -> None:
        known = {"a.py:1", "b.py:2"}

        summary = stability({"a.py:1": 3, "b.py:2": 3}, known, [1.0, 1.0, 1.0])

        self.assertEqual(summary["moved"], 0)
        self.assertEqual(summary["stable_rate"], 1.0)
        self.assertEqual(summary["never"], [])

    def test_one_pass_reports_no_stability_at_all(self) -> None:
        """With a single draw there is nothing to say about spread, so it says none."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Mock(
                state_file=root / "state.json",
                review_engine="antigravity",
                review_model="m",
                max_review_bundles=1,
            )
            with patch(
                "src.evaluate.replay_case",
                return_value={
                    "acted_on": 1,
                    "reproduced": 1,
                    "raised": 1,
                    "unverified_origin": 0,
                    "missed": [],
                    "anchors": ["a.py:1"],
                    "reproduced_anchors": ["a.py:1"],
                },
            ), patch("src.evaluate.reviewer_provenance", return_value={}), patch(
                "src.evaluate.cases_fingerprint", return_value="f"
            ), patch("sys.stdout", new_callable=StringIO) as output:
                replay(
                    settings,
                    (ReplayCase("owner/app", 1, "a" * 40, "b" * 40),),
                    BASELINE_ARM,
                    None,
                    {},
                    1,
                )

        self.assertNotIn("Across", output.getvalue())

    def test_more_than_one_pass_replays_every_case_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Mock(
                state_file=root / "state.json",
                review_engine="antigravity",
                review_model="m",
                max_review_bundles=1,
            )
            cases = (
                ReplayCase("owner/app", 1, "a" * 40, "b" * 40),
                ReplayCase("owner/app", 2, "c" * 40, "d" * 40),
            )
            with patch(
                "src.evaluate.replay_case",
                return_value={
                    "acted_on": 1,
                    "reproduced": 0,
                    "raised": 0,
                    "unverified_origin": 0,
                    "missed": ["a.py:1"],
                    "anchors": ["a.py:1"],
                    "reproduced_anchors": [],
                },
            ) as replay_case_mock, patch(
                "src.evaluate.reviewer_provenance", return_value={}
            ), patch("src.evaluate.cases_fingerprint", return_value="f"), patch(
                "sys.stdout", new_callable=StringIO
            ) as output:
                replay(settings, cases, BASELINE_ARM, None, {}, 3)

        self.assertEqual(replay_case_mock.call_count, 6)
        self.assertIn("Across 3 passes", output.getvalue())


class ParallelReplayTests(unittest.TestCase):
    """Running cases at the same time may change when each is measured, never what
    the measurement says."""

    CASES = tuple(
        ReplayCase("owner/app", number, f"{number:x}" * 40, "b" * 40)
        for number in (1, 2, 3, 4)
    )
    threads: "set[str]" = set()

    def settings(self, root: Path) -> Mock:
        settings = Mock(
            state_file=root / "state.json",
            review_engine="kiro",
            review_model="m",
            max_review_bundles=1,
        )
        for name in evaluate.OVERRIDABLE_SETTINGS:
            setattr(settings, name, None)
        return settings

    @staticmethod
    def score(settings, case, metrics, expected, **_options) -> dict:
        ParallelReplayTests.threads.add(threading.current_thread().name)
        # Later cases finish first, so a parallel run completes out of order.
        print(f"start {case.number}")
        time.sleep(0.02 * (5 - case.number))
        print(f"end {case.number}")
        found = case.number % 2 == 1
        return {
            "acted_on": 1,
            "unverified_origin": 0,
            # The fourth is never raised, so what the verifier kept and what the
            # review reproduced are different fractions.
            "raised": 1 if case.number < 4 else 0,
            "reproduced": 1 if found else 0,
            "missed": [] if found else [f"case{case.number}.py:1"],
            "first_judged": [],
            "anchors": [f"case{case.number}.py:1"],
            "reproduced_anchors": [f"case{case.number}.py:1"] if found else [],
        }

    def measure(self, concurrency: int) -> "tuple[dict, str]":
        ParallelReplayTests.threads = set()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "measurement.json"
            with patch("src.evaluate.replay_case", side_effect=self.score), patch(
                "src.evaluate.reviewer_provenance", return_value={}
            ), patch("src.evaluate.cases_fingerprint", return_value="f"), patch(
                "sys.stdout", new_callable=StringIO
            ) as output:
                replay(
                    self.settings(root),
                    self.CASES,
                    BASELINE_ARM,
                    str(destination),
                    {},
                    1,
                    concurrency=concurrency,
                )
                document = json.loads(destination.read_text(encoding="utf-8"))
        return document, output.getvalue()

    def test_a_parallel_replay_measures_what_a_sequential_one_does(self) -> None:
        sequential, _ = self.measure(1)
        self.assertEqual(len(self.threads), 1)
        parallel, _ = self.measure(3)

        self.assertGreater(len(self.threads), 1)
        self.assertEqual(parallel["labels"], sequential["labels"])
        self.assertEqual(parallel["labels_missed"], sequential["labels_missed"])
        self.assertEqual(parallel["cases"]["scored"], sequential["cases"]["scored"])
        self.assertEqual(parallel["labels"]["reproduced"], 2)

    def test_each_case_reports_as_one_block(self) -> None:
        """Interleaved, the transcript of a parallel pass could not be read back."""

        _, output = self.measure(3)
        lines = [line for line in output.splitlines() if line[:3] in ("sta", "end")]

        self.assertEqual(len(lines), 8)
        for start, end in zip(lines[::2], lines[1::2]):
            self.assertEqual(start.replace("start", "end"), end)
        self.assertIn("3 at a time", output)

    def test_the_verifier_is_judged_on_the_real_defects_it_was_shown(self) -> None:
        """A verifier arm that kept 4 of 5 labelled defects had the lower
        confirmation rate, so the confirmation rate cannot judge one."""

        document, output = self.measure(1)

        self.assertEqual(document["labels"]["retention_rate"], 0.667)
        self.assertEqual(document["labels"]["reproduction_rate"], 0.5)
        self.assertIn("The verifier kept 2 of the 3 labelled defects", output)

    def test_parallel_cases_never_lose_each_others_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            cases = [
                ReplayCase("owner/app", number, f"{number:040x}", "b" * 40)
                for number in range(1, 25)
            ]
            threads = [
                threading.Thread(
                    target=save_candidates, args=(path, case, [], "Reviewed by X")
                )
                for case in cases
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            stored = load_candidates(path)

        self.assertEqual(len(stored), 24)


class RecordedCandidateTests(unittest.TestCase):
    """The reviewer is the expensive half; keeping its output makes the other half
    cheap to question."""

    def finding(self) -> Finding:
        return Finding(
            severity="medium",
            title="Counter is not reset",
            path="app.py",
            line=110,
            problem="State survives a close.",
            suggestion="Reset on close.",
            trigger="Reopen the dialog.",
            impact="A stale count is submitted.",
            evidence=(
                FindingEvidence("app.py", 110, "const [count, setCount]", "state"),
            ),
            counterevidence=(FindingEvidence("app.py", 4, "import x", "unrelated"),),
            counterevidence_reason="No reset exists elsewhere.",
        )

    def test_a_candidate_survives_the_round_trip_with_its_citations(self) -> None:
        """A citation the controller validated must come back exactly, or the
        verifier would judge a different claim than the one that was made."""

        case = ReplayCase("owner/app", 1, "a" * 40, "b" * 40)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            save_candidates(path, case, [self.finding()], "Reviewed by X @ E")

            restored = candidates_for(load_candidates(path), case)

        self.assertIsNotNone(restored)
        findings, proposer = restored
        self.assertEqual(proposer, "Reviewed by X @ E")
        self.assertEqual(findings, [self.finding()])

    def test_recording_a_case_leaves_the_others_alone(self) -> None:
        first = ReplayCase("owner/app", 1, "a" * 40, "b" * 40)
        second = ReplayCase("owner/app", 2, "c" * 40, "d" * 40)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            save_candidates(path, first, [self.finding()], "Reviewed by X @ E")
            save_candidates(path, second, [], "Reviewed by X @ E")
            stored = load_candidates(path)

        self.assertEqual(len(stored), 2)
        self.assertEqual(len(candidates_for(stored, first)[0]), 1)
        self.assertEqual(candidates_for(stored, second)[0], [])

    def test_a_case_with_nothing_recorded_returns_nothing(self) -> None:
        case = ReplayCase("owner/app", 9, "f" * 40, "b" * 40)

        self.assertIsNone(candidates_for({}, case))

    def test_unreadable_recordings_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.json"
            path.write_text("{oops", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "Cannot read the candidates"):
                load_candidates(path)

    def test_verifying_recorded_candidates_runs_no_review(self) -> None:
        """This is the whole point: no reviewer invocation, so no reviewer budget."""

        case = ReplayCase("owner/app", 1, "a" * 40, "b" * 40)
        settings = Mock(
            max_diff_bytes=600,
            max_review_bundles=1,
            ignored_missing_patch_patterns=(),
            state_file=Path("/tmp/state.json"),
            design_level=True,
            docs_level=True,
        )
        with patch("src.evaluate._repository_for") as repository_for, patch(
            "src.evaluate.changed_files", return_value=[{"filename": "app.py"}]
        ), patch(
            "src.cli._bundled_material", return_value=(Mock(), ())
        ), patch("src.cli.create_review_engine"), patch(
            "src.cli._create_verification_pass"
        ), patch("src.cli._create_fallback_reviewer", return_value=None), patch(
            "src.cli.review_workspace"
        ) as workspace, patch(
            "src.evaluate.read_background", return_value=""
        ), patch("src.cli._design_level") as design, patch(
            "src.cli._docs_level"
        ) as docs, patch(
            "src.cli._report_diff_budget"
        ), patch(
            "src.cli._review_with_engine_fallback"
        ) as review, patch(
            "src.cli._verify_code_candidates"
        ) as verify, patch(
            "src.evaluate.replace", side_effect=lambda material, **kw: material
        ), patch("sys.stdout", new_callable=StringIO) as output:
            repository_for.return_value = Repository("app", "owner/app", Path("/tmp"))
            workspace.return_value.__enter__.return_value = repository_for.return_value
            workspace.return_value.dependency_links = ()
            workspace.return_value.dependency_notes = ()
            verify.return_value = ({}, [self.finding()], False, "", [])

            replay_case(
                settings,
                case,
                ReviewMetrics(),
                recorded=([self.finding()], "Reviewed by X @ E"),
            )

        self.assertFalse(review.called)
        # The advisory levels are reviewer work too, so they stay out of it.
        self.assertFalse(design.called)
        self.assertFalse(docs.called)
        self.assertTrue(verify.called)
        self.assertIn("Verifying 1 recorded candidate", output.getvalue())


class LabelFileTests(unittest.TestCase):
    def test_labels_are_looked_up_by_case_and_pinned_head(self) -> None:
        """A line number only means something at the commit it was read from."""

        labels = {
            "owner/app#18@abcdef012345": {
                "expected": [{"path": "app.py", "line": 5, "outcome": ACTED_ON}]
            }
        }
        matching = ReplayCase("owner/app", 18, "abcdef012345" + "0" * 28, "b" * 40)
        other_head = ReplayCase("owner/app", 18, "f" * 40, "b" * 40)

        self.assertEqual(len(expected_for(labels, matching)), 1)
        self.assertEqual(expected_for(labels, other_head), [])

    def test_missing_labels_are_simply_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_labels(Path(directory) / "absent.json"), {})

    def test_unreadable_labels_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.json"
            path.write_text("{broken", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "Cannot read the labels"):
                load_labels(path)


class HarnessTranscriptTests(unittest.TestCase):
    """A measurement whose evidence scrolled past measured nothing."""

    def setUp(self) -> None:
        # Pinning asks the real GitHub CLI for a stored login, which no test has.
        pinning = patch("src.evaluate.pin_github_account")
        pinning.start()
        self.addCleanup(pinning.stop)

    def test_each_command_names_its_own_transcript(self) -> None:
        for command in ("capture", "label", "replay"):
            name = transcript_name(command)

            self.assertTrue(name.startswith(f"eval-{command}-"))
            self.assertTrue(name.endswith(".log"))
            self.assertIn(str(os.getpid()), name)

    def test_a_harness_transcript_is_told_apart_from_a_review_one(self) -> None:
        """These two are read for different reasons and must not be confused."""

        self.assertFalse(transcript_name("replay").startswith("run-"))

    def test_a_replay_writes_what_it_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evals").mkdir()
            (root / "evals" / "cases.json").write_text(
                json.dumps({"cases": []}), encoding="utf-8"
            )
            settings = Mock(
                state_file=root / ".state" / "state.json",
                run_log_retention_days=7,
                repositories=(),
            )
            with patch("src.evaluate.load_settings", return_value=settings), patch(
                "src.evaluate.load_cases", return_value=()
            ), patch("src.evaluate.replay") as replay_call:
                replay_call.side_effect = lambda *a, **k: (
                    print("      -> Rejected candidate [HIGH] app.py:4 Something"),
                    0,
                )[1]
                exit_code = main(
                    [
                        "--cases",
                        str(root / "evals" / "cases.json"),
                        "replay",
                    ]
                )

            self.assertEqual(exit_code, 0)
            logs = sorted((root / ".state" / "run-logs").glob("eval-replay-*.log"))
            self.assertEqual(len(logs), 1)
            self.assertIn(
                "Rejected candidate", logs[0].read_text(encoding="utf-8")
            )

    def test_an_interrupted_measurement_reports_its_own_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Mock(
                state_file=root / ".state" / "state.json",
                run_log_retention_days=7,
                repositories=(),
            )
            with patch("src.evaluate.load_settings", return_value=settings), patch(
                "src.evaluate._run_command", side_effect=KeyboardInterrupt
            ):
                self.assertEqual(main(["capture"]), 130)

    def test_a_reviewer_without_a_login_measures_nothing(self) -> None:
        """Mining or replaying as another account reads another account's view."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Mock(
                state_file=root / ".state" / "state.json",
                run_log_retention_days=7,
                repositories=(),
                reviewer_username="reviewer",
            )
            refusal = CommandError("GitHub CLI has no login for reviewer", command="gh")
            with patch("src.evaluate.load_settings", return_value=settings), patch(
                "src.evaluate.pin_github_account", side_effect=refusal
            ) as pin, patch("src.evaluate._run_command") as command, patch(
                "sys.stderr", new_callable=StringIO
            ) as errors:
                self.assertEqual(main(["label"]), 1)

            pin.assert_called_once_with("reviewer", ())
            command.assert_not_called()
            self.assertIn("no login for reviewer", errors.getvalue())


class ScopedMiningTests(unittest.TestCase):
    def test_mining_one_repository_keeps_the_rest_of_the_corpus(self) -> None:
        """Scoping the work is not scoping the file; a truncated corpus compares
        nothing."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases_path = root / "cases.json"
            labels_path = root / "labels.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "slug": "owner/other",
                                "number": 3,
                                "head": "c" * 40,
                                "base": "d" * 40,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            labels_path.write_text(
                json.dumps(
                    {
                        "labels": {
                            "owner/other#3@cccccccccccc": {
                                "slug": "owner/other",
                                "number": 3,
                                "head": "c" * 40,
                                "expected": [
                                    {
                                        "path": "kept.py",
                                        "line": 1,
                                        "outcome": ACTED_ON,
                                    }
                                ],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = Mock(
                repositories=(Repository("app", "owner/app", root),),
                state_file=root / "state.json",
            )
            with patch(
                "src.github.list_review_comments_by_reviewer", return_value=()
            ):
                mine_labels(settings, cases_path, labels_path, ["owner/app"])

            kept = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
            self.assertEqual([case["slug"] for case in kept], ["owner/other"])
            self.assertIn(
                "owner/other#3@cccccccccccc", load_labels(labels_path)
            )

    def test_a_case_is_pinned_against_the_commit_the_pr_opened_from(self) -> None:
        """A merged pull request's base branch contains the commit we reviewed.

        Asking git for the merge base of the two then answers with that commit, and the
        case reviews an empty diff: nine of forty-seven mined cases were silently doing
        exactly that, scoring nothing while looking like a corpus.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            comment = {
                "comment_id": 7,
                "number": 3,
                "path": "app.py",
                "line": 4,
                "anchor": "a" * 40,
                "title": "t",
                "attribution": "Reviewed by X @ E",
                "created": "2026-09-01T00:00:00Z",
                "head": "a" * 40,
                "base_ref": "develop",
                "base_sha": "b" * 40,
                "merged": True,
            }
            with patch(
                "src.github.list_review_comments_by_reviewer", return_value=[comment]
            ), patch("src.evaluate._ensure_object", return_value=True), patch(
                "src.evaluate.merge_base", return_value="c" * 40
            ) as merge, patch(
                "src.evaluate._git", return_value=""
            ):
                mine_labels(
                    Mock(
                        repositories=(Repository("app", "owner/app", root),),
                        state_file=root / "state.json",
                    ),
                    root / "cases.json",
                    root / "labels.json",
                )

        # The base branch name is never what the merge base is taken against.
        self.assertEqual(merge.call_args.args[2], "b" * 40)

    def test_a_case_with_no_diff_is_not_pinned_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            comment = {
                "comment_id": 7,
                "number": 3,
                "path": "app.py",
                "line": 4,
                "anchor": "a" * 40,
                "title": "t",
                "attribution": "Reviewed by X @ E",
                "created": "",
                "head": "a" * 40,
                "base_ref": "develop",
                "base_sha": "b" * 40,
                "merged": True,
            }
            with patch(
                "src.github.list_review_comments_by_reviewer", return_value=[comment]
            ), patch("src.evaluate._ensure_object", return_value=True), patch(
                "src.evaluate.merge_base", return_value="a" * 40
            ), patch(
                "src.evaluate._git", return_value=""
            ):
                result = mine_labels(
                    Mock(
                        repositories=(Repository("app", "owner/app", root),),
                        state_file=root / "state.json",
                    ),
                    root / "cases.json",
                    root / "labels.json",
                )

        self.assertEqual(result["cases"], [])
        self.assertEqual(result["labels"], {})

    def test_mining_everything_replaces_everything(self) -> None:
        """A full mine is the whole truth, so a stale case must not survive it."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases_path = root / "cases.json"
            cases_path.write_text(
                json.dumps({"cases": [{"slug": "owner/gone", "number": 1}]}),
                encoding="utf-8",
            )
            settings = Mock(repositories=(), state_file=root / "state.json")

            mine_labels(settings, cases_path, root / "labels.json")

            self.assertEqual(
                json.loads(cases_path.read_text(encoding="utf-8"))["cases"], []
            )


class ComparisonGuardTests(unittest.TestCase):
    def document(self, **overrides) -> dict:
        document = {
            "engine": "antigravity",
            "model": None,
            "arm": {"name": "baseline"},
            "reviewer": {"version": "0.23.0", "commit": "abc1234", "dirty": False},
            "cases": {"count": 2, "fingerprint": "ffff0000"},
            "settings": {"design_level": True, "max_findings": 10},
            "summary": {"invocations": 4, "confirmed": 1},
        }
        document.update(overrides)
        return document

    def test_two_case_sets_that_differ_are_called_out(self) -> None:
        report = compare(
            self.document(),
            self.document(cases={"count": 3, "fingerprint": "0000ffff"}),
        )

        self.assertIn("DIFFERENT CASE SETS", report)

    def test_a_replay_compared_with_a_live_run_is_called_out(self) -> None:
        live = self.document()
        del live["cases"]

        report = compare(self.document(), live)

        self.assertIn("not the same work", report)

    def test_changing_two_things_at_once_is_called_out(self) -> None:
        report = compare(
            self.document(),
            self.document(
                engine="claude",
                settings={"design_level": False, "max_findings": 10},
            ),
        )

        self.assertIn("2 things differ", report)
        self.assertIn("design_level", report)
        self.assertIn("engine antigravity -> claude", report)

    def test_one_change_is_not_warned_about(self) -> None:
        report = compare(self.document(), self.document(engine="claude"))

        self.assertNotIn("things differ", report)
        self.assertNotIn("DIFFERENT CASE SETS", report)

    def test_an_uncommitted_tree_is_called_out(self) -> None:
        report = compare(
            self.document(),
            self.document(
                reviewer={"version": "0.23.0", "commit": "abc1234", "dirty": True}
            ),
        )

        self.assertIn("uncommitted tree", report)

    def test_each_side_names_its_arm_and_build(self) -> None:
        report = compare(self.document(), self.document(arm={"name": "claude"}))

        self.assertIn("arm=baseline abc1234", report)
        self.assertIn("arm=claude abc1234", report)

    def test_what_a_replay_measured_against_production_is_compared(self) -> None:
        """A comparison reported every engine number and none of the ones a change
        to the reviewer is judged on."""

        report = compare(
            self.document(
                labels={"reproduction_rate": 0.4},
                confirmed={"unmatched_rate": 0.2},
                controls={"approval_rate": 0.9},
            ),
            self.document(
                labels={"reproduction_rate": 0.5},
                confirmed={"unmatched_rate": 0.5},
                controls={"approval_rate": 0.6},
            ),
        )

        self.assertIn("labels.reproduction_rate: 0.4 -> 0.5", report)
        self.assertIn("confirmed.unmatched_rate: 0.2 -> 0.5", report)
        self.assertIn("controls.approval_rate: 0.9 -> 0.6", report)


def confirmed_finding(path: str, line: int) -> Finding:
    return Finding(
        severity="medium",
        title="Title",
        path=path,
        line=line,
        problem="Problem",
        suggestion="Suggestion",
    )


class UnmatchedFindingTests(unittest.TestCase):
    """Recall alone cannot tell a better reviewer from one confirming more of
    everything."""

    def test_confirmed_findings_are_split_by_what_authors_did(self) -> None:
        expected = [
            {"path": "a.py", "line": 10, "outcome": ACTED_ON},
            {"path": "b.py", "line": 5, "outcome": NOT_ACTED_ON},
        ]
        confirmed = [
            confirmed_finding("a.py", 11),
            confirmed_finding("b.py", 5),
            confirmed_finding("c.py", 1),
        ]

        score = score_against_labels(expected, confirmed, confirmed)

        self.assertEqual(score["confirmed"], 3)
        self.assertEqual(score["unacted"], 1)
        self.assertEqual(score["unacted_anchors"], ["b.py:5"])
        self.assertEqual(score["unlabelled"], 1)
        self.assertEqual(score["unlabelled_anchors"], ["c.py:1"])
        self.assertEqual(score["reproduced"], 1)

    def test_a_finding_on_an_acted_on_line_is_never_unmatched(self) -> None:
        """One line can carry a comment authors acted on and one they ignored."""

        expected = [
            {"path": "a.py", "line": 10, "outcome": ACTED_ON},
            {"path": "a.py", "line": 10, "outcome": NOT_ACTED_ON},
        ]

        score = score_against_labels(
            expected, [confirmed_finding("a.py", 10)], [confirmed_finding("a.py", 10)]
        )

        self.assertEqual((score["unacted"], score["unlabelled"]), (0, 0))


class ReplayDecisionTests(unittest.TestCase):
    """What a replay reports as decided must be what a live run would deliver,
    because a control case is judged on nothing else."""

    def run_case(
        self,
        reviews: "list",
        verifications: "list" = (),
        *,
        bundled: bool = False,
        partial: bool = False,
        recorded=None,
        body: str = "",
    ):
        case = ReplayCase("owner/app", 1, "a" * 40, "b" * 40, body=body)
        repository = Repository("app", "owner/app", Path("/tmp"))
        settings = Mock(
            max_diff_bytes=600,
            max_review_bundles=4,
            ignored_missing_patch_patterns=(),
            state_file=Path("/tmp/state.json"),
            repositories=(repository,),
            design_level=False,
            docs_level=False,
        )
        primary = Mock(
            diff="one",
            added_lines={"a.py": frozenset({1})},
            diff_budget=Mock(is_partial=partial, reviewed_files=3, total_files=5),
        )
        second = Mock(
            diff="two",
            added_lines={"b.py": frozenset({1})},
            diff_budget=Mock(reviewed_files=1),
        )
        bundles = (second,) if bundled else ()
        output = StringIO()
        with patch("src.evaluate._repository_for", return_value=repository), patch(
            "src.evaluate.changed_files", return_value=[{"filename": "a.py"}]
        ), patch(
            "src.cli._bundled_material", return_value=(primary, bundles)
        ), patch(
            "src.cli._bundle_material_from", return_value=second
        ), patch("src.cli.create_review_engine"), patch(
            "src.cli._create_verification_pass"
        ), patch("src.cli._create_fallback_reviewer", return_value=None), patch(
            "src.cli.review_workspace"
        ) as workspace, patch(
            "src.evaluate.read_background", return_value=""
        ), patch("src.cli._report_diff_budget"), patch(
            "src.cli._review_with_engine_fallback", side_effect=reviews
        ) as review_call, patch(
            "src.cli._verify_code_candidates", side_effect=list(verifications)
        ), patch(
            "src.evaluate.replace", side_effect=lambda material, **kw: material
        ), patch(
            "sys.stdout", output
        ):
            workspace.return_value.__enter__.return_value = repository
            workspace.return_value.dependency_links = ()
            workspace.return_value.dependency_notes = ()
            score = replay_case(settings, case, ReviewMetrics(), recorded=recorded)
        self.review_call = review_call
        return score, output.getvalue()

    @staticmethod
    def reviewed(findings=(), approve=True, reason="The change is correct."):
        return ({}, list(findings), approve, reason, [], Mock(), Mock())

    def test_a_clean_review_reports_the_approval_a_live_run_would_send(self) -> None:
        score, output = self.run_case([self.reviewed()])

        self.assertIs(score["approved"], True)
        self.assertIn("Would approve", output)
        self.assertEqual(score["confirmed"], 0)

    def test_the_review_reads_the_description_a_live_review_reads(self) -> None:
        """Without it a replay confirmed a regression the description named as the
        change itself."""

        self.run_case([self.reviewed()], body="Stop trimming trailing spaces.")

        pull_request = self.review_call.call_args.args[4]
        self.assertEqual(pull_request.body, "Stop trimming trailing spaces.")

    def test_a_code_level_failure_is_a_failure_not_a_clean_case(self) -> None:
        """An empty result here was scored as a review that found nothing."""

        with self.assertRaises(EngineError):
            self.run_case([EngineError("provider refused")])

    def test_a_partial_review_never_counts_as_an_approval(self) -> None:
        score, output = self.run_case([self.reviewed()], partial=True)

        self.assertIs(score["approved"], False)
        self.assertIn("read 3 of 5 changed files", score["approval_reason"])
        self.assertIn("Would withhold approval", output)

    def test_a_bundle_finding_takes_the_approval_away(self) -> None:
        finding = confirmed_finding("b.py", 1)

        score, _ = self.run_case(
            [self.reviewed(), self.reviewed([finding], approve=False)],
            [({}, [finding], False, "", [])],
            bundled=True,
        )

        self.assertIs(score["approved"], False)
        self.assertEqual(score["confirmed"], 1)

    def test_a_failed_bundle_leaves_the_approval_undecided(self) -> None:
        """The live review fails whole there, so it approves nothing."""

        score, output = self.run_case(
            [self.reviewed(), EngineError("cut mid-stream")], bundled=True
        )

        self.assertIsNone(score["approved"])
        self.assertIn("Approval undecided: bundle 2 failed", output)

    def test_verifying_an_empty_recording_reports_no_approval(self) -> None:
        """Verify mode never asks for a verdict, and used to crash looking for one."""

        score, output = self.run_case([], recorded=([], "Reviewer @ Engine"))

        self.assertIsNone(score["approved"])
        self.assertNotIn("approve", output.lower().replace("approval", ""))

    def test_confirmed_findings_are_reported_without_any_label(self) -> None:
        finding = confirmed_finding("a.py", 1)

        score, output = self.run_case(
            [self.reviewed([finding], approve=False)],
            [({}, [finding], False, "a defect remains", [])],
        )

        self.assertEqual(score["unlabelled_anchors"], ["a.py:1"])
        self.assertIn("1 confirmed finding match nothing authors acted on", output)
        self.assertIs(score["approved"], False)


class ControlScoringTests(unittest.TestCase):
    def replay_scores(self, scores: "list[dict]", controls: "tuple[bool, ...]"):
        cases = tuple(
            ReplayCase("owner/app", number, f"{number:x}" * 40, "b" * 40)
            for number in range(1, len(scores) + 1)
        )
        labels = {
            case.pinned: {
                "slug": case.slug,
                "number": case.number,
                "expected": [],
                **({"control": {"approved_review": "1"}} if control else {}),
            }
            for case, control in zip(cases, controls)
        }
        settings = Mock(
            review_engine="kiro", review_model="model", max_review_bundles=1
        )
        document: dict = {"summary": {}}
        metrics = Mock()
        metrics.document.return_value = document
        output = StringIO()
        with patch("src.evaluate.replay_case", side_effect=scores), patch(
            "src.evaluate.ReviewMetrics", return_value=metrics
        ), patch("src.evaluate.reviewer_provenance", return_value={}), patch(
            "src.evaluate.cases_fingerprint", return_value="f"
        ), patch(
            "sys.stdout", output
        ):
            replay(settings, cases, BASELINE_ARM, None, labels=labels)
        return document, output.getvalue()

    @staticmethod
    def score(approved, reason="", unacted=(), unlabelled=(), fixed_later=()):
        return {
            "acted_on": 0,
            "confirmed": len(unacted) + len(unlabelled),
            "unacted_anchors": list(unacted),
            "unlabelled_anchors": list(unlabelled),
            "fixed_later_anchors": list(fixed_later),
            "approved": approved,
            "approval_reason": reason,
        }

    def test_what_authors_changed_later_is_taken_out_of_the_noise(self) -> None:
        document, output = self.replay_scores(
            [
                self.score(
                    False,
                    unlabelled=("a.py:1", "b.py:2"),
                    fixed_later=("a.py:1",),
                )
            ],
            (False,),
        )

        self.assertEqual(document["confirmed"]["fixed_later"], 1)
        self.assertEqual(document["confirmed"]["unexplained"], 1)
        self.assertEqual(document["confirmed"]["unexplained_rate"], 0.5)
        self.assertEqual(
            document["confirmed_fixed_later"], ["owner/app#1@111111111111 a.py:1"]
        )
        self.assertIn("leaving 1 unexplained", output)

    def test_the_final_head_reaches_the_case(self) -> None:
        case = ReplayCase("owner/app", 1, "1" * 40, "b" * 40)
        labels = {case.pinned: {"expected": [], "final_head": "f" * 40}}
        settings = Mock(review_engine="kiro", review_model="m", max_review_bundles=1)
        metrics = Mock()
        metrics.document.return_value = {"summary": {}}
        with patch("src.evaluate.replay_case", return_value={}) as replayed, patch(
            "src.evaluate.ReviewMetrics", return_value=metrics
        ), patch("src.evaluate.reviewer_provenance", return_value={}), patch(
            "src.evaluate.cases_fingerprint", return_value="f"
        ), patch(
            "sys.stdout", new_callable=StringIO
        ):
            replay(settings, (case,), BASELINE_ARM, None, labels=labels)

        self.assertEqual(replayed.call_args.kwargs["final_head"], "f" * 40)

    def test_controls_report_how_many_were_approved_again(self) -> None:
        document, output = self.run_controls()

        self.assertEqual(
            document["controls"],
            {
                "cases": 3,
                "approved": 1,
                "withheld": 1,
                "undecided": 1,
                "approval_rate": 0.5,
            },
        )
        self.assertEqual(len(document["controls_withheld"]), 1)
        self.assertIn("a defect remains", document["controls_withheld"][0])
        self.assertIn("Approved 1 of 3 control cases", output)

    def run_controls(self):
        return self.replay_scores(
            [
                self.score(True),
                self.score(False, "a defect remains", unlabelled=("a.py:1",)),
                self.score(None, "bundle 2 failed"),
            ],
            (True, True, True),
        )

    def test_a_case_that_is_not_a_control_is_not_counted_as_one(self) -> None:
        document, _ = self.replay_scores([self.score(True)], (False,))

        self.assertNotIn("controls", document)

    def test_what_no_author_acted_on_is_totalled_for_the_run(self) -> None:
        document, output = self.replay_scores(
            [
                self.score(False, unacted=("b.py:5",), unlabelled=("c.py:1",)),
                self.score(False, unlabelled=("d.py:2",)),
            ],
            (False, False),
        )

        self.assertEqual(
            document["confirmed"],
            {
                "total": 3,
                "matched": 0,
                "unacted": 1,
                "unlabelled": 2,
                "unmatched_rate": 1.0,
                "fixed_later": 0,
                "unexplained": 3,
                "unexplained_rate": 1.0,
            },
        )
        self.assertEqual(
            document["confirmed_unacted"], ["owner/app#1@111111111111 b.py:5"]
        )
        self.assertIn("3 of 3 confirmed findings match nothing", output)


class ControlSelectionTests(unittest.TestCase):
    """Controls are chosen apart, so a recall pass does not pay for them unasked."""

    setUp = LabelledOnlySelectionTests.setUp
    run_replay = LabelledOnlySelectionTests.run_replay

    def write_corpus(self, root: Path, outcomes: "tuple[str, str]"):
        cases_path, labels_path = LabelledOnlySelectionTests.write_corpus(
            self, root, outcomes
        )
        labels = json.loads(labels_path.read_text(encoding="utf-8"))
        control = labels["labels"]["owner/app#2@cccccccccccc"]
        control["expected"] = []
        control["control"] = {"approved_review": "9", "merged_unchanged": True}
        labels_path.write_text(json.dumps(labels), encoding="utf-8")
        return cases_path, labels_path

    def test_controls_alone_are_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, replay_call = self.run_replay(
                Path(directory), (ACTED_ON, ACTED_ON), "--controls"
            )

            replayed = replay_call.call_args.args[1]
            self.assertEqual([case.number for case in replayed], [2])

    def test_controls_and_labelled_cases_are_replayed_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, replay_call = self.run_replay(
                Path(directory), (ACTED_ON, ACTED_ON), "--labelled-only", "--controls"
            )

            replayed = replay_call.call_args.args[1]
            self.assertEqual([case.number for case in replayed], [1, 2])

    def test_a_control_is_named_by_its_label(self) -> None:
        labels = {"owner/app#2@cccccccccccc": {"control": {"approved_review": "9"}}}

        self.assertIsNotNone(
            control_for(labels, ReplayCase("owner/app", 2, "c" * 40, "d" * 40))
        )
        self.assertIsNone(
            control_for(labels, ReplayCase("owner/app", 1, "a" * 40, "b" * 40))
        )


class ControlMiningTests(unittest.TestCase):
    """A delivered record does not say it was an approval; GitHub has to."""

    HEADS = {number: f"{number}" * 40 for number in range(1, 8)}

    def mine(self, commented=frozenset()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"

            def record(number, status="delivered", destination="github"):
                key = f"owner/app#{number}#{self.HEADS[number]}#{destination}"
                return key, {"status": status, "receipt_id": f"{number}{number}"}

            records = dict(
                [
                    record(1),
                    record(2),
                    record(3),
                    record(4),
                    record(5),
                    record(6, destination="slack"),
                    record(7, status="approval_withheld"),
                ]
            )
            state.write_text(
                json.dumps({"version": 1, "records": records}), encoding="utf-8"
            )
            settings = Mock(
                repositories=(Repository("app", "owner/app", root),),
                state_file=state,
            )

            def refs(_repository, number):
                head = self.HEADS[number]
                return {
                    # Pushed after the approval.
                    "head": "f" * 40 if number == 2 else head,
                    "merged": number != 5,
                    "base_sha": "b" * 40,
                    "base_ref": "develop",
                    "title": f"PR {number}",
                    "body": f"Why PR {number} exists",
                }

            def reviews(_repository, pull_request):
                number = pull_request.number
                return [
                    {
                        "id": int(f"{number}{number}"),
                        "state": "COMMENTED" if number == 3 else "APPROVED",
                        "commit_id": self.HEADS[number],
                    }
                ]

            with patch(
                "src.github.get_pull_request_refs", side_effect=refs
            ) as refs_call, patch(
                "src.github.list_reviews", side_effect=reviews
            ), patch(
                "src.evaluate._ensure_object", return_value=True
            ), patch(
                "src.evaluate.merge_base", return_value="e" * 40
            ), patch(
                "sys.stdout", new_callable=StringIO
            ):
                mined = mine_controls(settings, set(), set(commented))
        return mined, refs_call

    def test_only_an_approved_head_that_merged_as_is_is_a_control(self) -> None:
        mined, _ = self.mine()

        self.assertEqual([case["number"] for case, _ in mined], [1, 4])
        case, label = mined[0]
        self.assertEqual(case["head"], self.HEADS[1])
        self.assertEqual(case["base"], "e" * 40)
        self.assertEqual(case["title"], "PR 1")
        self.assertEqual(case["body"], "Why PR 1 exists")
        self.assertEqual(label["final_head"], self.HEADS[1])
        self.assertEqual(label["expected"], [])
        self.assertEqual(
            label["control"], {"approved_review": "11", "merged_unchanged": True}
        )

    def test_a_pull_request_the_reviewer_commented_on_is_not_a_control(self) -> None:
        """It needed something, whatever its final head received."""

        mined, refs_call = self.mine(commented={("owner/app", 4)})

        self.assertEqual([case["number"] for case, _ in mined], [1])
        asked = [call.args[1] for call in refs_call.call_args_list]
        self.assertNotIn(4, asked)

    def test_mined_controls_are_written_with_the_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = (
                {
                    "slug": "owner/app",
                    "number": 9,
                    "head": "9" * 40,
                    "base": "e" * 40,
                    "title": "",
                    "base_ref": "develop",
                    "captured": "mined",
                },
                {
                    "slug": "owner/app",
                    "number": 9,
                    "head": "9" * 40,
                    "expected": [],
                    "control": {"approved_review": "99", "merged_unchanged": True},
                },
            )
            with patch(
                "src.github.list_review_comments_by_reviewer", return_value=()
            ), patch("src.evaluate.mine_controls", return_value=[control]), patch(
                "sys.stdout", new_callable=StringIO
            ):
                result = mine_labels(
                    Mock(
                        repositories=(Repository("app", "owner/app", root),),
                        state_file=root / "state.json",
                    ),
                    root / "cases.json",
                    root / "labels.json",
                )

            self.assertEqual(result["tally"], {"control": 1})
            written = load_labels(root / "labels.json")
            self.assertIn("control", written["owner/app#9@999999999999"])
            cases = load_cases(root / "cases.json")
            self.assertEqual([case.number for case in cases], [9])


class MinedDescriptionTests(unittest.TestCase):
    def test_a_mined_case_keeps_its_description_and_where_it_ended(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            comment = {
                "comment_id": 7,
                "number": 3,
                "path": "app.py",
                "line": 4,
                "anchor": "a" * 40,
                "title": "[LOW] Finding title",
                "attribution": "Reviewed by X @ E",
                "created": "",
                "head": "f" * 40,
                "base_ref": "develop",
                "base_sha": "b" * 40,
                "merged": True,
                "pr_title": "feat: keep trailing spaces",
                "pr_body": "Stop client-side trimming.",
            }
            with patch(
                "src.github.list_review_comments_by_reviewer", return_value=[comment]
            ), patch("src.evaluate._ensure_object", return_value=True), patch(
                "src.evaluate.merge_base", return_value="c" * 40
            ), patch(
                "src.evaluate._git", return_value=""
            ), patch(
                "src.evaluate.mine_controls", return_value=[]
            ), patch(
                "sys.stdout", new_callable=StringIO
            ):
                result = mine_labels(
                    Mock(
                        repositories=(Repository("app", "owner/app", root),),
                        state_file=root / "state.json",
                    ),
                    root / "cases.json",
                    root / "labels.json",
                )

            case = load_cases(root / "cases.json")[0]
            self.assertEqual(case.title, "feat: keep trailing spaces")
            self.assertEqual(case.body, "Stop client-side trimming.")
            label = result["labels"]["owner/app#3@aaaaaaaaaaaa"]
            self.assertEqual(label["final_head"], "f" * 40)
            self.assertEqual(
                final_head_for(result["labels"], case), "f" * 40
            )


class ChangedLaterTests(unittest.TestCase):
    """A confirmed finding nobody posted, on lines its author then changed, is the
    evidence a label rests on, and is not noise."""

    def history(self, directory: str) -> "tuple[Path, str, str]":
        root = Path(directory)
        git(root, "init", "-b", "main")
        git(root, "config", "user.name", "Replay Test")
        git(root, "config", "user.email", "replay@example.invalid")
        lines = [f"line {number}\n" for number in range(1, 21)]
        (root / "app.py").write_text("".join(lines), encoding="utf-8")
        git(root, "add", "app.py")
        git(root, "commit", "-m", "reviewed head")
        head = git(root, "rev-parse", "HEAD").strip()
        lines[9] = "line 10, fixed\n"
        (root / "app.py").write_text("".join(lines), encoding="utf-8")
        git(root, "commit", "-am", "author fixes line 10")
        final = git(root, "rev-parse", "HEAD").strip()
        return root, head, final

    def test_only_the_lines_the_author_changed_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, head, final = self.history(directory)

            changed = changed_later(
                root, head, final, [("app.py", 10), ("app.py", 18), ("gone.py", 1)]
            )

        self.assertEqual(changed, ["app.py:10"])

    def test_a_pull_request_that_never_moved_changed_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, head, _ = self.history(directory)

            self.assertEqual(changed_later(root, head, head, [("app.py", 10)]), [])
            self.assertEqual(changed_later(root, head, None, [("app.py", 10)]), [])

    def test_a_rebase_proves_nothing(self) -> None:
        """Its final head does not descend from the reviewed one, so every line
        looks changed."""

        with tempfile.TemporaryDirectory() as directory:
            root, head, final = self.history(directory)
            git(root, "checkout", "-q", "--orphan", "rewritten")
            git(root, "commit", "-q", "-m", "rewritten history")
            rewritten = git(root, "rev-parse", "HEAD").strip()

            changed = changed_later(root, head, rewritten, [("app.py", 10)])

        self.assertEqual(changed, [])


def make_settings(root: Path, repository: Repository, **overrides) -> Settings:
    values = dict(
        reviewer_username="reviewer",
        repositories=[repository],
        dry_run=True,
        slack_webhook_url=None,
        state_file=root / "state.json",
        review_engine="kiro",
        review_engine_command=("kiro-cli",),
        review_engine_env_allowlist=(),
        review_model=None,
        review_timeout_seconds=300,
        max_diff_bytes=200000,
        max_findings=10,
    )
    values.update(overrides)
    return Settings(**values)


class PullRequestSpecTests(unittest.TestCase):
    def test_a_pull_request_may_name_the_head_it_is_pinned_to(self) -> None:
        self.assertEqual(
            parse_pull_request_spec("owner/app#12"), ("owner/app", 12, None)
        )
        self.assertEqual(
            parse_pull_request_spec("owner/app.js#12@abc1234"),
            ("owner/app.js", 12, "abc1234"),
        )

    def test_anything_else_is_refused(self) -> None:
        for spec in ("owner/app", "app#12", "owner/app#x", "owner/app#12@zz", "a#1@"):
            with self.subTest(spec=spec):
                with self.assertRaises(ConfigError):
                    parse_pull_request_spec(spec)


class AdHocCaseTests(unittest.TestCase):
    """A pull request named on the command line is pinned the way a review read it."""

    def history(self, directory: str) -> "dict[str, Any]":
        """An origin with a fork point, two feature heads, and a develop that moved."""

        origin = Path(directory) / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "develop")
        git(origin, "config", "user.name", "Replay Test")
        git(origin, "config", "user.email", "replay@example.invalid")
        (origin / "app.py").write_text("value = 1\n", encoding="utf-8")
        git(origin, "add", "app.py")
        git(origin, "commit", "-q", "-m", "base")
        fork = git(origin, "rev-parse", "HEAD").strip()
        git(origin, "checkout", "-q", "-b", "feature")
        (origin / "app.py").write_text("value = 2\n", encoding="utf-8")
        git(origin, "commit", "-q", "-am", "first push")
        first = git(origin, "rev-parse", "HEAD").strip()
        (origin / "app.py").write_text("value = 3\n", encoding="utf-8")
        git(origin, "commit", "-q", "-am", "second push")
        head = git(origin, "rev-parse", "HEAD").strip()
        git(origin, "checkout", "-q", "develop")
        (origin / "other.py").write_text("other = 1\n", encoding="utf-8")
        git(origin, "add", "other.py")
        git(origin, "commit", "-q", "-m", "develop moves on")
        git(origin, "update-ref", "refs/pull/7/head", head)
        clone = Path(directory) / "clone"
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(clone)],
            check=True,
            capture_output=True,
        )
        return {
            "origin": origin,
            "clone": clone,
            "fork": fork,
            "first": first,
            "head": head,
        }

    def resolve(self, root: Path, clone: Path, spec: str, refs: "dict[str, Any]"):
        settings = make_settings(root, Repository("app", "owner/app", clone))
        with patch("src.github.get_pull_request_refs", return_value=refs):
            return case_for_pull_request(settings, spec)

    def refs(self, history: "dict[str, Any]", **overrides) -> "dict[str, Any]":
        refs = {
            "head": history["head"],
            "head_ref": "feature",
            "base_ref": "develop",
            "merged": False,
            "merge_commit": None,
            "title": "feat: raise the value",
            "body": "Raises it on purpose.",
        }
        refs.update(overrides)
        return refs

    def test_an_open_pull_request_is_pinned_against_its_fork_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = self.history(directory)

            case = self.resolve(
                Path(directory), history["clone"], "owner/app#7", self.refs(history)
            )

        self.assertEqual((case.head, case.base), (history["head"], history["fork"]))
        self.assertEqual((case.head_ref, case.base_ref), ("feature", "develop"))
        self.assertEqual(case.title, "feat: raise the value")
        self.assertEqual(case.body, "Raises it on purpose.")

    def test_a_merged_pull_request_is_pinned_against_the_branch_it_left(
        self,
    ) -> None:
        """Merged, its head is an ancestor of develop, so the merge base with
        develop's tip is the head itself and the pinned diff would be empty."""

        with tempfile.TemporaryDirectory() as directory:
            history = self.history(directory)
            origin = history["origin"]
            git(origin, "merge", "-q", "--no-ff", "-m", "merge", "feature")
            merge = git(origin, "rev-parse", "HEAD").strip()
            (origin / "later.py").write_text("later = 1\n", encoding="utf-8")
            git(origin, "add", "later.py")
            git(origin, "commit", "-q", "-m", "after the merge")

            case = self.resolve(
                Path(directory),
                history["clone"],
                "owner/app#7",
                self.refs(history, merged=True, merge_commit=merge),
            )

        self.assertEqual((case.head, case.base), (history["head"], history["fork"]))

    def test_an_earlier_head_can_be_named_by_a_short_sha(self) -> None:
        """An advisory level ran once, at whichever head it first saw."""

        with tempfile.TemporaryDirectory() as directory:
            history = self.history(directory)

            case = self.resolve(
                Path(directory),
                history["clone"],
                f"owner/app#7@{history['first'][:10]}",
                self.refs(history),
            )

        self.assertEqual((case.head, case.base), (history["first"], history["fork"]))

    def test_a_head_the_pull_request_never_had_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = self.history(directory)

            with self.assertRaisesRegex(ConfigError, "not a commit"):
                self.resolve(
                    Path(directory),
                    history["clone"],
                    "owner/app#7@deadbeef12",
                    self.refs(history),
                )

    def test_a_pull_request_github_cannot_describe_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = self.history(directory)

            with self.assertRaisesRegex(ConfigError, "could not describe"):
                self.resolve(Path(directory), history["clone"], "owner/app#7", {})


class BaselineEngineTests(unittest.TestCase):
    """A verifier prompt from another commit is judged beside today's."""

    def repository(self, directory: str) -> "tuple[Path, str]":
        root = Path(directory)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.name", "Replay Test")
        git(root, "config", "user.email", "replay@example.invalid")
        (root / "src").mkdir()
        (root / "src" / "__init__.py").write_text("", encoding="utf-8")
        (root / "src" / "models.py").write_text("VALUE = 'old'\n", encoding="utf-8")
        (root / "src" / "engine.py").write_text(
            "from .models import VALUE\n\n\n"
            "def build_docs_verification_prompt(repository, pull_request, material):\n"
            "    return 'baseline ' + VALUE\n",
            encoding="utf-8",
        )
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "baseline")
        commit = git(root, "rev-parse", "HEAD").strip()
        (root / "src" / "models.py").write_text("VALUE = 'new'\n", encoding="utf-8")
        return root, commit

    def docs_material(self) -> ReviewMaterial:
        candidate = DocsFinding(
            severity="LOW",
            title="t",
            category="missing_list_entry",
            audience="human",
            documented="d",
            behavior="b",
            proposed_update="| a | b |",
            documentation=FindingEvidence("README.md", 3, "| x | y |", "r"),
            citations=(FindingEvidence("app.py", 1, "value = 1", "r"),),
        )
        return ReviewMaterial(
            "diff",
            {"app.py": frozenset({1})},
            level=DOCS_LEVEL,
            docs_candidates=(candidate,),
        )

    def test_the_baseline_is_the_committed_package_not_the_working_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, commit = self.repository(directory)

            with baseline_engine("HEAD", root) as (module, resolved):
                built = module.build_docs_verification_prompt(None, None, None)
                package = module.__name__.rsplit(".", 1)[0]
                self.assertIn(package, sys.modules)

        self.assertEqual(built, "baseline old")
        self.assertEqual(resolved, commit)
        self.assertNotIn(package, sys.modules)

    def test_inside_the_swap_the_verifier_reads_the_baseline_prompt(self) -> None:
        repository = Repository("app", "owner/app", Path("."))
        pull_request = PullRequest(1, "Title", "", "author", "head", "url")
        material = self.docs_material()
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self.repository(directory)

            with baseline_engine("HEAD", root) as (module, _):
                with verification_prompts_from(module, DOCS_LEVEL):
                    swapped = build_prompt(repository, pull_request, material, 10)
            restored = build_prompt(repository, pull_request, material, 10)

        self.assertEqual(swapped, "baseline old")
        self.assertIn("adversarial verifier", restored)

    def test_a_ref_that_names_no_commit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self.repository(directory)

            with self.assertRaisesRegex(ConfigError, "no-such-ref"):
                with baseline_engine("no-such-ref", root):
                    pass

    def test_a_baseline_without_the_level_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self.repository(directory)

            with baseline_engine("HEAD", root) as (module, _):
                with self.assertRaisesRegex(ConfigError, "design"):
                    with verification_prompts_from(module, DESIGN_LEVEL):
                        pass


def docs_finding(title: str, category: str = "missing_list_entry") -> DocsFinding:
    return DocsFinding(
        severity="LOW",
        title=title,
        category=category,
        audience="human",
        documented="d",
        behavior="b",
        proposed_update="| a | b |",
        documentation=FindingEvidence("README.md", 3, "| x | y |", "r"),
        citations=(FindingEvidence("app.py", 1, "value = 1", "r"),),
    )


class AdvisoryCaseTests(unittest.TestCase):
    """One advisory level alone, its proposals judged now and at a baseline."""

    def run_case(self, baseline, outputs, assessment_for):
        """Run one docs case whose verifier answers `outputs` in order."""

        case = ReplayCase("owner/app", 1, "a" * 40, "b" * 40)
        repository = Repository("app", "owner/app", Path("/tmp"))
        prepared = evaluate._PreparedCase(
            repository,
            PullRequest(1, "Title", "", "replay", "a" * 40, ""),
            repository,
            ReviewMaterial("diff", {"app.py": frozenset({1})}),
            (),
        )
        verifier = Mock()
        verifier.review.side_effect = outputs
        settings = Mock(state_file=Path("/tmp/state.json"))
        candidates = (
            docs_finding("Commands table lacks export"),
            docs_finding("Other"),
        )

        def docs_level(settings, engine, verification, *args, **kwargs):
            judged = ReviewMaterial(
                "diff",
                {"app.py": frozenset({1})},
                level=DOCS_LEVEL,
                docs_candidates=candidates,
            )
            verification.engine.review(repository, None, judged)
            return assessment_for(candidates)

        context = Mock()
        context.__enter__ = Mock(return_value=prepared)
        context.__exit__ = Mock(return_value=False)
        with patch("src.evaluate._prepared_case", return_value=context), patch(
            "src.cli.create_review_engine"
        ), patch(
            "src.cli._create_verification_pass",
            return_value=VerificationPass(engine=verifier, settings=settings),
        ), patch("src.cli._create_fallback_reviewer", return_value=None), patch(
            "src.cli._MeasuredEngine", side_effect=lambda inner, *args: inner
        ), patch("src.cli._docs_level", side_effect=docs_level), patch(
            "src.evaluate.verification_prompts_from"
        ), patch("sys.stdout", new_callable=StringIO) as output:
            record = advisory_case(
                settings, case, DOCS_LEVEL, ReviewMetrics(), baseline
            )
        return record, output.getvalue(), verifier

    def judgments(self, *verdicts: str) -> "dict[str, Any]":
        return {
            "judgments": [
                {"ref": f"DC{index}", "verdict": verdict, "reason": f"{verdict} why"}
                for index, verdict in enumerate(verdicts, start=1)
            ]
        }

    def test_the_same_proposals_are_judged_now_and_at_the_baseline(self) -> None:
        def assessment(candidates):
            return DocsAssessment(
                understanding="u",
                verdict="stale",
                findings=(candidates[0],),
                rejected=(RejectedDocsCandidate(candidates[1], "silent"),),
            )

        record, reported, verifier = self.run_case(
            (Mock(), "1234567890ab" + "0" * 28),
            [self.judgments("rejected", "rejected"), self.judgments("confirmed")],
            assessment,
        )

        self.assertEqual(verifier.review.call_count, 2)
        proposals = record["proposals"]
        self.assertEqual(
            [
                (item["now"]["verdict"], item["baseline"]["verdict"])
                for item in proposals
            ],
            [("confirmed", "rejected"), ("rejected", "rejected")],
        )
        self.assertEqual(proposals[0]["proposal"]["category"], "missing_list_entry")
        self.assertIn("missing_list_entry", reported)
        self.assertIn("at 1234567890ab: rejected because rejected why", reported)
        self.assertIn("now: confirmed", reported)

    def test_without_a_baseline_only_today_s_verifier_judges(self) -> None:
        def assessment(candidates):
            return DocsAssessment(
                understanding="u", verdict="stale", findings=candidates
            )

        record, _, verifier = self.run_case(None, [self.judgments()], assessment)

        self.assertEqual(verifier.review.call_count, 1)
        self.assertEqual(
            [item["baseline"] for item in record["proposals"]], [None, None]
        )

    def test_a_baseline_that_fails_is_recorded_not_a_verdict(self) -> None:
        """An error must never read as a rejection, nor as a confirmation."""

        def assessment(candidates):
            return DocsAssessment(
                understanding="u", verdict="stale", findings=candidates
            )

        record, reported, _ = self.run_case(
            (Mock(), "f" * 40),
            [{"judgments": []}, self.judgments("confirmed", "confirmed")],
            assessment,
        )

        self.assertIn("unjudged", record["baseline_error"])
        self.assertEqual(
            [item["baseline"] for item in record["proposals"]], [None, None]
        )
        self.assertIn("baseline verifier failed", reported)

    def test_a_failed_level_is_a_failed_case(self) -> None:
        case = ReplayCase("owner/app", 1, "a" * 40, "b" * 40)
        repository = Repository("app", "owner/app", Path("/tmp"))
        context = Mock()
        context.__enter__ = Mock(
            return_value=evaluate._PreparedCase(
                repository,
                PullRequest(1, "Title", "", "replay", "a" * 40, ""),
                repository,
                ReviewMaterial("diff", {}),
                (),
            )
        )
        context.__exit__ = Mock(return_value=False)
        with patch("src.evaluate._prepared_case", return_value=context), patch(
            "src.cli.create_review_engine"
        ), patch("src.cli._create_verification_pass", return_value=None), patch(
            "src.cli._create_fallback_reviewer", return_value=None
        ), patch("src.cli._MeasuredEngine", side_effect=lambda inner, *a: inner), patch(
            "src.cli._design_level", side_effect=ReviewError("no usable response")
        ), patch("sys.stdout", new_callable=StringIO):
            record = advisory_case(
                Mock(state_file=Path("/tmp/s.json")),
                case,
                DESIGN_LEVEL,
                ReviewMetrics(),
            )

        self.assertEqual(record["failed"], "no usable response")
        self.assertEqual(record["proposals"], [])


class AdvisoryCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        pinning = patch("src.evaluate.pin_github_account")
        pinning.start()
        self.addCleanup(pinning.stop)

    def run_main(self, argv, record=None):
        settings = Mock(
            state_file=Path(tempfile.mkdtemp()) / "state.json",
            run_log_retention_days=7,
            reviewer_username="reviewer",
        )
        with patch("src.evaluate.load_settings", return_value=settings), patch(
            "src.evaluate.case_for_pull_request",
            side_effect=lambda settings, spec: ReplayCase(
                "owner/app", 1, "a" * 40, "b" * 40
            ),
        ) as resolve, patch(
            "src.evaluate.advisory_case",
            return_value=record or {"case": "c", "proposals": []},
        ) as run_case, patch(
            "src.evaluate.measured_engine", return_value=("kiro", "m")
        ), patch("src.evaluate.reviewer_provenance", return_value={}), patch(
            "sys.stdout", new_callable=StringIO
        ) as output, patch("sys.stderr", new_callable=StringIO) as errors:
            code = main(argv)
        return code, resolve, run_case, output.getvalue(), errors.getvalue()

    def test_a_named_pull_request_runs_only_the_named_level(self) -> None:
        code, resolve, run_case, _, _ = self.run_main(
            ["advisory", "--level", "docs", "--pr", "owner/app#1"]
        )

        self.assertEqual(code, 0)
        resolve.assert_called_once()
        self.assertEqual(run_case.call_args.args[2], DOCS_LEVEL)

    def test_naming_nothing_to_replay_is_refused(self) -> None:
        code, _, run_case, _, errors = self.run_main(["advisory", "--level", "docs"])

        self.assertEqual(code, 2)
        run_case.assert_not_called()
        self.assertIn("--pr", errors)

    def test_a_failed_case_fails_the_run(self) -> None:
        """An error must never become an empty successful measurement."""

        code, _, _, _, _ = self.run_main(
            ["advisory", "--level", "design", "--pr", "owner/app#1"],
            record={"case": "c", "failed": "boom", "proposals": []},
        )

        self.assertEqual(code, 1)

    def test_the_measurement_is_written_where_asked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "docs.json"

            code, _, _, reported, _ = self.run_main(
                [
                    "advisory",
                    "--level",
                    "docs",
                    "--pr",
                    "owner/app#1",
                    "--metrics",
                    str(target),
                ],
                record={
                    "case": "owner/app#1@aaaaaaaaaaaa",
                    "proposals": [
                        {
                            "proposal": {"title": "t", "category": "c"},
                            "now": {"verdict": "confirmed", "reason": ""},
                            "baseline": None,
                        }
                    ],
                },
            )

            written = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(written["command"], "advisory")
        self.assertEqual(written["level"], DOCS_LEVEL)
        self.assertEqual(written["summary"]["proposed"], 1)
        self.assertEqual(written["summary"]["confirmed_now"], 1)
        self.assertIn("Proposed 1 documentation candidate", reported)


if __name__ == "__main__":
    unittest.main()
