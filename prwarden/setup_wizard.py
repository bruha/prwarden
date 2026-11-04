"""`prwarden setup`: write a working configuration by asking, or import a clone's.

It asks which GitHub account reviews, which AI CLI reviews and which verifies, and
which repositories to watch, then writes `.env` and `repositories.json` into the
configuration directory. A repository named without a local clone is cloned by PR
Warden on its first review, and setup says where and roughly how large before
anyone agrees to it. Slack is offered for the reviews of the operator's own pull
requests, with Slack's page for a new app opened already filled in, and a webhook
is kept only once Slack has taken a test message through it. It ends dry: a
preview of one pull request costs credits and posts nothing, and going live and
installing a schedule are each asked for.

`--slack` connects Slack to an existing configuration the same way, and
`--import CHECKOUT` copies a source checkout's configuration and delivery records
instead, so an installed copy carries on where the clone left off without
reviewing anything twice.
"""

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from . import __version__, delivery, github, schedule, tools
from .commands import CommandError, run_cmd
from .config import (
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

# An app's incoming webhook. A Workflow Builder webhook, under /triggers/, takes
# its own variables rather than a message, so it cannot carry a review.
SLACK_WEBHOOK = re.compile(
    r"https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+"
)
SLACK_NEW_APP_PAGE = "https://api.slack.com/apps?new_app=1&manifest_json="
# A Slack app that can do one thing: post to the channel picked when installing it.
SLACK_APP_MANIFEST = {
    "_metadata": {"major_version": 1, "minor_version": 1},
    "display_information": {
        "name": "PR Warden",
        "description": "Reviews of your own pull requests, and failed runs, from "
        "PR Warden.",
    },
    # Slack refuses a manifest that asks for a bot scope without a bot user.
    "features": {"bot_user": {"display_name": "PR Warden", "always_online": False}},
    "oauth_config": {"scopes": {"bot": ["incoming-webhook"]}},
    "settings": {
        "incoming_webhooks": {"incoming_webhooks_enabled": True},
        "org_deploy_enabled": False,
        "socket_mode_enabled": False,
        "token_rotation_enabled": False,
    },
}
# Sent before any repository has been chosen for Slack, so it names none.
SLACK_TEST_MESSAGE = (
    "PR Warden is connected. The reviews of your own pull requests, and alerts "
    "when a run fails, arrive in this channel."
)


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


# The tools ---------------------------------------------------------------------


def _install(
    prompter: Prompter, tool: "tools.Tool", run: "tools.Runner"
) -> "tools.ToolState":
    """Install one tool with its vetted command once the operator agrees."""

    plan = tools.install_plan(tool)
    if plan.command is None:
        prompter.say(f"{tool.name}: {plan.description}")
        return tools.check(tool)
    prompter.say(f"{tool.name} installs with: {plan.description}")
    if not prompter.confirm(f"Run it now to install {tool.name}?"):
        return tools.check(tool)
    code = run(plan.command)
    state = tools.check(tool)
    if not state.installed:
        prompter.say(
            f"{tool.name} is still not installed (the installer exited {code}); see "
            f"{tool.instructions}"
        )
        return state
    version = f" {state.version}" if state.version else ""
    prompter.say(f"{tool.name}{version} is installed.")
    if state.signed_in is False:
        state = _sign_in(prompter, state, run)
    return state


def _sign_in(
    prompter: Prompter, state: "tools.ToolState", run: "tools.Runner"
) -> "tools.ToolState":
    """Run a tool's own sign-in in this terminal, then check it took."""

    tool = state.tool
    if not prompter.confirm(
        f"Sign in to {tool.name} now? {tool.login_hint}", default=True
    ):
        return state
    run(tools.login_command(state))
    after = tools.check(tool)
    if after.signed_in:
        prompter.say(f"{tool.name} is signed in.")
    else:
        prompter.say(f"{tool.name} is still not signed in.")
    return after


def _numbers(answer: str, count: int) -> "List[int]":
    chosen = []
    for part in answer.replace(",", " ").split():
        if part.isdigit() and 1 <= int(part) <= count and int(part) - 1 not in chosen:
            chosen.append(int(part) - 1)
    return chosen


def ensure_tools(
    prompter: Prompter,
    run: "tools.Runner" = tools.run_in_terminal,
    check_all: "Callable[[], List[tools.ToolState]]" = tools.check_all,
) -> "List[tools.ToolState]":
    """Check every tool, and install and sign in to what the operator chooses.

    GitHub CLI is required. Of the AI CLIs at least one must end up signed in, and
    two from different vendors are suggested, so one can verify the other.
    """

    prompter.say("Checking the tools PR Warden uses...")
    states = {state.tool.key: state for state in check_all()}
    for line in tools.render_table(list(states.values())):
        prompter.say(f"  {line}")

    gh = states["gh"]
    if not gh.installed:
        prompter.say("GitHub CLI is required: PR Warden reads and posts through it.")
        gh = states["gh"] = _install(prompter, gh.tool, run)
    if not gh.installed:
        raise SetupError("GitHub CLI is required; install it and run setup again")
    if not gh.signed_in:
        gh = states["gh"] = _sign_in(prompter, gh, run)
    if not gh.signed_in:
        raise SetupError("Sign in with `gh auth login` and run setup again")

    engines = [state for state in states.values() if state.tool.engine]
    missing = [state.tool for state in engines if not state.installed]
    ready = [state for state in engines if state.installed and state.signed_in]
    if missing:
        if len(ready) < 2:
            prompter.say(
                "Two AI CLIs from different companies let one verify what the other "
                "finds. Each needs its own account or subscription."
            )
        for index, tool in enumerate(missing, 1):
            prompter.say(f"  {index}. {tool.name}")
        answer = prompter.text(
            "Install which? Numbers separated by spaces, or Enter for none"
        )
        for index in _numbers(answer, len(missing)):
            states[missing[index].key] = _install(prompter, missing[index], run)
    for state in list(states.values()):
        if state.tool.engine and state.installed and state.signed_in is False:
            states[state.tool.key] = _sign_in(prompter, state, run)
    return list(states.values())


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


def usable_engines(states: "Sequence[tools.ToolState]") -> "List[str]":
    """Return the engines whose CLI is installed and not known to be signed out."""

    usable = {
        state.tool.engine
        for state in states
        if state.tool.engine and state.installed and state.signed_in is not False
    }
    return [engine for engine in ENGINE_ORDER if engine in usable]


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


def ask_engines(
    prompter: Prompter, states: "Sequence[tools.ToolState]"
) -> "dict[str, str]":
    engines = usable_engines(states)
    if not engines:
        raise SetupError(
            "No AI CLI is signed in: install and sign in to one of kiro-cli, claude, "
            "codex, or agy, then run setup again"
        )
    prompter.say(f"AI CLIs ready: {', '.join(engines)}")
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


# Slack -------------------------------------------------------------------------


def slack_app_url() -> str:
    """Return Slack's page for a new app, with PR Warden's webhook app filled in."""

    manifest = json.dumps(SLACK_APP_MANIFEST, separators=(",", ":"))
    return SLACK_NEW_APP_PAGE + urllib.parse.quote(manifest, safe="")


def open_in_browser(
    url: str,
    platform: Optional[str] = None,
    environ: "Optional[dict[str, str]]" = None,
    run: "Callable[..., subprocess.CompletedProcess]" = subprocess.run,
) -> bool:
    """Open ``url`` in a desktop browser, when this terminal sits in front of one.

    Over SSH the page would open on a screen nobody is looking at, and on Linux
    without a display a browser may be a text one that takes over this terminal,
    so in both the address is printed instead.
    """

    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    if environ.get("SSH_CONNECTION") or environ.get("SSH_TTY"):
        return False
    if platform == "darwin":
        command = ["open", url]
    elif platform.startswith("linux") and (
        environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")
    ):
        command = ["xdg-open", url]
    else:
        return False
    try:
        completed = run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def guide_slack_app(prompter: Prompter) -> None:
    """Walk the operator through making a Slack app that holds one webhook."""

    url = slack_app_url()
    prompter.say(
        "PR Warden posts through a Slack app of your own that holds one incoming "
        "webhook. Setup can open Slack's page for a new app with it filled in. There:"
    )
    prompter.say("  1. Pick the workspace, then press Next until Create, and Create.")
    prompter.say(
        "  2. Press Install to Workspace, pick the channel for the messages (a "
        "private one must be a channel you are in), and press Allow."
    )
    prompter.say(
        "  3. Open Incoming Webhooks on the left, and Copy the Webhook URL there."
    )
    prompter.say(
        "A workspace where an admin approves new apps shows Request to Install "
        "instead; carry on from step 2 once it is approved."
    )
    if prompter.confirm("Open that page in your browser now?", default=True):
        if open_in_browser(url):
            return
    prompter.say(f"Open this address in a browser signed in to Slack:\n{url}")


def ask_webhook(prompter: Prompter) -> Optional[str]:
    """Read a webhook URL hidden, and keep it once Slack takes a test message."""

    while True:
        webhook = prompter.secret(
            "Paste the Webhook URL (it stays hidden), or Enter to skip"
        )
        if not webhook:
            return None
        if not SLACK_WEBHOOK.fullmatch(webhook):
            prompter.say(
                "That is not an incoming webhook URL; those start with "
                "https://hooks.slack.com/services/."
            )
            continue
        prompter.say("Sending a test message through it...")
        try:
            delivery.send_slack_message(webhook, SLACK_TEST_MESSAGE)
        except delivery.DeliveryUncertainError as error:
            prompter.say(f"Slack could not be reached to check it: {error}")
            if prompter.confirm("Keep this URL anyway?"):
                return webhook
            continue
        except delivery.DeliveryError as error:
            prompter.say(
                f"{error}. Check that the whole URL was copied and that the app is "
                "still installed."
            )
            continue
        prompter.say("Slack took the test message; it is in the channel you picked.")
        return webhook


def ask_slack_repositories(prompter: Prompter, slugs: Sequence[str]) -> "List[str]":
    if len(slugs) < 2:
        return list(slugs)
    prompter.say(
        "A webhook belongs to one Slack workspace. A repository left out is never "
        "named there: your own pull requests in it are reviewed on GitHub instead, "
        "and its failures stay in the run log."
    )
    for index, slug in enumerate(slugs, 1):
        prompter.say(f"  {index}. {slug}")
    while True:
        answer = prompter.text(
            "Which report to Slack? Numbers separated by spaces, or Enter for all"
        )
        if not answer:
            return list(slugs)
        chosen = _numbers(answer, len(slugs))
        if chosen:
            return [slugs[index] for index in sorted(chosen)]
        prompter.say(f"Choose numbers from 1 to {len(slugs)}.")


def ask_slack(
    prompter: Prompter, slugs: Sequence[str], reviewer: Optional[str]
) -> "Optional[tuple[str, List[str]]]":
    """Connect a Slack webhook, and choose the repositories that report to it.

    Returns the webhook and the slugs that use it, or None when Slack stays off.
    """

    account = reviewer or "the reviewer account"
    prompter.say(
        f"Slack is optional. GitHub does not let {account} approve its own pull "
        "requests, so their reviews go to Slack, or without it to the pull request "
        "as comments. Slack also gets failed runs and requests to review by hand."
    )
    if not prompter.confirm("Send the reviews of your own pull requests to Slack?"):
        return None
    if not prompter.confirm("Do you have a Slack incoming webhook URL for it already?"):
        guide_slack_app(prompter)
    webhook = ask_webhook(prompter)
    if webhook is None:
        return None
    return webhook, ask_slack_repositories(prompter, slugs)


def apply_slack(entries: "List[dict]", slack_slugs: "Optional[Sequence[str]]") -> None:
    """Turn Slack on for the entries named, and off for every other one."""

    for entry in entries:
        if slack_slugs is not None and entry.get("slug") in slack_slugs:
            entry.pop("slack", None)
        else:
            entry["slack"] = False


def connect_slack(prompter: Prompter, locations: Locations) -> int:
    """Connect Slack to an existing configuration, or replace its webhook."""

    env_file = locations.env_file.resolve()
    if not env_file.exists():
        raise SetupError(f"{env_file} does not exist; run `prwarden setup` first")
    declared = load_env_file(env_file)
    configured = declared.get("REPOSITORIES_FILE")
    repositories_file = (
        resolve_path(configured, locations.config_directory)
        if configured
        else locations.repositories_file.resolve()
    )
    if not repositories_file.exists():
        raise SetupError(
            f"{repositories_file} does not exist; run `prwarden setup` first"
        )
    load_repositories(repositories_file, locations.checkouts_directory)
    entries = json.loads(repositories_file.read_text(encoding="utf-8"))
    if declared.get("SLACK_WEBHOOK_URL"):
        prompter.say("A Slack webhook is set already; connecting one replaces it.")
    connected = ask_slack(
        prompter,
        [entry["slug"] for entry in entries],
        declared.get("GITHUB_REVIEWER_USERNAME") or None,
    )
    if connected is None:
        prompter.say("Nothing changed.")
        return 0
    webhook, slugs = connected
    for path in (env_file, repositories_file):
        backup = _backup(path)
        if backup:
            prompter.say(f"Kept the previous {path.name} as {backup}")
    set_env_value(env_file, "SLACK_WEBHOOK_URL", webhook)
    apply_slack(entries, slugs)
    _write_private(repositories_file, json.dumps(entries, indent=2) + "\n")
    prompter.say(
        f"Slack is on for {len(slugs)} of {len(entries)} repositories, from the "
        "next run."
    )
    return 0


# The rest of setup -------------------------------------------------------------


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
    states = ensure_tools(prompter)
    reviewer, logins = ask_reviewer(prompter)
    env_values = {"AUTO_REVIEW_DRY_RUN": "1", "GITHUB_REVIEWER_USERNAME": reviewer}
    env_values.update(ask_engines(prompter, states))
    entries = ask_repositories(prompter, reviewer, logins, locations)
    connected = ask_slack(prompter, [entry["slug"] for entry in entries], reviewer)
    apply_slack(entries, connected[1] if connected else None)
    if connected:
        env_values["SLACK_WEBHOOK_URL"] = connected[0]
    else:
        prompter.say("Slack is off. Connect it later with: prwarden setup --slack")
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
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument(
        "--slack",
        action="store_true",
        help="connect Slack to the existing configuration, or replace its webhook",
    )
    choice.add_argument(
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
        if args.slack:
            return connect_slack(prompter, locations)
        return interactive(prompter, locations)
    except (SetupError, ConfigError, CommandError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nSetup stopped; files already written were kept.", file=sys.stderr)
        return 130
