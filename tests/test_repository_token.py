"""A repository read and reviewed with a token it names in `.env`, such as a
fine-grained token limited to its owner's repositories."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch

import prwarden.delivery as delivery
import prwarden.github as github
from prwarden.config import ConfigError, load_repositories
from prwarden.models import Repository

SHA = "a" * 40
SHARED = Repository("team", "team/app", Path("."))
ORG_APP = Repository("app", "org/app", Path("."), github_token_env="ORG_TOKEN")
ORG_SITE = Repository("site", "org/site", Path("."), github_token_env="ORG_TOKEN")
# Built here, so no source line holds a credential-shaped value.
ORG_TOKEN = "fine-grained-" + "Ab1Cd2Ef3Gh4"


def github_cli(logins: "dict[str, str]"):
    """Answer `gh auth token` with the reviewer's token, and `gh api user` with the
    login GitHub reports for the token the call carries."""

    def run(args, **options):
        if list(args[:4]) == ["gh", "auth", "token", "--user"]:
            return f"token-{args[4].casefold()}"
        if list(args) == ["gh", "api", "user", "--jq", ".login"]:
            return logins[options["environment"]["GH_TOKEN"]]
        return json.dumps({"object": {"sha": SHA}})

    return run


class RepositoryTokenConfigurationTests(unittest.TestCase):
    def load(self, entry: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repositories.json"
            path.write_text(f"[{entry}]", encoding="utf-8")
            return load_repositories(path)

    def test_a_repository_can_name_the_variable_holding_its_token(self):
        (repository,) = self.load('{"slug": "org/app", "github_token_env": "ORG_T"}')
        self.assertEqual(repository.github_token_env, "ORG_T")

    def test_the_name_must_be_an_environment_variable(self):
        for value in ("42", '""', '"org token"', '"$ORG"', '"1ORG"'):
            with self.subTest(value):
                with self.assertRaisesRegex(ConfigError, r"\.github_token_env"):
                    self.load(f'{{"slug": "org/app", "github_token_env": {value}}}')


class RepositoryTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        for state in (
            github._pinned_account,
            github._repository_accounts,
            github._repository_account_failures,
            github._repository_tokens,
            github._repository_token_failures,
            delivery._held_secrets,
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

    def test_a_repository_is_read_with_its_token_and_the_rest_with_the_login(self):
        os.environ["ORG_TOKEN"] = ORG_TOKEN
        run_cli = github_cli({ORG_TOKEN: "reviewer"})
        with patch("prwarden.github.commands.run_cmd", side_effect=run_cli) as run:
            github.pin_github_account("reviewer", [SHARED, ORG_APP, ORG_SITE])
            asked = [
                call for call in run.call_args_list
                if list(call.args[0]) == ["gh", "api", "user", "--jq", ".login"]
            ]

            self.assertEqual(self.token_used(run, ORG_APP), ORG_TOKEN)
            self.assertEqual(self.token_used(run, ORG_SITE), ORG_TOKEN)
            self.assertEqual(self.token_used(run, SHARED), "token-reviewer")
        # One token, asked about once, and kept out of everything posted.
        self.assertEqual(len(asked), 1)
        self.assertIn(ORG_TOKEN, delivery._held_secrets)
        self.assertIsNone(github.repository_account_problem(ORG_APP))

    def test_a_missing_token_fails_its_repository_alone(self):
        with patch(
            "prwarden.github.commands.run_cmd", side_effect=github_cli({})
        ) as run:
            github.pin_github_account("reviewer", [SHARED, ORG_APP])

            self.assertEqual(self.token_used(run, SHARED), "token-reviewer")
            with self.assertRaisesRegex(github.CommandError, "does not set"):
                github.get_branch_head_sha(ORG_APP, "main")
        self.assertIn("ORG_TOKEN", github.repository_account_problem(ORG_APP))

    def test_a_token_of_another_account_is_never_used(self):
        """A token pasted under the wrong name would read, review, and approve as
        somebody else."""

        os.environ["ORG_TOKEN"] = ORG_TOKEN
        with patch(
            "prwarden.github.commands.run_cmd",
            side_effect=github_cli({ORG_TOKEN: "someone-else"}),
        ) as run:
            github.pin_github_account("reviewer", [ORG_APP])
            with self.assertRaisesRegex(github.CommandError, "belongs to someone-else"):
                github.get_branch_head_sha(ORG_APP, "main")

        # Only the call asking GitHub whose it is ever carried it.
        self.assertNotIn(
            ORG_TOKEN,
            [
                (call.kwargs.get("environment") or {}).get("GH_TOKEN")
                for call in run.call_args_list
                if list(call.args[0]) != ["gh", "api", "user", "--jq", ".login"]
            ],
        )

    def test_a_repository_with_its_own_account_and_token_uses_the_token(self):
        """The login of an account whose repositories all name a token is not
        needed, so it is not asked for."""

        owned = Repository(
            "own", "owner/own", Path("."), github_account="owner",
            github_token_env="OWNER_TOKEN",
        )
        os.environ["OWNER_TOKEN"] = ORG_TOKEN
        with patch(
            "prwarden.github.commands.run_cmd",
            side_effect=github_cli({ORG_TOKEN: "Owner"}),
        ) as run:
            github.pin_github_account("reviewer", [owned])

            self.assertEqual(self.token_used(run, owned), ORG_TOKEN)
            self.assertNotIn(
                ["gh", "auth", "token", "--user", "owner"],
                [list(call.args[0]) for call in run.call_args_list],
            )


if __name__ == "__main__":
    unittest.main()
