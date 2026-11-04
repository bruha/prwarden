import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from prwarden import doctor
from prwarden.commands import CommandError
from prwarden.doctor import NOTE, OK, PROBLEM, Check
from prwarden.models import Repository
from prwarden.paths import Locations
from tests.test_slack_scope import settings_for


def statuses(checks):
    return [check.status for check in checks]


class EnvFileTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX permissions are not available")
    def test_a_readable_secrets_file_is_a_problem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("AUTO_REVIEW_DRY_RUN=1\n", encoding="utf-8")
            env_file.chmod(0o644)
            loose = doctor.check_env_file(env_file)
            env_file.chmod(0o600)
            private = doctor.check_env_file(env_file)
            missing = doctor.check_env_file(Path(directory) / "absent.env")

        self.assertEqual(statuses(loose), [PROBLEM])
        self.assertIn("chmod 600", loose[0].message)
        self.assertEqual(statuses(private), [OK])
        self.assertEqual(statuses(missing), [NOTE])


class EngineTests(unittest.TestCase):
    def test_every_configured_role_must_find_its_cli(self) -> None:
        settings = replace(
            settings_for("/tmp", Repository("app", "owner/app")),
            review_engine="kiro",
            review_engine_command=("kiro-cli",),
            verify_engine="claude",
            verify_engine_command=("/nowhere/claude",),
        )
        with patch("prwarden.doctor.shutil.which", return_value="/opt/bin/kiro-cli"):
            checks = doctor.check_engines(settings)

        self.assertEqual(statuses(checks), [OK, PROBLEM])
        self.assertIn("verify engine claude", checks[1].message)


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in (
            ("repository_reachable", "owner/app"),
            ("repository_account_problem", None),
        ):
            stub = patch(f"prwarden.doctor.github.{name}", return_value=value)
            stub.start()
            self.addCleanup(stub.stop)

    def check(self, repository, remote="git@github.com:owner/app.git"):
        with patch("prwarden.doctor.run_cmd", return_value=remote):
            return doctor.check_repository(repository)

    def test_a_clone_of_the_right_repository_is_ready(self) -> None:
        checks = self.check(Repository("app", "owner/app", self.root))

        self.assertEqual(statuses(checks), [OK])

    def test_a_clone_of_another_repository_is_a_problem(self) -> None:
        checks = self.check(
            Repository("app", "owner/app", self.root),
            remote="git@github.com:owner/other.git",
        )

        self.assertEqual(statuses(checks), [PROBLEM])

    def test_a_missing_clone_is_a_problem_but_a_managed_one_is_not(self) -> None:
        missing = self.check(Repository("app", "owner/app", self.root / "gone"))
        managed = self.check(
            Repository(
                "app", "owner/app", self.root / "gone", managed_checkout=True
            )
        )

        self.assertEqual(statuses(missing), [PROBLEM])
        self.assertEqual(statuses(managed), [OK])
        self.assertIn("cloned on its first review", managed[0].message)

    def test_a_repository_github_cannot_read_is_a_problem(self) -> None:
        with patch(
            "prwarden.doctor.github.repository_reachable",
            side_effect=CommandError("HTTP 404"),
        ):
            checks = self.check(Repository("app", "owner/app", self.root))

        self.assertEqual(statuses(checks), [PROBLEM])
        self.assertIn("HTTP 404", checks[0].message)


class ScheduleTests(unittest.TestCase):
    def check(self, installed: bool, other: bool):
        def status(_args, print_line):
            if other:
                print_line("Also running PR Warden: /agents/hand-written.plist")
            if installed:
                print_line("Runs Mon-Fri at 08:00")
            return 0 if installed else 1

        with patch("prwarden.doctor.schedule.status", side_effect=status):
            return doctor.check_schedule()

    def test_two_schedules_are_a_problem_and_one_of_either_kind_is_not(self) -> None:
        self.assertEqual(statuses(self.check(installed=True, other=True)), [PROBLEM])
        self.assertEqual(statuses(self.check(installed=True, other=False)), [OK])
        self.assertEqual(statuses(self.check(installed=False, other=True)), [OK])
        self.assertEqual(statuses(self.check(installed=False, other=False)), [NOTE])


class MainTests(unittest.TestCase):
    def run_main(self, checks):
        lines = []
        with patch("prwarden.doctor.diagnose", return_value=(["header"], checks)):
            code = doctor.main([], lines.append)
        return code, lines

    def test_only_a_problem_fails_the_check(self) -> None:
        code, lines = self.run_main([Check(OK, "a"), Check(NOTE, "b")])
        self.assertEqual(code, 0)
        self.assertEqual(lines[-1], "Ready to review")

        code, lines = self.run_main([Check(OK, "a"), Check(PROBLEM, "c")])
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "1 problem found")

    def test_a_dry_run_configuration_is_noted_not_failed(self) -> None:
        settings = settings_for("/tmp", Repository("app", "owner/app"))
        locations = Locations(
            "user", Path("/c"), Path("/s"), Path("/s/checkouts")
        )

        dry = doctor.check_delivery(replace(settings, dry_run=True), locations)
        live = doctor.check_delivery(settings, locations)

        self.assertEqual(statuses(dry)[0], NOTE)
        self.assertEqual(statuses(live)[0], OK)


class DispatchTests(unittest.TestCase):
    def test_prwarden_doctor_reaches_the_doctor(self) -> None:
        import prwarden.cli as cli

        with patch("prwarden.doctor.main", return_value=1) as entry:
            self.assertEqual(cli.main(["doctor", "--env-file", "x"]), 1)

        entry.assert_called_once_with(["--env-file", "x"])


if __name__ == "__main__":
    unittest.main()
