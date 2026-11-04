"""`prwarden setup`: write a working configuration by asking, or import a clone's.

It asks which GitHub account reviews, which AI CLI reviews and which verifies, and
which repositories to watch, then writes `.env` and `repositories.json` into the
configuration directory. A repository named without a local clone is cloned by PR
Warden on its first review, and setup says where and roughly how large before
anyone agrees to it. It ends dry: a preview of one pull request costs credits and
posts nothing, and going live and installing a schedule are each asked for.

`--import CHECKOUT` copies a source checkout's configuration and delivery records
instead, so an installed copy carries on where the clone left off without
reviewing anything twice.
"""

import argparse
import getpass
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from . import __version__, github, schedule
from .commands import CommandError, run_cmd
from .config import (
    DEFAULT_ENGINE_COMMANDS,
    ConfigError,
    default_locations,
    load_env_file,
    load_repositories,
    managed_checkout_path,
    resolve_path,
)
from .models import Repository
from .paths import Locations


ENV_REFERENCE = "https://github.com/bruha/prwarden/blob/main/.env.example"
ENGINE_ORDER = ("antigravity", "claude", "codex", "kiro")


class SetupError(RuntimeError):
    """Raised when setup cannot continue."""


class Prompter:
    """Asks the operator, through functions a test can replace."""

    def __init__(
        self,
        ask: Callable[[str], str] = input,
        ask_secret: Callable[[str], str] = getpass.getpass,
        say: Callable[[str], None] = print,
    ):
        self._ask = ask
        self._ask_secret = ask_secret
        self.say = say

    def text(self, question: str, default: str = "") -> str:
        shown = f" [{default}]" if default else ""
        answer = self._ask(f"{question}{shown}: ").strip()
        return answer or default

    def secret(self, question: str) -> str:
        return self._ask_secret(f"{question}: ").strip()

    def confirm(self, question: str, default: bool = False) -> bool:
        while True:
            answer = self._ask(f"{question} [{'Y/n' if default else 'y/N'}]: ")
            answer = answer.strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self.say("Answer y or n.")

    def choose(self, question: str, choices: Sequence[str], default: str) -> str:
        while True:
            answer = self.text(question, default)
            if answer in choices:
                return answer
            self.say(f"Choose one of: {', '.join(choices)}")


# What there is to choose from --------------------------------------------------


def github_accounts() -> "tuple[List[str], Optional[str]]":
    """Return GitHub CLI's signed-in accounts and the active one."""

    try:
        payload = json.loads(run_cmd(["gh", "auth", "status", "--json", "hosts"]))
        entries = payload["hosts"]["github.com"]
    except (CommandError, ValueError, KeyError, TypeError) as error:
        raise SetupError(
            f"GitHub CLI is not signed in to github.com; run `gh auth login`: {error}"
        ) from error
    logins = [
        entry["login"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("state") == "success"
    ]
    active = next(
        (entry["login"] for entry in entries if entry.get("active")), None
    )
    if not logins:
        raise SetupError("GitHub CLI holds no working login; run `gh auth login`")
    return logins, active if active in logins else logins[0]


def installed_engines() -> "List[str]":
    """Return the built-in engines whose CLI is installed, in recommended order."""

    found = []
    for engine in ENGINE_ORDER:
        name = DEFAULT_ENGINE_COMMANDS[engine][0]
        if shutil.which(name) or (Path.home() / ".local" / "bin" / name).exists():
            found.append(engine)
    return found


def describe_size(kilobytes: Optional[int]) -> str:
    if kilobytes is None:
        return "size unknown"
    if kilobytes >= 1024 * 1024:
        return f"about {kilobytes / (1024 * 1024):.1f} GB"
    return f"about {max(1, round(kilobytes / 1024))} MB"


# Writing -----------------------------------------------------------------------


def _backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.name}.before-setup-{stamp}")
    shutil.copy2(path, backup)
    return backup


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` readable by its owner alone, replacing ``path`` atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def render_env(values: "dict[str, str]") -> str:
    lines = [
        f"# Written by `prwarden setup` {__version__} on "
        f"{datetime.now().strftime('%Y-%m-%d')}.",
        f"# Every setting is described in {ENV_REFERENCE}",
        "",
        "# Two delivery gates: set this to 0 and also pass --live.",
    ]
    for key, value in values.items():
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def set_env_value(path: Path, key: str, value: str) -> None:
    """Set one setting in an existing `.env`, keeping every other line as it is."""

    lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        name = line.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[7:].strip()
        if not line.lstrip().startswith("#") and name == key:
            lines[index] = f"{key}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{key}={value}")
    _write_private(path, "\n".join(lines) + "\n")


def write_configuration(
    locations: Locations,
    env_values: "dict[str, str]",
    entries: "List[dict]",
    say: Callable[[str], None],
) -> None:
    for path in (locations.env_file, locations.repositories_file):
        backup = _backup(path)
        if backup:
            say(f"Kept the previous {path.name} as {backup}")
    _write_private(locations.env_file, render_env(env_values))
    _write_private(
        locations.repositories_file, json.dumps(entries, indent=2) + "\n"
    )
    say(f"Wrote {locations.env_file} and {locations.repositories_file}")


# The questions -----------------------------------------------------------------


def ask_reviewer(prompter: Prompter) -> "tuple[str, List[str]]":
    logins, active = github_accounts()
    prompter.say(f"GitHub CLI is signed in as: {', '.join(logins)}")
    prompter.say(
        "PR Warden posts as one of them and never approves that account's own pull "
        "requests, so a separate reviewer account works best."
    )
    return prompter.choose("Review as", logins, active or logins[0]), logins


def ask_engines(prompter: Prompter) -> "dict[str, str]":
    engines = installed_engines()
    if not engines:
        raise SetupError(
            "No supported AI CLI was found: install and sign in to one of kiro-cli, "
            "claude, codex, or agy, then run setup again"
        )
    prompter.say(f"AI CLIs found: {', '.join(engines)}")
    review = prompter.choose("Review with", engines, engines[0])
    others = [engine for engine in engines if engine != review]
    values = {"REVIEW_ENGINE": review}
    if others:
        prompter.say(
            "A second engine tries to refute each finding, so one model is not the "
            "only judge of its own output."
        )
        verify = prompter.choose(
            "Verify with (or none)", [*others, review, "none"], others[0]
        )
        if verify != "none":
            values["REVIEW_VERIFY_ENGINE"] = verify
    return values


def ask_repositories(
    prompter: Prompter, reviewer: str, logins: Sequence[str], locations: Locations
) -> "List[dict]":
    entries: "List[dict]" = []
    while not entries:
        answer = prompter.text("Repositories to review, as owner/name, space-separated")
        for slug in answer.replace(",", " ").split():
            entry = ask_repository(prompter, slug, reviewer, logins, locations)
            if entry:
                entries.append(entry)
        if not entries:
            prompter.say("Name at least one repository PR Warden can read.")
    return entries


def ask_repository(
    prompter: Prompter,
    slug: str,
    reviewer: str,
    logins: Sequence[str],
    locations: Locations,
) -> Optional[dict]:
    try:
        managed = managed_checkout_path(locations.checkouts_directory, slug)
    except ConfigError as error:
        prompter.say(f"Skipped {slug}: {error}")
        return None
    account = None
    for candidate in [reviewer, *[login for login in logins if login != reviewer]]:
        repository = Repository(
            slug.split("/")[1],
            slug,
            managed,
            github_account=None if candidate == reviewer else candidate,
            managed_checkout=True,
        )
        github.pin_github_account(reviewer, [repository])
        try:
            github.repository_reachable(repository)
        except CommandError:
            continue
        account = candidate
        break
    if account is None:
        prompter.say(f"Skipped {slug}: none of {', '.join(logins)} can read it")
        return None
    if account != reviewer:
        prompter.say(
            f"{slug} is readable as {account} only, so it is read and reviewed as "
            f"{account}."
        )
    entry: dict = {"slug": slug}
    if account != reviewer:
        entry["github_account"] = account
    size = describe_size(github.repository_size(repository))
    path = prompter.text(
        f"{slug}: path of a local clone, or Enter to let PR Warden clone it into "
        f"{managed} ({size})"
    )
    if path:
        resolved = resolve_path(path, Path.cwd())
        if not resolved.is_dir():
            prompter.say(f"{resolved} does not exist; PR Warden will clone it instead")
        else:
            entry["path"] = str(resolved)
    return entry


def ask_slack(prompter: Prompter, entries: "List[dict]") -> Optional[str]:
    prompter.say(
        "Slack is optional. It receives the reviews of your own pull requests, which "
        "GitHub does not let you approve, and failure alerts."
    )
    webhook = prompter.secret("Slack incoming webhook URL, or Enter to skip")
    if not webhook:
        for entry in entries:
            entry["slack"] = False
        return None
    if not webhook.startswith("https://hooks.slack.com/"):
        prompter.say("That does not look like a Slack webhook; Slack is left off.")
        for entry in entries:
            entry["slack"] = False
        return None
    return webhook


def preview(prompter: Prompter, locations: Locations) -> None:
    """Offer one dry-run review, which costs credits and posts nothing."""

    if not prompter.confirm(
        "Preview one pull request now as a dry run? The model runs and spends "
        "credits; nothing is posted"
    ):
        return
    slug = prompter.text("Repository, as owner/name")
    number = prompter.text("Pull request number")
    if not slug or not number.isdigit():
        prompter.say("Skipped the preview.")
        return
    from . import cli

    cli.main(["--repo", slug, "--pr", number])


def go_live(prompter: Prompter, locations: Locations) -> None:
    if not prompter.confirm(
        "Post reviews from now on? This sets AUTO_REVIEW_DRY_RUN=0; each run still "
        "needs --live"
    ):
        prompter.say(
            f"Left dry. Set AUTO_REVIEW_DRY_RUN=0 in {locations.env_file} when ready."
        )
        return
    set_env_value(locations.env_file, "AUTO_REVIEW_DRY_RUN", "0")
    prompter.say("Live delivery is on.")
    if not prompter.confirm(
        "Review on a schedule, Mon-Fri at 08:00, 12:00, and 16:00?", default=True
    ):
        prompter.say("Add one later with: prwarden schedule install")
        return
    try:
        schedule.install(
            argparse.Namespace(
                env_file=None,
                days=schedule.DEFAULT_DAYS,
                at=",".join(schedule.DEFAULT_TIMES),
                print=False,
            ),
            prompter.say,
        )
    except (schedule.ScheduleError, CommandError) as error:
        prompter.say(f"No schedule was installed: {error}")


def interactive(prompter: Prompter, locations: Locations) -> int:
    prompter.say(f"PR Warden {__version__} setup")
    prompter.say(
        f"Configuration goes in {locations.config_directory}, review records in "
        f"{locations.state_directory}."
    )
    if locations.env_file.exists() or locations.repositories_file.exists():
        if not prompter.confirm(
            "A configuration exists there. Replace it? The old files are kept beside "
            "it"
        ):
            prompter.say("Nothing changed.")
            return 0
    reviewer, logins = ask_reviewer(prompter)
    env_values = {"AUTO_REVIEW_DRY_RUN": "1", "GITHUB_REVIEWER_USERNAME": reviewer}
    env_values.update(ask_engines(prompter))
    entries = ask_repositories(prompter, reviewer, logins, locations)
    webhook = ask_slack(prompter, entries)
    if webhook:
        env_values["SLACK_WEBHOOK_URL"] = webhook
    write_configuration(locations, env_values, entries, prompter.say)
    preview(prompter, locations)
    go_live(prompter, locations)
    prompter.say("Check everything at any time with: prwarden doctor")
    return 0


# Importing a clone -------------------------------------------------------------


def import_checkout(
    checkout: Path, locations: Locations, say: Callable[[str], None] = print
) -> int:
    """Copy a source checkout's configuration and delivery records here.

    Relative paths are made absolute against the checkout, so they keep meaning
    the same places. The delivery records come too, because without them every
    open pull request would be reviewed and posted to again.
    """

    checkout = checkout.expanduser().resolve()
    source_env = checkout / ".env"
    declared = load_env_file(source_env)
    configured_repositories = declared.get("REPOSITORIES_FILE", "repositories.json")
    source_repositories = resolve_path(configured_repositories, checkout)
    configured_state = declared.get("STATE_FILE", ".state/review-state.json")
    source_state = resolve_path(configured_state, checkout)
    if not source_repositories.exists():
        raise SetupError(f"{checkout} has no {configured_repositories} to import")
    if locations.config_directory == checkout:
        raise SetupError("That checkout is this installation's own configuration")
    for path in (locations.env_file, locations.repositories_file, locations.state_file):
        if path.exists():
            raise SetupError(
                f"{path} already exists; move it away first, so nothing is merged "
                "by accident"
            )

    entries = json.loads(source_repositories.read_text(encoding="utf-8"))
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            entry["path"] = str(resolve_path(entry["path"], source_repositories.parent))
    lines = []
    for raw in source_env.read_text(encoding="utf-8").splitlines():
        key = raw.split("=", 1)[0].strip()
        # The copied files are now where these defaults point.
        if key in ("REPOSITORIES_FILE", "STATE_FILE"):
            continue
        if key in ("GITHUB_COMMENT_TEMPLATE_FILE", "GITHUB_COMMENT_TAGS_FILE"):
            value = raw.split("=", 1)[1].strip().strip("'\"")
            raw = f"{key}={resolve_path(value, checkout)}"
        lines.append(raw)
    _write_private(locations.env_file, "\n".join(lines) + "\n")
    _write_private(locations.repositories_file, json.dumps(entries, indent=2) + "\n")
    load_repositories(locations.repositories_file, locations.checkouts_directory)
    say(f"Imported {source_env} and {source_repositories}")
    if source_state.exists():
        locations.state_directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_state, locations.state_file)
        os.chmod(locations.state_file, 0o600)
        say(f"Imported the delivery records in {source_state}")
    say(
        "Check it with `prwarden doctor`. The checkout's own schedule still runs: "
        "remove it before `prwarden schedule install`, so one schedule reviews."
    )
    return 0


def main(argv: Sequence[str], prompter: Optional[Prompter] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prwarden setup",
        description="Write PR Warden's configuration by asking, or import a clone's.",
    )
    parser.add_argument(
        "--import",
        dest="import_from",
        type=Path,
        metavar="CHECKOUT",
        help="copy a source checkout's .env, repositories, and delivery records",
    )
    args = parser.parse_args(argv)
    try:
        locations = default_locations()
        if args.import_from is not None:
            return import_checkout(args.import_from, locations)
        if prompter is None:
            if not sys.stdin.isatty():
                raise SetupError("setup asks questions; run it in a terminal")
            prompter = Prompter()
        return interactive(prompter, locations)
    except (SetupError, ConfigError, CommandError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nSetup stopped; files already written were kept.", file=sys.stderr)
        return 130
