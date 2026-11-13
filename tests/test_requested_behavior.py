"""The design check for asked-for behaviour that nothing in the head delivers."""

import os
import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prwarden import cli
from prwarden.config import Settings
from prwarden.delivery import format_design_slack_message
from prwarden.engine import (
    DESIGN_CATEGORIES,
    DESIGN_LEVEL,
    MAX_SEARCH_MATCHES,
    MAX_SEARCHED_FILE_BYTES,
    REQUESTED_BEHAVIOR_MISSING,
    build_design_prompt,
    build_design_schema,
    build_design_verification_prompt,
)
from prwarden.evaluate import ReplayCase, _PreparedCase, _prepared_case, advisory_case
from prwarden.github import format_design_comment
from prwarden.metrics import ReviewMetrics
from prwarden.models import (
    DependencyLink,
    DesignAssessment,
    DesignFinding,
    FindingEvidence,
    LinkedIssue,
    PullRequest,
    Repository,
    ReviewMaterial,
    WorkspaceSearch,
)
from prwarden.review import ReviewError, validate_design_findings


REPOSITORY = Repository("app", "owner/app")
ASKED = PullRequest(
    1, "Add CSV export", "Adds the CSV export the report page asks for.", "a", "h", "u"
)
SILENT = replace(ASKED, body="  ")
ISSUE = LinkedIssue(source="jira", reference="APP-12", title="Export reports")


def material(**changes) -> ReviewMaterial:
    base = ReviewMaterial(
        diff="diff --git a/api.py b/api.py\n+++ b/api.py\n@@ -0,0 +1 @@\n+route = 1",
        added_lines={"api.py": frozenset({1})},
        level=DESIGN_LEVEL,
    )
    return replace(base, **changes)


def search(**changes) -> dict:
    payload = {
        "pattern": "/reports/export",
        "scope": ".",
        "purpose": "a client call to the new export route",
        "matched_files": ["api.py"],
    }
    payload.update(changes)
    return payload


def missing_behavior(**changes) -> dict:
    finding = {
        "title": "Nothing calls the new export route",
        "severity": "MEDIUM",
        "category": REQUESTED_BEHAVIOR_MISSING,
        "problem": "The export route is added, but no screen requests it.",
        "consequence": "The report page still offers no way to download a CSV.",
        "why_not_preference": "The description asks for the export on that page.",
        "alternative": "Call the route from the report page's download button.",
        "citations": [
            {
                "path": "api.py",
                "line": 1,
                "excerpt": 'route("/reports/export")',
                "relevance": "The route nothing calls.",
            }
        ],
        "searches": [search()],
    }
    finding.update(changes)
    return {
        "understanding": "It adds a CSV export route for the report page.",
        "verdict": "flawed",
        "findings": [finding],
    }


def candidate(searches=()) -> DesignFinding:
    return DesignFinding(
        severity="MEDIUM",
        title="Nothing calls the new export route",
        category=REQUESTED_BEHAVIOR_MISSING,
        problem="p",
        consequence="c",
        why_not_preference="w",
        alternative="a",
        citations=(FindingEvidence("api.py", 1, "route", "the route"),),
        searches=searches,
    )


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.write("api.py", 'route("/reports/export")\n')
        self.write("web/report.tsx", "export function Report() {}\n")

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def validate(self, response: dict, **changes):
        return validate_design_findings(
            response, material(**changes), self.root, 4
        )


class SearchValidationTests(WorkspaceTests):
    def test_a_search_that_holds_keeps_the_controllers_own_files(self) -> None:
        _, _, findings = self.validate(missing_behavior())

        self.assertEqual(
            findings[0].searches,
            (
                WorkspaceSearch(
                    "/reports/export",
                    ".",
                    "a client call to the new export route",
                    ("api.py",),
                ),
            ),
        )

    def test_a_file_the_search_missed_rejects_the_response(self) -> None:
        """The file a search left out may be the code that delivers what it asks."""

        self.write("web/client.ts", 'fetch("/REPORTS/export")\n')

        with self.assertRaisesRegex(
            ReviewError, r"also matches web/client\.ts, which 'matched_files'"
        ):
            self.validate(missing_behavior())

    def test_a_file_the_search_did_not_match_rejects_the_response(self) -> None:
        response = missing_behavior(
            searches=[search(matched_files=["api.py", "web/report.tsx"])]
        )

        with self.assertRaisesRegex(ReviewError, r"does not match web/report\.tsx"):
            self.validate(response)

    def test_a_search_matching_many_files_shows_nothing_missing(self) -> None:
        for index in range(MAX_SEARCH_MATCHES):
            self.write(f"web/page{index}.tsx", "/reports/export\n")

        with self.assertRaisesRegex(ReviewError, "too many to show"):
            self.validate(missing_behavior())

    def test_a_missing_behaviour_needs_a_search(self) -> None:
        with self.assertRaisesRegex(ReviewError, "at least one search"):
            self.validate(missing_behavior(searches=[]))

    def test_a_listed_file_too_large_to_search_is_believed(self) -> None:
        """Unsearched is not unmatched: refusing it would refuse a true answer."""

        self.write("openapi.json", "x" * MAX_SEARCHED_FILE_BYTES + "/reports/export")

        _, _, findings = self.validate(
            missing_behavior(
                searches=[search(matched_files=["api.py", "openapi.json"])]
            )
        )

        self.assertEqual(
            findings[0].searches[0].matched_files, ("api.py", "openapi.json")
        )

    def test_a_file_too_large_to_search_is_not_required_either(self) -> None:
        self.write("openapi.json", "x" * MAX_SEARCHED_FILE_BYTES + "/reports/export")

        _, _, findings = self.validate(missing_behavior())

        self.assertEqual(findings[0].searches[0].matched_files, ("api.py",))

    def test_other_categories_still_read_without_searches(self) -> None:
        """A finding from before searches existed validates exactly as it did."""

        response = missing_behavior(category="contract_mismatch")
        del response["findings"][0]["searches"]

        _, _, findings = self.validate(response)

        self.assertEqual(findings[0].searches, ())

    def test_matching_ignores_case_and_skips_git_binaries_and_links(self) -> None:
        # A worktree's `.git` is a file, a submodule's a directory.
        self.write(".git", "gitdir: /reports/export\n")
        self.write("vendor/.git/config", "/reports/export\n")
        (self.root / "logo.png").write_bytes(b"\0/reports/export")
        dependency = tempfile.TemporaryDirectory()
        self.addCleanup(dependency.cleanup)
        (Path(dependency.name) / "index.js").write_text("/reports/export\n")
        os.symlink(dependency.name, self.root / "node_modules")
        os.symlink(Path(dependency.name) / "index.js", self.root / "linked.js")
        self.write(".github/workflow.yml", "/reports/export\n")

        _, _, findings = self.validate(
            missing_behavior(
                searches=[
                    search(
                        pattern="/REPORTS/Export",
                        matched_files=["./api.py", ".github/workflow.yml"],
                    )
                ]
            )
        )

        self.assertEqual(
            findings[0].searches[0].matched_files, (".github/workflow.yml", "api.py")
        )

    def test_a_scope_outside_this_repositorys_own_files_is_refused(self) -> None:
        dependency = tempfile.TemporaryDirectory()
        self.addCleanup(dependency.cleanup)
        os.symlink(dependency.name, self.root / "node_modules")
        link = DependencyLink("node_modules", dependency.name)

        for scope, message in (
            ("../elsewhere", "directory or file of this workspace"),
            (".git", "directory or file of this workspace"),
            ("missing", "does not exist"),
            ("node_modules", "not a linked dependency"),
        ):
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(ReviewError, message):
                    self.validate(
                        missing_behavior(searches=[search(scope=scope)]),
                        dependency_links=(link,),
                    )

    def test_a_narrower_scope_searches_only_there(self) -> None:
        self.write("web/client.ts", "/reports/export\n")

        _, _, findings = self.validate(
            missing_behavior(
                searches=[search(scope="web/", matched_files=["web/client.ts"])]
            )
        )

        self.assertEqual(findings[0].searches[0].scope, "web")


class PromptTests(unittest.TestCase):
    def test_the_check_is_offered_only_against_a_stated_intent(self) -> None:
        stated = build_design_prompt(REPOSITORY, ASKED, material())
        unstated = build_design_prompt(REPOSITORY, SILENT, material())
        issue_only = build_design_prompt(
            REPOSITORY, SILENT, material(linked_issues=(ISSUE,))
        )

        self.assertIn(REQUESTED_BEHAVIOR_MISSING, DESIGN_CATEGORIES)
        for prompt in (stated, issue_only):
            self.assertIn(f"- `{REQUESTED_BEHAVIOR_MISSING}`: a behaviour", prompt)
            self.assertIn("3. List each behaviour", prompt)
            self.assertIn("5. Only then write findings.", prompt)
            # The general reach rule would otherwise omit every absence.
            self.assertIn(
                "must be omitted.\n  A `requested_behavior_missing` consequence is "
                "reached by the request",
                prompt,
            )
        self.assertNotIn(REQUESTED_BEHAVIOR_MISSING, unstated)
        self.assertNotIn("List each behaviour", unstated)
        self.assertIn("3. Decide what the change", unstated)
        self.assertIn("4. Only then write findings.", unstated)
        self.assertIn("must be omitted.\n- `why_not_preference`", unstated)

    def test_every_design_prompt_says_how_an_absence_is_shown(self) -> None:
        """The schema asks every finding for searches, so every prompt explains them."""

        prompt = build_design_prompt(REPOSITORY, SILENT, material())

        self.assertIn("`searches` is empty unless the finding claims", prompt)
        self.assertIn("The controller runs each search", prompt)
        # The reviewer can leave out what the controller cannot search on purpose.
        self.assertIn("skipping\n  installed dependencies and files over 1 MB", prompt)

    def test_every_finding_answers_searches_in_the_schema(self) -> None:
        finding = build_design_schema(4)["properties"]["findings"]["items"]
        search_item = finding["properties"]["searches"]["items"]

        self.assertIn("searches", finding["required"])
        self.assertEqual(
            search_item["required"], ["pattern", "scope", "purpose", "matched_files"]
        )
        self.assertFalse(search_item["additionalProperties"])
        self.assertEqual(
            search_item["properties"]["matched_files"]["maxItems"], MAX_SEARCH_MATCHES
        )

    def test_the_verifier_searches_again_only_for_a_missing_behaviour(self) -> None:
        searched = candidate(
            (WorkspaceSearch("/reports/export", ".", "a client call", ("api.py",)),)
        )
        other = replace(searched, category="contract_mismatch", searches=())

        missing = build_design_verification_prompt(
            REPOSITORY, ASKED, material(design_candidates=(searched,))
        )
        unrelated = build_design_verification_prompt(
            REPOSITORY, ASKED, material(design_candidates=(other,))
        )

        self.assertIn(f"it is a `{REQUESTED_BEHAVIOR_MISSING}` candidate", missing)
        self.assertIn("never a reason to reject it", missing)
        self.assertIn("is reached by the request itself: confirm", missing)
        self.assertIn('"matched_files": [', missing)
        self.assertNotIn(REQUESTED_BEHAVIOR_MISSING, unrelated)
        self.assertNotIn('"searches"', unrelated)


class DeliveryTests(unittest.TestCase):
    SEARCHED = candidate(
        (
            WorkspaceSearch("/reports/export", ".", "a client call", ("api.py",)),
            WorkspaceSearch("downloadCsv", "web", "a download handler"),
        )
    )

    def test_the_comment_shows_every_search_and_what_it_matched(self) -> None:
        comment = format_design_comment([self.SEARCHED], "kiro", None)

        self.assertIn("**Searches:**", comment)
        self.assertIn(
            "- `/reports/export` in `.` — a client call: matched only `api.py`",
            comment,
        )
        self.assertIn(
            "- `downloadCsv` in `web` — a download handler: no match", comment
        )

    def test_a_finding_without_searches_reads_as_before(self) -> None:
        comment = format_design_comment([candidate()], "kiro", None)

        self.assertNotIn("Searches", comment)

    def test_the_slack_message_shows_the_searches(self) -> None:
        # A pattern that looks like an absolute path is shortened like any other in
        # Slack, which never carries local paths; the comment keeps it whole.
        message = format_design_slack_message(
            REPOSITORY, ASKED, [self.SEARCHED], "kiro", None
        )

        self.assertIn("• Searches: `", message)
        self.assertIn(
            "in `.` — matched only `api.py`; `downloadCsv` in `web` — no match",
            message,
        )


class OrchestrationTests(WorkspaceTests):
    def settings(self) -> Settings:
        return Settings(
            reviewer_username="reviewer",
            repositories=[],
            dry_run=True,
            slack_webhook_url=None,
            state_file=self.root / "state.json",
            review_engine="antigravity",
            review_engine_command=("agy",),
            review_engine_env_allowlist=(),
            review_model=None,
            review_timeout_seconds=300,
            max_diff_bytes=100000,
            max_findings=10,
        )

    def test_a_missed_file_is_corrected_then_the_verifier_judges_it(self) -> None:
        """The correction names the file, so the reviewer can read it and decide."""

        self.write("web/client.ts", 'fetch("/reports/export")\n')
        engine = Mock()
        engine.review.side_effect = [
            missing_behavior(),
            missing_behavior(
                searches=[search(matched_files=["api.py", "web/client.ts"])]
            ),
            {"judgments": [{"ref": "D1", "verdict": "confirmed", "reason": "r"}]},
        ]
        repository = Repository("app", "owner/app", self.root)

        with patch("sys.stdout", new_callable=StringIO):
            assessment = cli._design_level(
                self.settings(), engine, None, repository, ASKED, material()
            )

        retried = engine.review.call_args_list[1].args[2]
        self.assertIn("also matches web/client.ts", retried.validation_error)
        judged = engine.review.call_args_list[2].args[2]
        self.assertEqual(
            judged.design_candidates[0].searches[0].matched_files,
            ("api.py", "web/client.ts"),
        )
        self.assertEqual(len(assessment.findings), 1)


class ReplayTests(unittest.TestCase):
    def test_an_advisory_record_names_the_issues_it_read(self) -> None:
        """Issues are read live, so a record must say which inputs it judged."""

        repository = Repository("app", "owner/app", Path("/tmp"))
        prepared = _PreparedCase(
            repository,
            ASKED,
            repository,
            material(linked_issues=(ISSUE,)),
            (),
        )
        context = Mock()
        context.__enter__ = Mock(return_value=prepared)
        context.__exit__ = Mock(return_value=False)
        assessment = DesignAssessment(understanding="u", verdict="sound")
        with patch("prwarden.evaluate._prepared_case", return_value=context), patch(
            "prwarden.cli.create_review_engine"
        ), patch("prwarden.cli._create_verification_pass", return_value=None), patch(
            "prwarden.cli._create_fallback_reviewer", return_value=None
        ), patch(
            "prwarden.cli._MeasuredEngine", side_effect=lambda inner, *args: inner
        ), patch(
            "prwarden.cli._design_level", return_value=assessment
        ), patch(
            "sys.stdout", new_callable=StringIO
        ):
            record = advisory_case(
                Mock(state_file=Path("/tmp/state.json")),
                ReplayCase("owner/app", 1, "a" * 40, "b" * 40),
                DESIGN_LEVEL,
                ReviewMetrics(),
            )

        self.assertEqual(record["linked_issues"], ["APP-12"])

    def test_a_replay_reads_the_issues_a_live_review_reads(self) -> None:
        """A design replay without them could not measure a check against intent."""

        repository = Repository("app", "owner/app", Path("/tmp"))
        settings = Mock(state_file=Path("/tmp/state.json"))
        case = ReplayCase("owner/app", 1, "a" * 40, "b" * 40, body="Adds export.")
        with patch(
            "prwarden.evaluate._repository_for", return_value=repository
        ), patch(
            "prwarden.evaluate.changed_files", return_value=[{"filename": "api.py"}]
        ), patch(
            "prwarden.cli._bundled_material", return_value=(material(), ())
        ), patch(
            "prwarden.cli.review_workspace"
        ) as workspace, patch(
            "prwarden.evaluate.read_background", return_value=""
        ), patch(
            "prwarden.cli._read_file_history", return_value=()
        ), patch(
            "prwarden.cli.read_review_rules", return_value=Mock(sections=())
        ), patch(
            "prwarden.cli._report_diff_budget"
        ), patch(
            "prwarden.cli._read_linked_issues", return_value=(ISSUE,)
        ) as read, patch(
            "sys.stdout", new_callable=StringIO
        ):
            workspace.return_value.__enter__.return_value = repository
            workspace.return_value.dependency_links = ()
            workspace.return_value.dependency_notes = ()
            with _prepared_case(settings, case) as prepared:
                _, pull_request, _, prepared_material, _ = prepared

        read.assert_called_once_with(repository, pull_request)
        self.assertEqual(pull_request.body, "Adds export.")
        self.assertEqual(prepared_material.linked_issues, (ISSUE,))


if __name__ == "__main__":
    unittest.main()
