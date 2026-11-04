"""Tests for the structured measurement of what a run's reviews decided."""

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from prwarden.compare_metrics import main as compare_main
from prwarden.metrics import ReviewMetrics, compare, load


def code_review(findings: int = 1) -> dict:
    return {
        "verdict": "do_not_approve" if findings else "approve",
        "findings": [{"title": f"f{index}"} for index in range(findings)],
    }


def judgments(*verdicts) -> dict:
    return {
        "judgments": [
            {
                "ref": f"F{index}",
                "verdict": verdict,
                "reason": "r",
                **({"rejection_class": rejection} if rejection else {}),
            }
            for index, (verdict, rejection) in enumerate(verdicts, start=1)
        ]
    }


class CoverageWarningTests(unittest.TestCase):
    """The same corpus is not the same coverage: four cases lost mid-stream on
    2026-09-23 made a smaller sample read as a regression."""

    def _document(self, scored, reproduced):
        return {
            "engine": "antigravity",
            "model": "gemini-3.1-pro-high",
            "summary": {"reproduced": reproduced},
            "cases": {"count": 3, "fingerprint": "same", "scored": scored},
        }

    def test_different_coverage_is_named_with_the_missing_cases(self):
        before = self._document(["app#1", "app#2", "app#3"], 8)
        after = self._document(["app#1", "app#2"], 3)

        report = compare(before, after)

        self.assertIn("DIFFERENT COVERAGE", report)
        self.assertIn("3 case(s) measured before, 2 after, 2 in both", report)
        self.assertIn("not measured after: app#3", report)

    def test_equal_coverage_says_nothing(self):
        before = self._document(["app#1", "app#2"], 8)
        after = self._document(["app#1", "app#2"], 3)

        self.assertNotIn("COVERAGE", compare(before, after))

    def test_a_different_corpus_still_outranks_coverage(self):
        before = self._document(["app#1"], 8)
        after = self._document(["app#2"], 3)
        after["cases"]["fingerprint"] = "other"

        report = compare(before, after)

        self.assertIn("DIFFERENT CASE SETS", report)
        self.assertNotIn("DIFFERENT COVERAGE", report)


class RecordingTests(unittest.TestCase):
    def metrics(self) -> ReviewMetrics:
        return ReviewMetrics()

    def record(self, metrics: ReviewMetrics, **overrides) -> None:
        fields = {
            "slug": "owner/app",
            "number": 1,
            "head_sha": "abcdef0123456789",
            "label": "code",
            "seconds": 1.5,
            "level": "code",
            "corrected": False,
        }
        fields.update(overrides)
        metrics.record(**fields)

    def test_a_review_records_its_verdict_and_candidate_count(self) -> None:
        metrics = self.metrics()

        self.record(metrics, payload=code_review(findings=2))

        entry = metrics.invocations[0]
        self.assertEqual(entry["pull_request"], "owner/app#1")
        self.assertEqual(entry["head"], "abcdef012345")
        self.assertEqual(entry["verdict"], "do_not_approve")
        self.assertEqual(entry["findings"], 2)
        self.assertEqual(entry["seconds"], 1.5)

    def test_a_verification_records_what_it_threw_out_and_why(self) -> None:
        metrics = self.metrics()

        self.record(
            metrics,
            label="code verification",
            payload=judgments(
                ("confirmed", None),
                ("rejected", "wrong_anchor"),
                ("rejected", "not_a_defect"),
            ),
        )

        entry = metrics.invocations[0]
        self.assertEqual((entry["confirmed"], entry["rejected"]), (1, 2))
        self.assertEqual(
            entry["rejection_classes"], {"wrong_anchor": 1, "not_a_defect": 1}
        )

    def test_an_unclassified_rejection_is_counted_as_one(self) -> None:
        metrics = self.metrics()

        self.record(
            metrics, label="code verification", payload=judgments(("rejected", None))
        )

        self.assertEqual(
            metrics.invocations[0]["rejection_classes"], {"unclassified": 1}
        )

    def test_a_failed_invocation_records_the_failure(self) -> None:
        metrics = self.metrics()

        self.record(metrics, error="EngineError: provider refused")

        entry = metrics.invocations[0]
        self.assertIn("provider refused", entry["error"])
        self.assertNotIn("verdict", entry)

    def test_a_correction_retry_is_marked(self) -> None:
        """A rising count means the prompt and the schema disagree somewhere."""

        metrics = self.metrics()

        self.record(metrics, corrected=True, payload=code_review())

        self.assertTrue(metrics.invocations[0]["corrected"])
        self.assertEqual(metrics.summary()["corrected_invocations"], 1)

    def test_an_unusable_payload_is_recorded_without_a_verdict(self) -> None:
        """Measurement reads defensively: it must never fail a review."""

        metrics = self.metrics()

        self.record(metrics, payload="not a response")
        self.record(metrics, payload=None)

        self.assertEqual(len(metrics.invocations), 2)
        for entry in metrics.invocations:
            self.assertNotIn("verdict", entry)

    def test_the_caller_cannot_mutate_what_was_recorded(self) -> None:
        metrics = self.metrics()
        self.record(metrics, payload=code_review())

        metrics.invocations[0]["verdict"] = "tampered"

        self.assertEqual(metrics.invocations[0]["verdict"], "do_not_approve")


class SummaryTests(unittest.TestCase):
    def populated(self) -> ReviewMetrics:
        metrics = ReviewMetrics()
        metrics.record(
            slug="owner/app",
            number=1,
            head_sha="a" * 40,
            label="design",
            seconds=61.0,
            level="design",
            corrected=False,
            payload={"verdict": "sound", "findings": []},
        )
        metrics.record(
            slug="owner/app",
            number=1,
            head_sha="a" * 40,
            label="code",
            seconds=30.0,
            level="code",
            corrected=False,
            payload=code_review(findings=2),
        )
        metrics.record(
            slug="owner/app",
            number=1,
            head_sha="a" * 40,
            label="code verification",
            seconds=9.0,
            level="code",
            corrected=False,
            payload=judgments(("confirmed", None), ("rejected", "not_a_defect")),
        )
        metrics.record(
            slug="owner/app",
            number=2,
            head_sha="b" * 40,
            label="code",
            seconds=20.0,
            level="code",
            corrected=True,
            payload=code_review(findings=0),
        )
        return metrics

    def test_the_summary_counts_the_run(self) -> None:
        summary = self.populated().summary()

        self.assertEqual(summary["pull_requests"], 2)
        self.assertEqual(summary["invocations"], 4)
        self.assertEqual(summary["invocations_by_label"]["code"], 2)
        self.assertEqual(summary["seconds"], 120.0)
        self.assertEqual(summary["seconds_by_label"]["design"], 61.0)
        self.assertEqual(summary["corrected_invocations"], 1)
        self.assertEqual(summary["failed_invocations"], 0)

    def test_the_summary_reports_how_verification_judged(self) -> None:
        summary = self.populated().summary()

        self.assertEqual(summary["candidates"], 2)
        self.assertEqual(summary["confirmed"], 1)
        self.assertEqual(summary["confirmation_rate"], 0.5)
        self.assertEqual(summary["rejection_classes"], {"not_a_defect": 1})

    def test_the_summary_counts_verdicts(self) -> None:
        summary = self.populated().summary()

        self.assertEqual(summary["verdicts"]["sound"], 1)
        self.assertEqual(summary["verdicts"]["do_not_approve"], 1)
        self.assertEqual(summary["verdicts"]["approve"], 1)

    def test_a_run_that_reviewed_nothing_summarises_to_nothing(self) -> None:
        summary = ReviewMetrics().summary()

        self.assertEqual(summary["invocations"], 0)
        self.assertIsNone(summary["confirmation_rate"])

    def test_the_document_names_the_engine_it_measured(self) -> None:
        document = self.populated().document(engine="antigravity", model="gemini")

        self.assertEqual(document["engine"], "antigravity")
        self.assertEqual(document["model"], "gemini")
        self.assertEqual(document["schema"], 1)
        self.assertEqual(len(document["invocations"]), 4)


class ComparisonTests(unittest.TestCase):
    def document(self, **summary) -> dict:
        base = {
            "pull_requests": 3,
            "invocations": 6,
            "confirmed": 2,
            "candidates": 4,
            "confirmation_rate": 0.5,
            "rejection_classes": {"not_a_defect": 2},
            "seconds": 100.0,
        }
        base.update(summary)
        return {"engine": "antigravity", "model": "gemini", "summary": base}

    def test_the_numbers_that_moved_come_first(self) -> None:
        report = compare(
            self.document(),
            self.document(confirmed=3, confirmation_rate=0.75),
        )

        moved = report.split("moved:")[1]
        self.assertIn("+ confirmed: 2 -> 3", moved)
        self.assertIn("+ confirmation_rate: 0.5 -> 0.75", moved)
        self.assertIn("unchanged:", report)
        self.assertIn("= candidates: 4", report)

    def test_a_drop_is_marked_as_one(self) -> None:
        report = compare(self.document(), self.document(confirmed=1))

        self.assertIn("- confirmed: 2 -> 1", report)

    def test_a_class_that_appeared_is_reported(self) -> None:
        report = compare(
            self.document(),
            self.document(rejection_classes={"not_a_defect": 2, "wrong_anchor": 1}),
        )

        self.assertIn("rejection_classes.wrong_anchor: none -> 1", report)

    def test_two_identical_measurements_report_no_movement(self) -> None:
        report = compare(self.document(), self.document())

        self.assertIn("0 numbers moved:", report)
        self.assertIn("(none)", report)

    def test_both_engines_are_named_so_a_mismatch_is_visible(self) -> None:
        after = self.document()
        after["engine"] = "claude"

        report = compare(self.document(), after)

        self.assertIn("before: antigravity gemini", report)
        self.assertIn("after:  claude gemini", report)


class RunWritingTests(unittest.TestCase):
    """A run asked for a measurement must produce one, or say why not."""

    def setUp(self) -> None:
        # Pinning asks the real GitHub CLI for a stored login, which no test has.
        pinning = patch("prwarden.cli.pin_github_account")
        pinning.start()
        self.addCleanup(pinning.stop)

    def settings(self, directory: str):
        from prwarden.config import Settings
        from prwarden.models import Repository

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
            review_model="gemini-3.1-pro-high",
            review_timeout_seconds=300,
            max_diff_bytes=100000,
            max_findings=10,
        )

    def run_with_metrics(self, directory: str, metrics_path):
        from argparse import Namespace

        from prwarden import cli

        settings = self.settings(directory)
        args = Namespace(
            env_file=Path(directory) / ".env",
            repositories_file=None,
            state_file=None,
            repo=[],
            pr=None,
            live=False,
            force=False,
            engine=None,
            metrics=metrics_path,
        )
        with patch("prwarden.cli.load_settings", return_value=settings), patch(
            "prwarden.cli.create_review_engine"
        ), patch("prwarden.cli.list_open_prs", return_value=[]), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            status = cli.run(args)
        return status, output.getvalue()

    def test_a_run_writes_the_measurement_where_it_was_asked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "runs" / "before.json"

            status, terminal = self.run_with_metrics(directory, str(destination))

            self.assertEqual(status, 0)
            self.assertTrue(destination.is_file())
            document = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(document["engine"], "antigravity")
            self.assertEqual(document["model"], "gemini-3.1-pro-high")
            self.assertEqual(document["summary"]["invocations"], 0)
            self.assertIn("Measurement written to", terminal)

    def test_a_run_not_asked_for_one_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status, terminal = self.run_with_metrics(directory, None)

            self.assertEqual(status, 0)
            self.assertNotIn("Measurement written", terminal)
            self.assertEqual(list(Path(directory).glob("*.json")), [])

    def test_an_unwritable_destination_does_not_fail_the_run(self) -> None:
        """The measurement is diagnostic; losing it must not lose the run."""

        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "blocked"
            blocked.write_text("not a directory\n", encoding="utf-8")

            status, terminal = self.run_with_metrics(
                directory, str(blocked / "run.json")
            )

            self.assertEqual(status, 0)
            self.assertIn("Could not write the run measurement", terminal)


class CommandTests(unittest.TestCase):
    def test_two_files_are_compared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, confirmed in enumerate((2, 3)):
                path = Path(directory) / f"run{index}.json"
                path.write_text(
                    json.dumps(
                        {
                            "engine": "antigravity",
                            "model": None,
                            "summary": {"confirmed": confirmed},
                        }
                    ),
                    encoding="utf-8",
                )
                paths.append(str(path))

            self.assertEqual(compare_main(paths), 0)
            self.assertEqual(load(paths[0])["summary"]["confirmed"], 2)

    def test_wrong_usage_explains_itself(self) -> None:
        self.assertEqual(compare_main([]), 2)

    def test_a_missing_file_fails_without_a_traceback(self) -> None:
        with patch("sys.stderr", new_callable=StringIO) as errors:
            status = compare_main(["/nonexistent/a.json", "/nonexistent/b.json"])

        self.assertEqual(status, 1)
        self.assertIn("ERROR:", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
