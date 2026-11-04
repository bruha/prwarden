"""Install, show, and remove the timer that runs PR Warden on a schedule.

macOS gets a per-user LaunchAgent and Linux a systemd user timer. Either one runs
`prwarden --live`: a dry run records nothing, so a scheduled one would review every
open pull request again at every slot and spend credits each time for a log nobody
reads. Installing therefore requires the configuration's own live gate to be open,
which is the operator's decision, made after a preview.
"""

import argparse
import os
import plistlib
import re
import shlex
import shutil
import sys
from xml.parsers.expat import ExpatError
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from .commands import CommandError, run_cmd
from .config import ConfigError, Settings, default_locations, load_settings
from .paths import PACKAGE_ROOT, Locations, is_source_checkout


LAUNCHD_LABEL = "io.github.bruha.prwarden"
SYSTEMD_UNIT = "prwarden"
DEFAULT_TIMES = ("08:00", "12:00", "16:00")
DEFAULT_DAYS = "mon-fri"
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TIME_PATTERN = re.compile(r"\A([01]?\d|2[0-3]):([0-5]\d)\Z")
# Variables that move where a run reads its configuration and keeps its state. The
# scheduler starts with almost no environment, so each one set now is carried over.
LOCATION_VARIABLES = (
    "PRWARDEN_HOME",
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
)
STANDARD_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
# macOS asks before a program reads these, once per binary, and an unattended run
# waits at that question until somebody answers it.
PROTECTED_FOLDERS = ("Desktop", "Documents", "Downloads")


NOT_INSTALLED = "No schedule is installed. Install one with: prwarden schedule install"


class ScheduleError(RuntimeError):
    """Raised when a schedule cannot be installed, read, or removed."""


@dataclass(frozen=True)
class Slots:
    """When the schedule runs: weekdays as indexes into DAY_NAMES, and times."""

    days: "tuple[int, ...]"
    times: "tuple[tuple[int, int], ...]"

    def describe(self) -> str:
        names = [DAY_NAMES[day].capitalize() for day in self.days]
        if self.days == tuple(range(5)):
            span = "Mon-Fri"
        elif self.days == tuple(range(7)):
            span = "every day"
        else:
            span = ", ".join(names)
        clock = ", ".join(f"{hour:02d}:{minute:02d}" for hour, minute in self.times)
        return f"{span} at {clock}"


def parse_days(value: str) -> "tuple[int, ...]":
    text = value.strip().lower()
    if text in ("daily", "every-day"):
        return tuple(range(7))
    if text in ("weekdays", "mon-fri"):
        return tuple(range(5))
    days = set()
    for part in text.split(","):
        start, _, end = part.strip().partition("-")
        if start not in DAY_NAMES or (end and end not in DAY_NAMES):
            raise ScheduleError(
                "--days takes day names such as mon-fri or mon,wed,fri, "
                f"not {value!r}"
            )
        first = DAY_NAMES.index(start)
        last = DAY_NAMES.index(end) if end else first
        if last < first:
            raise ScheduleError(f"--days range {part!r} runs backwards")
        days.update(range(first, last + 1))
    return tuple(sorted(days))


def parse_times(value: str) -> "tuple[tuple[int, int], ...]":
    times = set()
    for part in value.split(","):
        match = TIME_PATTERN.fullmatch(part.strip())
        if not match:
            raise ScheduleError(
                f"--at takes 24-hour times such as 08:00, not {part!r}"
            )
        times.add((int(match.group(1)), int(match.group(2))))
    return tuple(sorted(times))


def run_command(
    home: Path, env_file: Optional[Path] = None
) -> "tuple[list[str], Path]":
    """Return the command a scheduled run starts, and the directory it starts in.

    Code run from a source checkout is scheduled the same way, with this
    interpreter. An installed copy runs the `prwarden` command found on PATH, which
    Homebrew and pipx keep at one path across upgrades, where the interpreter's
    own path changes.
    """

    if is_source_checkout(PACKAGE_ROOT):
        command, directory = [sys.executable, "-m", "prwarden"], PACKAGE_ROOT
    else:
        installed = shutil.which("prwarden")
        command = [installed] if installed else [sys.executable, "-m", "prwarden"]
        directory = home
    if env_file is not None:
        command += ["--env-file", str(env_file.expanduser().resolve())]
    return [*command, "--live"], directory


def _executable_directories(settings: Settings) -> "list[str]":
    commands = [
        settings.review_engine_command,
        settings.verify_engine_command,
        settings.fallback_engine_command,
        settings.verify_fallback_engine_command,
    ]
    found = []
    for name in ("gh", "git"):
        resolved = shutil.which(name)
        if resolved:
            found.append(str(Path(resolved).parent))
    for command in commands:
        if command and os.sep in command[0]:
            found.append(str(Path(command[0]).parent))
    return found


def run_environment(settings: Settings, home: Path) -> "dict[str, str]":
    """Return the environment a scheduled run needs and nothing else.

    PATH names where GitHub CLI, Git, and every configured AI CLI live, because a
    scheduler does not read the shell's startup files.
    """

    directories = _executable_directories(settings)
    directories += [str(home / ".local" / "bin"), *STANDARD_PATH]
    path = []
    for directory in directories:
        if directory not in path:
            path.append(directory)
    environment = {"HOME": str(home), "PATH": os.pathsep.join(path)}
    for name in LOCATION_VARIABLES:
        value = os.environ.get(name, "").strip()
        if value:
            environment[name] = value
    if is_source_checkout(PACKAGE_ROOT):
        environment["PYTHONPATH"] = str(PACKAGE_ROOT)
    return environment


def protected_paths(settings: Settings, home: Path) -> "list[Path]":
    """Return configured paths inside folders macOS guards with a consent prompt."""

    guarded = [home / folder for folder in PROTECTED_FOLDERS]
    paths = [settings.state_file] + [
        repository.path for repository in settings.repositories if repository.path
    ]
    if is_source_checkout(PACKAGE_ROOT):
        paths.append(PACKAGE_ROOT)
    return sorted(
        {
            path
            for path in paths
            if any(folder == path or folder in path.parents for folder in guarded)
        }
    )


# launchd -----------------------------------------------------------------------


def launchd_plist_path(home: Path) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_log_path(home: Path) -> Path:
    return home / "Library" / "Logs" / "prwarden.log"


def launchd_plist(
    command: Sequence[str],
    directory: Path,
    environment: Mapping[str, str],
    slots: Slots,
    home: Path,
) -> "dict[str, object]":
    """Return the LaunchAgent for the schedule.

    `caffeinate` holds the Mac awake for exactly as long as the run takes: a Mac
    asleep at the slot runs the job at its next wake, often a DarkWake shorter than
    one review, and the review CLIs would die with it.
    """

    # launchd counts Sunday as 0 and Monday as 1; DAY_NAMES starts at Monday.
    intervals = [
        {"Weekday": (day + 1) % 7, "Hour": hour, "Minute": minute}
        for day in slots.days
        for hour, minute in slots.times
    ]
    log = str(launchd_log_path(home))
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": ["/usr/bin/caffeinate", "-i", "-s", *command],
        "WorkingDirectory": str(directory),
        "EnvironmentVariables": dict(environment),
        "StartCalendarInterval": intervals,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
        "Umask": 0o077,
    }


def launchd_slots(plist: Mapping[str, object]) -> Optional[Slots]:
    intervals = plist.get("StartCalendarInterval")
    if not isinstance(intervals, list):
        return None
    days = set()
    times = set()
    for interval in intervals:
        if not isinstance(interval, dict):
            return None
        days.add((int(interval.get("Weekday", 0)) - 1) % 7)
        times.add((int(interval.get("Hour", 0)), int(interval.get("Minute", 0))))
    return Slots(tuple(sorted(days)), tuple(sorted(times)))


def other_launch_agents(home: Path) -> "list[Path]":
    """Return LaunchAgents other than this one that already run PR Warden."""

    found = []
    directory = home / "Library" / "LaunchAgents"
    for path in sorted(directory.glob("*.plist")) if directory.is_dir() else ():
        if path.name == f"{LAUNCHD_LABEL}.plist":
            continue
        try:
            with path.open("rb") as handle:
                arguments = plistlib.load(handle).get("ProgramArguments") or []
        except (OSError, ExpatError, plistlib.InvalidFileException, ValueError):
            # Not a property list this module can read, so not one it wrote.
            continue
        if any(
            isinstance(argument, str) and Path(argument).name == "prwarden"
            for argument in arguments
        ):
            found.append(path)
    return found


# systemd -----------------------------------------------------------------------


def systemd_directory(home: Path) -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(configured) if configured and Path(configured).is_absolute() else (
        home / ".config"
    )
    return base / "systemd" / "user"


def _systemd_quote(value: str, command: bool = False) -> str:
    """Quote one ExecStart argument or Environment assignment for systemd.

    Both expand `%` specifiers; only a command line also expands `$` variables.
    """

    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def systemd_units(
    command: Sequence[str],
    directory: Path,
    environment: Mapping[str, str],
    slots: Slots,
) -> "dict[str, str]":
    """Return the service and timer unit files for the schedule.

    `Persistent=true` runs a slot missed while the machine was off once it is back,
    as launchd does for a Mac that was asleep.
    """

    days = ",".join(DAY_NAMES[day].capitalize() for day in slots.days)
    calendars = "".join(
        f"OnCalendar={days} *-*-* {hour:02d}:{minute:02d}:00\n"
        for hour, minute in slots.times
    )
    assignments = " ".join(
        _systemd_quote(f"{name}={value}") for name, value in environment.items()
    )
    executable = " ".join(_systemd_quote(argument, True) for argument in command)
    service = (
        "[Unit]\n"
        "Description=PR Warden pull-request review run\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={str(directory).replace('%', '%%')}\n"
        f"Environment={assignments}\n"
        "UMask=0077\n"
        f"ExecStart={executable}\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Run PR Warden on its schedule\n\n"
        "[Timer]\n"
        f"{calendars}"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {f"{SYSTEMD_UNIT}.service": service, f"{SYSTEMD_UNIT}.timer": timer}


def systemd_slots(timer: str) -> Optional[Slots]:
    days: "set[int]" = set()
    times = set()
    for line in timer.splitlines():
        if not line.startswith("OnCalendar="):
            continue
        spec = line.split("=", 1)[1].split()
        if len(spec) != 3:
            return None
        for name in spec[0].split(","):
            days.add(DAY_NAMES.index(name.lower()))
        hour, minute, _ = spec[2].split(":")
        times.add((int(hour), int(minute)))
    if not times:
        return None
    return Slots(tuple(sorted(days)), tuple(sorted(times)))


# commands ----------------------------------------------------------------------


def _write_atomically(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _platform() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise ScheduleError(
        f"Scheduling is supported on macOS and Linux, not {sys.platform}"
    )


def _require_live(settings: Settings, locations: Locations) -> None:
    if settings.dry_run:
        raise ScheduleError(
            "A schedule always runs live, and AUTO_REVIEW_DRY_RUN is not 0 in "
            f"{locations.env_file}. Preview one pull request first with "
            "`prwarden --repo OWNER/NAME --pr NUMBER`, then set "
            "AUTO_REVIEW_DRY_RUN=0 and install the schedule again."
        )


def install(
    args: argparse.Namespace,
    print_line: Callable[[str], None] = print,
    home: Optional[Path] = None,
) -> int:
    home = Path.home() if home is None else home
    slots = Slots(parse_days(args.days), parse_times(args.at))
    platform = _platform()
    locations = default_locations()
    settings = load_settings(args.env_file)
    if not args.print:
        _require_live(settings, locations)
    command, directory = run_command(home, args.env_file)
    environment = run_environment(settings, home)

    if platform == "launchd":
        others = other_launch_agents(home)
        if others and not args.print:
            listed = ", ".join(str(path) for path in others)
            raise ScheduleError(
                f"Another LaunchAgent already runs PR Warden: {listed}. Two "
                "schedules would review the same pull requests; remove it first "
                f'with `launchctl bootout "gui/$(id -u)" PLIST` and deleting it.'
            )
        plist = launchd_plist(command, directory, environment, slots, home)
        content = plistlib.dumps(plist)
        path = launchd_plist_path(home)
        if args.print:
            print_line(content.decode("utf-8"))
            return 0
        domain = f"gui/{os.getuid()}"
        try:
            run_cmd(["launchctl", "bootout", f"{domain}/{LAUNCHD_LABEL}"])
        except CommandError:
            pass  # Not loaded yet, which is the usual case.
        launchd_log_path(home).parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(path, content)
        run_cmd(["launchctl", "bootstrap", domain, str(path)])
        print_line(f"Installed {path}")
        print_line(f"Runs {slots.describe()}, live: {shlex.join(command)}")
        print_line(f"Log: {launchd_log_path(home)}")
    else:
        units = systemd_units(command, directory, environment, slots)
        if args.print:
            for name, text in units.items():
                print_line(f"# {name}\n{text}")
            return 0
        unit_directory = systemd_directory(home)
        for name, text in units.items():
            _write_atomically(unit_directory / name, text.encode("utf-8"))
        run_cmd(["systemctl", "--user", "daemon-reload"])
        run_cmd(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"])
        print_line(f"Installed {unit_directory / (SYSTEMD_UNIT + '.timer')}")
        print_line(f"Runs {slots.describe()}, live: {shlex.join(command)}")
        print_line(f"Log: journalctl --user -u {SYSTEMD_UNIT}.service")
        _report_linger(print_line)
    guarded = protected_paths(settings, home) if platform == "launchd" else []
    for path in guarded:
        print_line(
            f"Note: {path} is in a folder macOS asks about before a program reads "
            "it; answer that prompt once, or a scheduled run waits at it."
        )
    return 0


def _report_linger(print_line: Callable[[str], None]) -> None:
    user = os.environ.get("USER", "")
    try:
        linger = run_cmd(["loginctl", "show-user", user, "--property=Linger"])
    except CommandError:
        return
    if linger.strip() == "Linger=no":
        print_line(
            "Note: systemd runs your timers only while you are logged in. To run "
            f"them at other times too: loginctl enable-linger {user}"
        )


def status(
    args: argparse.Namespace,
    print_line: Callable[[str], None] = print,
    home: Optional[Path] = None,
) -> int:
    """Print whether a schedule is installed and when it runs; 1 when there is none."""

    home = Path.home() if home is None else home
    if _platform() == "launchd":
        path = launchd_plist_path(home)
        for other in other_launch_agents(home):
            print_line(f"Also running PR Warden: {other}")
        if not path.exists():
            print_line(NOT_INSTALLED)
            return 1
        with path.open("rb") as handle:
            plist = plistlib.load(handle)
        slots = launchd_slots(plist)
        print_line(f"Schedule: {path}")
        if slots:
            print_line(f"Runs {slots.describe()}")
        arguments = [str(argument) for argument in plist.get("ProgramArguments", [])]
        print_line(f"Command: {shlex.join(arguments)}")
        try:
            details = run_cmd(
                ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"]
            )
        except CommandError:
            print_line("Loaded: no. Load it again with: prwarden schedule install")
            return 1
        exit_code = re.search(r"last exit code = (.+)", details)
        print_line(
            "Loaded: yes"
            + (f"; last exit code {exit_code.group(1).strip()}" if exit_code else "")
        )
        print_line(f"Log: {launchd_log_path(home)}")
        return 0
    timer = systemd_directory(home) / f"{SYSTEMD_UNIT}.timer"
    if not timer.exists():
        print_line(NOT_INSTALLED)
        return 1
    slots = systemd_slots(timer.read_text(encoding="utf-8"))
    print_line(f"Schedule: {timer}")
    if slots:
        print_line(f"Runs {slots.describe()}")
    try:
        details = run_cmd(
            [
                "systemctl", "--user", "show", f"{SYSTEMD_UNIT}.timer",
                "--property=ActiveState", "--property=NextElapseUSecRealtime",
            ]
        )
        print_line(" ".join(details.split()))
    except CommandError as error:
        print_line(f"systemd could not report on it: {error}")
        return 1
    print_line(f"Log: journalctl --user -u {SYSTEMD_UNIT}.service")
    return 0


def remove(
    args: argparse.Namespace,
    print_line: Callable[[str], None] = print,
    home: Optional[Path] = None,
) -> int:
    home = Path.home() if home is None else home
    if _platform() == "launchd":
        path = launchd_plist_path(home)
        try:
            run_cmd(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
        except CommandError:
            pass  # Already unloaded.
        if path.exists():
            path.unlink()
            print_line(f"Removed {path}")
        else:
            print_line("No schedule was installed")
        return 0
    directory = systemd_directory(home)
    timer = directory / f"{SYSTEMD_UNIT}.timer"
    if not timer.exists():
        print_line("No schedule was installed")
        return 0
    try:
        run_cmd(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.timer"])
    except CommandError:
        pass  # Already disabled.
    for name in (f"{SYSTEMD_UNIT}.timer", f"{SYSTEMD_UNIT}.service"):
        (directory / name).unlink(missing_ok=True)
    run_cmd(["systemctl", "--user", "daemon-reload"])
    print_line(f"Removed {timer}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prwarden schedule",
        description="Run PR Warden live on a schedule: a LaunchAgent on macOS, a "
        "systemd user timer on Linux.",
    )
    parser.add_argument("--env-file", type=Path, help="dotenv file to load")
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser(
        "install", help="install or update the schedule"
    )
    install_parser.add_argument(
        "--at",
        default=",".join(DEFAULT_TIMES),
        help=f"comma-separated 24-hour times (default {','.join(DEFAULT_TIMES)})",
    )
    install_parser.add_argument(
        "--days",
        default=DEFAULT_DAYS,
        help="days, such as mon-fri, daily, or mon,wed,fri (default mon-fri)",
    )
    install_parser.add_argument(
        "--print",
        action="store_true",
        help="print what would be installed and change nothing",
    )
    commands.add_parser("status", help="show the installed schedule")
    commands.add_parser("remove", help="stop and remove the schedule")
    return parser


def main(argv: Sequence[str]) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"install": install, "status": status, "remove": remove}
    try:
        return handlers[args.command](args)
    except (ScheduleError, ConfigError, CommandError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2 if isinstance(error, (ScheduleError, ConfigError)) else 1
