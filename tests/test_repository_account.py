"""A repository read and reviewed as its own GitHub account, not the reviewer's."""

import json
import os
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch

import prwarden.cli as cli
import prwarden.github as github
from prwarden.commands import CommandError
from prwarden.config import ConfigError, load_repositories
from prwarden.models import PullRequest, Repository
from prwarden.public_safety import refresh_terms
from prwarden.state import JsonStateStore
from tests.test_slack_scope import APPROVING, READY_SNAPSHOT, settings_for
from tests.test_review_rules import without_review_rules


SHA = "a" * 40
SHARED = Repository("team", "team/app", Path("."))
PERSONAL = Repository("own", "owner/own", Path("."), github_account="owner")


def tokens(args, **_options):
    """Answer `gh auth token --user NAME` with a token naming the account."""

    if list(args[:4]) == ["gh", "auth", "token", "--user"]:
        return f"token-{args[4].casefold()}"
    return json.dumps({"object": {"sha": SHA}})


class RepositoryAccountConfigurationTests(unittest.TestCase):
    def load(self, entry: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repositories.json"
            path.write_text(f"[{entry}]", encoding="utf-8")
            return load_repositories(path)

    def test_without_one_a_repository_is_read_as_the_reviewer(self):
        (repository,) = self.load('{"slug": "owner/app"}')
        self.assertIsNone(repository.github_account)

    def test_a_repository_can_name_the_account_it_is_read_as(self):
        (repository,) = self.load('{"slug": "owner/app", "github_account": " owner "}')
        self.assertEqual(repository.github_account, "owner")

    def test_the_account_must_be_a_github_login(self):
        """A login lands in a `gh auth token --user` argument, so anything else is
        refused before it can reach one."""

        for value in ("42", '""', '"-owner"', '"own er"', '"owner/app"'):
            with self.subTest(value):
                with self.assertRaisesRegex(ConfigError, r"\.github_account"):
                    self.load(f'{{"slug": "owner/app", "github_account": {value}}}')


class RepositoryAccountTokenTests(unittest.TestCase):
    """Each call carries the token of the account its repository is read as."""

    def setUp(self) -> None:
        for state in (
            github._pinned_account,
            github._repository_accounts,
            github._repository_account_failures,
        ):
            self.addCleanup(state.clear)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in github.GITHUB_TOKEN_VARIABLES:
            os.environ.pop(name, None)

    def token_used(self, run: Mock, repository: Repository) -> Optional[str]:
        github.get_branch_head_sha(repository, "main")
        return (run.call_args.kwargs.get("environment") or {}).get("GH_TOKEN")

    def test_a_repository_is_read_as_its_account_and_the_rest_as_the_reviewer(self):
        with patch("prwarden.github.commands.run_cmd", side_effect=tokens) as run:
            github.pin_github_account("reviewer", [SHARED, PERSONAL])

            self.assertEqual(self.token_used(run, PERSONAL), "token-owner")
            self.assertEqual(self.token_used(run, SHARED), "token-reviewer")

    def test_a_write_goes_out_as_the_repositorys_account(self):
        """Reading as one account and posting as another would put the review under
        a name that cannot see the repository."""

        pull_request = PullRequest(1, "Title", "", "owner", SHA, "url")
        with patch("prwarden.github.commands.run_cmd", side_effect=tokens):
            github.pin_github_account("reviewer", [PERSONAL])
        with patch("prwarden.github.commands.run_cmd", return_value='{"id": 7}') as run:
            github.submit_lgtm_comment(PERSONAL, pull_request, "body")

        self.assertEqual(run.call_args.kwargs["environment"]["GH_TOKEN"], "token-owner")

    def test_an_operator_token_does_not_override_a_repositorys_own_account(self):
        os.environ["GH_TOKEN"] = "operator-token"
        with patch("prwarden.github.commands.run_cmd", side_effect=tokens) as run:
            github.pin_github_account("reviewer", [SHARED, PERSONAL])

            self.assertEqual(self.token_used(run, PERSONAL), "token-owner")
            self.assertIsNone(self.token_used(run, SHARED))

    def test_logins_that_differ_only_in_case_are_one_account(self):
        loud = Repository("loud", "owner/loud", Path("."), github_account="OWNER")
        with patch("prwarden.github.commands.run_cmd", side_effect=tokens) as run:
            github.pin_github_account("reviewer", [PERSONAL, loud])

            logins = [
                call.args[0][4]
                for call in run.call_args_list
                if call.args[0][:3] == ["gh", "auth", "token"]
            ]
            self.assertEqual(logins, ["reviewer", "OWNER"])
            self.assertEqual(self.token_used(run, loud), "token-owner")

    def test_a_missing_login_fails_that_repository_and_no_other(self):
        def no_owner_login(args, **options):
            if list(args[:5]) == ["gh", "auth", "token", "--user", "owner"]:
                raise CommandError("gh exited with 1: no oauth token found")
            return tokens(args, **options)

        with patch(
            "prwarden.github.commands.run_cmd", side_effect=no_owner_login
        ) as run:
            github.pin_github_account("reviewer", [SHARED, PERSONAL])
            calls_after_pinning = run.call_count

            with self.assertRaises(CommandError) as raised:
                github.get_branch_head_sha(PERSONAL, "main")
            self.assertEqual(run.call_count, calls_after_pinning)
            self.assertEqual(self.token_used(run, SHARED), "token-reviewer")

        message = str(raised.exception)
        self.assertIn("no login for owner", message)
        self.assertIn("gh auth login", message)
        self.assertIn("no oauth token found", message)

    def test_an_account_that_was_never_pinned_is_not_read_as_the_reviewer(self):
        with patch("prwarden.github.commands.run_cmd", side_effect=tokens) as run:
            github.pin_github_account("reviewer", [SHARED])
            calls_after_pinning = run.call_count

            with self.assertRaisesRegex(CommandError, "is read as owner"):
                github.get_branch_head_sha(PERSONAL, "main")

        self.assertEqual(run.call_count, calls_after_pinning)

    def test_pinning_again_forgets_the_previous_accounts(self):
        with patch("prwarden.github.commands.run_cmd", side_effect=tokens):
            github.pin_github_account("reviewer", [PERSONAL])
            github.pin_github_account("reviewer", [SHARED])

            with self.assertRaises(CommandError):
                github.get_branch_head_sha(PERSONAL, "main")

    def test_the_account_is_who_the_repository_is_reviewed_as(self):
        self.assertEqual(github.reviewer_account(PERSONAL, "reviewer"), "owner")
        self.assertEqual(github.reviewer_account(SHARED, "reviewer"), "reviewer")


class RepositoryAccountReviewTests(unittest.TestCase):
    """Whose pull request it is follows the account the repository is read as."""

    def setUp(self) -> None:
        without_review_rules(self)


    def review(self, author: str):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text("value = 1\n", encoding="utf-8")
            repository = Repository(
                "own", "owner/own", root, slack=False, github_account="owner"
            )
            pull_request = PullRequest(1, "Title", "", author, "head", "url")
            settings = settings_for(directory, repository)
            engine = Mock()
            engine.review.side_effect = [APPROVING]
            with patch(
                "prwarden.cli.has_current_review", return_value=False
            ) as current, patch(
                "prwarden.cli.list_prior_review_comments", return_value=()
            ) as prior, patch(
                "prwarden.cli.list_changed_files",
                return_value=[
                    {"filename": "app.py", "patch": "@@ -0,0 +1 @@\n+value = 1"}
                ],
            ), patch("prwarden.cli.list_pull_request_checks", return_value=()), patch(
                "prwarden.cli._read_linked_issues", return_value=()
            ), patch(
                "prwarden.cli.get_pull_request_snapshot", return_value=READY_SNAPSHOT
            ), patch("prwarden.cli.review_workspace") as workspace, patch(
                "prwarden.cli.submit_approval", return_value="approval-1"
            ) as approval, patch(
                "prwarden.cli.submit_lgtm_comment", return_value="comment-1"
            ) as lgtm, patch(
                "prwarden.cli.hide_earlier_lgtm_comments", return_value=0
            ), patch("prwarden.cli.send_slack_message") as slack_message, patch(
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
        return {
            "approval": approval,
            "lgtm": lgtm,
            "slack": slack_message,
            "current": current,
            "prior": prior,
            "terminal": output.getvalue(),
        }

    def test_the_accounts_own_pull_request_is_never_approved(self):
        result = self.review("owner")

        result["approval"].assert_not_called()
        result["slack"].assert_not_called()
        result["lgtm"].assert_called_once()
        self.assertIn("-> GITHUB, own pull request", result["terminal"])
        self.assertEqual(result["prior"].call_args.args[2], "owner")

    def test_a_login_in_another_case_is_still_the_accounts_own(self):
        """GitHub refuses to let an author approve their own pull request, and its
        logins ignore case, so a case slip must not reach the approving route."""

        result = self.review("Owner")

        result["approval"].assert_not_called()
        result["lgtm"].assert_called_once()

    def test_the_reviewers_pull_request_there_is_a_teammates(self):
        result = self.review("reviewer")

        result["approval"].assert_called_once()
        result["lgtm"].assert_not_called()
        self.assertEqual(result["current"].call_args.args[2], "owner")
        self.assertEqual(result["prior"].call_args.args[2], "owner")
        self.assertIn("-> GITHUB\n", result["terminal"])


class RepositoryAccountWordListTests(unittest.TestCase):
    def test_a_repository_is_listed_as_its_account(self):
        """A private repository its owner alone can see lists nothing for the active
        login, so the word list would silently miss every name in it."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repositories.json").write_text(
                json.dumps(
                    [
                        {"slug": "team/app"},
                        {"slug": "owner/own", "github_account": "owner"},
                    ]
                )
            )
            seen: "list[tuple[str, Optional[str]]]" = []

            def run(args, environment=None):
                token = (environment or {}).get("GH_TOKEN")
                if args[:3] == ["gh", "auth", "token"]:
                    return f"token-{args[4]}\n"
                if args[:3] == ["gh", "api", "user"]:
                    return "operator\n"
                if args[:2] == ["gh", "pr"]:
                    seen.append((args[4], token))
                    return "[]"
                if args[:3] == ["gh", "api", "--paginate"]:
                    return ""
                raise AssertionError(args)

            with patch("prwarden.public_safety._git", return_value=""):
                refresh_terms(root, root / "repositories.json", run)

        self.assertEqual(seen, [("team/app", None), ("owner/own", "token-owner")])


if __name__ == "__main__":
    unittest.main()
