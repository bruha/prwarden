import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from src.engine import REJECTION_CLASSES, RESPONSE_FIELD_LIMITS
from src.models import (
    DependencyLink,
    Finding,
    FindingEvidence,
    PriorReviewComment,
    PullRequestCheck,
)
import src.review as review
from src.review import (
    OMITTED_PATCH_REASON,
    DiffTooLargeError,
    ReviewError,
    build_bundled_review_material,
    build_review_material,
    collect_rejection_reasons,
    dominant_directories,
    format_byte_size,
    validate_approval_verdict,
    validate_approval_reason,
    UNCLASSIFIED_REJECTION,
    CandidateRejection,
    validate_blocking_comment_ids,
    validate_code_judgments,
    validate_findings,
)


PATCH = """@@ -1,3 +1,4 @@
 context
-old value
+new value
+another value
 context two
"""


def evidence_fields(path: str, line: int, excerpt: str) -> dict[str, object]:
    return {
        "trigger": "Process the changed value.",
        "impact": "The operation returns an incorrect result.",
        "evidence": [
            {
                "path": path,
                "line": line,
                "excerpt": excerpt,
                "relevance": "This is the changed behavior.",
            }
        ],
        "counterevidence": [],
        "counterevidence_reason": "No direct counterevidence was found.",
    }


class CitationMismatchFeedbackTests(unittest.TestCase):
    """A rejection the model cannot act on costs a whole review: on [pr:djf5] the
    retry was told only that the cited line was blank, and failed the same way."""

    def _validate(self, directory, contents, line):
        root = Path(directory)
        (root / "store.test.ts").write_text(contents, encoding="utf-8")
        item = {
            "path": "store.test.ts",
            "line": line,
            "excerpt": "expect(open).toHaveBeenCalled()",
            "relevance": "the assertion already covers this",
        }
        return review._required_evidence(
            {"counterevidence": [item]},
            "counterevidence",
            root,
        )

    def test_an_ambiguous_excerpt_names_every_line_it_is_on(self):
        contents = (
            "test('a', () => {\n"
            "  expect(open).toHaveBeenCalled()\n"
            "})\n"
            "\n"
            "test('b', () => {\n"
            "  expect(open).toHaveBeenCalled()\n"
            "})\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ReviewError) as caught:
                self._validate(directory, contents, 4)

        message = str(caught.exception)
        self.assertIn("that line is ''", message)
        self.assertIn("that text is on lines 2, 6", message)
        self.assertIn("cite the one this finding means", message)

    def test_an_excerpt_on_no_line_says_so(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ReviewError) as caught:
                self._validate(directory, "const a = 1\n", 1)

        message = str(caught.exception)
        self.assertIn("that text is on no line of this file", message)

    def test_a_unique_excerpt_still_corrects_the_line_silently(self):
        contents = "test('a', () => {\n  expect(open).toHaveBeenCalled()\n})\n"

        with tempfile.TemporaryDirectory() as directory:
            evidence = self._validate(directory, contents, 1)

        self.assertEqual(evidence[0].line, 2)

    def test_an_absolute_path_inside_the_workspace_is_rewritten(self):
        """It names the file a relative path would, and the relative form is what an
        inline comment needs, so refusing it only cost a retry."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "store.test.ts").write_text(
                "test('a', () => {\n  expect(open).toHaveBeenCalled()\n})\n",
                encoding="utf-8",
            )
            item = {
                "path": str(root / "store.test.ts"),
                "line": 2,
                "excerpt": "expect(open).toHaveBeenCalled()",
                "relevance": "the assertion already covers this",
            }

            evidence = review._required_evidence(
                {"counterevidence": [item]}, "counterevidence", root
            )

        self.assertEqual(evidence[0].path, "store.test.ts")

    def test_an_absolute_path_outside_the_workspace_still_fails(self):
        """That one is not a formatting slip, and rewriting it would invent evidence
        from a file this review never read."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            (root / "src").mkdir(parents=True)
            outside = Path(directory) / "elsewhere"
            outside.mkdir()
            (outside / "secret.py").write_text("token = 1\n", encoding="utf-8")
            item = {
                "path": str(outside / "secret.py"),
                "line": 1,
                "excerpt": "token = 1",
                "relevance": "claims to show the problem",
            }

            with self.assertRaises(ReviewError) as caught:
                review._required_evidence(
                    {"counterevidence": [item]}, "counterevidence", root
                )

        message = str(caught.exception)
        self.assertIn("must be workspace-relative", message)
        self.assertIn("like src/module.py", message)

    def test_a_long_line_is_quoted_whole(self):
        """The cut used to land inside the part that differed."""

        excerpt = "const value = " + "'x' + " * 40 + "'end'"

        quoted = review._quote_line(excerpt)

        self.assertIn("'end'", quoted)
        self.assertNotIn("...", quoted)


class ReviewValidationTests(unittest.TestCase):
    def test_approval_verdict_requires_clean_findings(self):
        self.assertTrue(
            validate_approval_verdict(
                {"verdict": "approve", "findings": []},
                [],
            )
        )
        with self.assertRaisesRegex(ReviewError, "cannot approve"):
            validate_approval_verdict(
                {"verdict": "approve", "findings": [{}]},
                [object()],
            )
        self.assertFalse(
            validate_approval_verdict(
                {"verdict": "do_not_approve", "findings": []},
                [],
            )
        )

    def test_approval_reason_is_required_and_bounded(self):
        self.assertEqual(
            validate_approval_reason(
                {"approval_reason": "  Existing defect remains.  "}
            ),
            "Existing defect remains.",
        )
        with self.assertRaisesRegex(ReviewError, "non-empty approval_reason"):
            validate_approval_reason({"approval_reason": " "})
        with self.assertRaisesRegex(ReviewError, "exceeds 800"):
            validate_approval_reason({"approval_reason": "x" * 801})

    def test_blocking_reference_is_matched_regardless_of_case(self):
        """Case is the one variation worth accepting in a two-character token."""

        unresolved = PriorReviewComment(
            "1234567890", "inline", "bot", "Defect", "app.py", 1, False
        )

        self.assertEqual(
            validate_blocking_comment_ids(
                {"blocking_comment_ids": [" c1 "]},
                [],
                False,
                (unresolved,),
            ),
            ("1234567890",),
        )

    def test_blocking_reference_with_a_dropped_digit_is_named_in_the_error(self):
        """One review [pr:47dz] cited a reference with its last digit dropped, twice."""

        unresolved = PriorReviewComment(
            "1234567890", "inline", "bot", "Defect", "app.py", 1, False
        )

        with self.assertRaisesRegex(ReviewError, "supplied references: C1"):
            validate_blocking_comment_ids(
                {"blocking_comment_ids": ["123456789"]},
                [],
                False,
                (unresolved,),
            )

    def test_withheld_approval_requires_current_unresolved_comment(self):
        unresolved = PriorReviewComment(
            "inline:1", "inline", "reviewer", "Defect", "app.py", 1, False
        )
        resolved = PriorReviewComment(
            "inline:2", "inline", "reviewer", "Fixed", "app.py", 2, True
        )
        # The engine cites the reference it was given; the controller returns the
        # GitHub ID that reference stands for.
        self.assertEqual(
            validate_blocking_comment_ids(
                {"blocking_comment_ids": ["C1"]},
                [],
                False,
                (unresolved, resolved),
            ),
            ("inline:1",),
        )
        with self.assertRaisesRegex(ReviewError, "already resolved: C2"):
            validate_blocking_comment_ids(
                {"blocking_comment_ids": ["C2"]},
                [],
                False,
                (unresolved, resolved),
            )
        # A real GitHub ID is no longer a valid citation, and the rejection names
        # what was supplied so the correction retry can succeed.
        with self.assertRaisesRegex(ReviewError, "supplied references: C1, C2"):
            validate_blocking_comment_ids(
                {"blocking_comment_ids": ["inline:1"]},
                [],
                False,
                (unresolved, resolved),
            )
        with self.assertRaisesRegex(ReviewError, "requires at least one"):
            validate_blocking_comment_ids(
                {"blocking_comment_ids": []},
                [],
                False,
                (unresolved,),
            )

    def bundle_files(self, count: int, padding: int = 400) -> list:
        """Return changed files whose patches are each about ``padding`` bytes."""

        return [
            {
                "filename": f"src/file{index:02d}.py",
                "patch": "@@ -1,3 +1,4 @@\n context\n+" + "x" * padding,
            }
            for index in range(count)
        ]

    def test_a_change_that_fits_produces_one_bundle(self):
        """The ordinary path must be untouched: one review, exactly as before."""

        bundles, unread = build_bundled_review_material(
            self.bundle_files(3), 100000, max_bundles=4
        )

        self.assertEqual(len(bundles), 1)
        self.assertEqual(unread, ())
        self.assertEqual(bundles[0].diff_budget.deferred, ())

    def third_of(self, files: list) -> int:
        """Return a budget about a third of the whole change, measured not guessed."""

        whole = build_review_material(files, 10_000_000)
        return whole.diff_budget.total_bytes // 3

    def test_a_change_too_large_is_read_across_bundles(self):
        files = self.bundle_files(8)

        bundles, unread = build_bundled_review_material(
            files, self.third_of(files), max_bundles=8
        )

        self.assertGreater(len(bundles), 1)
        self.assertEqual(unread, ())
        read = [path for bundle in bundles for path in bundle.added_lines]
        self.assertEqual(sorted(read), sorted(f["filename"] for f in files))
        # No file is read twice, so no finding can be raised against it twice.
        self.assertEqual(len(read), len(set(read)))

    def test_no_bundle_exceeds_the_budget(self):
        files = self.bundle_files(8)
        budget = self.third_of(files)

        bundles, _ = build_bundled_review_material(files, budget, max_bundles=8)

        for bundle in bundles:
            self.assertLessEqual(bundle.diff_budget.reviewed_bytes, budget)

    def test_the_cap_leaves_the_rest_deferred_as_before(self):
        """Whatever the cap leaves over must degrade to today's outcome."""

        files = self.bundle_files(8)

        bundles, unread = build_bundled_review_material(
            files, self.third_of(files), max_bundles=2
        )

        self.assertEqual(len(bundles), 2)
        self.assertTrue(unread)
        read = {path for bundle in bundles for path in bundle.added_lines}
        self.assertTrue({file.path for file in unread}.isdisjoint(read))

    def test_one_bundle_is_the_single_review_this_tool_always_did(self):
        files = self.bundle_files(8)
        budget = self.third_of(files)

        bundles, unread = build_bundled_review_material(
            files, budget, max_bundles=1
        )
        single = build_review_material(files, budget)

        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0].diff, single.diff)
        self.assertEqual(unread, single.diff_budget.oversized)

    def test_a_file_too_large_alone_is_never_split(self):
        """A reviewer that saw half a function would cite lines nobody can
        reproduce."""

        files = self.bundle_files(2) + [
            {
                "filename": "src/huge.py",
                "patch": "@@ -1,3 +1,4 @@\n context\n+" + "y" * 5000,
            }
        ]

        bundles, unread = build_bundled_review_material(
            files, 1200, max_bundles=4
        )

        self.assertEqual([file.path for file in unread], ["src/huge.py"])
        for bundle in bundles:
            self.assertNotIn("src/huge.py", bundle.added_lines)

    def test_prior_comments_reach_every_bundle(self):
        """They describe the whole pull request, not one bundle of it."""

        comment = PriorReviewComment(
            "inline:1", "inline", "human", "please fix", "src/file00.py", 2, False
        )
        files = self.bundle_files(6)
        budget = self.third_of(files)

        bundles, _ = build_bundled_review_material(
            files, budget, max_bundles=8, prior_comments=(comment,)
        )

        self.assertGreater(len(bundles), 1)
        for bundle in bundles:
            self.assertEqual(bundle.prior_comments, (comment,))

    def test_builds_added_line_map(self):
        material = build_review_material(
            [{"filename": "src/example.py", "patch": PATCH}],
            10000,
        )
        self.assertEqual(material.added_lines["src/example.py"], frozenset({2, 3}))

    def test_accepts_finding_on_added_line(self):
        material = build_review_material(
            [{"filename": "src/example.py", "patch": PATCH}],
            10000,
        )
        findings = validate_findings(
            {
                "findings": [
                    {
                        "severity": "medium",
                        "title": "Incorrect value",
                        "path": "src/example.py",
                        "line": 2,
                        "problem": "This value causes a definite failure.",
                        "suggestion": "Use the validated value instead.",
                        **evidence_fields("src/example.py", 2, "new value"),
                    }
                ]
            },
            material,
            10,
        )
        self.assertEqual(findings[0].line, 2)

    def test_verifies_evidence_excerpt_against_reviewed_workspace(self):
        material = build_review_material(
            [{"filename": "src/example.py", "patch": PATCH}],
            10000,
        )
        payload = {
            "findings": [
                {
                    "severity": "medium",
                    "title": "Incorrect value",
                    "path": "src/example.py",
                    "line": 2,
                    "problem": "This value causes a definite failure.",
                    "suggestion": "Use the validated value instead.",
                    **evidence_fields("package.json", 2, '"library": "4.0.0"'),
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text(
                '{\n  "library": "4.0.0"\n}\n',
                encoding="utf-8",
            )
            findings = validate_findings(payload, material, 10, root)
            self.assertEqual(findings[0].evidence[0].path, "package.json")

            payload["findings"][0]["evidence"][0]["excerpt"] = '"library": "3.0.0"'
            with self.assertRaises(ReviewError) as raised:
                validate_findings(payload, material, 10, root)

        detail = str(raised.exception)
        self.assertIn("excerpt does not match package.json:2", detail)
        self.assertIn("cited '\"library\": \"3.0.0\"'", detail)
        self.assertIn("that line is '\"library\": \"4.0.0\"'", detail)

    def test_repairs_unique_evidence_line_from_exact_excerpt(self):
        material = build_review_material(
            [{"filename": "src/example.py", "patch": PATCH}],
            10000,
        )
        payload = {
            "findings": [
                {
                    "severity": "medium",
                    "title": "Incorrect value",
                    "path": "src/example.py",
                    "line": 2,
                    "problem": "This value causes a definite failure.",
                    "suggestion": "Use the validated value instead.",
                    **evidence_fields(
                        "package.json",
                        5,
                        '"library": "4.0.0"',
                    ),
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package.json").write_text(
                '{\n  "library": "4.0.0"\n}\n',
                encoding="utf-8",
            )

            findings = validate_findings(payload, material, 10, root)

        self.assertEqual(findings[0].evidence[0].line, 2)

    def test_rejects_ambiguous_evidence_line_correction(self):
        material = build_review_material(
            [{"filename": "src/example.py", "patch": PATCH}],
            10000,
        )
        payload = {
            "findings": [
                {
                    "severity": "medium",
                    "title": "Incorrect value",
                    "path": "src/example.py",
                    "line": 2,
                    "problem": "This value causes a definite failure.",
                    "suggestion": "Use the validated value instead.",
                    **evidence_fields("repeated.txt", 2, "repeated value"),
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repeated.txt").write_text(
                "repeated value\nother\nrepeated value\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ReviewError, "excerpt does not match"):
                validate_findings(payload, material, 10, root)

    def test_rejects_multiline_evidence_excerpt(self):
        material = build_review_material(
            [{"filename": "src/example.py", "patch": PATCH}],
            10000,
        )
        payload = {
            "findings": [
                {
                    "severity": "medium",
                    "title": "Incorrect value",
                    "path": "src/example.py",
                    "line": 2,
                    "problem": "This value causes a definite failure.",
                    "suggestion": "Use the validated value instead.",
                    **evidence_fields(
                        "example.txt",
                        1,
                        "first line\nsecond line",
                    ),
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.txt").write_text(
                "first line\nsecond line\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ReviewError, "excerpt must be single-line"):
                validate_findings(payload, material, 10, root)

    def test_repairs_unique_omitted_directory_for_added_line(self):
        material = build_review_material(
            [
                {
                    "filename": "src/widgets/Table/TableRow.tsx",
                    "patch": "@@ -169,0 +170 @@\n+  'max-w-80',",
                }
            ],
            10000,
        )
        findings = validate_findings(
            {
                "findings": [
                    {
                        "severity": "medium",
                        "title": "Incorrect width",
                        "path": "src/widgets/TableRow.tsx",
                        "line": 170,
                        "problem": "Problem.",
                        "suggestion": "Suggestion.",
                        **evidence_fields(
                            "src/widgets/Table/TableRow.tsx",
                            170,
                            "max-w-80",
                        ),
                    }
                ]
            },
            material,
            10,
        )

        self.assertEqual(
            findings[0].path,
            "src/widgets/Table/TableRow.tsx",
        )

    def test_rejects_ambiguous_filename_correction_and_includes_title(self):
        material = build_review_material(
            [
                {"filename": "src/one/Table.tsx", "patch": PATCH},
                {"filename": "src/two/Table.tsx", "patch": PATCH},
            ],
            10000,
        )
        with self.assertRaisesRegex(
            ReviewError,
            "Finding 'Ambiguous path' location is not an added diff line",
        ):
            validate_findings(
                {
                    "findings": [
                        {
                            "severity": "medium",
                            "title": "Ambiguous path",
                            "path": "Table.tsx",
                            "line": 2,
                            "problem": "Problem.",
                            "suggestion": "Suggestion.",
                            **evidence_fields("src/one/Table.tsx", 2, "new value"),
                        }
                    ]
                },
                material,
                10,
            )

    def test_rejects_context_line(self):
        material = build_review_material(
            [{"filename": "src/example.py", "patch": PATCH}],
            10000,
        )
        with self.assertRaisesRegex(ReviewError, "not an added diff line"):
            validate_findings(
                {
                    "findings": [
                        {
                            "severity": "medium",
                            "title": "Wrong location",
                            "path": "src/example.py",
                            "line": 1,
                            "problem": "Problem.",
                            "suggestion": "Suggestion.",
                            **evidence_fields("src/example.py", 2, "new value"),
                        }
                    ]
                },
                material,
                10,
            )

    def budgeted_files(self):
        """Return two changed files, the larger worth 542 bytes of diff."""

        return [
            {
                "filename": "app.py",
                "patch": "@@ -0,0 +1,40 @@\n"
                + "\n".join("+value = {0}".format(number) for number in range(40)),
            },
            {"filename": "other.py", "patch": "@@ -0,0 +1 @@\n+other = 1"},
        ]

    def test_a_change_within_the_budget_is_measured_and_fully_reviewed(self):
        material = build_review_material(self.budgeted_files(), 100000)

        budget = material.diff_budget
        self.assertFalse(budget.is_partial)
        self.assertEqual(budget.total_bytes, budget.reviewed_bytes)
        self.assertEqual(budget.total_files, 2)
        self.assertEqual(budget.deferred, ())

    def test_an_oversized_change_reviews_what_fits_and_names_the_rest(self):
        """Reviewing part of a large change beats reviewing none of it.

        The budget is filled largest change first, because the most changed lines
        carry the most risk and the tail of a large pull request is one-line edits.
        """

        material = build_review_material(self.budgeted_files(), 600)

        budget = material.diff_budget
        self.assertTrue(budget.is_partial)
        self.assertEqual(budget.total_bytes, 630)
        self.assertEqual(budget.reviewed_bytes, 542)
        self.assertEqual(budget.reviewed_files, 1)
        self.assertEqual([file.path for file in budget.deferred], ["other.py"])

        # The deferred file is not in the diff and cannot carry a finding: its
        # added lines are unknown, so any claim about it fails validation.
        self.assertIn("app.py", material.diff)
        self.assertNotIn("other.py", material.diff)
        self.assertEqual(sorted(material.added_lines), ["app.py"])

    def test_a_change_where_no_file_fits_is_still_oversized(self):
        """One file bigger than the whole budget leaves nothing to review."""

        with self.assertRaises(DiffTooLargeError) as raised:
            build_review_material(self.budgeted_files(), 50)

        message = str(raised.exception)
        self.assertIn("MAX_DIFF_BYTES (50)", message)
        # The measurement is reported, which the old mid-loop check could not do.
        self.assertIn("complete diff is 630 bytes", message)
        self.assertIn("smallest changed file alone is 88 bytes", message)

    def test_deferred_bytes_are_ranked_by_directory(self):
        material = build_review_material(
            [
                {
                    "filename": "src/features/example/page.tsx",
                    "patch": "@@ -0,0 +1,40 @@\n"
                    + "\n".join("+a = {0}".format(n) for n in range(40)),
                },
                {
                    "filename": "src/i18n/locales/en.ts",
                    "patch": "@@ -0,0 +1,4 @@\n+a\n+b\n+c\n+d",
                },
                {"filename": "src/i18n/locales/de.ts", "patch": "@@ -0,0 +1 @@\n+a"},
            ],
            560,
        )

        self.assertEqual(
            dominant_directories(material.diff_budget.deferred),
            (("src/i18n/locales", 283),),
        )

    def test_byte_sizes_read_as_sizes(self):
        self.assertEqual(format_byte_size(512), "512 B")
        self.assertEqual(format_byte_size(271218), "265 KB")
        self.assertEqual(format_byte_size(3 * 1024 * 1024), "3.0 MB")

    def test_rejects_a_pull_request_of_nothing_but_unreadable_files(self):
        """One unreadable file is deferred; a whole change of them is nothing."""

        with self.assertRaisesRegex(ReviewError, "no reviewable changed files"):
            build_review_material([{"filename": "image.png"}], 10000)
        with self.assertRaisesRegex(ReviewError, "MAX_DIFF_BYTES"):
            build_review_material(
                [{"filename": "src/example.py", "patch": PATCH}],
                20,
            )

    def test_skips_configured_missing_patch_and_keeps_reviewable_files(self):
        material = build_review_material(
            [
                {"filename": "cache/tsconfig.tsbuildinfo"},
                {"filename": "src/example.py", "patch": PATCH},
            ],
            10000,
            ignored_missing_patch_patterns=("*.tsbuildinfo",),
        )

        self.assertEqual(
            material.ignored_missing_patch_files,
            ("cache/tsconfig.tsbuildinfo",),
        )
        self.assertIn("src/example.py", material.added_lines)
        self.assertNotIn("tsconfig.tsbuildinfo", material.diff)

    def test_rejects_pull_request_when_every_patch_is_ignored(self):
        with self.assertRaisesRegex(ReviewError, "no reviewable changed files"):
            build_review_material(
                [{"filename": "tsconfig.tsbuildinfo"}],
                10000,
                ignored_missing_patch_patterns=("*.tsbuildinfo",),
            )

    def test_binary_change_is_reviewed_with_the_rest_of_the_pull_request(self):
        """One image used to cost the whole pull request its review."""

        material = build_review_material(
            [
                {
                    "filename": "public/assets/example.png",
                    "status": "added",
                    "additions": 0,
                    "deletions": 0,
                    "changes": 0,
                },
                {"filename": "src/example.py", "patch": PATCH},
            ],
            10000,
        )

        self.assertEqual(
            material.textless_files, ("public/assets/example.png",)
        )
        self.assertIn("src/example.py", material.added_lines)
        self.assertNotIn("example.png", material.diff)
        # Nothing in it can be cited, so no finding may be posted against it.
        self.assertEqual(
            material.added_lines["public/assets/example.png"], frozenset()
        )

    def test_withheld_patch_with_counted_lines_defers_that_file(self):
        """Counted lines mean text exists, so this file was not reviewed.

        Failing the whole pull request reviewed nothing at all and demanded a glob
        for every generated file anybody might commit. The file is deferred
        instead, which withholds the approval and names it, exactly like a file
        the byte budget could not fit.
        """

        material = build_review_material(
            [
                {
                    "filename": "package-lock.json",
                    "status": "modified",
                    "additions": 4000,
                    "deletions": 3000,
                    "changes": 7000,
                },
                {"filename": "src/example.py", "patch": PATCH},
            ],
            10000,
        )
        budget = material.diff_budget

        self.assertIn("src/example.py", material.added_lines)
        self.assertTrue(budget.is_partial)
        # Nothing here is a budget problem, so no ratio may be reported.
        self.assertFalse(budget.exceeds_limit)
        self.assertEqual(
            [(file.path, file.reason) for file in budget.unreadable],
            [("package-lock.json", OMITTED_PATCH_REASON)],
        )
        self.assertEqual(budget.oversized, ())
        # No finding can be anchored in a file nobody read.
        self.assertEqual(material.added_lines["package-lock.json"], frozenset())

    def test_a_configured_pattern_still_ignores_a_file_outright(self):
        """The glob keeps its own meaning: ignored, and still approvable."""

        material = build_review_material(
            [
                {
                    "filename": "package-lock.json",
                    "status": "modified",
                    "additions": 4000,
                    "deletions": 3000,
                    "changes": 7000,
                },
                {"filename": "src/example.py", "patch": PATCH},
            ],
            10000,
            ignored_missing_patch_patterns=("package-lock.json",),
        )

        self.assertEqual(
            material.ignored_missing_patch_files,
            ("package-lock.json",),
        )
        self.assertFalse(material.diff_budget.is_partial)
        self.assertNotIn("package-lock.json", material.added_lines)

    def test_withheld_patch_missing_a_count_is_never_assumed_readable(self):
        """An unusable count is not proof that there is nothing to read.

        A textless file is reviewed around, because nothing was lost. This one
        cannot be proved textless, so it is deferred and the approval goes with
        it rather than being granted over a file nobody could read.
        """

        material = build_review_material(
            [
                {
                    "filename": "public/assets/logo.png",
                    "status": "added",
                    "additions": 0,
                    "deletions": 0,
                },
                {"filename": "src/example.py", "patch": PATCH},
            ],
            10000,
        )

        self.assertEqual(material.textless_files, ())
        self.assertTrue(material.diff_budget.is_partial)
        self.assertEqual(
            [file.path for file in material.diff_budget.unreadable],
            ["public/assets/logo.png"],
        )

    def test_binary_only_pull_request_stays_unreviewable(self):
        """Approving it would be an unverified clean result."""

        with self.assertRaisesRegex(ReviewError, "no reviewable changed files"):
            build_review_material(
                [
                    {
                        "filename": "public/assets/logo.png",
                        "status": "added",
                        "additions": 0,
                        "deletions": 0,
                        "changes": 0,
                    }
                ],
                10000,
            )

    def test_compacts_deletion_only_file_without_requiring_patch(self):
        material = build_review_material(
            [
                {
                    "filename": "package-lock.json",
                    "status": "removed",
                    "additions": 0,
                    "deletions": 9395,
                }
            ],
            20,
        )

        self.assertEqual(material.diff, "")
        self.assertEqual(material.added_lines["package-lock.json"], frozenset())
        self.assertEqual(material.removed_files[0].path, "package-lock.json")
        self.assertEqual(material.removed_files[0].deletions, 9395)

    def test_deletion_only_patch_does_not_consume_diff_limit(self):
        material = build_review_material(
            [
                {
                    "filename": "removed.txt",
                    "status": "removed",
                    "additions": 0,
                    "deletions": 1000,
                    "patch": "-deleted\n" * 1000,
                }
            ],
            20,
        )

        self.assertEqual(material.diff, "")
        self.assertEqual(len(material.removed_files), 1)

    def test_content_free_rename_is_recorded_instead_of_failing(self):
        material = build_review_material(
            [
                {
                    "filename": "src/shared/Button.tsx",
                    "previous_filename": "src/legacy/Button.tsx",
                    "status": "renamed",
                    "additions": 0,
                    "deletions": 0,
                    "changes": 0,
                },
                {"filename": "src/app.py", "patch": PATCH},
            ],
            10000,
        )

        self.assertEqual(len(material.moved_files), 1)
        moved = material.moved_files[0]
        self.assertEqual(moved.path, "src/shared/Button.tsx")
        self.assertEqual(moved.previous_path, "src/legacy/Button.tsx")
        self.assertNotIn("Button.tsx", material.diff)
        self.assertEqual(
            material.added_lines["src/shared/Button.tsx"],
            frozenset(),
        )
        self.assertEqual(material.ignored_missing_patch_files, ())

    def test_content_free_rename_without_previous_filename_is_accepted(self):
        material = build_review_material(
            [
                {
                    "filename": "moved.txt",
                    "status": "renamed",
                    "additions": 0,
                    "deletions": 0,
                },
                {"filename": "src/app.py", "patch": PATCH},
            ],
            10000,
        )

        self.assertEqual(material.moved_files[0].path, "moved.txt")
        self.assertIsNone(material.moved_files[0].previous_path)

    def test_rename_carrying_content_changes_is_reviewed_normally(self):
        material = build_review_material(
            [
                {
                    "filename": "src/renamed.py",
                    "previous_filename": "src/original.py",
                    "status": "renamed",
                    "additions": 1,
                    "deletions": 0,
                    "patch": PATCH,
                }
            ],
            10000,
        )

        self.assertEqual(material.moved_files, ())
        self.assertIn("src/renamed.py", material.diff)
        self.assertEqual(material.added_lines["src/renamed.py"], frozenset({2, 3}))

    def test_rename_missing_patch_with_reported_changes_is_not_a_move(self):
        """A rename that also changed lines is not a content-free move."""

        with self.assertRaisesRegex(ReviewError, "no reviewable changed files"):
            build_review_material(
                [
                    {
                        "filename": "src/renamed.py",
                        "previous_filename": "src/original.py",
                        "status": "renamed",
                        "additions": 4,
                        "deletions": 2,
                    }
                ],
                10000,
            )

    def test_content_free_rename_does_not_make_a_rename_only_pull_request_reviewable(
        self,
    ):
        with self.assertRaisesRegex(ReviewError, "no reviewable changed files"):
            build_review_material(
                [
                    {
                        "filename": "moved.txt",
                        "previous_filename": "old.txt",
                        "status": "renamed",
                        "additions": 0,
                        "deletions": 0,
                    }
                ],
                10000,
            )


def rejection_candidate(path: str = "app.py", line: int = 4) -> Finding:
    return Finding(
        severity="medium",
        title="Candidate",
        path=path,
        line=line,
        problem="problem",
        suggestion="suggestion",
        trigger="trigger",
        impact="impact",
        evidence=(FindingEvidence(path, line, "value = 1", "relevance"),),
    )


class CodeJudgmentTests(unittest.TestCase):
    """The verifier judges references, so it cannot rewrite what it approves."""

    def candidates(self):
        return (
            rejection_candidate("app.py", 1),
            rejection_candidate("web/ui.tsx", 12),
        )

    def test_confirmed_candidate_is_the_controller_object(self):
        candidates = self.candidates()

        confirmed = validate_code_judgments(
            {
                "judgments": [
                    {"ref": "F1", "verdict": "confirmed", "reason": "Read the caller."},
                    {"ref": "F2", "verdict": "rejected", "reason": "Type is checked."},
                ]
            },
            candidates,
        )

        self.assertEqual(len(confirmed), 1)
        self.assertIs(confirmed[0], candidates[0])
        self.assertEqual(confirmed[0].evidence[0].excerpt, "value = 1")

    def test_severity_is_the_only_correction_allowed(self):
        candidates = (rejection_candidate("app.py", 1),)

        confirmed = validate_code_judgments(
            {
                "judgments": [
                    {
                        "ref": "F1",
                        "verdict": "confirmed",
                        "reason": "The impact is worse than claimed.",
                        "severity": "HIGH",
                    }
                ]
            },
            candidates,
        )

        self.assertEqual(confirmed[0].severity, "high")
        self.assertEqual(confirmed[0].path, candidates[0].path)
        self.assertEqual(confirmed[0].evidence, candidates[0].evidence)

    def test_unsupported_severity_correction_is_rejected(self):
        with self.assertRaises(ReviewError) as raised:
            validate_code_judgments(
                {
                    "judgments": [
                        {
                            "ref": "F1",
                            "verdict": "confirmed",
                            "reason": "r",
                            "severity": "catastrophic",
                        }
                    ]
                },
                (rejection_candidate("app.py", 1),),
            )

        self.assertIn("unsupported severity", str(raised.exception))

    def test_every_candidate_must_be_judged_exactly_once(self):
        candidates = self.candidates()
        cases = {
            "unknown reference": [
                {"ref": "F9", "verdict": "confirmed", "reason": "r"}
            ],
            "left unjudged": [{"ref": "F1", "verdict": "confirmed", "reason": "r"}],
            "judged twice": [
                {"ref": "F1", "verdict": "confirmed", "reason": "r"},
                {"ref": "F1", "verdict": "rejected", "reason": "r"},
                {"ref": "F2", "verdict": "rejected", "reason": "r"},
            ],
            "unknown verdict": [
                {"ref": "F1", "verdict": "maybe", "reason": "r"},
                {"ref": "F2", "verdict": "rejected", "reason": "r"},
            ],
            "missing reason": [
                {"ref": "F1", "verdict": "rejected", "reason": " "},
                {"ref": "F2", "verdict": "rejected", "reason": "r"},
            ],
            "not an array": None,
        }
        for label, judgments in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ReviewError):
                    validate_code_judgments({"judgments": judgments}, candidates)

    def test_reference_case_and_padding_are_tolerated(self):
        confirmed = validate_code_judgments(
            {
                "judgments": [
                    {"ref": " f1 ", "verdict": "confirmed", "reason": "Read it."}
                ]
            },
            (rejection_candidate("app.py", 1),),
        )

        self.assertEqual(len(confirmed), 1)


class RejectionClassTests(unittest.TestCase):
    """The class is measurement: it normalizes, and it never gates a review."""

    def collect(self, rejection_class):
        judgment = {"ref": "F1", "verdict": "rejected", "reason": "Kept."}
        if rejection_class is not None:
            judgment["rejection_class"] = rejection_class
        return collect_rejection_reasons(
            {"judgments": [judgment]}, (rejection_candidate("app.py", 1),)
        )["F1"]

    def test_a_supplied_class_is_normalized_and_kept(self):
        for supplied in (" wrong_anchor ", "WRONG_ANCHOR", "wrong_anchor"):
            with self.subTest(supplied=supplied):
                self.assertEqual(self.collect(supplied).rejection_class, "wrong_anchor")

    def test_every_documented_class_survives_collection(self):
        for name in REJECTION_CLASSES:
            with self.subTest(name=name):
                self.assertEqual(self.collect(name).rejection_class, name)

    def test_an_absent_or_unusable_class_counts_as_unclassified(self):
        for supplied in (None, "", "   ", "invented", 7, ["wrong_anchor"]):
            with self.subTest(supplied=supplied):
                rejection = self.collect(supplied)
                self.assertEqual(rejection.rejection_class, UNCLASSIFIED_REJECTION)
                # The reason still arrives: a bad class loses nothing else.
                self.assertEqual(rejection.reason, "Kept.")

    def test_wrong_anchor_exists_because_it_is_the_measurement(self):
        """Judging by reference gave up silent re-anchoring; this counts the cost."""

        self.assertIn("wrong_anchor", REJECTION_CLASSES)


class RejectionReasonTests(unittest.TestCase):
    def test_reasons_are_keyed_by_candidate_reference(self):
        candidates = (
            rejection_candidate("app.py", 4),
            rejection_candidate("web/ui.tsx", 12),
        )

        reasons = collect_rejection_reasons(
            {
                "judgments": [
                    {
                        "ref": "F1",
                        "verdict": "rejected",
                        "reason": "The guard exists above.",
                    },
                    {"ref": "F2", "verdict": "rejected", "reason": "Type is checked."},
                ]
            },
            candidates,
        )

        self.assertEqual(
            reasons,
            {
                "F1": CandidateRejection(
                    "The guard exists above.", UNCLASSIFIED_REJECTION
                ),
                "F2": CandidateRejection(
                    "Type is checked.", UNCLASSIFIED_REJECTION
                ),
            },
        )

    def test_two_candidates_on_one_line_each_keep_their_reason(self):
        """A review that raises two defects in one expression used to lose one."""

        candidates = (
            rejection_candidate("app.py", 4),
            rejection_candidate("app.py", 4),
        )

        reasons = collect_rejection_reasons(
            {
                "judgments": [
                    {"ref": "F1", "verdict": "rejected", "reason": "Guard exists."},
                    {"ref": "F2", "verdict": "rejected", "reason": "Type is checked."},
                ]
            },
            candidates,
        )

        self.assertEqual(len(reasons), 2)
        self.assertEqual(reasons["F1"].reason, "Guard exists.")
        self.assertEqual(reasons["F2"].reason, "Type is checked.")

    def test_a_reason_is_collapsed_and_bounded(self):
        limit = RESPONSE_FIELD_LIMITS["rejection_reason"]
        candidates = (
            rejection_candidate("app.py", 1),
            rejection_candidate("app.py", 2),
        )

        reasons = collect_rejection_reasons(
            {
                "judgments": [
                    {
                        "ref": "F1",
                        "verdict": "rejected",
                        "reason": "  wrapped\n  reason  ",
                    },
                    {
                        "ref": "F2",
                        "verdict": "rejected",
                        "reason": "x" * (limit + 50),
                    },
                ]
            },
            candidates,
        )

        self.assertEqual(reasons["F1"].reason, "wrapped reason")
        self.assertEqual(len(reasons["F2"].reason), limit)

    def test_unusable_entries_never_fail_a_sound_review(self):
        """These reasons are diagnostic, so a bad one must not lose a review."""

        candidates = (rejection_candidate("app.py", 7),)

        reasons = collect_rejection_reasons(
            {
                "judgments": [
                    "not an object",
                    {"ref": "", "verdict": "rejected", "reason": "Empty reference."},
                    {"ref": "F9", "verdict": "rejected", "reason": "Unknown ref."},
                    {"ref": "F1", "verdict": "rejected", "reason": "   "},
                    {"ref": "F1", "verdict": "rejected"},
                    {"ref": "F1", "verdict": "rejected", "reason": "Kept."},
                ]
            },
            candidates,
        )

        self.assertEqual(
            reasons,
            {"F1": CandidateRejection("Kept.", UNCLASSIFIED_REJECTION)},
        )

    def test_a_missing_or_malformed_record_yields_nothing(self):
        for structured_output in (
            {},
            {"judgments": None},
            {"judgments": {}},
            "not a response",
            None,
        ):
            with self.subTest(structured_output=structured_output):
                self.assertEqual(
                    collect_rejection_reasons(
                        structured_output, (rejection_candidate(),)
                    ),
                    {},
                )

    def test_the_first_reason_for_a_candidate_wins(self):
        reasons = collect_rejection_reasons(
            {
                "judgments": [
                    {"ref": "F1", "verdict": "rejected", "reason": "First."},
                    {"ref": "F1", "verdict": "rejected", "reason": "Second."},
                ]
            },
            (rejection_candidate("app.py", 1),),
        )

        self.assertEqual(
            reasons,
            {"F1": CandidateRejection("First.", UNCLASSIFIED_REJECTION)},
        )

class BuildClaimGateTests(unittest.TestCase):
    """A green exact-head build check disproves a whole-head compile claim."""

    def material(self, checks: tuple[PullRequestCheck, ...]):
        return replace(
            build_review_material(
                [{"filename": "src/example.py", "patch": PATCH}],
                10000,
            ),
            checks=checks,
        )

    def response(self, **overrides) -> dict[str, object]:
        finding: dict[str, object] = {
            "severity": "high",
            "title": "Invalid React export SubmitEvent causes build failure",
            "path": "src/example.py",
            "line": 2,
            "problem": (
                "React does not export SubmitEvent, so tsc fails with TS2305 and "
                "the build breaks."
            ),
            "suggestion": "Import FormEvent instead.",
            **evidence_fields("src/example.py", 2, "new value"),
        }
        finding.update(overrides)
        return {"findings": [finding]}

    def test_rejects_compile_failure_claim_contradicted_by_passing_check(self):
        material = self.material(
            (PullRequestCheck("Build and typecheck", "completed", "success"),)
        )

        with self.assertRaisesRegex(ReviewError, "Build and typecheck"):
            validate_findings(self.response(), material, 10)

    def test_accepts_the_same_claim_when_no_build_check_passed(self):
        for checks in (
            (),
            (PullRequestCheck("Build and typecheck", "completed", "failure"),),
            (PullRequestCheck("Build and typecheck", "in_progress", None),),
            (PullRequestCheck("CodeQL", "completed", "success"),),
            (PullRequestCheck("Lint", "completed", "success"),),
        ):
            with self.subTest(checks=checks):
                findings = validate_findings(
                    self.response(),
                    self.material(checks),
                    10,
                )
                self.assertEqual(len(findings), 1)

    def test_accepts_runtime_findings_while_a_build_check_passed(self):
        material = self.material(
            (PullRequestCheck("Build and typecheck", "completed", "success"),)
        )
        findings = validate_findings(
            self.response(
                title="Unvalidated input reaches the query",
                problem="The changed value is interpolated into SQL without escaping.",
                suggestion="Use a parameterized query.",
            ),
            material,
            10,
        )
        self.assertEqual(len(findings), 1)


class DependencyEvidenceTests(unittest.TestCase):
    """Citations inside a linked dependency directory are verifiable evidence."""

    def test_accepts_evidence_inside_a_linked_dependency_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "worktree"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "example.py").write_text(
                "context\nnew value\nanother value\ncontext two\n"
            )
            installed = Path(directory) / "checkout" / "node_modules"
            typings = installed / "@types" / "react"
            typings.mkdir(parents=True)
            (typings / "index.d.ts").write_text(
                "interface FormEvent<T> {}\ninterface SubmitEvent<T> {}\n"
            )
            (root / "node_modules").symlink_to(installed, target_is_directory=True)
            material = replace(
                build_review_material(
                    [{"filename": "src/example.py", "patch": PATCH}],
                    10000,
                ),
                dependency_links=(
                    DependencyLink(
                        name="node_modules",
                        target=str(installed),
                        lockfiles=("bun.lock",),
                    ),
                ),
            )
            citation = {
                "path": "node_modules/@types/react/index.d.ts",
                "line": 2,
                "excerpt": "interface SubmitEvent<T> {}",
                "relevance": "The installed typings export SubmitEvent.",
            }
            response = {
                "findings": [
                    {
                        "severity": "medium",
                        "title": "Deprecated event type",
                        "path": "src/example.py",
                        "line": 2,
                        "problem": "The handler uses a deprecated event type.",
                        "suggestion": "Use the current event type.",
                        **evidence_fields("src/example.py", 2, "new value"),
                        "counterevidence": [citation],
                    }
                ]
            }

            verified = validate_findings(response, material, 10, root)
            self.assertEqual(
                verified[0].counterevidence[0].path,
                "node_modules/@types/react/index.d.ts",
            )
            self.assertEqual(verified[0].counterevidence[0].line, 2)

    def test_rejects_dependency_evidence_without_a_linked_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "worktree"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "example.py").write_text(
                "context\nnew value\nanother value\ncontext two\n"
            )
            installed = Path(directory) / "checkout" / "node_modules"
            typings = installed / "@types" / "react"
            typings.mkdir(parents=True)
            (typings / "index.d.ts").write_text("interface SubmitEvent<T> {}\n")
            (root / "node_modules").symlink_to(installed, target_is_directory=True)
            material = build_review_material(
                [{"filename": "src/example.py", "patch": PATCH}],
                10000,
            )

            with self.assertRaisesRegex(ReviewError, "escapes the review workspace"):
                validate_findings(
                    {
                        "findings": [
                            {
                                "severity": "medium",
                                "title": "Deprecated event type",
                                "path": "src/example.py",
                                "line": 2,
                                "problem": "The handler uses a deprecated type.",
                                "suggestion": "Use the current event type.",
                                **evidence_fields(
                                    "node_modules/@types/react/index.d.ts",
                                    1,
                                    "interface SubmitEvent<T> {}",
                                ),
                            }
                        ]
                    },
                    material,
                    10,
                    root,
                )

    def test_rejects_absolute_evidence_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "worktree"
            root.mkdir()
            (root / "src").mkdir()
            (root / "src" / "example.py").write_text(
                "context\nnew value\nanother value\ncontext two\n"
            )
            installed = Path(directory) / "checkout" / "node_modules"
            installed.mkdir(parents=True)
            (installed / "index.d.ts").write_text("interface SubmitEvent<T> {}\n")
            material = replace(
                build_review_material(
                    [{"filename": "src/example.py", "patch": PATCH}],
                    10000,
                ),
                dependency_links=(
                    DependencyLink("node_modules", str(installed), ("bun.lock",)),
                ),
            )

            with self.assertRaisesRegex(ReviewError, "must be workspace-relative"):
                validate_findings(
                    {
                        "findings": [
                            {
                                "severity": "medium",
                                "title": "Deprecated event type",
                                "path": "src/example.py",
                                "line": 2,
                                "problem": "The handler uses a deprecated type.",
                                "suggestion": "Use the current event type.",
                                **evidence_fields(
                                    str(installed / "index.d.ts"),
                                    1,
                                    "interface SubmitEvent<T> {}",
                                ),
                            }
                        ]
                    },
                    material,
                    10,
                    root,
                )


if __name__ == "__main__":
    unittest.main()
