"""`prwarden doctor`: check that this installation can review, without reviewing.

Every check reads; none posts, clones, or calls a model, so it spends no credits.
A problem is something that stops reviews or delivery and makes the command exit 1;
a note is worth knowing and changes nothing.
"""

import argparse
import os
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from . import MINIMUM_PYTHON, __version__, github, schedule, tools
from .commands import CommandError, run_cmd
from .config import ConfigError, Settings, default_locations, load_settings
from .paths import LAYOUT_CHECKOUT, LAYOUT_HOME, Locations
from .models import Repository
from .workspace import remote_matches_slug


OK, PROBLEM, NOTE = "ok", "!!", "--"
LAYOUT_NAMES = {
    LAYOUT_CHECKOUT: "a source checkout",
    LAYOUT_HOME: "PRWARDEN_HOME",
    "user": "an installed copy",
}


@dataclass(frozen=True)
class Check:
    status: str
    message: str


def _home_relative(path: Path) -> str:
    home = Path.home()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def check_env_file(env_file: Path) -> List[Check]:
    if not env_file.exists():
        shown = _home_relative(env_file)
        return [Check(NOTE, f"no {shown}; every setting is its default")]
    mode = stat.S_IMODE(env_file.stat().st_mode)
    if os.name != "nt" and mode != 0o600:
        return [
            Check(
                PROBLEM,
                f"{_home_relative(env_file)} has mode {mode:04o}; it holds secrets, "
                f"so run: chmod 600 {env_file}",
            )
        ]
    return [Check(OK, f"{_home_relative(env_file)} is private (0600)")]


def check_engines(
    settings: Settings, states: "Sequence[tools.ToolState]" = ()
) -> List[Check]:
    """Check that every configured engine's CLI is there and signed in."""

    signed_in = {
        state.tool.engine: state for state in states if state.tool.engine
    }
    roles = (
        ("review", settings.review_engine, settings.review_engine_command),
        ("verify", settings.verify_engine, settings.verify_engine_command),
        ("fallback", settings.fallback_engine, settings.fallback_engine_command),
        (
            "verify fallback",
            settings.verify_fallback_engine,
            settings.verify_fallback_engine_command,
        ),
    )
    checks = []
    for role, engine, command in roles:
        if not engine or not command:
            continue
        executable = command[0]
        if os.sep in executable:
            found = executable if os.access(executable, os.X_OK) else None
        else:
            found = shutil.which(executable)
        if not found:
            checks.append(
                Check(PROBLEM, f"{role} engine {engine}: {executable} was not found")
            )
            continue
        state = signed_in.get(engine)
        if state is not None and state.signed_in is False:
            login = " ".join([state.tool.command, *state.tool.login])
            checks.append(
                Check(
                    PROBLEM,
                    f"{role} engine {engine} is not signed in; sign in with: {login}",
                )
            )
        elif state is not None and state.signed_in is None:
            checks.append(
                Check(NOTE, f"{role} engine {engine}: could not tell if signed in")
            )
        else:
            checks.append(Check(OK, f"{role} engine {engine}: {found}"))
    return checks


def check_github(settings: Settings) -> List[Check]:
    if not shutil.which("gh"):
        return [Check(PROBLEM, "GitHub CLI (gh) was not found on PATH")]
    try:
        github.pin_github_account(settings.reviewer_username, settings.repositories)
    except CommandError as error:
        return [Check(PROBLEM, str(error))]
    return [
        Check(OK, f"reviewer {settings.reviewer_username} is signed in to GitHub CLI")
    ]


def check_repository(repository: Repository) -> List[Check]:
    label = repository.slug
    problem = github.repository_account_problem(repository)
    if problem:
        return [Check(PROBLEM, f"{label}: {problem}")]
    account = f" as {repository.github_account}" if repository.github_account else ""
    try:
        github.repository_reachable(repository)
    except CommandError as error:
        return [Check(PROBLEM, f"{label}: GitHub cannot be read{account}: {error}")]
    path = repository.path
    if path is None:
        return [Check(PROBLEM, f"{label}: no local checkout is configured")]
    if repository.managed_checkout:
        state = "cloned" if (path / ".git").exists() else "cloned on its first review"
        return [
            Check(OK, f"{label}: readable{account}; PR Warden's own clone, {state}")
        ]
    if not path.is_dir():
        return [Check(PROBLEM, f"{label}: the checkout {path} does not exist")]
    try:
        remote = run_cmd(["git", "-C", str(path), "remote", "get-url", "origin"])
    except CommandError as error:
        return [Check(PROBLEM, f"{label}: {path} is not a usable clone: {error}")]
    if not remote_matches_slug(remote, repository.slug):
        return [Check(PROBLEM, f"{label}: {path} is a clone of {remote.strip()}")]
    return [Check(OK, f"{label}: readable{account}; clone {_home_relative(path)}")]


def check_jira(
    settings: Settings, check: "Callable[[tools.Tool], tools.ToolState]" = tools.check
) -> List[Check]:
    """Check `acli` when a repository reads its pull requests' Jira work items.

    A note, never a problem: a work item that cannot be read is left out of the
    review, which goes ahead as it did before work items were read.
    """

    using = [
        repository for repository in settings.repositories if repository.jira_projects
    ]
    if not using:
        return []
    count = (
        f"{len(using)} of {len(settings.repositories)} repositories name Jira projects"
    )
    state = check(tools.JIRA_TOOL)
    if not state.installed:
        return [
            Check(
                NOTE,
                f"Jira: {count}, but acli is not installed, so reviews read no work "
                "items; install it with: brew install atlassian/acli/acli",
            )
        ]
    if state.signed_in is not True:
        return [
            Check(
                NOTE,
                f"Jira: {count}, but acli is not signed in, so reviews read no work "
                "items; sign in with: acli jira auth login --web",
            )
        ]
    return [Check(OK, f"Jira: {count}; acli is signed in")]


def check_delivery(settings: Settings, locations: Locations) -> List[Check]:
    checks = []
    if settings.dry_run:
        checks.append(
            Check(
                NOTE,
                "dry run: reviews post nothing until AUTO_REVIEW_DRY_RUN=0 is set "
                f"in {_home_relative(locations.env_file)} and --live is passed",
            )
        )
    else:
        checks.append(Check(OK, "live delivery is enabled; runs with --live post"))
    slack = [repository for repository in settings.repositories if repository.slack]
    total = len(settings.repositories)
    if slack and not settings.slack_webhook_url:
        checks.append(
            Check(
                NOTE,
                f"no SLACK_WEBHOOK_URL, so {len(slack)} repositories with Slack on "
                "report only to the terminal; connect Slack with `prwarden setup "
                '--slack`, or set "slack": false to say so',
            )
        )
    elif settings.slack_webhook_url:
        checks.append(
            Check(
                OK,
                f"Slack: {len(slack)} of {total} repositories report to the webhook; "
                "`prwarden setup --slack` replaces it",
            )
        )
    return checks


def check_schedule() -> List[Check]:
    lines: List[str] = []
    try:
        installed = schedule.status(argparse.Namespace(), lines.append) == 0
    except (schedule.ScheduleError, CommandError, OSError) as error:
        return [Check(NOTE, f"schedule: {error}")]
    others = [
        line.split(": ", 1)[1] for line in lines if line.startswith("Also running")
    ]
    if installed and others:
        # Two schedules review the same pull requests at every slot.
        return [
            Check(PROBLEM, f"schedule: {other} runs PR Warden as well")
            for other in others
        ]
    if installed:
        runs = next((line for line in lines if line.startswith("Runs ")), "Runs")
        return [Check(OK, f"schedule: r{runs[1:]}")] + check_schedule_python()
    if others:
        return [Check(OK, f"schedule: {other} runs PR Warden") for other in others]
    return [Check(NOTE, "no schedule; add one with: prwarden schedule install")]


# The command that prints an interpreter's version, run with the scheduled Python.
PYTHON_VERSION_PROBE = "import sys; print('%d.%d' % sys.version_info[:2])"


def check_schedule_python(
    home: Optional[Path] = None,
    run: "Callable[..., str]" = run_cmd,
) -> List[Check]:
    """Check that the schedule's Python is one PR Warden still runs on.

    A schedule installed from a clone keeps the interpreter that installed it. One
    older than the floor stops at the version check on every slot, before anything
    could send an error notification, so this is the only place it shows.
    """

    try:
        command = schedule.scheduled_command(home)
    except (schedule.ScheduleError, OSError, ValueError) as error:
        return [Check(NOTE, f"schedule: cannot read its command: {error}")]
    interpreter = schedule.command_interpreter(command or ())
    if interpreter is None:
        return []
    floor = "{0}.{1}".format(*MINIMUM_PYTHON)
    try:
        version = run([interpreter, "-c", PYTHON_VERSION_PROBE], timeout_seconds=30)
        parsed = tuple(int(part) for part in version.strip().split("."))
    except (CommandError, OSError, ValueError) as error:
        return [
            Check(
                PROBLEM,
                f"schedule: its Python, {interpreter}, does not run: {error}",
            )
        ]
    if parsed >= MINIMUM_PYTHON:
        return []
    return [
        Check(
            PROBLEM,
            f"schedule: runs Python {version.strip()} ({interpreter}), and PR Warden "
            f"needs {floor} or newer, so every scheduled run stops at once; run "
            f"`prwarden schedule install` again with Python {floor} or newer",
        )
    ]


def diagnose(env_file: Optional[Path]) -> "tuple[List[str], List[Check]]":
    """Return the header lines and the checks for this installation."""

    try:
        locations = default_locations()
    except ConfigError as error:
        return [f"PR Warden {__version__}"], [Check(PROBLEM, str(error))]
    header = [
        f"PR Warden {__version__}, "
        f"{LAYOUT_NAMES.get(locations.layout, locations.layout)}",
        f"  configuration  {_home_relative(locations.config_directory)}",
        f"  state          {_home_relative(locations.state_directory)}",
        f"  managed clones {_home_relative(locations.checkouts_directory)}",
    ]
    states = tools.check_all()
    header += [f"  {line}" for line in tools.render_table(states)]
    checks = check_env_file(env_file or locations.env_file)
    if any(check.status == PROBLEM for check in checks):
        return header, checks
    try:
        settings = load_settings(env_file)
    except (ConfigError, CommandError) as error:
        return header, checks + [Check(PROBLEM, str(error))]
    checks += check_engines(settings, states)
    github_checks = check_github(settings)
    checks += github_checks
    if all(check.status != PROBLEM for check in github_checks):
        for repository in settings.repositories:
            checks += check_repository(repository)
    checks += check_jira(settings)
    checks += check_delivery(settings, locations)
    checks += check_schedule()
    return header, checks


def main(
    argv: Sequence[str], print_line: Callable[[str], None] = print
) -> int:
    parser = argparse.ArgumentParser(
        prog="prwarden doctor",
        description="Check this installation without reviewing, posting, or "
        "spending credits.",
    )
    parser.add_argument("--env-file", type=Path, help="dotenv file to load")
    args = parser.parse_args(argv)
    header, checks = diagnose(args.env_file)
    for line in header:
        print_line(line)
    for check in checks:
        print_line(f"{check.status}  {check.message}")
    problems = sum(1 for check in checks if check.status == PROBLEM)
    if problems:
        print_line(f"{problems} problem{'s' if problems != 1 else ''} found")
        return 1
    print_line("Ready to review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
