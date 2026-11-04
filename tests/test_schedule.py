import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from argparse import Namespace
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from prwarden import schedule
from prwarden.models import Repository
from prwarden.schedule import ScheduleError, Slots
from tests.test_slack_scope import settings_for


WEEKDAYS_THREE_TIMES = Slots(tuple(range(5)), ((8, 0), (12, 0), (16, 0)))


class SlotParsingTests(unittest.TestCase):
    def test_days_and_times_are_read_the_way_people_write_them(self) -> None:
        self.assertEqual(schedule.parse_days("mon-fri"), (0, 1, 2, 3, 4))
        self.assertEqual(schedule.parse_days("daily"), tuple(range(7)))
        self.assertEqual(schedule.parse_days("Mon,wed-thu, sun"), (0, 2, 3, 6))
        self.assertEqual(
            schedule.parse_times("16:00, 8:00,12:30"), ((8, 0), (12, 30), (16, 0))
        )

    def test_anything_else_is_refused_rather_than_guessed(self) -> None:
        for days in ("monday", "fri-mon", "mon,,tue", ""):
            with self.subTest(days=days), self.assertRaises(ScheduleError):
                schedule.parse_days(days)
        for times in ("8", "24:00", "08:60", "8am", ""):
            with self.subTest(times=times), self.assertRaises(ScheduleError):
                schedule.parse_times(times)

    def test_a_schedule_describes_itself(self) -> None:
        self.assertEqual(
            WEEKDAYS_THREE_TIMES.describe(), "Mon-Fri at 08:00, 12:00, 16:00"
        )
        self.assertEqual(Slots((0, 6), ((9, 5),)).describe(), "Mon, Sun at 09:05")


class LaunchdTests(unittest.TestCase):
    def test_the_agent_runs_live_under_caffeinate_at_every_slot(self) -> None:
        home = Path("/Users/someone")
        plist = schedule.launchd_plist(
            ["/opt/bin/prwarden", "--live"],
            home,
            {"HOME": str(home), "PATH": "/opt/bin:/usr/bin:/bin"},
            Slots((0, 6), ((8, 0), (16, 30))),
            home,
        )

        self.assertEqual(
            plist["ProgramArguments"],
            ["/usr/bin/caffeinate", "-i", "-s", "/opt/bin/prwarden", "--live"],
        )
        # launchd numbers Sunday 0 and Monday 1.
        self.assertEqual(
            plist["StartCalendarInterval"],
            [
                {"Weekday": 1, "Hour": 8, "Minute": 0},
                {"Weekday": 1, "Hour": 16, "Minute": 30},
                {"Weekday": 0, "Hour": 8, "Minute": 0},
                {"Weekday": 0, "Hour": 16, "Minute": 30},
            ],
        )
        self.assertEqual(plist["Umask"], 0o077)
        self.assertEqual(
            plist["StandardOutPath"], "/Users/someone/Library/Logs/prwarden.log"
        )
        self.assertEqual(
            schedule.launchd_slots(plist), Slots((0, 6), ((8, 0), (16, 30)))
        )
        plistlib.dumps(plist)

    def test_another_agent_running_prwarden_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            for name, arguments in (
                ("com.example.prwarden.plist", ["/usr/bin/python3", "-m", "prwarden"]),
                ("com.example.other.plist", ["/usr/bin/true"]),
                (f"{schedule.LAUNCHD_LABEL}.plist", ["/opt/bin/prwarden"]),
            ):
                with (agents / name).open("wb") as handle:
                    plistlib.dump({"ProgramArguments": arguments}, handle)
            (agents / "broken.plist").write_text("<plist><dict>&</dict>")

            found = schedule.other_launch_agents(home)

        self.assertEqual([path.name for path in found], ["com.example.prwarden.plist"])


class SystemdTests(unittest.TestCase):
    def units(self, command=("/opt/bin/prwarden", "--live"), environment=None):
        return schedule.systemd_units(
            list(command),
            Path("/home/someone"),
            environment or {"HOME": "/home/someone", "PATH": "/opt/bin:/usr/bin"},
            WEEKDAYS_THREE_TIMES,
        )

    def test_the_timer_runs_every_slot_and_catches_up_after_downtime(self) -> None:
        timer = self.units()["prwarden.timer"]

        self.assertIn("OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 08:00:00\n", timer)
        self.assertIn("OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 16:00:00\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertEqual(schedule.systemd_slots(timer), WEEKDAYS_THREE_TIMES)

    def test_values_are_quoted_so_systemd_expands_nothing(self) -> None:
        """`%` is a specifier everywhere and `$` a variable on the command line."""

        service = self.units(
            command=("/opt/my tools/prwarden", "--env-file", "/x/100%$HOME.env"),
            environment={"PATH": '/a b:/c"d', "NOTE": "50%$"},
        )["prwarden.service"]

        self.assertIn(
            'ExecStart="/opt/my tools/prwarden" "--env-file" "/x/100%%$$HOME.env"\n',
            service,
        )
        self.assertIn('Environment="PATH=/a b:/c\\"d" "NOTE=50%%$"\n', service)
        self.assertIn("Type=oneshot\n", service)

    @unittest.skipIf(shutil.which("systemd-analyze") is None, "needs systemd")
    def test_systemd_accepts_the_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for name, text in self.units(command=("/bin/true", "--live")).items():
                (Path(directory) / name).write_text(text, encoding="utf-8")
            completed = subprocess.run(
                [
                    "systemd-analyze",
                    "--user",
                    "verify",
                    str(Path(directory) / "prwarden.timer"),
                    str(Path(directory) / "prwarden.service"),
                ],
                capture_output=True,
                text=True,
            )
        self.assertNotIn("prwarden", completed.stderr)


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        repository = Repository("app", "owner/app", self.home / "src" / "app")
        self.settings = settings_for(temporary.name, repository)
        self.calls: "list[list[str]]" = []
        environment = patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def args(self, **values):
        defaults = {"days": "mon-fri", "at": "08:00,12:00", "print": False}
        defaults.update(values)
        return Namespace(env_file=None, **defaults)

    def run_install(self, platform: str, settings=None, **values):
        output = StringIO()

        def record(args, **_options):
            self.calls.append(list(args))
            return ""

        with patch("prwarden.schedule._platform", return_value=platform), patch(
            "prwarden.schedule.load_settings", return_value=settings or self.settings
        ), patch("prwarden.schedule.run_cmd", side_effect=record), patch(
            "prwarden.schedule.shutil.which", return_value="/opt/bin/prwarden"
        ), patch(
            "prwarden.schedule.is_source_checkout", return_value=False
        ):
            result = schedule.install(
                self.args(**values), lambda line: output.write(line + "\n"), self.home
            )
        return result, output.getvalue()

    def test_a_schedule_is_refused_until_the_live_gate_is_open(self) -> None:
        """A dry run records nothing, so it would pay again at every slot."""

        dry = replace(self.settings, dry_run=True)
        for platform in ("launchd", "systemd"):
            with self.subTest(platform=platform):
                with self.assertRaisesRegex(ScheduleError, "AUTO_REVIEW_DRY_RUN"):
                    self.run_install(platform, settings=dry)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.home / "Library").exists())
        self.assertFalse((self.home / ".config").exists())

    def test_launchd_writes_the_agent_and_loads_it(self) -> None:
        result, output = self.run_install("launchd")

        path = schedule.launchd_plist_path(self.home)
        with path.open("rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(result, 0)
        self.assertEqual(
            plist["ProgramArguments"][3:], ["/opt/bin/prwarden", "--live"]
        )
        self.assertEqual([call[:2] for call in self.calls], [
            ["launchctl", "bootout"],
            ["launchctl", "bootstrap"],
        ])
        self.assertIn("Runs Mon-Fri at 08:00, 12:00, live", output)

    def test_a_second_agent_is_refused_rather_than_doubling_every_review(
        self,
    ) -> None:
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        with (agents / "com.example.prwarden.plist").open("wb") as handle:
            plistlib.dump({"ProgramArguments": ["/opt/bin/prwarden", "--live"]}, handle)

        with self.assertRaisesRegex(ScheduleError, "com.example.prwarden.plist"):
            self.run_install("launchd")
        self.assertEqual(self.calls, [])

    def test_a_folder_macos_guards_is_named(self) -> None:
        guarded = replace(
            self.settings,
            repositories=[
                Repository("app", "owner/app", self.home / "Documents" / "app")
            ],
        )
        _, output = self.run_install("launchd", settings=guarded)

        self.assertIn(f"Note: {self.home / 'Documents' / 'app'} is in a folder", output)

    def test_systemd_writes_both_units_and_enables_the_timer(self) -> None:
        result, output = self.run_install("systemd")

        directory = self.home / ".config" / "systemd" / "user"
        self.assertEqual(result, 0)
        self.assertTrue((directory / "prwarden.service").is_file())
        self.assertIn(
            "OnCalendar=Mon,Tue,Wed,Thu,Fri *-*-* 12:00:00",
            (directory / "prwarden.timer").read_text(encoding="utf-8"),
        )
        self.assertIn(
            ["systemctl", "--user", "enable", "--now", "prwarden.timer"], self.calls
        )
        self.assertIn("journalctl --user -u prwarden.service", output)

    def test_print_shows_the_units_and_changes_nothing(self) -> None:
        dry = replace(self.settings, dry_run=True)
        _, output = self.run_install("systemd", settings=dry, print=True)

        self.assertIn("# prwarden.timer", output)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.home / ".config").exists())


class RunCommandTests(unittest.TestCase):
    def test_an_installed_copy_runs_the_command_on_path(self) -> None:
        home = Path("/home/someone")
        with patch("prwarden.schedule.is_source_checkout", return_value=False), patch(
            "prwarden.schedule.shutil.which", return_value="/opt/bin/prwarden"
        ):
            command, directory = schedule.run_command(home, Path("/srv/pw/.env"))

        self.assertEqual(
            command, ["/opt/bin/prwarden", "--env-file", "/srv/pw/.env", "--live"]
        )
        self.assertEqual(directory, home)

    def test_a_checkout_runs_its_own_code_with_this_interpreter(self) -> None:
        with patch("prwarden.schedule.is_source_checkout", return_value=True):
            command, directory = schedule.run_command(Path("/home/someone"))
            environment = schedule.run_environment(
                settings_for("/tmp", Repository("app", "owner/app")),
                Path("/home/someone"),
            )

        self.assertEqual(command[1:], ["-m", "prwarden", "--live"])
        self.assertEqual(directory, schedule.PACKAGE_ROOT)
        self.assertEqual(environment["PYTHONPATH"], str(schedule.PACKAGE_ROOT))

    def test_the_environment_finds_every_configured_cli_and_keeps_locations(
        self,
    ) -> None:
        settings = replace(
            settings_for("/tmp", Repository("app", "owner/app")),
            review_engine_command=("/opt/kiro/bin/kiro-cli",),
            verify_engine_command=("/opt/claude/bin/claude",),
        )
        with patch.dict(
            os.environ, {"PRWARDEN_HOME": "/srv/prwarden"}, clear=True
        ), patch("prwarden.schedule.is_source_checkout", return_value=False), patch(
            "prwarden.schedule.shutil.which", return_value=None
        ):
            environment = schedule.run_environment(settings, Path("/home/someone"))

        path = environment["PATH"].split(os.pathsep)
        self.assertEqual(path[:2], ["/opt/kiro/bin", "/opt/claude/bin"])
        self.assertIn("/home/someone/.local/bin", path)
        self.assertEqual(environment["PRWARDEN_HOME"], "/srv/prwarden")
        self.assertNotIn("PYTHONPATH", environment)


class RemoveTests(unittest.TestCase):
    def test_both_platforms_unload_before_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            plist = schedule.launchd_plist_path(home)
            plist.parent.mkdir(parents=True)
            plist.write_bytes(b"")
            units = home / ".config" / "systemd" / "user"
            units.mkdir(parents=True)
            for name in ("prwarden.timer", "prwarden.service"):
                (units / name).write_text("", encoding="utf-8")
            calls: "list[list[str]]" = []

            def record(args, **_options):
                calls.append(list(args))
                return ""

            with patch.dict(os.environ, {}, clear=True), patch(
                "prwarden.schedule.run_cmd", side_effect=record
            ):
                for platform in ("launchd", "systemd"):
                    with patch("prwarden.schedule._platform", return_value=platform):
                        schedule.remove(Namespace(), lambda line: None, home)

            self.assertFalse(plist.exists())
            self.assertEqual(list(units.iterdir()), [])
        self.assertEqual(calls[0][:2], ["launchctl", "bootout"])
        self.assertEqual(
            calls[1], ["systemctl", "--user", "disable", "--now", "prwarden.timer"]
        )


class DispatchTests(unittest.TestCase):
    def test_prwarden_schedule_reaches_the_schedule_command(self) -> None:
        import prwarden.cli as cli

        with patch("prwarden.schedule.main", return_value=0) as entry:
            self.assertEqual(cli.main(["schedule", "status"]), 0)

        entry.assert_called_once_with(["status"])


if __name__ == "__main__":
    unittest.main()
