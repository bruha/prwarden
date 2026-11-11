import os
import plistlib
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

        with patch("prwarden.doctor.schedule.status", side_effect=status), patch(
            "prwarden.doctor.check_schedule_python", return_value=[]
        ):
            return doctor.check_schedule()

    def test_two_schedules_are_a_problem_and_one_of_either_kind_is_not(self) -> None:
        self.assertEqual(statuses(self.check(installed=True, other=True)), [PROBLEM])
        self.assertEqual(statuses(self.check(installed=True, other=False)), [OK])
        self.assertEqual(statuses(self.check(installed=False, other=True)), [OK])
        self.assertEqual(statuses(self.check(installed=False, other=False)), [NOTE])

    def test_an_installed_schedule_also_has_its_python_checked(self) -> None:
        old = Check(PROBLEM, "schedule: runs Python 3.9")

        def status(_args, print_line):
            print_line("Runs Mon-Fri at 08:00")
            return 0

        with patch("prwarden.doctor.schedule.status", side_effect=status), patch(
            "prwarden.doctor.check_schedule_python", return_value=[old]
        ):
            checks = doctor.check_schedule()

        self.assertEqual(statuses(checks), [OK, PROBLEM])
        self.assertIs(checks[1], old)


class SchedulePythonTests(unittest.TestCase):
    """A schedule on a Python below the floor stops before it can report anything."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.probed: "list[list[str]]" = []

    def launch_agent(self, *arguments: str) -> None:
        directory = self.home / "Library" / "LaunchAgents"
        directory.mkdir(parents=True)
        with (directory / "io.github.bruha.prwarden.plist").open("wb") as handle:
            plistlib.dump(
                {"ProgramArguments": ["/usr/bin/caffeinate", "-i", "-s", *arguments]},
                handle,
            )

    def check(self, version="3.15", platform="launchd", error=None):
        def run(args, **_options):
            self.probed.append(list(args))
            if error is not None:
                raise error
            return version

        with patch("prwarden.schedule._platform", return_value=platform), patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(self.home / ".config")}
        ):
            return doctor.check_schedule_python(self.home, run)

    def test_a_clone_scheduled_on_an_old_python_is_a_problem(self) -> None:
        self.launch_agent("/usr/bin/python3", "-m", "prwarden", "--live")

        checks = self.check("3.9")

        self.assertEqual(statuses(checks), [PROBLEM])
        self.assertIn("runs Python 3.9 (/usr/bin/python3)", checks[0].message)
        self.assertIn("needs 3.11 or newer", checks[0].message)
        self.assertEqual(self.probed[0][:2], ["/usr/bin/python3", "-c"])

    def test_a_supported_python_says_nothing(self) -> None:
        self.launch_agent("/opt/python/bin/python3", "-m", "prwarden", "--live")

        self.assertEqual(self.check("3.11"), [])
        self.assertEqual(self.check("3.15"), [])

    def test_an_installed_script_is_checked_through_its_first_line(self) -> None:
        script = self.home / "bin" / "prwarden"
        script.parent.mkdir()
        script.write_text("#!/opt/old/bin/python3.10\nimport sys\n", encoding="utf-8")
        self.launch_agent(str(script), "--live")

        checks = self.check("3.10")

        self.assertEqual(statuses(checks), [PROBLEM])
        self.assertEqual(self.probed[0][0], "/opt/old/bin/python3.10")

    def test_a_script_that_finds_python_through_env_is_not_guessed(self) -> None:
        script = self.home / "bin" / "prwarden"
        script.parent.mkdir()
        script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        self.launch_agent(str(script), "--live")

        self.assertEqual(self.check("3.9"), [])
        self.assertEqual(self.probed, [])

    def test_a_systemd_service_is_read_the_same_way(self) -> None:
        units = self.home / ".config" / "systemd" / "user"
        units.mkdir(parents=True)
        (units / "prwarden.service").write_text(
            "[Service]\n"
            # Written with systemd's own escapes, which `schedule install` adds.
            'ExecStart="/opt/py%%3/$$bin/python3" "-m" "prwarden" "--live"\n',
            encoding="utf-8",
        )

        checks = self.check("3.10", platform="systemd")

        self.assertEqual(statuses(checks), [PROBLEM])
        self.assertEqual(self.probed[0][0], "/opt/py%3/$bin/python3")

    def test_a_python_that_does_not_run_is_a_problem(self) -> None:
        self.launch_agent("/gone/python3", "-m", "prwarden", "--live")

        checks = self.check(error=CommandError("Required command not found"))

        self.assertEqual(statuses(checks), [PROBLEM])
        self.assertIn("/gone/python3", checks[0].message)

    def test_a_python_that_cannot_be_executed_is_a_problem_not_a_crash(self) -> None:
        """Through the real `run_cmd`, which passes such an OSError through."""

        interpreter = self.home / "python3"
        interpreter.write_text("not a program\n", encoding="utf-8")
        interpreter.chmod(0o644)
        self.launch_agent(str(interpreter), "-m", "prwarden", "--live")

        with patch("prwarden.schedule._platform", return_value="launchd"):
            checks = doctor.check_schedule_python(self.home)

        self.assertEqual(statuses(checks), [PROBLEM])
        self.assertIn("does not run", checks[0].message)

    def test_no_schedule_checks_nothing(self) -> None:
        self.assertEqual(self.check("3.9"), [])
        self.assertEqual(self.probed, [])


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

    def test_slack_is_reported_with_how_to_connect_it(self) -> None:
        settings = settings_for("/tmp", Repository("app", "owner/app"))
        locations = Locations("user", Path("/c"), Path("/s"), Path("/s/checkouts"))

        missing = doctor.check_delivery(
            replace(settings, slack_webhook_url=None), locations
        )
        connected = doctor.check_delivery(
            replace(settings, slack_webhook_url="https://hooks.slack.com/x"), locations
        )

        self.assertEqual(missing[-1].status, NOTE)
        self.assertIn("prwarden setup --slack", missing[-1].message)
        self.assertEqual(connected[-1].status, OK)
        self.assertIn("1 of 1 repositories", connected[-1].message)
        self.assertNotIn("hooks.slack.com", connected[-1].message)


class DispatchTests(unittest.TestCase):
    def test_prwarden_doctor_reaches_the_doctor(self) -> None:
        import prwarden.cli as cli

        with patch("prwarden.doctor.main", return_value=1) as entry:
            self.assertEqual(cli.main(["doctor", "--env-file", "x"]), 1)

        entry.assert_called_once_with(["--env-file", "x"])



class JiraCheckTests(unittest.TestCase):
    """Jira is context for a review, so its state is a note, never a problem."""

    def settings(self, projects):
        from tempfile import gettempdir

        repository = Repository("app", "acme/app", Path("."), jira_projects=projects)
        return settings_for(gettempdir(), repository)

    def state(self, installed=True, signed_in=True):
        from prwarden import tools

        return tools.ToolState(
            tools.JIRA_TOOL, "/opt/bin/acli" if installed else None, "1.3", signed_in
        )

    def test_nothing_is_checked_while_no_repository_names_a_project(self) -> None:
        def unexpected(tool):
            raise AssertionError("acli was checked")

        self.assertEqual(doctor.check_jira(self.settings(()), unexpected), [])

    def test_each_state_of_acli_is_reported(self) -> None:
        settings = self.settings(("APP",))
        for state, status, words in (
            (self.state(), OK, "acli is signed in"),
            (self.state(signed_in=False), NOTE, "acli jira auth login --web"),
            (self.state(signed_in=None), NOTE, "not signed in"),
            (self.state(installed=False), NOTE, "brew install atlassian/acli/acli"),
        ):
            with self.subTest(words=words):
                checks = doctor.check_jira(settings, lambda tool, state=state: state)
                self.assertEqual(statuses(checks), [status])
                self.assertIn(words, checks[0].message)
                self.assertIn(
                    "1 of 1 repositories name Jira projects", checks[0].message
                )


if __name__ == "__main__":
    unittest.main()
