import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src import github
from src.comment_template import (
    DEFAULT_GITHUB_COMMENT_TAGS_FILE,
    DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE,
    load_comment_template,
)
from src.commands import CommandError
from src.config import Settings
from src.engine import build_engine_environment
from src.github import (
    DOCS_COMMENT_MARKER,
    LGTM_COMMENT_MARKER,
    delivered_review_findings_resolved,
    format_github_comment,
    get_pull_request_snapshot,
    hide_earlier_lgtm_comments,
    list_changed_files,
    list_open_prs,
    list_own_advisory_notes,
    list_pull_request_checks,
    list_prior_review_comments,
    submit_approval,
    submit_review,
)
from src.models import Finding, FindingEvidence, PullRequest, Repository


class ObjectNameValidationTests(unittest.TestCase):
    """Every commit from GitHub is handed to git as a revision expression."""

    def test_a_commit_name_passes(self) -> None:
        from src.github import validate_object_name

        name = "0123456789ab" + "0" * 28

        self.assertEqual(validate_object_name(name, "head"), name)

    def test_a_sha256_object_name_passes(self) -> None:
        """A repository can be SHA-256, and refusing it would break that."""

        from src.github import validate_object_name

        self.assertEqual(len(validate_object_name("a" * 64, "head")), 64)

    def test_a_revision_expression_is_refused(self) -> None:
        """`HEAD` and `main@{1}` resolve to something, just not to what was meant."""

        from src.github import validate_object_name

        for value in ("HEAD", "main@{1}", "HEAD~3", "refs/heads/main"):
            with self.assertRaisesRegex(CommandError, "not a Git object name"):
                validate_object_name(value, "head")

    def test_an_option_and_a_path_are_refused(self) -> None:
        from src.github import validate_object_name

        for value in ("--upload-pack=touch /tmp/x", "../../etc/passwd", "a" * 39):
            with self.assertRaisesRegex(CommandError, "not a Git object name"):
                validate_object_name(value, "head")

    def test_an_abbreviated_or_uppercase_name_is_refused(self) -> None:
        """Git accepts both, but nothing here should be producing them."""

        from src.github import validate_object_name

        for value in ("a3c4ff1", ("A" * 40)):
            with self.assertRaisesRegex(CommandError, "not a Git object name"):
                validate_object_name(value, "head")

    def test_the_snapshot_refuses_a_head_that_is_not_an_object_name(self) -> None:
        response = json.dumps(
            {
                "head": {"sha": "HEAD"},
                "state": "open",
                "draft": False,
                "labels": [],
                "changed_files": 1,
                "mergeable": True,
                "base": {"ref": "develop"},
            }
        )
        pull_request = PullRequest(
            7, "Title", "", "author", "b" * 40, "url"
        )
        with patch("src.github.run_cmd", return_value=response):
            with self.assertRaisesRegex(CommandError, "not a Git object name"):
                get_pull_request_snapshot(Repository("app", "owner/app"), pull_request)


class ReviewerCommentMiningTests(unittest.TestCase):
    """Reading delivered findings back out of GitHub, for the evaluation corpus."""

    def test_findings_are_selected_by_the_template_marker(self) -> None:
        """The reviewer posts under a human account, and that person comments too.

        Selecting by author collected that person's own remarks as though the
        reviewer had written them, which would have poisoned any corpus built
        from this history.
        """

        from src.github import REVIEW_COMMENT_MARKER, list_review_comments_by_reviewer

        record = {
            "comment_id": 1,
            "number": 18,
            "path": "app.py",
            "line": 42,
            "anchor": "a" * 40,
            "created": "2026-09-01T10:00:00Z",
            "body": (
                "**[HIGH] Something** detail\n"
                "_Reviewed by Gem @ E, verified by V @ K_\n"
                f"{REVIEW_COMMENT_MARKER}"
            ),
        }
        with patch("src.github.run_cmd") as run_cmd:
            run_cmd.side_effect = [
                json.dumps(record),
                json.dumps(
                    {
                        "head": "b" * 40,
                        "base_ref": "develop",
                        "merged": True,
                        "title": "t",
                    }
                ),
            ]
            found = list_review_comments_by_reviewer(
                Repository("app", "owner/app", Path(".")), "pr-author"
            )

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["line"], 42)
        self.assertEqual(found[0]["anchor"], "a" * 40)
        self.assertEqual(found[0]["title"], "[HIGH] Something")
        self.assertEqual(
            found[0]["attribution"], "Reviewed by Gem @ E, verified by V @ K"
        )
        self.assertEqual(found[0]["head"], "b" * 40)
        # The pull request's own title and description travel apart from the
        # finding's, so a replay can read what the live review read.
        self.assertEqual(found[0]["pr_title"], "t")
        self.assertEqual(found[0]["pr_body"], "")
        refs_query = " ".join(run_cmd.call_args_list[1].args[0])
        self.assertIn("body: .body", refs_query)
        # The marker is what the query filters on, under either name of this tool,
        # so findings delivered before the rename stay in the corpus.
        query = " ".join(run_cmd.call_args_list[0].args[0])
        self.assertIn(
            'select(.body | (contains("<!-- prwarden -->") '
            'or contains("<!-- ai-code-reviewer -->")))',
            query,
        )
        self.assertIn("in_reply_to_id == null", query)


class GitHubFeedbackTests(unittest.TestCase):
    def test_detects_when_all_delivered_review_findings_are_resolved(self):
        repository = Repository("app", "owner/app")
        pull_request = PullRequest(7, "Title", "", "author", "sha", "url")
        responses = [
            json.dumps({"id": 99, "state": "COMMENTED"}),
            json.dumps([[{"id": 123}, {"id": 124}]]),
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [
                                        {
                                            "isResolved": True,
                                            "path": "src/app.py",
                                            "line": 12,
                                            "comments": {
                                                "pageInfo": {
                                                    "hasNextPage": False
                                                },
                                                "nodes": [
                                                    {
                                                        "id": "node-123",
                                                        "databaseId": 123,
                                                        "author": {
                                                            "login": "reviewer"
                                                        },
                                                        "body": "Finding",
                                                    }
                                                ],
                                            },
                                        }
                                    ],
                                }
                            }
                        }
                    }
                }
            ),
        ]
        with patch("src.github.run_cmd", side_effect=responses):
            resolved = delivered_review_findings_resolved(
                repository,
                pull_request,
                "99",
            )

        self.assertTrue(resolved)

    def test_keeps_delivered_review_terminal_while_a_finding_is_unresolved(self):
        repository = Repository("app", "owner/app")
        pull_request = PullRequest(7, "Title", "", "author", "sha", "url")
        responses = [
            json.dumps({"id": 99, "state": "COMMENTED"}),
            json.dumps([[{"id": 123}, {"id": 124}]]),
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [
                                        {
                                            "isResolved": False,
                                            "path": "src/app.py",
                                            "line": 12,
                                            "comments": {
                                                "pageInfo": {
                                                    "hasNextPage": False
                                                },
                                                "nodes": [
                                                    {
                                                        "id": "node-123",
                                                        "databaseId": 123,
                                                        "author": {
                                                            "login": "reviewer"
                                                        },
                                                        "body": "Finding",
                                                    }
                                                ],
                                            },
                                        }
                                    ],
                                }
                            }
                        }
                    }
                }
            ),
        ]
        with patch("src.github.run_cmd", side_effect=responses):
            resolved = delivered_review_findings_resolved(
                repository,
                pull_request,
                "99",
            )

        self.assertFalse(resolved)

    def test_approved_delivery_is_not_treated_as_resolved_findings(self):
        repository = Repository("app", "owner/app")
        pull_request = PullRequest(7, "Title", "", "author", "sha", "url")
        with patch(
            "src.github.run_cmd",
            return_value=json.dumps({"id": 99, "state": "APPROVED"}),
        ) as run:
            resolved = delivered_review_findings_resolved(
                repository,
                pull_request,
                "99",
            )

        self.assertFalse(resolved)
        run.assert_called_once()

    def test_reads_complete_pull_request_snapshot(self):
        repository = Repository("app", "owner/app")
        head = "0123456789ab" + "0" * 28
        pull_request = PullRequest(7, "Title", "", "author", head, "url")
        response = json.dumps(
            {
                "head": {"sha": head},
                "state": "open",
                "draft": False,
                "labels": [{"name": "Frontend"}],
                "changed_files": 2,
                "mergeable": True,
                "base": {"ref": "develop"},
            }
        )
        with patch("src.github.run_cmd", return_value=response):
            snapshot = get_pull_request_snapshot(repository, pull_request)

        self.assertEqual(snapshot.head_sha, head)
        self.assertEqual(snapshot.labels, frozenset({"frontend"}))
        self.assertEqual(snapshot.changed_files, 2)
        self.assertTrue(snapshot.mergeable)
        self.assertEqual(snapshot.base_ref, "develop")

    def test_changed_files_rejects_api_limit_and_partial_response(self):
        repository = Repository("app", "owner/app")
        pull_request = PullRequest(7, "Title", "", "author", "sha", "url")

        with patch("src.github.run_cmd") as run:
            with self.assertRaisesRegex(CommandError, "3,000-file API limit"):
                list_changed_files(repository, pull_request, 3001)
            run.assert_not_called()

        response = json.dumps(
            [[{"filename": "one.py", "patch": "@@ -0,0 +1 @@\n+one"}]]
        )
        with patch("src.github.run_cmd", return_value=response):
            with self.assertRaisesRegex(CommandError, "refusing a partial review"):
                list_changed_files(repository, pull_request, 2)

    def test_list_open_prs_excludes_drafts_and_in_progress_labels(self):
        repository = Repository("app", "owner/app")
        response = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Draft",
                    "body": "",
                    "author": {"login": "alice"},
                    "headRefOid": "d" * 40,
                    "url": "draft-url",
                    "isDraft": True,
                    "labels": [],
                },
                {
                    "number": 2,
                    "title": "Labelled in progress",
                    "body": "",
                    "author": {"login": "bob"},
                    "headRefOid": "1" * 40,
                    "url": "labelled-url",
                    "isDraft": False,
                    "labels": [{"name": "In Progress"}],
                },
                {
                    "number": 3,
                    "title": "Ready",
                    "body": "",
                    "author": {"login": "carol"},
                    "headRefOid": "e" * 40,
                    "url": "ready-url",
                    "isDraft": False,
                    "labels": [{"name": "frontend"}],
                },
            ]
        )
        with patch("src.github.run_cmd", return_value=response) as run:
            pull_requests = list_open_prs(
                repository,
                frozenset({"in progress"}),
            )

        self.assertEqual([pull_request.number for pull_request in pull_requests], [3])
        args = run.call_args.args[0]
        self.assertIn("isDraft", args[args.index("--json") + 1])
        self.assertIn("labels", args[args.index("--json") + 1])
        self.assertTrue(run.call_args.kwargs["retry_transient"])

    def test_submits_only_inline_comments_without_review_body(self):
        repository = Repository("app", "owner/app")
        pull_request = PullRequest(7, "Title", "", "author", "sha", "url")
        finding = Finding(
            "medium",
            "Incorrect value",
            "src/app.py",
            12,
            "This value causes a definite failure.",
            "Use the validated value instead.",
            trigger="The invalid branch receives an empty identifier.",
            impact="The request fails before data is persisted.",
            evidence=(
                FindingEvidence(
                    "src/app.py",
                    12,
                    "save(value)",
                    "This call receives the unvalidated value.",
                ),
            ),
            counterevidence_reason="No targeted test covers an empty identifier.",
        )
        with patch(
            "src.github.run_cmd",
            side_effect=['{"id": 99}', '{"id": 99}'],
        ) as run:
            receipt = submit_review(
                repository,
                pull_request,
                [finding],
                "antigravity",
                "gemini-3.6-flash-high",
                load_comment_template(
                    DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE,
                    DEFAULT_GITHUB_COMMENT_TAGS_FILE,
                ),
            )

        self.assertEqual(receipt, "99")
        pending_payload = json.loads(run.call_args_list[0].kwargs["input_text"])
        self.assertNotIn("retry_transient", run.call_args_list[0].kwargs)
        self.assertNotIn("body", pending_payload)
        self.assertNotIn("event", pending_payload)
        self.assertEqual(len(pending_payload["comments"]), 1)
        self.assertEqual(
            pending_payload["comments"][0]["body"],
            "\n\n".join(
                [
                    "**[MEDIUM] Incorrect value**",
                    "This value causes a definite failure.",
                    "**Trigger:** The invalid branch receives an empty identifier.",
                    "**Impact:** The request fails before data is persisted.",
                    "**Evidence:**\n- `src/app.py:12` — This call receives the "
                    "unvalidated value. (`save(value)`)",
                    "**Counterevidence considered:** No targeted test covers an "
                    "empty identifier.",
                    "**Suggested change:** Use the validated value instead.",
                    "_Reviewed by Gemini 3.6 Flash (High) @ Antigravity_",
                    "<!-- prwarden -->",
                ]
            ),
        )
        self.assertIn(
            "_Reviewed by Gemini 3.6 Flash (High) @ Antigravity_",
            pending_payload["comments"][0]["body"],
        )
        self.assertIn("**Trigger:**", pending_payload["comments"][0]["body"])
        self.assertIn("`src/app.py:12`", pending_payload["comments"][0]["body"])
        self.assertIn(
            "**Counterevidence considered:**",
            pending_payload["comments"][0]["body"],
        )
        self.assertNotIn("\n---\n", pending_payload["comments"][0]["body"])
        submit_payload = json.loads(run.call_args_list[1].kwargs["input_text"])
        self.assertEqual(submit_payload, {"event": "COMMENT"})
        self.assertTrue(
            any(
                argument.endswith("reviews/99/events")
                for argument in run.call_args_list[1].args[0]
            )
        )

    def test_inline_comment_names_the_verification_engine(self):
        repository = Repository("app", "owner/app")
        pull_request = PullRequest(7, "Title", "", "author", "sha", "url")
        finding = Finding(
            "medium",
            "Incorrect value",
            "src/app.py",
            12,
            "This value causes a definite failure.",
            "Use the validated value instead.",
            trigger="The invalid branch receives an empty identifier.",
            impact="The request fails before data is persisted.",
            evidence=(
                FindingEvidence(
                    "src/app.py",
                    12,
                    "save(value)",
                    "This call receives the unvalidated value.",
                ),
            ),
            counterevidence_reason="No targeted test covers an empty identifier.",
        )
        template = load_comment_template(
            DEFAULT_GITHUB_COMMENT_TEMPLATE_FILE,
            DEFAULT_GITHUB_COMMENT_TAGS_FILE,
        )

        with patch(
            "src.github.run_cmd",
            side_effect=['{"id": 99}', '{"id": 99}'],
        ) as run:
            submit_review(
                repository,
                pull_request,
                [finding],
                "antigravity",
                "gemini-3.1-pro-high",
                template,
                "kiro",
                "gpt-5.6-terra",
            )

        body = json.loads(run.call_args_list[0].kwargs["input_text"])["comments"][0][
            "body"
        ]
        self.assertIn(
            "_Reviewed by Gemini 3.1 Pro (High) @ Antigravity, "
            "verified by GPT-5.6 Terra @ Kiro_",
            body,
        )

    def test_verifier_tags_fall_back_to_the_reviewing_engine(self):
        finding = Finding(
            "low",
            "Title",
            "src/app.py",
            1,
            "Problem.",
            "Change.",
            trigger="Trigger.",
            impact="Impact.",
            evidence=(FindingEvidence("src/app.py", 1, "value", "Relevance."),),
            counterevidence_reason="None.",
        )
        template_file, tags_file = self.write_verifier_template()
        template = load_comment_template(template_file, tags_file)

        verified_elsewhere = format_github_comment(
            finding,
            "antigravity",
            "gemini-3.1-pro-high",
            template,
            "kiro",
            "gpt-5.6-terra",
        )
        verified_by_reviewer = format_github_comment(
            finding,
            "antigravity",
            "gemini-3.1-pro-high",
            template,
        )

        self.assertEqual(
            verified_elsewhere,
            "GPT-5.6 Terra @ Kiro | Kiro | GPT-5.6 Terra\n"
            "<!-- prwarden -->",
        )
        self.assertEqual(
            verified_by_reviewer,
            "Gemini 3.1 Pro (High) @ Antigravity | Antigravity | "
            "Gemini 3.1 Pro (High)\n<!-- prwarden -->",
        )

    def write_verifier_template(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        template_file = root / "comment.md"
        tags_file = root / "tags.json"
        template_file.write_text(
            "[verifier] | [verify-engine] | [verify-model]\n[marker]",
            encoding="utf-8",
        )
        tags_file.write_text(
            json.dumps(
                {
                    "verifier": "verifier_identity",
                    "verify-engine": "verify_engine",
                    "verify-model": "verify_model",
                    "marker": "marker",
                }
            ),
            encoding="utf-8",
        )
        return template_file, tags_file

    def test_submits_bodyless_approval_for_exact_head(self):
        repository = Repository("app", "owner/app")
        pull_request = PullRequest(7, "Title", "", "author", "sha", "url")
        with patch("src.github.run_cmd", return_value='{"id": 99}') as run:
            receipt = submit_approval(repository, pull_request)

        self.assertEqual(receipt, "99")
        payload = json.loads(run.call_args.kwargs["input_text"])
        self.assertNotIn("retry_transient", run.call_args.kwargs)
        self.assertEqual(
            payload,
            {
                "commit_id": "sha",
                "event": "APPROVE",
            },
        )

    def test_collects_other_reviewers_feedback_and_locations(self):
        repository = Repository("app", "owner/app")
        pull_request = PullRequest(7, "Title", "", "author", "sha", "url")
        responses = [
            json.dumps(
                [
                    [
                        {"id": 1, "user": {"login": "alice"}, "body": "General defect"},
                        {"id": 2, "user": {"login": "reviewer"}, "body": "Own review"},
                    ]
                ]
            ),
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [
                                        {
                                            "isResolved": False,
                                            "path": "src/app.py",
                                            "line": 12,
                                            "comments": {
                                                "pageInfo": {"hasNextPage": False},
                                                "nodes": [
                                                    {
                                                        "id": "node-3",
                                                        "databaseId": 3,
                                                        "author": {"login": "bob"},
                                                        "body": "Inline defect",
                                                    }
                                                ],
                                            },
                                        },
                                        {
                                            "isResolved": True,
                                            "path": "src/fixed.py",
                                            "line": 4,
                                            "comments": {
                                                "pageInfo": {"hasNextPage": False},
                                                "nodes": [
                                                    {
                                                        "id": "node-6",
                                                        "databaseId": 6,
                                                        "author": {"login": "erin"},
                                                        "body": "Resolved defect",
                                                    }
                                                ],
                                            },
                                        },
                                    ],
                                }
                            }
                        }
                    }
                }
            ),
            json.dumps(
                [
                    [
                        {
                            "id": 4,
                            "user": {"login": "carol"},
                            "body": "Conversation defect",
                        },
                        {
                            "id": 7,
                            "user": {"login": "deploy-bot[bot]"},
                            "body": "Deployment successful",
                        },
                        {"id": 5, "user": {"login": "dave"}, "body": "  "},
                    ]
                ]
            ),
        ]
        with patch("src.github.run_cmd", side_effect=responses) as run:
            comments = list_prior_review_comments(repository, pull_request, "reviewer")

        self.assertEqual(
            [comment.comment_id for comment in comments],
            ["review:1", "inline:3", "inline:6", "conversation:4"],
        )
        self.assertEqual(comments[1].path, "src/app.py")
        self.assertEqual(comments[1].line, 12)
        self.assertFalse(comments[1].resolved)
        self.assertTrue(comments[2].resolved)
        self.assertEqual(run.call_count, 3)
        graphql_args = run.call_args_list[1].args[0]
        self.assertIn("graphql", graphql_args)
        self.assertTrue(run.call_args_list[1].kwargs["retry_transient"])

    def test_lists_only_this_reviewers_notes_for_one_level(self):
        """Author and marker together: the account is also a person's, and anyone
        can paste a marker into a comment."""

        repository = Repository("app", "owner/app")
        pull_request = PullRequest(7, "Title", "", "author", "sha", "url")
        marker = "<!-- prwarden:docs -->"
        records = [
            (1, "Reviewer", f"Note\n{marker}"),
            (2, "reviewer", "By hand"),
            (3, "mallory", f"Fake\n{marker}"),
            (4, "reviewer", "Design\n<!-- prwarden:design -->"),
        ]
        response = json.dumps(
            [
                [
                    {"id": number, "user": {"login": login}, "body": body}
                    for number, login, body in records
                ]
            ]
        )
        with patch("src.github.run_cmd", return_value=response) as run:
            notes = list_own_advisory_notes(
                repository, pull_request, "reviewer", marker
            )

        self.assertEqual([note.comment_id for note in notes], ["conversation:1"])
        self.assertEqual(notes[0].body, f"Note\n{marker}")
        self.assertIn(
            "repos/owner/app/issues/7/comments?per_page=100",
            run.call_args.args[0],
        )

    def test_a_note_posted_under_the_old_name_is_still_this_levels(self):
        records = [
            (1, "reviewer", "Old note\n<!-- ai-code-reviewer:docs -->"),
            (2, "reviewer", "Old design\n<!-- ai-code-reviewer:design -->"),
            (3, "reviewer", "Old finding\n<!-- ai-code-reviewer -->"),
        ]
        response = json.dumps(
            [
                [
                    {"id": number, "user": {"login": login}, "body": body}
                    for number, login, body in records
                ]
            ]
        )
        with patch("src.github.run_cmd", return_value=response):
            notes = list_own_advisory_notes(
                Repository("app", "owner/app"),
                PullRequest(7, "Title", "", "author", "sha", "url"),
                "reviewer",
                DOCS_COMMENT_MARKER,
            )

        self.assertEqual([note.comment_id for note in notes], ["conversation:1"])

    def test_a_note_without_an_id_is_an_error_rather_than_skipped(self):
        marker = "<!-- prwarden:docs -->"
        response = json.dumps(
            [[{"user": {"login": "reviewer"}, "body": f"Note\n{marker}"}]]
        )
        with patch("src.github.run_cmd", return_value=response):
            with self.assertRaisesRegex(CommandError, "without an ID"):
                list_own_advisory_notes(
                    Repository("app", "owner/app"),
                    PullRequest(7, "Title", "", "author", "sha", "url"),
                    "reviewer",
                    marker,
                )

    def lgtm_page(self, nodes, cursor=None):
        return json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "comments": {
                                "pageInfo": {
                                    "hasNextPage": cursor is not None,
                                    "endCursor": cursor,
                                },
                                "nodes": nodes,
                            }
                        }
                    }
                }
            }
        )

    def lgtm_node(self, number, login="reviewer", body=None, minimized=False):
        if body is None:
            body = f"## LGTM\n\nAt `head{number}`.\n{LGTM_COMMENT_MARKER}\n"
        return {
            "id": f"IC_{number}",
            "databaseId": number,
            "body": body,
            "isMinimized": minimized,
            "author": {"login": login},
        }

    def hide(self, pages, keep="9", confirmed=True):
        hidden_ids = []

        def run(args, **kwargs):
            query = next(arg for arg in args if arg.startswith("query="))
            if "minimizeComment" in query:
                self.assertIn("classifier: OUTDATED", query)
                # A write: never retried blindly.
                self.assertNotEqual(kwargs.get("retry_transient"), True)
                hidden_ids.append(next(a for a in args if a.startswith("id=")))
                state = {"isMinimized": confirmed}
                return json.dumps(
                    {"data": {"minimizeComment": {"minimizedComment": state}}}
                )
            self.assertTrue(kwargs.get("retry_transient"))
            return pages.pop(0)

        with patch("src.github.run_cmd", side_effect=run):
            count = hide_earlier_lgtm_comments(
                Repository("app", "owner/app"),
                PullRequest(7, "Title", "", "reviewer", "head9", "url"),
                "reviewer",
                keep,
            )
        return count, hidden_ids

    def test_hides_this_reviewers_earlier_lgtm_comments_across_pages(self):
        pages = [
            self.lgtm_page(
                [
                    self.lgtm_node(1, login="Reviewer"),
                    self.lgtm_node(2, login="mallory"),
                    self.lgtm_node(3, body="LGTM, typed by hand"),
                    self.lgtm_node(4, minimized=True),
                ],
                cursor="next",
            ),
            self.lgtm_page([self.lgtm_node(5), self.lgtm_node(9)]),
        ]

        count, hidden = self.hide(pages)

        # Not a stranger's, not one typed by hand, not one already hidden, and
        # never the comment that was just posted.
        self.assertEqual(hidden, ["id=IC_1", "id=IC_5"])
        self.assertEqual(count, 2)

    def test_findings_keep_no_lgtm_comment(self):
        count, hidden = self.hide(
            [self.lgtm_page([self.lgtm_node(1), self.lgtm_node(9)])], keep=None
        )

        self.assertEqual(hidden, ["id=IC_1", "id=IC_9"])

    def test_an_lgtm_posted_under_the_old_name_is_still_hidden(self):
        """Comments from before the rename carry the old marker, not none."""

        old = self.lgtm_node(
            1, body="## LGTM\n\nAt `head1`.\n<!-- ai-code-reviewer:lgtm -->\n"
        )
        other_level = self.lgtm_node(2, body="Design\n<!-- ai-code-reviewer:design -->")

        count, hidden = self.hide([self.lgtm_page([old, other_level])])

        self.assertEqual(hidden, ["id=IC_1"])
        self.assertEqual(count, 1)

    def test_an_unconfirmed_hide_is_an_error(self):
        with self.assertRaisesRegex(CommandError, "did not confirm"):
            self.hide([self.lgtm_page([self.lgtm_node(1)])], confirmed=False)

    def test_lists_exact_head_check_runs(self):
        repository = Repository("app", "owner/app")
        pull_request = PullRequest(7, "Title", "", "author", "sha", "url")
        response = json.dumps(
            [
                {
                    "check_runs": [
                        {
                            "name": "Build and typecheck",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                }
            ]
        )
        with patch("src.github.run_cmd", return_value=response) as run:
            checks = list_pull_request_checks(repository, pull_request)

        self.assertEqual(checks[0].name, "Build and typecheck")
        self.assertEqual(checks[0].conclusion, "success")
        self.assertIn("commits/sha/check-runs", " ".join(run.call_args.args[0]))
        self.assertTrue(run.call_args.kwargs["retry_transient"])


class GitHubAccountPinningTests(unittest.TestCase):
    """A run must read and post as the reviewer, whichever account is active."""

    SHA = "a" * 40

    def setUp(self) -> None:
        self.addCleanup(github._pinned_account.clear)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in github.GITHUB_TOKEN_VARIABLES:
            os.environ.pop(name, None)

    def branch_tip(self, run: "Mock") -> None:
        run.return_value = json.dumps({"object": {"sha": self.SHA}})
        github.get_branch_head_sha(Repository("app", "owner/app", Path(".")), "main")

    def test_every_call_carries_the_reviewers_own_token(self) -> None:
        with patch("src.github.commands.run_cmd", return_value="token-1") as run:
            github.pin_github_account("reviewer")
            run.assert_called_once_with(
                ["gh", "auth", "token", "--user", "reviewer"], timeout_seconds=30
            )
            self.branch_tip(run)

        options = run.call_args.kwargs
        self.assertEqual(options["environment"], {"GH_TOKEN": "token-1"})
        # The token is added to what the call asked for, never in place of it.
        self.assertTrue(options["retry_transient"])

    def test_an_environment_a_call_names_is_kept(self) -> None:
        with patch("src.github.commands.run_cmd", return_value="token-1") as run:
            github.pin_github_account("reviewer")
            github.run_cmd(["gh", "api", "user"], environment={"GH_HOST": "example"})

        self.assertEqual(
            run.call_args.kwargs["environment"],
            {"GH_HOST": "example", "GH_TOKEN": "token-1"},
        )

    def test_an_account_without_a_login_stops_with_what_to_do(self) -> None:
        refusal = CommandError(
            "gh exited with 1: no oauth token found for github.com account reviewer"
        )
        with patch("src.github.commands.run_cmd", side_effect=refusal):
            with self.assertRaises(CommandError) as raised:
                github.pin_github_account("reviewer")

        message = str(raised.exception)
        self.assertIn("no login for reviewer", message)
        self.assertIn("gh auth login", message)
        self.assertIn("no oauth token found", message)
        self.assertEqual(raised.exception.command, "gh")

    def test_an_empty_token_is_refused_rather_than_pinned(self) -> None:
        with patch("src.github.commands.run_cmd", return_value=""):
            with self.assertRaises(CommandError):
                github.pin_github_account("reviewer")

        self.assertEqual(github._pinned_account, {})

    def test_a_token_the_operator_set_is_left_to_decide(self) -> None:
        os.environ["GH_TOKEN"] = "operator-token"
        with patch("src.github.commands.run_cmd") as run:
            github.pin_github_account("reviewer")
            self.branch_tip(run)

        self.assertEqual(run.call_count, 1)
        self.assertNotIn("environment", run.call_args.kwargs)

    def test_the_review_engines_never_receive_the_token(self) -> None:
        """An engine reads untrusted content, so it must not hold a write token,
        even when an allowlist names the variable the token travels in."""

        with patch("src.github.commands.run_cmd", return_value="token-1"):
            github.pin_github_account("reviewer")
        settings = Settings(
            reviewer_username="reviewer",
            repositories=[],
            dry_run=True,
            slack_webhook_url=None,
            state_file=Path("state.json"),
            review_engine="kiro",
            review_engine_command=("kiro-cli",),
            review_engine_env_allowlist=github.GITHUB_TOKEN_VARIABLES,
            review_model=None,
            review_timeout_seconds=300,
            max_diff_bytes=100000,
            max_findings=10,
        )

        environment = build_engine_environment(settings)

        self.assertNotIn("token-1", environment.values())
        for name in github.GITHUB_TOKEN_VARIABLES:
            self.assertNotIn(name, environment)


if __name__ == "__main__":
    unittest.main()
