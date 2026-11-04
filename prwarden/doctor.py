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

from . import __version__, github, schedule
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


def check_engines(settings: Settings) -> List[Check]:
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
        if found:
            checks.append(Check(OK, f"{role} engine {engine}: {found}"))
        else:
            checks.append(
                Check(PROBLEM, f"{role} engine {engine}: {executable} was not found")
            )
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
    if slack and not settings.slack_webhook_url:
        checks.append(
            Check(
                NOTE,
                f"no SLACK_WEBHOOK_URL, so {len(slack)} repositories with Slack on "
                'report only to the terminal; set "slack": false to say so',
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
        return [Check(OK, f"schedule: r{runs[1:]}")]
    if others:
        return [Check(OK, f"schedule: {other} runs PR Warden") for other in others]
    return [Check(NOTE, "no schedule; add one with: prwarden schedule install")]


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
    checks = check_env_file(env_file or locations.env_file)
    if any(check.status == PROBLEM for check in checks):
        return header, checks
    try:
        settings = load_settings(env_file)
    except (ConfigError, CommandError) as error:
        return header, checks + [Check(PROBLEM, str(error))]
    checks += check_engines(settings)
    github_checks = check_github(settings)
    checks += github_checks
    if all(check.status != PROBLEM for check in github_checks):
        for repository in settings.repositories:
            checks += check_repository(repository)
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
