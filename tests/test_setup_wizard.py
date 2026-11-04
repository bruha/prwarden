import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prwarden import setup_wizard
from prwarden.commands import CommandError
from prwarden.config import load_env_file, load_repositories
from prwarden.paths import Locations
from prwarden.setup_wizard import Prompter, SetupError


class ScriptedPrompter(Prompter):
    """Answers each question with the next scripted answer, recording all of it."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.transcript = []
        super().__init__(self._answer, self._answer, self.transcript.append)

    def _answer(self, question):
        self.transcript.append(question)
        if not self.answers:
            raise AssertionError(f"no answer scripted for {question!r}")
        return self.answers.pop(0)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class WizardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.locations = Locations(
            "user",
            self.root / "config",
            self.root / "state",
            self.root / "share" / "checkouts",
        )
        self.clone = self.root / "src" / "private"
        self.clone.mkdir(parents=True)
        for target, value in (
            ("github_accounts", (["reviewer", "owner"], "reviewer")),
            ("installed_engines", ["claude", "kiro"]),
        ):
            stub = patch(f"prwarden.setup_wizard.{target}", return_value=value)
            stub.start()
            self.addCleanup(stub.stop)
        for target, value in (
            ("pin_github_account", None),
            ("repository_size", 2048),
        ):
            stub = patch(f"prwarden.setup_wizard.github.{target}", return_value=value)
            stub.start()
            self.addCleanup(stub.stop)

        def reachable(repository):
            # The private repository can be read only by its owner's account.
            if repository.slug == "owner/private" and not repository.github_account:
                raise CommandError("HTTP 404")
            return repository.slug

        stub = patch(
            "prwarden.setup_wizard.github.repository_reachable", side_effect=reachable
        )
        stub.start()
        self.addCleanup(stub.stop)

    def run_wizard(self, answers):
        prompter = ScriptedPrompter(answers)
        result = setup_wizard.interactive(prompter, self.locations)
        self.assertEqual(prompter.answers, [], "every scripted answer was asked for")
        return result, prompter

    ANSWERS = [
        "",  # review as the active account
        "",  # review with the first engine found
        "",  # verify with the next one
        "acme/app owner/private",
        "",  # acme/app: let PR Warden clone it
        None,  # owner/private: the clone, filled in by the test
        "",  # no Slack
        "n",  # no preview
        "n",  # stay dry
    ]

    def answers(self, **changes):
        answers = list(self.ANSWERS)
        answers[5] = str(self.clone)
        for index, value in changes.items():
            answers[int(index[1:])] = value
        return answers

    def test_a_new_configuration_is_written_dry_and_private(self) -> None:
        result, prompter = self.run_wizard(self.answers())

        self.assertEqual(result, 0)
        env_file = self.locations.env_file
        self.assertEqual(mode(env_file), 0o600)
        self.assertEqual(mode(self.locations.repositories_file), 0o600)
        with patch.dict(os.environ, {}, clear=True):
            values = load_env_file(env_file)
        self.assertEqual(values["AUTO_REVIEW_DRY_RUN"], "1")
        self.assertEqual(values["GITHUB_REVIEWER_USERNAME"], "reviewer")
        self.assertEqual(values["REVIEW_ENGINE"], "claude")
        self.assertEqual(values["REVIEW_VERIFY_ENGINE"], "kiro")
        self.assertNotIn("SLACK_WEBHOOK_URL", values)

        app, private = load_repositories(
            self.locations.repositories_file, self.locations.checkouts_directory
        )
        self.assertTrue(app.managed_checkout)
        self.assertFalse(app.slack)
        self.assertEqual(private.path, self.clone)
        self.assertEqual(private.github_account, "owner")
        # The clone is announced with where it goes and how large it is.
        question = next(
            line for line in prompter.transcript if "acme/app: path" in line
        )
        managed = self.locations.checkouts_directory / "acme" / "app"
        self.assertIn(str(managed), question)
        self.assertIn("about 2 MB", question)

    def test_an_existing_configuration_is_kept_unless_replacing_it_is_confirmed(
        self,
    ) -> None:
        self.locations.config_directory.mkdir(parents=True)
        self.locations.env_file.write_text("KEEP=1\n", encoding="utf-8")

        result, _ = self.run_wizard(["n"])

        self.assertEqual(result, 0)
        self.assertEqual(self.locations.env_file.read_text(), "KEEP=1\n")

        self.run_wizard(["y", *self.answers()])
        backups = list(self.locations.config_directory.glob(".env.before-setup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "KEEP=1\n")

    def test_going_live_opens_the_gate_and_offers_the_schedule(self) -> None:
        with patch("prwarden.setup_wizard.schedule.install", return_value=0) as install:
            self.run_wizard(self.answers(a8="y") + ["y"])

        with patch.dict(os.environ, {}, clear=True):
            values = load_env_file(self.locations.env_file)
        self.assertEqual(values["AUTO_REVIEW_DRY_RUN"], "0")
        install.assert_called_once()

    def test_a_webhook_is_kept_secret_and_turns_slack_on(self) -> None:
        hook = "https://hooks.slack.com/services/T000/B000/XXXX"
        self.run_wizard(self.answers(a6=hook))

        with patch.dict(os.environ, {}, clear=True):
            values = load_env_file(self.locations.env_file)
        self.assertEqual(values["SLACK_WEBHOOK_URL"], hook)
        entries = json.loads(self.locations.repositories_file.read_text())
        self.assertTrue(all("slack" not in entry for entry in entries))

    def test_a_repository_nobody_can_read_is_skipped(self) -> None:
        answers = self.answers(a3="acme/app nobody/secret")
        del answers[5]  # no path is asked for the unreadable one

        def reachable(repository):
            if repository.slug == "nobody/secret":
                raise CommandError("HTTP 404")
            return repository.slug

        with patch(
            "prwarden.setup_wizard.github.repository_reachable", side_effect=reachable
        ):
            _, prompter = self.run_wizard(answers)

        entries = json.loads(self.locations.repositories_file.read_text())
        self.assertEqual([entry["slug"] for entry in entries], ["acme/app"])
        self.assertTrue(
            any("Skipped nobody/secret" in line for line in prompter.transcript)
        )


class SetEnvValueTests(unittest.TestCase):
    def test_one_value_changes_and_everything_else_stays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# AUTO_REVIEW_DRY_RUN=1 is the default\nAUTO_REVIEW_DRY_RUN=1\n"
                "REVIEW_ENGINE=kiro\n",
                encoding="utf-8",
            )

            setup_wizard.set_env_value(path, "AUTO_REVIEW_DRY_RUN", "0")

            self.assertEqual(
                path.read_text(),
                "# AUTO_REVIEW_DRY_RUN=1 is the default\nAUTO_REVIEW_DRY_RUN=0\n"
                "REVIEW_ENGINE=kiro\n",
            )
            self.assertEqual(mode(path), 0o600)


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "projects" / "prwarden"
        (self.checkout / ".state").mkdir(parents=True)
        env_file = self.checkout / ".env"
        env_file.write_text(
            "AUTO_REVIEW_DRY_RUN=0\nREPOSITORIES_FILE=repositories.json\n"
            "STATE_FILE=.state/review-state.json\n"
            "GITHUB_COMMENT_TEMPLATE_FILE=github-comment.md\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        (self.checkout / "repositories.json").write_text(
            '[{"slug": "acme/app", "path": "../app"}, {"slug": "acme/lib"}]',
            encoding="utf-8",
        )
        (self.checkout / ".state" / "review-state.json").write_text(
            '{"records": {}}', encoding="utf-8"
        )
        self.locations = Locations(
            "user",
            self.root / "config",
            self.root / "state",
            self.root / "share" / "checkouts",
        )

    def test_configuration_and_delivery_records_carry_over(self) -> None:
        """Without the records every open pull request would be posted to again."""

        result = setup_wizard.import_checkout(
            self.checkout, self.locations, lambda line: None
        )

        self.assertEqual(result, 0)
        with patch.dict(os.environ, {}, clear=True):
            values = load_env_file(self.locations.env_file)
        self.assertEqual(values["AUTO_REVIEW_DRY_RUN"], "0")
        self.assertNotIn("REPOSITORIES_FILE", values)
        self.assertNotIn("STATE_FILE", values)
        self.assertEqual(
            values["GITHUB_COMMENT_TEMPLATE_FILE"],
            str(self.checkout / "github-comment.md"),
        )
        app, lib = load_repositories(
            self.locations.repositories_file, self.locations.checkouts_directory
        )
        self.assertEqual(app.path, self.root / "projects" / "app")
        self.assertTrue(lib.managed_checkout)
        self.assertEqual(
            self.locations.state_file.read_text(encoding="utf-8"), '{"records": {}}'
        )
        self.assertEqual(mode(self.locations.state_file), 0o600)

    def test_nothing_is_merged_into_an_existing_installation(self) -> None:
        self.locations.state_directory.mkdir(parents=True)
        self.locations.state_file.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(SetupError, "already exists"):
            setup_wizard.import_checkout(self.checkout, self.locations, print)
        self.assertFalse(self.locations.env_file.exists())


class MainTests(unittest.TestCase):
    def test_without_a_terminal_setup_refuses_rather_than_hanging(self) -> None:
        with patch("sys.stdin", io.StringIO()), patch(
            "sys.stderr", new_callable=io.StringIO
        ) as error:
            self.assertEqual(setup_wizard.main([]), 2)
        self.assertIn("run it in a terminal", error.getvalue())

    def test_prwarden_setup_reaches_the_wizard(self) -> None:
        import prwarden.cli as cli

        with patch("prwarden.setup_wizard.main", return_value=0) as entry:
            self.assertEqual(cli.main(["setup", "--import", "x"]), 0)

        entry.assert_called_once_with(["--import", "x"])


if __name__ == "__main__":
    unittest.main()
