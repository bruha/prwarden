"""Tests for the advisory documentation review level."""

import tempfile
import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from prwarden import cli
from prwarden.commands import CommandError
from prwarden.config import Settings
from prwarden.delivery import format_docs_slack_message
from prwarden.engine import (
    DOCS_CATEGORIES,
    DOCS_LEVEL,
    DOCS_LIST_ENTRY_RULE,
    EngineError,
    build_docs_verification_schema,
    build_prompt,
    build_schema,
    docs_candidate_refs,
    response_discriminator_keys,
)
from prwarden.github import (
    DOCS_COMMENT_MARKER,
    MAX_PRIOR_REVIEW_BYTES,
    format_docs_comment,
)
from prwarden.models import (
    DocsAssessment,
    DocsFinding,
    DocumentationFile,
    FindingEvidence,
    MovedFile,
    PriorReviewComment,
    PullRequest,
    PullRequestSnapshot,
    RejectedDocsCandidate,
    Repository,
    ReviewMaterial,
)
from prwarden.review import (
    ReviewError,
    discover_documentation,
    documentation_change_fingerprint,
    is_documentation_path,
    validate_docs_findings,
    validate_docs_judgments,
)
from prwarden.state import JsonStateStore
from tests.test_review_rules import without_review_rules


READY_SNAPSHOT = PullRequestSnapshot(
    head_sha="head",
    state="open",
    is_draft=False,
    labels=frozenset(),
    changed_files=1,
    mergeable=True,
)

DOCUMENTED_LINE = "The reviewer reads at most two files per run."
README_TEXT = f"# App\n\n{DOCUMENTED_LINE}\n"
OWN_DOCS_NOTE = PriorReviewComment(
    "conversation:11",
    "conversation",
    "reviewer",
    "## Documentation updates this change needs\n\n"
    "README.md:3 should say ten files per run.\n" + DOCS_COMMENT_MARKER,
)
# The pull request's own edit to its README, as GitHub reports the patch.
README_EDIT = f"@@ -3 +3,2 @@\n {DOCUMENTED_LINE}\n+See the setup guide."
# The same edit after a rebase: the hunk moved and its context changed.
README_EDIT_REBASED = "@@ -9 +9,2 @@\n Other context.\n+See the setup guide."
README_EDIT_REVISED = (
    f"@@ -3 +3,2 @@\n {DOCUMENTED_LINE}\n+See the setup guide for every limit."
)
NO_PROPOSALS = {"understanding": "u", "verdict": "consistent", "findings": []}


def docs_response(**overrides):
    finding = {
        "title": "README still promises a two-file limit",
        "severity": "MEDIUM",
        "category": "documented_default_or_setting_changed",
        "documented": "The README says the reviewer reads at most two files per run.",
        "behavior": "The change raises that limit to ten files per run.",
        "proposed_update": "The reviewer reads at most ten files per run.",
        "documentation": {
            "path": "README.md",
            "line": 3,
            "excerpt": "at most two files",
        },
        "citations": [
            {
                "path": "app.py",
                "line": 1,
                "excerpt": "value = 1",
                "relevance": "The line that raises the limit.",
            }
        ],
    }
    finding.update(overrides.pop("finding", {}))
    payload = {
        "understanding": "It raises how many files one run reads.",
        "verdict": "stale",
        "findings": [finding],
    }
    payload.update(overrides)
    return payload


def docs_candidate(title: str = "One", audience: str = "human") -> DocsFinding:
    return DocsFinding(
        severity="MEDIUM",
        title=title,
        category="documented_behavior_no_longer_true",
        audience=audience,
        documented="d",
        behavior="b",
        proposed_update="The reviewer reads at most ten files per run.",
        documentation=FindingEvidence("README.md", 3, "at most two files", "r"),
        citations=(FindingEvidence("app.py", 1, "value = 1", "r"),),
    )


def fallback_for(settings, engine):
    """Return a fallback reviewer on another provider, or None without an engine."""

    if engine is None:
        return None
    return cli.FallbackReviewer(
        engine=engine,
        settings=Settings(
            **{
                **settings.__dict__,
                "review_engine": "kiro",
                "review_model": "claude-opus-5.5",
            }
        ),
    )


class DocumentationDiscoveryTests(unittest.TestCase):
    def write(self, root: Path, relative: str, text: str = "text\n") -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_the_audience_of_a_document_comes_from_where_it_lives(self) -> None:
        """An agent instruction file is the one nobody notices going stale."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "README.md")
            self.write(root, "docs/scheduling.md")
            self.write(root, "AGENTS.md")
            self.write(root, ".kiro/steering/style.md")

            found = {file.path: file.audience for file in discover_documentation(root)}

            self.assertEqual(found["README.md"], "human")
            self.assertEqual(found["docs/scheduling.md"], "human")
            self.assertEqual(found["AGENTS.md"], "ai")
            self.assertEqual(found[".kiro/steering/style.md"], "ai")

    def test_a_record_of_the_past_is_not_a_promise_about_the_present(self) -> None:
        """A changelog and a licence cannot be contradicted by a change."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "CHANGELOG.md")
            self.write(root, "LICENSE.md")
            self.write(root, "docs/CHANGELOG.md")
            self.write(root, "README.md")

            found = [file.path for file in discover_documentation(root)]

            self.assertEqual(found, ["README.md"])

    def test_a_dependency_or_a_plan_is_not_this_project_documentation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "node_modules/left-pad/README.md")
            self.write(root, "docs/node_modules/guide.md")
            self.write(root, ".kiro/specs/contact-form/design.md")
            self.write(root, "packages/ui/README.md")

            found = [file.path for file in discover_documentation(root)]

            self.assertEqual(found, ["packages/ui/README.md"])

    def test_an_empty_or_enormous_document_is_left_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write(root, "README.md", "")
            self.write(root, "AGENTS.md", "x" * 200_001)
            self.write(root, "CLAUDE.md", "read AGENTS.md\n")

            found = [file.path for file in discover_documentation(root)]

            self.assertEqual(found, ["CLAUDE.md"])

    def test_a_workspace_that_cannot_be_read_yields_nothing(self) -> None:
        self.assertEqual(discover_documentation(None), ())


class DocsValidationTests(unittest.TestCase):
    def workspace(self, root: Path) -> ReviewMaterial:
        (root / "app.py").write_text("value = 1\n", encoding="utf-8")
        (root / "README.md").write_text(README_TEXT, encoding="utf-8")
        (root / "AGENTS.md").write_text(README_TEXT, encoding="utf-8")
        return ReviewMaterial(
            "diff",
            {"app.py": frozenset({1})},
            documentation_files=(
                DocumentationFile("README.md", len(README_TEXT), "human"),
                DocumentationFile("AGENTS.md", len(README_TEXT), "ai"),
            ),
        )

    def test_a_validated_proposal_keeps_both_sides_on_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = self.workspace(root)

            understanding, verdict, findings = validate_docs_findings(
                docs_response(), material, root, 3
            )

            self.assertIn("raises how many files", understanding)
            self.assertEqual(verdict, "stale")
            self.assertEqual(findings[0].documentation.path, "README.md")
            self.assertEqual(findings[0].documentation.line, 3)
            self.assertEqual(findings[0].citations[0].path, "app.py")
            self.assertIn("ten files", findings[0].proposed_update)

    def test_a_renamed_file_is_part_of_the_change_a_proposal_may_cite(self) -> None:
        """A rename has no added lines, but the file is still what the change did."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = replace(
                self.workspace(root),
                moved_files=(MovedFile("src/limits.py", "src/old_limits.py"),),
            )
            (root / "src").mkdir()
            (root / "src" / "limits.py").write_text("LIMIT = 10\n", encoding="utf-8")
            citation = {
                "path": "src/limits.py",
                "line": 1,
                "excerpt": "LIMIT = 10",
                "relevance": "The renamed module that holds the limit.",
            }

            _, _, findings = validate_docs_findings(
                docs_response(finding={"citations": [citation]}), material, root, 3
            )

            self.assertEqual(findings[0].citations[0].path, "src/limits.py")

    def test_the_audience_is_taken_from_the_controller_not_the_model(self) -> None:
        """The model never states the audience, so it cannot misreport it."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = self.workspace(root)

            _, _, findings = validate_docs_findings(
                docs_response(
                    finding={
                        "documentation": {
                            "path": "AGENTS.md",
                            "line": 3,
                            "excerpt": "at most two files",
                        }
                    }
                ),
                material,
                root,
                3,
            )

            self.assertEqual(findings[0].audience, "ai")

    def test_a_proposal_about_a_file_that_is_not_documentation_is_refused(
        self,
    ) -> None:
        """Otherwise the level could rewrite source code as if it were prose."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = self.workspace(root)

            with self.assertRaisesRegex(ReviewError, "not one of this workspace"):
                validate_docs_findings(
                    docs_response(
                        finding={
                            "documentation": {
                                "path": "app.py",
                                "line": 1,
                                "excerpt": "value = 1",
                            }
                        }
                    ),
                    material,
                    root,
                    3,
                )

    def test_a_proposal_citing_nothing_this_change_touched_is_refused(self) -> None:
        """Documentation already stale before the change is not this PR's business."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = self.workspace(root)
            (root / "other.py").write_text("legacy = 2\n", encoding="utf-8")

            with self.assertRaisesRegex(ReviewError, "cites no file this pull"):
                validate_docs_findings(
                    docs_response(
                        finding={
                            "citations": [
                                {
                                    "path": "other.py",
                                    "line": 1,
                                    "excerpt": "legacy = 2",
                                    "relevance": "r",
                                }
                            ]
                        }
                    ),
                    material,
                    root,
                    3,
                )

    def test_a_quoted_documentation_line_must_be_that_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = self.workspace(root)

            with self.assertRaisesRegex(ReviewError, "excerpt does not match"):
                validate_docs_findings(
                    docs_response(
                        finding={
                            "documentation": {
                                "path": "README.md",
                                "line": 3,
                                "excerpt": "at most nine files",
                            }
                        }
                    ),
                    material,
                    root,
                    3,
                )

    def test_documentation_that_still_holds_carries_no_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = self.workspace(root)

            with self.assertRaisesRegex(ReviewError, "not consistent"):
                validate_docs_findings(
                    docs_response(verdict="consistent"), material, root, 3
                )

            understanding, verdict, findings = validate_docs_findings(
                {
                    "understanding": "It changes nothing anybody documented.",
                    "verdict": "consistent",
                    "findings": [],
                },
                material,
                root,
                3,
            )
            self.assertEqual((verdict, findings), ("consistent", []))
            self.assertTrue(understanding)

    def test_more_proposals_than_configured_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = self.workspace(root)
            response = docs_response()
            response["findings"] = response["findings"] * 2

            with self.assertRaisesRegex(ReviewError, "exceeding the configured"):
                validate_docs_findings(response, material, root, 1)

    def test_a_deferred_file_is_still_part_of_the_change(self) -> None:
        """A file the budget could not fit can be read in the workspace."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            material = self.workspace(root)
            (root / "big.py").write_text("limit = 10\n", encoding="utf-8")
            material = ReviewMaterial(
                material.diff,
                {},
                documentation_files=material.documentation_files,
                ignored_missing_patch_files=("big.py",),
            )

            _, _, findings = validate_docs_findings(
                docs_response(
                    finding={
                        "citations": [
                            {
                                "path": "big.py",
                                "line": 1,
                                "excerpt": "limit = 10",
                                "relevance": "r",
                            }
                        ]
                    }
                ),
                material,
                root,
                3,
            )

            self.assertEqual(findings[0].citations[0].path, "big.py")


LISTS_README = """# App

| Script | Purpose |
| --- | --- |
| `npm run dev` | Start the dev server |

- [Setup](docs/setup.md) explains installation
- [Deploy](docs/deploy.md) explains releases

The reviewer reads at most two files per run.
"""


class MissingListEntryShapeTests(unittest.TestCase):
    """The controller, not a prompt, decides what counts as a missing entry.

    Two wordings of a verifier rule about records each let a neighbouring proposal
    through, a clarification of a rule that was still true and a whole new section,
    so the category is accepted only in the one shape that cannot be either.
    """

    def validate(
        self,
        line: int,
        excerpt: str,
        proposed_update: str,
        category: str = "missing_list_entry",
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            (root / "README.md").write_text(LISTS_README, encoding="utf-8")
            material = ReviewMaterial(
                "diff",
                {"app.py": frozenset({1})},
                documentation_files=(
                    DocumentationFile("README.md", len(LISTS_README), "human"),
                ),
            )
            return validate_docs_findings(
                docs_response(
                    finding={
                        "category": category,
                        "documentation": {
                            "path": "README.md",
                            "line": line,
                            "excerpt": excerpt,
                        },
                        "proposed_update": proposed_update,
                    }
                ),
                material,
                root,
                3,
            )[2]

    def test_new_rows_for_an_existing_table_are_accepted(self) -> None:
        for proposed in (
            "| `npm run e2e` | Run the end-to-end suite |",
            "| `npm run e2e` | Run it |\n| `npm run e2e:ui` | Open the runner |",
            "| `npm run grep` | Match `a\\|b` in specs |",
        ):
            with self.subTest(proposed=proposed):
                findings = self.validate(5, "`npm run dev`", proposed)

                self.assertEqual(findings[0].category, "missing_list_entry")
                self.assertEqual(findings[0].proposed_update, proposed)

    def test_a_new_list_item_may_wrap_onto_indented_lines(self) -> None:
        proposed = "- [Testing](docs/testing.md) explains how\n  the suites are run"

        findings = self.validate(8, "[Deploy]", proposed)

        self.assertEqual(findings[0].proposed_update, proposed)

    def test_the_cited_line_must_be_an_entry_of_the_list(self) -> None:
        for line, excerpt in ((10, "at most two files"), (4, "| --- | --- |")):
            with self.subTest(line=line):
                with self.assertRaisesRegex(
                    ReviewError, "must cite an existing list item or table row"
                ):
                    self.validate(line, excerpt, "| `npm run e2e` | Run it |")

    def test_the_proposal_adds_entries_and_nothing_else(self) -> None:
        cases = {
            "an instruction around the row": (
                5,
                "`npm run dev`",
                "Insert after the dev row:\n| `npm run e2e` | Run it |",
            ),
            "a heading and a new table": (
                5,
                "`npm run dev`",
                "### E2E\n\n| Spec | Asserts |\n| --- | --- |\n| `a.spec.ts` | b |",
            ),
            "a row of another table": (
                5,
                "`npm run dev`",
                "| `npm run e2e` | Run it | Node 22 |",
            ),
            "an item of another list": (8, "[Deploy]", "* [Testing](docs/testing.md)"),
            "a sentence instead of an item": (
                8,
                "[Deploy]",
                "Testing is described in docs/testing.md.",
            ),
        }
        for name, (line, excerpt, proposed) in cases.items():
            with self.subTest(name):
                with self.assertRaisesRegex(
                    ReviewError, "must hold only new entries formatted like"
                ):
                    self.validate(line, excerpt, proposed)

    def test_other_categories_keep_their_free_form_update(self) -> None:
        """A corrected sentence is the replacement text of every other category."""

        findings = self.validate(
            10,
            "at most two files",
            "The reviewer reads at most ten files per run.",
            category="documented_default_or_setting_changed",
        )

        self.assertEqual(findings[0].category, "documented_default_or_setting_changed")


class DocsJudgmentTests(unittest.TestCase):
    def test_a_rejected_proposal_carries_the_reason_it_was_thrown_out(self) -> None:
        candidates = (docs_candidate("One"), docs_candidate("Two"))

        confirmed, rejected = validate_docs_judgments(
            {
                "judgments": [
                    {"ref": "DC1", "verdict": "confirmed", "reason": "Read it."},
                    {
                        "ref": "DC2",
                        "verdict": "rejected",
                        "reason": "The document already says the new thing.",
                    },
                ]
            },
            candidates,
        )

        self.assertEqual([finding.title for finding in confirmed], ["One"])
        self.assertEqual(rejected[0].finding.title, "Two")
        self.assertIn("already says", rejected[0].reason)

    def test_a_proposal_nobody_judged_loses_the_pass(self) -> None:
        candidates = (docs_candidate("One"), docs_candidate("Two"))

        with self.assertRaisesRegex(ReviewError, "left candidates unjudged"):
            validate_docs_judgments(
                {
                    "judgments": [
                        {"ref": "DC1", "verdict": "confirmed", "reason": "Read it."}
                    ]
                },
                candidates,
            )

    def test_a_judgment_of_something_never_supplied_is_refused(self) -> None:
        with self.assertRaisesRegex(ReviewError, "was not supplied"):
            validate_docs_judgments(
                {
                    "judgments": [
                        {"ref": "D1", "verdict": "confirmed", "reason": "r"}
                    ]
                },
                (docs_candidate(),),
            )

    def test_confirmation_keeps_the_controller_validated_text(self) -> None:
        """The verifier judges by reference, so it cannot edit what it approves."""

        candidate = docs_candidate()

        confirmed, _ = validate_docs_judgments(
            {"judgments": [{"ref": "DC1", "verdict": "confirmed", "reason": "r"}]},
            (candidate,),
        )

        self.assertIs(confirmed[0], candidate)
        self.assertEqual(list(docs_candidate_refs((candidate,))), ["DC1"])


class DocumentationChangeTests(unittest.TestCase):
    """What decides that a push changed this pull request's documentation."""

    def test_a_path_counts_as_documentation_exactly_where_discovery_looks(
        self,
    ) -> None:
        documentation = (
            "README.md",
            "docs/guide.md",
            "docs/a/b/deep.md",
            "docs/.hidden/notes.md",
            "packages/app/README.md",
            "AGENTS.md",
            ".kiro/steering/rules.md",
            ".cursor/rules/style.mdc",
        )
        not_documentation = (
            "app.py",
            "a/b/c/README.md",
            "docs/CHANGELOG.md",
            "CHANGELOG.md",
            "node_modules/pkg/README.md",
            "docs/dist/page.md",
            ".kiro/specs/plan.md",
            "docs/image.png",
        )
        for path in documentation:
            with self.subTest(path):
                self.assertTrue(is_documentation_path(path))
        for path in not_documentation:
            with self.subTest(path):
                self.assertFalse(is_documentation_path(path))

    def test_the_path_test_agrees_with_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for path in (
                "README.md",
                "docs/a.md",
                "docs/x/y.md",
                "pkg/README.md",
                "a/b/c/README.md",
                "docs/CHANGELOG.md",
                ".kiro/specs/plan.md",
            ):
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                (root / path).write_text("text\n", encoding="utf-8")

            discovered = {item.path for item in discover_documentation(root)}

            for path in (
                str(item.relative_to(root)) for item in root.rglob("*.md")
            ):
                with self.subTest(path):
                    self.assertEqual(is_documentation_path(path), path in discovered)

    def test_a_pull_request_with_no_documentation_edits_has_no_fingerprint(
        self,
    ) -> None:
        self.assertIsNone(
            documentation_change_fingerprint(
                [{"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}]
            )
        )

    def test_only_a_different_documentation_edit_changes_the_fingerprint(
        self,
    ) -> None:
        code = {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
        edit = {"filename": "README.md", "patch": README_EDIT}

        original = documentation_change_fingerprint([code, edit])

        self.assertIsNotNone(original)
        # A rebase, and a code-only push, leave the documentation edit alone.
        rebased = dict(edit, patch=README_EDIT_REBASED)
        recoded = dict(code, patch="@@ -0,0 +1 @@\n+value = 2")
        self.assertEqual(documentation_change_fingerprint([rebased, code]), original)
        self.assertEqual(documentation_change_fingerprint([recoded, edit]), original)
        # A different edit, a newly edited document, or a deleted one does not.
        revised = dict(edit, patch=README_EDIT_REVISED)
        guide = {"filename": "docs/guide.md", "patch": "@@ -0,0 +1 @@\n+Guide"}
        deleted = {"filename": "docs/old.md", "status": "removed", "patch": "-Old"}
        for changed in ([code, revised], [code, edit, guide], [code, edit, deleted]):
            with self.subTest(changed=[item["filename"] for item in changed]):
                self.assertNotEqual(
                    documentation_change_fingerprint(changed), original
                )

    def test_a_document_renamed_away_still_counts(self) -> None:
        renamed = {
            "filename": "notes/guide.txt",
            "previous_filename": "docs/guide.md",
            "status": "renamed",
        }

        self.assertIsNotNone(documentation_change_fingerprint([renamed]))

    def test_a_document_without_a_patch_is_judged_by_its_blob(self) -> None:
        def large(sha: str) -> dict:
            return {"filename": "docs/huge.md", "status": "modified", "sha": sha}

        self.assertEqual(
            documentation_change_fingerprint([large("aaa")]),
            documentation_change_fingerprint([large("aaa")]),
        )
        self.assertNotEqual(
            documentation_change_fingerprint([large("aaa")]),
            documentation_change_fingerprint([large("bbb")]),
        )


class DocsPromptTests(unittest.TestCase):
    def material(self, **overrides) -> ReviewMaterial:
        fields = {
            "documentation_files": (
                DocumentationFile("README.md", 20480, "human"),
                DocumentationFile("AGENTS.md", 6144, "ai"),
            ),
            "level": DOCS_LEVEL,
            "max_docs_findings": 3,
        }
        fields.update(overrides)
        return ReviewMaterial("diff body", {"app.py": frozenset({1})}, **fields)

    def prompt(self, material: ReviewMaterial) -> str:
        return build_prompt(
            Repository("app", "owner/app", Path(".")),
            PullRequest(1, "Title", "body", "author", "head", "url"),
            material,
            10,
        )

    def test_the_prompt_names_the_documents_rather_than_pasting_them(self) -> None:
        """119 KB of documentation would make the diff a footnote."""

        prompt = self.prompt(self.material())

        self.assertIn("README.md (20 KB, written for people)", prompt)
        self.assertIn("AGENTS.md (6 KB, written for coding agents)", prompt)
        self.assertNotIn(README_TEXT, prompt)
        self.assertIn("diff body", prompt)

    def test_the_prompt_carries_the_configured_cap_and_its_boundaries(self) -> None:
        prompt = self.prompt(self.material(max_docs_findings=2))

        self.assertIn("Report at most 2", prompt)
        self.assertIn("Do not propose a code change", prompt)
        self.assertIn("Not an instruction to somebody", prompt)

    def test_a_workspace_without_documentation_says_so(self) -> None:
        prompt = self.prompt(self.material(documentation_files=()))

        self.assertIn("(none found in this workspace)", prompt)

    def test_the_verification_prompt_supplies_candidates_by_reference(self) -> None:
        prompt = self.prompt(self.material(docs_candidates=(docs_candidate(),)))

        self.assertIn('"ref": "DC1"', prompt)
        self.assertIn("Never restate a candidate's text", prompt)

    def test_the_verifier_rejects_a_proposal_prior_feedback_already_made(
        self,
    ) -> None:
        """The second check against a repeat, as the design verifier has.

        A level that runs at every head sees its own earlier notes; the reviewer is
        told to omit what they already ask for, and the verifier must be able to
        throw out what the reviewer repeats anyway.
        """

        earlier = PriorReviewComment(
            "conversation:7",
            "conversation",
            "reviewer",
            "Update README.md:3 to say twenty files per run.",
        )
        material = self.material(
            docs_candidates=(docs_candidate(),), prior_comments=(earlier,)
        )
        prompt = self.prompt(material)

        self.assertIn("by earlier runs of\nthis reviewer", prompt)
        self.assertIn("twenty files per run", prompt)
        self.assertIn(
            "prior feedback above already asks for the same documentation update",
            prompt,
        )

    def test_both_passes_read_one_rule_for_a_missing_list_entry(self) -> None:
        """The reviewer may report a new script a scripts table lacks, and the
        verifier, told to reject anything merely silent, threw out every such
        proposal because the table never claimed to be complete: four of the six
        documentation rejections in four days of live runs. A rule loosening that
        for every candidate also passed a clarification and a new section, so the
        exception belongs to the one category the controller checks."""

        review = self.prompt(self.material())
        verification = self.prompt(
            self.material(docs_candidates=(docs_candidate(),))
        )

        for name, prompt in (("review", review), ("verification", verification)):
            with self.subTest(pass_=name):
                self.assertIn(DOCS_LIST_ENTRY_RULE, prompt)
        self.assertIn("or is merely silent rather than wrong;", verification)
        self.assertIn("For a `missing_list_entry` candidate alone", verification)
        self.assertIn("missing_list_entry", DOCS_CATEGORIES)

    def test_a_narrated_transcript_still_yields_the_docs_response(self) -> None:
        """An adapter reading narration needs this level's keys, not the code level's.

        The Kiro salvage path once rejected a valid design judgment for exactly this
        reason, so a new level that forgets to register is a silent failure.
        """

        self.assertEqual(
            response_discriminator_keys(self.material()),
            frozenset({"understanding", "verdict", "findings"}),
        )
        self.assertEqual(
            response_discriminator_keys(
                self.material(docs_candidates=(docs_candidate(),))
            ),
            frozenset({"judgments"}),
        )

    def test_the_schema_follows_the_level_and_its_candidates(self) -> None:
        review_schema = build_schema(10, self.material())
        judgment_schema = build_schema(
            10, self.material(docs_candidates=(docs_candidate(),))
        )

        self.assertEqual(
            review_schema["properties"]["verdict"]["enum"],
            ["consistent", "stale"],
        )
        self.assertEqual(
            review_schema["properties"]["findings"]["items"]["required"][-2:],
            ["documentation", "citations"],
        )
        self.assertEqual(judgment_schema, build_docs_verification_schema(1))


class DocsDeliveryFormatTests(unittest.TestCase):
    def test_the_transcript_names_each_proposal_s_category(self) -> None:
        """A measurement of one category cannot be read off a log that omits it."""

        assessment = DocsAssessment(
            understanding="u",
            verdict="stale",
            findings=(replace(docs_candidate("Kept"), category="missing_list_entry"),),
            rejected=(RejectedDocsCandidate(docs_candidate("Dropped"), "silent"),),
        )

        with patch("sys.stdout", new_callable=StringIO) as output:
            cli._report_docs_assessment(assessment, 2)

        reported = output.getvalue()
        self.assertIn(
            "Rejected documentation proposal [MEDIUM] "
            "(documented_behavior_no_longer_true) Dropped",
            reported,
        )
        self.assertIn("needs an update (missing_list_entry): Kept", reported)
    def test_the_comment_fences_the_proposed_text_and_carries_the_marker(
        self,
    ) -> None:
        """The proposal is meant to be copied, so Markdown in it must not render."""

        body = format_docs_comment(
            (
                DocsFinding(
                    severity="MEDIUM",
                    title="README still promises two files",
                    category="documented_default_or_setting_changed",
                    audience="human",
                    documented="It says two files.",
                    behavior="It is now ten files.",
                    proposed_update="- reads at most `ten` files per run",
                    documentation=FindingEvidence(
                        "README.md", 3, "at most two files", "r"
                    ),
                    citations=(
                        FindingEvidence("app.py", 1, "value = 1", "raises it"),
                    ),
                ),
            ),
            "antigravity",
            "gemini-3.1-pro-high",
        )

        # Still advisory in what it can do, which the controller enforces; the
        # heading does not say so.
        self.assertTrue(
            body.startswith("## Documentation updates this change needs\n\n")
        )
        self.assertNotIn("(advisory)", body)
        self.assertIn("documentation for people", body)
        self.assertIn("```\n- reads at most `ten` files per run\n```", body)
        self.assertIn("`README.md:3`", body)
        self.assertTrue(body.rstrip().endswith(DOCS_COMMENT_MARKER))

    def test_a_missing_list_entry_says_where_its_rows_go(self) -> None:
        """Its proposal is only the new rows, so the position has to be stated."""

        finding = replace(
            docs_candidate(),
            category="missing_list_entry",
            proposed_update="| `npm run e2e` | Run the end-to-end suite |",
        )

        body = format_docs_comment((finding,), "kiro", None)
        message = format_docs_slack_message(
            Repository("app", "owner/app", Path(".")),
            PullRequest(7, "Title", "", "author", "head", "url"),
            (finding,),
            "kiro",
            None,
        )

        self.assertIn("**Entries to add after `README.md:3`:**", body)
        self.assertNotIn("**Proposed documentation update:**", body)
        self.assertIn("• Add after `README.md:3`: | `npm run e2e` |", message)

    def test_the_slack_message_redacts_local_paths(self) -> None:
        message = format_docs_slack_message(
            Repository("app", "owner/app", Path("/Users/someone/checkouts/app")),
            PullRequest(7, "Title", "", "author", "head", "url"),
            (docs_candidate(audience="ai"),),
            "antigravity",
            None,
        )

        self.assertIn("Documentation Updates", message)
        self.assertIn("The code may be right and the document out of date.", message)
        self.assertNotIn("dvisory", message)
        self.assertIn("for coding agents", message)
        self.assertNotIn("/Users/someone", message)


class DocsOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        without_review_rules(self)

    def settings(
        self,
        directory: str,
        repository: Repository,
        docs_level: bool = True,
        design_level: bool = False,
    ) -> Settings:
        return Settings(
            reviewer_username="reviewer",
            repositories=[repository],
            dry_run=False,
            slack_webhook_url="https://example.invalid/webhook",
            state_file=Path(directory) / "state.json",
            review_engine="antigravity",
            review_engine_command=("agy",),
            review_engine_env_allowlist=(),
            review_model=None,
            review_timeout_seconds=300,
            max_diff_bytes=100000,
            max_findings=10,
            design_level=design_level,
            docs_level=docs_level,
        )

    def engine_for(
        self,
        docs_review=None,
        docs_judgment=None,
        design_review=None,
        design_judgment=None,
    ) -> Mock:
        """Return an engine answering each level by what it was handed."""

        def review(repository, pull_request, material, **kwargs):
            if material.level == "docs":
                if material.docs_candidates:
                    return docs_judgment or {
                        "judgments": [
                            {
                                "ref": ref,
                                "verdict": "confirmed",
                                "reason": "Read both sides.",
                            }
                            for ref in docs_candidate_refs(material.docs_candidates)
                        ]
                    }
                return docs_review if docs_review is not None else docs_response()
            if material.level == "design":
                if material.design_candidates:
                    return design_judgment or {"judgments": []}
                return design_review or {
                    "understanding": "u",
                    "verdict": "sound",
                    "findings": [],
                }
            return {
                "verdict": "approve",
                "approval_reason": "The change is correct.",
                "findings": [],
            }

        engine = Mock()
        engine.review.side_effect = review
        return engine

    def run_review(
        self,
        directory: str,
        engine: Mock,
        *,
        docs_level: bool = True,
        design_level: bool = False,
        author: str = "author",
        live: bool = True,
        force: bool = False,
        head: str = "head",
        documented: bool = True,
        store=None,
        fallback_engine=None,
        slack: bool = True,
        own_notes=(),
        readme_patch=None,
    ):
        root = Path(directory)
        (root / "app.py").write_text("value = 1\n", encoding="utf-8")
        if documented:
            (root / "README.md").write_text(README_TEXT, encoding="utf-8")
        repository = Repository("app", "owner/app", root, slack=slack)
        changed_files = [{"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}]
        if readme_patch is not None:
            # The pull request also edits its README, with this patch.
            changed_files.append({"filename": "README.md", "patch": readme_patch})
        pull_request = PullRequest(1, "Title", "", author, head, "url")
        settings = self.settings(
            directory, repository, docs_level=docs_level, design_level=design_level
        )
        lookup = (
            {"side_effect": own_notes}
            if isinstance(own_notes, Exception)
            else {"return_value": tuple(own_notes)}
        )

        with patch("prwarden.cli.has_current_review", return_value=False), patch(
            "prwarden.cli.list_prior_review_comments", return_value=()
        ), patch(
            "prwarden.cli.list_own_advisory_notes", **lookup
        ) as own_lookup, patch(
            "prwarden.cli.list_changed_files",
            return_value=changed_files,
        ), patch(
            "prwarden.cli.list_pull_request_checks", return_value=()
        ), patch(
            "prwarden.cli._read_linked_issues", return_value=()
        ), patch(
            "prwarden.cli._read_file_history", return_value=()
        ), patch(
            "prwarden.cli.get_pull_request_snapshot",
            return_value=PullRequestSnapshot(
                head_sha=head,
                state="open",
                is_draft=False,
                labels=frozenset(),
                changed_files=1,
                mergeable=True,
            ),
        ), patch(
            "prwarden.cli.review_workspace"
        ) as workspace, patch(
            "prwarden.cli.submit_docs_notes", return_value="docs-comment-1"
        ) as docs_notes, patch(
            "prwarden.cli.submit_design_notes", return_value="design-comment-1"
        ) as design_notes, patch(
            "prwarden.cli.submit_approval", return_value="approval-1"
        ) as approval, patch(
            "prwarden.cli.submit_review", return_value="review-1"
        ) as review, patch(
            "prwarden.cli.send_slack_message", return_value="http-200"
        ) as slack, patch(
            "prwarden.cli.submit_lgtm_comment", return_value="lgtm-comment-1"
        ) as lgtm, patch(
            "prwarden.cli.hide_earlier_lgtm_comments", return_value=0
        ), patch(
            "sys.stdout", new_callable=StringIO
        ) as output:
            workspace.return_value.__enter__.return_value = repository
            workspace.return_value.dependency_links = ()
            workspace.return_value.dependency_notes = ()
            owned_store = store
            if owned_store is None:
                owned_store = JsonStateStore(settings.state_file)
            with owned_store as opened:
                cli._process_pull_request(
                    settings,
                    repository,
                    pull_request,
                    opened,
                    engine,
                    live=live,
                    force=force,
                    fallback=fallback_for(settings, fallback_engine),
                )
        return {
            "terminal": output.getvalue(),
            "docs_notes": docs_notes,
            "design_notes": design_notes,
            "approval": approval,
            "review": review,
            "slack": slack,
            "lgtm": lgtm,
            "settings": settings,
            "own_lookup": own_lookup,
        }

    def levels_reviewed(self, engine: Mock) -> list:
        """Return the level of each review invocation, verification passes aside."""

        return [
            call.args[2].level
            for call in engine.review.call_args_list
            if not call.args[2].docs_candidates
            and not call.args[2].design_candidates
            and not call.args[2].verification_candidates
        ]

    def primary_failing_docs(self, error: Exception) -> Mock:
        """A primary that cannot answer the documentation level but approves code."""

        answering = self.engine_for()

        def review(repository, pull_request, material, **kwargs):
            if material.level == "docs":
                raise error
            return answering.review(repository, pull_request, material, **kwargs)

        engine = Mock()
        engine.review.side_effect = review
        return engine

    def test_an_engine_failure_hands_the_documentation_review_over(self) -> None:
        """From 2026-10-13 a withdrawn model failed 48 documentation reviews in a
        week while the code level carried on through its fallback."""

        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)
        with tempfile.TemporaryDirectory() as directory:
            engine = self.primary_failing_docs(
                EngineError("model gemini-3.1-pro-high is not recognized")
            )
            fallback = self.engine_for()

            result = self.run_review(directory, engine, fallback_engine=fallback)
            terminal = result["terminal"]

            self.assertIn(
                "Running fallback documentation review with kiro (claude-opus-5.5)",
                terminal,
            )
            self.assertNotIn("Documentation level failed", terminal)
            # The fallback reviewed and, with no separate verifier, also judged.
            self.assertEqual(
                [call.args[2].level for call in fallback.review.call_args_list],
                ["docs", "docs"],
            )
            self.assertTrue(fallback.review.call_args_list[1].args[2].docs_candidates)
            body = result["docs_notes"].call_args.args[2]
            self.assertIn("Reviewed by Claude Opus 5.5 @ Kiro", body)
            self.assertNotIn("Antigravity", body)
            # The code level stayed on the primary, which could still answer it.
            result["approval"].assert_called_once()
            self.assertNotIn("Running fallback review", terminal)

    def test_a_failed_documentation_fallback_names_both(self) -> None:
        cli._PRIMARY_ENGINE.reset()
        self.addCleanup(cli._PRIMARY_ENGINE.reset)
        with tempfile.TemporaryDirectory() as directory:
            engine = self.primary_failing_docs(EngineError("UNAVAILABLE: try again"))
            fallback = Mock()
            fallback.review.side_effect = EngineError(
                "Individual quota reached. Resets in 9h12m."
            )

            result = self.run_review(directory, engine, fallback_engine=fallback)

            self.assertIn(
                "Documentation level failed: antigravity documentation review "
                "failed: UNAVAILABLE: try again; kiro (claude-opus-5.5) fallback "
                "also failed",
                result["terminal"],
            )
            self.assertEqual(cli._PRIMARY_ENGINE.exhausted_reason, "")
            result["docs_notes"].assert_not_called()
            result["approval"].assert_called_once()

    def test_the_documentation_level_posts_its_own_advisory_comment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            result = self.run_review(directory, engine)

            result["docs_notes"].assert_called_once()
            body = result["docs_notes"].call_args.args[2]
            self.assertIn("README.md:3", body)
            self.assertIn("ten files per run", body)
            self.assertIn("Documentation judged stale", result["terminal"])
            self.assertIn("README.md:3 needs an update", result["terminal"])
            # Advisory: the code level still approves.
            result["approval"].assert_called_once()

    def test_the_documentation_level_follows_the_design_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            self.run_review(directory, engine, design_level=True)

            self.assertEqual(self.levels_reviewed(engine), ["design", "docs", "code"])

    def test_a_repository_carrying_no_documentation_costs_no_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            result = self.run_review(directory, engine, documented=False)

            self.assertEqual(self.levels_reviewed(engine), ["code"])
            self.assertIn("carries no documentation to check", result["terminal"])

    def test_a_disabled_documentation_level_costs_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            self.run_review(directory, engine, docs_level=False)

            self.assertEqual(self.levels_reviewed(engine), ["code"])

    def test_a_settled_documentation_level_is_not_checked_again(self) -> None:
        """Keyed by pull request: a later push must not earn a second comment."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()
            settings_file = Path(directory) / "state.json"

            with JsonStateStore(settings_file) as store:
                first = self.run_review(directory, engine, store=store)
                again = self.run_review(directory, engine, store=store, head="head2")

            first["docs_notes"].assert_called_once()
            again["docs_notes"].assert_not_called()
            self.assertIn(
                "Documentation level already settled", again["terminal"]
            )
            # Two runs, two code reviews, one documentation check.
            self.assertEqual(self.levels_reviewed(engine), ["docs", "code", "code"])

    def test_force_checks_a_settled_documentation_level_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()
            state_file = Path(directory) / "state.json"

            with JsonStateStore(state_file) as store:
                self.run_review(directory, engine, store=store)
                again = self.run_review(
                    directory, engine, store=store, head="head2", force=True
                )

            self.assertNotIn(
                "Documentation level already settled", again["terminal"]
            )
            again["docs_notes"].assert_called_once()

    def test_a_push_that_edits_the_documentation_earns_another_check(self) -> None:
        """New documentation text is text the level has never read. In a repository
        that is mostly documentation that is nearly every push."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            with JsonStateStore(Path(directory) / "state.json") as store:
                self.run_review(
                    directory, engine, store=store, readme_patch=README_EDIT
                )
                again = self.run_review(
                    directory,
                    engine,
                    store=store,
                    head="head2",
                    readme_patch=README_EDIT_REVISED,
                )

            self.assertIn(
                "Documentation edits in this pull request changed since the last "
                "documentation check (delivered)",
                again["terminal"],
            )
            again["docs_notes"].assert_called_once()
            again["own_lookup"].assert_called_once()
            self.assertEqual(
                self.levels_reviewed(engine), ["docs", "code", "docs", "code"]
            )

    def test_a_rebase_that_leaves_the_documentation_edits_alone_settles(
        self,
    ) -> None:
        """A rebase moves hunks and their context; the edit itself is unchanged."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            with JsonStateStore(Path(directory) / "state.json") as store:
                self.run_review(
                    directory, engine, store=store, readme_patch=README_EDIT
                )
                again = self.run_review(
                    directory,
                    engine,
                    store=store,
                    head="head2",
                    readme_patch=README_EDIT_REBASED,
                )

            self.assertIn(
                "its documentation edits are unchanged", again["terminal"]
            )
            again["docs_notes"].assert_not_called()
            self.assertEqual(self.levels_reviewed(engine), ["docs", "code", "code"])

    def test_the_documentation_edits_checked_are_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            files = [
                {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"},
                {"filename": "README.md", "patch": README_EDIT},
            ]
            for name, engine in (
                ("delivered", self.engine_for()),
                ("no_findings", self.engine_for(docs_review=NO_PROPOSALS)),
            ):
                with self.subTest(name):
                    state_file.unlink(missing_ok=True)
                    self.run_review(
                        directory,
                        engine,
                        store=JsonStateStore(state_file),
                        readme_patch=README_EDIT,
                    )
                    with JsonStateStore(state_file) as opened:
                        record = opened.get(
                            opened.pull_request_key("owner/app", 1, "docs")
                        )
                    self.assertEqual(record["status"], name)
                    self.assertEqual(
                        record["docs_fingerprint"],
                        documentation_change_fingerprint(files),
                    )

    def test_a_check_recorded_before_fingerprints_reads_as_no_edits(self) -> None:
        """Existing state must neither recheck every pull request nor settle one
        whose documentation edits it never recorded."""

        for readme_patch, levels in ((None, ["code"]), (README_EDIT, ["docs", "code"])):
            with self.subTest(edits_documentation=readme_patch is not None):
                with tempfile.TemporaryDirectory() as directory:
                    engine = self.engine_for()

                    with JsonStateStore(Path(directory) / "state.json") as store:
                        store.set_status(
                            store.pull_request_key("owner/app", 1, "docs"),
                            "no_findings",
                        )
                        self.run_review(
                            directory,
                            engine,
                            store=store,
                            readme_patch=readme_patch,
                        )

                    self.assertEqual(self.levels_reviewed(engine), levels)

    def test_a_check_again_sees_its_own_notes_and_the_code_level_does_not(
        self,
    ) -> None:
        """Prior feedback leaves out the reviewer's own comments, so a second check
        would otherwise propose the same edit again. The code level must never see
        these notes: it could cite one as a reason to withhold approval, and an
        advisory note may not block."""

        with tempfile.TemporaryDirectory() as directory:
            with JsonStateStore(Path(directory) / "state.json") as store:
                self.run_review(
                    directory, self.engine_for(), store=store, readme_patch=README_EDIT
                )
                engine = self.engine_for()
                result = self.run_review(
                    directory,
                    engine,
                    store=store,
                    head="head2",
                    readme_patch=README_EDIT_REVISED,
                    own_notes=(OWN_DOCS_NOTE,),
                )

            lookup = result["own_lookup"]
            lookup.assert_called_once()
            self.assertEqual(
                lookup.call_args.args[2:], ("reviewer", DOCS_COMMENT_MARKER)
            )
            materials = [call.args[2] for call in engine.review.call_args_list]
            docs = [material for material in materials if material.level == "docs"]
            # The review and its verification both read them.
            self.assertEqual(len(docs), 2)
            for material in docs:
                self.assertIn(OWN_DOCS_NOTE, material.prior_comments)
            code = [
                material
                for material in materials
                if material.level not in ("docs", "design")
            ]
            self.assertEqual(code[0].prior_comments, ())
            self.assertIn(
                "Considering 1 earlier documentation note of this reviewer",
                result["terminal"],
            )

    def test_a_delivery_that_never_recorded_its_outcome_reads_its_notes(
        self,
    ) -> None:
        """The comment may have posted before the run stopped."""

        with tempfile.TemporaryDirectory() as directory:
            with JsonStateStore(Path(directory) / "state.json") as store:
                store.set_status(
                    store.pull_request_key("owner/app", 1, "docs"),
                    "delivery_started",
                )
                result = self.run_review(directory, self.engine_for(), store=store)

            result["own_lookup"].assert_called_once()

    def test_force_also_shows_the_level_its_own_earlier_notes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            result = self.run_review(
                directory, engine, force=True, own_notes=(OWN_DOCS_NOTE,)
            )

            result["own_lookup"].assert_called_once()

    def test_a_first_check_spends_no_lookup_on_notes_it_never_posted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            result = self.run_review(directory, engine)

            result["own_lookup"].assert_not_called()
            result["docs_notes"].assert_called_once()

    def test_a_failed_lookup_fails_only_the_documentation_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            result = self.run_review(
                directory,
                engine,
                force=True,
                own_notes=CommandError("listing comments failed"),
            )

            self.assertIn(
                "Documentation level failed: listing comments failed",
                result["terminal"],
            )
            self.assertEqual(self.levels_reviewed(engine), ["code"])
            result["docs_notes"].assert_not_called()
            result["approval"].assert_called_once()

    def test_a_defect_in_the_documentation_level_stays_in_that_level(self) -> None:
        """A bug here once took the code review and the whole run down with it."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            result = self.run_review(
                directory,
                engine,
                force=True,
                own_notes=AttributeError("'X' object has no attribute 'y'"),
            )

            self.assertIn(
                "Documentation level failed unexpectedly: AttributeError: "
                "'X' object has no attribute 'y'",
                result["terminal"],
            )
            self.assertEqual(self.levels_reviewed(engine), ["code"])
            result["docs_notes"].assert_not_called()
            result["approval"].assert_called_once()

    def test_earlier_notes_stay_within_the_review_context_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()
            oversized = replace(
                OWN_DOCS_NOTE, body="x" * (MAX_PRIOR_REVIEW_BYTES + 1)
            )

            result = self.run_review(
                directory, engine, force=True, own_notes=(oversized,)
            )

            self.assertIn("safe review-context limit", result["terminal"])
            self.assertEqual(self.levels_reviewed(engine), ["code"])
            result["approval"].assert_called_once()

    def test_a_failed_documentation_level_leaves_the_code_review_alone(self) -> None:
        """This level informs; it may not decide the fate of the review."""

        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for(
                docs_review={"understanding": "u", "verdict": "banana", "findings": []}
            )

            result = self.run_review(directory, engine)

            self.assertIn("Documentation level failed", result["terminal"])
            result["docs_notes"].assert_not_called()
            result["approval"].assert_called_once()

    def test_unverified_proposals_are_never_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for(
                docs_judgment={
                    "judgments": [
                        {
                            "ref": "DC1",
                            "verdict": "rejected",
                            "reason": "The README already says ten.",
                        }
                    ]
                }
            )

            result = self.run_review(directory, engine)

            result["docs_notes"].assert_not_called()
            self.assertIn("Rejected documentation proposal", result["terminal"])
            self.assertIn("Documentation still matches", result["terminal"])

    def test_your_own_pull_request_receives_the_proposals_in_slack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            result = self.run_review(directory, engine, author="reviewer")

            result["docs_notes"].assert_not_called()
            # The code level sends its own message afterwards, so find this one.
            messages = [call.args[1] for call in result["slack"].call_args_list]
            proposals = [
                message for message in messages if "Documentation Updates" in message
            ]
            self.assertEqual(len(proposals), 1)
            self.assertIn("README.md:3", proposals[0])

    def test_your_own_pull_request_outside_slack_receives_the_proposals_on_github(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            result = self.run_review(
                directory, engine, author="reviewer", slack=False
            )

            result["docs_notes"].assert_called_once()
            self.assertIn("README.md:3", result["docs_notes"].call_args.args[2])
            result["slack"].assert_not_called()
            result["approval"].assert_not_called()
            result["lgtm"].assert_called_once()

    def test_a_dry_run_proposes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()

            result = self.run_review(directory, engine, live=False)

            result["docs_notes"].assert_not_called()
            self.assertIn("Would propose 1 documentation update", result["terminal"])

    def test_a_release_promotion_never_checks_documentation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.engine_for()
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            (root / "README.md").write_text(README_TEXT, encoding="utf-8")
            repository = Repository("app", "owner/app", root)
            settings = self.settings(directory, repository)
            pull_request = PullRequest(
                1, "Release", "", "author", "head", "url", head_ref="develop",
                base_ref="main",
            )

            with patch("prwarden.cli.has_current_review", return_value=False), patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ), patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch(
                "prwarden.cli.list_pull_request_checks", return_value=()
            ), patch(
                "prwarden.cli._read_linked_issues", return_value=()
            ), patch(
                "prwarden.cli._read_file_history", return_value=()
            ), patch(
                "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
            ), patch(
                "prwarden.cli.review_workspace"
            ) as workspace, patch(
                "prwarden.cli.submit_docs_notes"
            ) as docs_notes, patch(
                "prwarden.cli.submit_approval", return_value="approval-1"
            ), patch(
                "sys.stdout", new_callable=StringIO
            ) as output:
                workspace.return_value.__enter__.return_value = repository
                workspace.return_value.dependency_links = ()
                workspace.return_value.dependency_notes = ()
                with JsonStateStore(settings.state_file) as store:
                    cli._process_pull_request(
                        settings,
                        repository,
                        pull_request,
                        store,
                        engine,
                        live=True,
                        force=False,
                    )

            docs_notes.assert_not_called()
            self.assertIn("Documentation level skipped", output.getvalue())
            self.assertEqual(self.levels_reviewed(engine), ["code"])


if __name__ == "__main__":
    unittest.main()
