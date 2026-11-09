"""The command-line tools PR Warden drives: whether each is installed and signed in,
and how to install it and sign in to it.

Every check is the tool's own status command, which reads a stored login and calls
no model, so checking costs nothing. The checks run in the environment a scheduled
run gets, because a check that sees more than the schedule does reports a login the
schedule cannot use.

Installing runs one vetted command per tool and platform: a Homebrew formula or
cask where Homebrew can install it, otherwise the vendor's own installer from the
vendor's own domain. Nothing here runs `sudo`; a tool that needs it is left with
instructions instead.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from .commands import CommandError
from .engine import scrubbed_environment


STATUS_TIMEOUT_SECONDS = 30
VERSION_PATTERN = re.compile(r"\d+\.\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Tool:
    key: str
    name: str
    command: str
    # The review engine this CLI is, or None for a tool every installation needs.
    engine: Optional[str]
    homebrew: "tuple[str, ...]"
    # The vendor's own installer for macOS and Linux, run when Homebrew cannot.
    installer: Optional[str]
    status: "tuple[str, ...]"
    login: "tuple[str, ...]"
    login_hint: str
    instructions: str


TOOLS = (
    Tool(
        key="gh",
        name="GitHub CLI",
        command="gh",
        engine=None,
        homebrew=("install", "gh"),
        installer=None,
        status=("auth", "status", "--json", "hosts"),
        login=("auth", "login"),
        login_hint="Answer its questions; it opens your browser to sign in.",
        instructions="https://github.com/cli/cli#installation",
    ),
    Tool(
        key="kiro",
        name="Kiro CLI",
        command="kiro-cli",
        engine="kiro",
        homebrew=("install", "--cask", "kiro-cli"),
        installer="curl -fsSL https://cli.kiro.dev/install | bash",
        status=("whoami",),
        login=("login",),
        login_hint="It opens your browser to sign in.",
        instructions="https://kiro.dev/docs/cli/installation/",
    ),
    Tool(
        key="claude",
        name="Claude Code",
        command="claude",
        engine="claude",
        homebrew=("install", "--cask", "claude-code"),
        installer="curl -fsSL https://claude.ai/install.sh | bash",
        status=("auth", "status"),
        login=("auth", "login"),
        login_hint="It opens your browser to sign in.",
        instructions="https://code.claude.com/docs/en/setup",
    ),
    Tool(
        key="codex",
        name="Codex",
        command="codex",
        engine="codex",
        homebrew=("install", "--cask", "codex"),
        installer="curl -fsSL https://chatgpt.com/codex/install.sh | sh",
        status=("login", "status"),
        login=("login",),
        login_hint="It opens your browser to sign in.",
        instructions="https://github.com/openai/codex#readme",
    ),
    Tool(
        key="antigravity",
        name="Antigravity",
        command="agy",
        engine="antigravity",
        homebrew=("install", "--cask", "antigravity-cli"),
        installer="curl -fsSL https://antigravity.google/cli/install.sh | bash",
        status=("models",),
        # Antigravity has no login command: it signs in the first time it starts.
        login=(),
        login_hint="Sign in with Google in the browser it opens, then quit it with "
        "/quit.",
        instructions="https://antigravity.google/cli",
    ),
)
TOOLS_BY_KEY = {tool.key: tool for tool in TOOLS}
# Optional: only an installation that reads Jira work items needs it, so setup asks
# about it in its own step rather than among the tools every installation needs.
JIRA_TOOL = Tool(
    key="acli",
    name="Atlassian CLI",
    command="acli",
    engine=None,
    homebrew=("install", "atlassian/acli/acli"),
    installer=None,
    status=("jira", "auth", "status"),
    login=("jira", "auth", "login", "--web"),
    login_hint="It opens your browser to sign in to Atlassian.",
    instructions="https://developer.atlassian.com/cloud/acli/guides/install-acli/",
)
TOOLS_BY_ENGINE = {tool.engine: tool for tool in TOOLS if tool.engine}


@dataclass(frozen=True)
class ToolState:
    tool: Tool
    path: Optional[str]
    version: Optional[str]
    signed_in: Optional[bool]
    # Who or how, when the tool says so without naming anything private.
    detail: str = ""

    @property
    def installed(self) -> bool:
        return self.path is not None


def search_path() -> str:
    """Return PATH with the places installers put these tools, in case the shell
    that started this run has not picked them up yet."""

    directories = os.environ.get("PATH", "").split(os.pathsep)
    for extra in (
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ):
        if extra not in directories:
            directories.append(extra)
    return os.pathsep.join(directory for directory in directories if directory)


def find(tool: Tool) -> Optional[str]:
    return shutil.which(tool.command, path=search_path())


def _check_environment() -> "dict[str, str]":
    environment = scrubbed_environment()
    environment["PATH"] = search_path()
    return environment


def _run_status(path: str, args: Sequence[str]) -> "tuple[int, str]":
    try:
        completed = subprocess.run(
            [path, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_check_environment(),
            timeout=STATUS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return completed.returncode, completed.stdout


def read_sign_in(tool: Tool, code: int, output: str) -> "tuple[Optional[bool], str]":
    """Return whether a tool's status output says it is signed in, and who as."""

    if code < 0:
        return None, "did not answer"
    if tool.key == "gh":
        try:
            entries = json.loads(output)["hosts"]["github.com"]
        except (ValueError, KeyError, TypeError):
            return code == 0, ""
        logins = [
            entry.get("login", "")
            for entry in entries
            if isinstance(entry, dict) and entry.get("state") == "success"
        ]
        return bool(logins), ", ".join(logins)
    if tool.key == "claude":
        try:
            payload = json.loads(output)
        except ValueError:
            return code == 0, ""
        method = payload.get("authMethod")
        signed_in = bool(payload.get("loggedIn"))
        return signed_in, str(method) if signed_in and method else ""
    return code == 0, ""


def check(tool: Tool) -> ToolState:
    path = find(tool)
    if path is None:
        return ToolState(tool, None, None, None)
    code, output = _run_status(path, ("--version",))
    match = VERSION_PATTERN.search(output) if code == 0 else None
    version = match.group(0) if match else None
    signed_in, detail = read_sign_in(tool, *_run_status(path, tool.status))
    return ToolState(tool, path, version, signed_in, detail)


def check_all(tools: Sequence[Tool] = TOOLS) -> "List[ToolState]":
    """Check every tool at once; the slowest status takes a few seconds alone."""

    with ThreadPoolExecutor(max_workers=len(tools)) as pool:
        return list(pool.map(check, tools))


def render_table(states: Sequence[ToolState]) -> "List[str]":
    rows = [("Tool", "Installed", "Signed in")]
    for state in states:
        if not state.installed:
            installed, signed_in = "no", "-"
        else:
            installed = state.version or "yes"
            if state.signed_in is None:
                signed_in = state.detail or "unknown"
            elif state.signed_in:
                signed_in = f"yes ({state.detail})" if state.detail else "yes"
            else:
                signed_in = "no"
        rows.append((state.tool.name, installed, signed_in))
    widths = [max(len(row[column]) for row in rows) for column in range(2)]
    return [
        f"{name.ljust(widths[0])}  {installed.ljust(widths[1])}  {signed_in}"
        for name, installed, signed_in in rows
    ]


# Installing and signing in ------------------------------------------------------


@dataclass(frozen=True)
class InstallPlan:
    """How one tool would be installed here: a command to run, or why not."""

    command: Optional["tuple[str, ...]"]
    description: str


def install_plan(tool: Tool, platform: Optional[str] = None) -> InstallPlan:
    platform = sys.platform if platform is None else platform
    brew = shutil.which("brew", path=search_path())
    # Casks are macOS only; a formula works under Homebrew on Linux as well.
    homebrew_works = brew is not None and (
        platform == "darwin" or "--cask" not in tool.homebrew
    )
    if homebrew_works:
        command = (brew, *tool.homebrew)
        return InstallPlan(command, " ".join(("brew", *tool.homebrew)))
    if tool.installer and (platform == "darwin" or platform.startswith("linux")):
        return InstallPlan(
            ("/bin/bash", "-c", f"set -o pipefail; {tool.installer}"),
            f"{tool.installer}   (the vendor's own installer)",
        )
    return InstallPlan(
        None,
        f"install it yourself, following {tool.instructions}, then run setup again",
    )


def login_command(state: ToolState) -> "tuple[str, ...]":
    if state.path is None:
        raise CommandError(
            f"{state.tool.name} is not installed", command=state.tool.command
        )
    return (state.path, *state.tool.login)


def run_in_terminal(command: Sequence[str]) -> int:
    """Run a command attached to this terminal, so its prompts reach the operator."""

    try:
        return subprocess.run(list(command), check=False).returncode
    except OSError as error:
        raise CommandError(str(error), command=command[0]) from error


def run_with_input(command: Sequence[str], text: str) -> int:
    """Run a command attached to this terminal, with ``text`` on its standard input.

    For a secret a CLI reads from standard input, so it never appears in an argument
    list, where any process on the machine could read it, and is never written to a
    file by this tool.
    """

    try:
        return subprocess.run(
            list(command), input=text, text=True, check=False
        ).returncode
    except OSError as error:
        raise CommandError(str(error), command=command[0]) from error


Runner = Callable[[Sequence[str]], int]
