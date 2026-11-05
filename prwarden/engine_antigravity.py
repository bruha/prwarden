import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import progress
from .commands import CommandError, run_cmd_streams
from .config import Settings
from .engine import (
    EngineError,
    build_engine_environment,
    build_prompt,
    build_schema,
    effort_for,
    host_loaded_instructions,
    is_exhausted_quota,
    refuse_host_configuration,
)
from .models import PullRequest, Repository, ReviewMaterial


# Antigravity loads a customization root from the directory it starts in, which is the
# reviewed head, and runs what it names before and around every model call: the MCP
# servers in `.agents/mcp_config.json` and the hooks in `.agents/hooks.json`, including
# those of any plugin under `.agents/plugins/`. Both were confirmed to execute in print
# mode with the terminal sandbox on and `command(*)` denied, because the sandbox gates
# the model's tool calls and not the host's own. `.agent/` is a legacy name for the
# same root. Only the text subtrees below may ship in a head this adapter reviews.
ANTIGRAVITY_DOCUMENT_DIRECTORIES = {
    ".agents": ("rules", "workflows"),
    ".agent": ("rules", "workflows"),
}

# What Antigravity adds to the model's context as rules from the directory it starts
# in, checked with a file in each place: rules in `.agents/rules` load only with a
# trigger in their front matter, so they are named whenever they exist.
ANTIGRAVITY_LOADED_INSTRUCTIONS = (
    "AGENTS.md",
    "GEMINI.md",
    ".agents/rules/*.md",
    ".agent/rules/*.md",
)

ANTIGRAVITY_MAX_RETRIES = 2
ANTIGRAVITY_RETRY_DELAY_SECONDS = 15
# The review timeout bounds the whole invocation, retries included, not each attempt.
# Multiplying it by the attempt count turned a provider that hangs into a 45-minute
# ceiling: on 2026-09-29 six invocations each burned 31 minutes — two attempts run out
# to the full timeout, then a third returned PERMISSION_DENIED — and cost that run
# 191 minutes of wall clock. A failure that arrives quickly still leaves most of the
# budget to retry inside, which is the case retrying was built for; one that consumes
# the whole budget has already had its answer.
#
# Below this, another attempt cannot produce a review, so the budget counts as spent
# rather than being handed to a call that is certain to time out.
ANTIGRAVITY_MIN_ATTEMPT_SECONDS = 60
ANTIGRAVITY_ERROR_DETAIL_LIMIT = 800
ANTIGRAVITY_WARNING_ONLY_FAILURE = (
    "warning: --mode plan has no effect while slash command expansion is disabled"
)
ANTIGRAVITY_BACKEND_ERROR_PATTERN = re.compile(
    r"\b(?:ABORTED|DEADLINE_EXCEEDED|FAILED_PRECONDITION|INTERNAL|"
    r"INVALID_ARGUMENT|NOT_FOUND|PERMISSION_DENIED|RESOURCE_EXHAUSTED|"
    r"UNAUTHENTICATED|UNAVAILABLE)(?: \(code \d+\))?: [^\r\n]+"
)
# The CLI refuses a model it no longer offers, or an effort that model does not take,
# before reaching the provider, so the refusal carries no status token. Unrecognised,
# it read as "exited without a usable provider diagnostic" for every review from
# 2026-10-13, when the configured model was withdrawn, and the cause sat in a log
# nobody was pointed at.
ANTIGRAVITY_MODEL_SELECTION_PATTERN = re.compile(
    r"invalid model selection \([^)\r\n]*\): [^\r\n]+"
)
ANTIGRAVITY_LOG_LINE_PREFIX = re.compile(r"^[IWEF]\d{4} ")
ANTIGRAVITY_JSON_ERROR_PATTERNS = (
    re.compile(
        r'"message"\s*:\s*"([^"\r\n]+)".{0,1000}?'
        r'"status"\s*:\s*"([A-Z_]+)"',
        re.DOTALL,
    ),
    re.compile(
        r'"status"\s*:\s*"([A-Z_]+)".{0,1000}?'
        r'"message"\s*:\s*"([^"\r\n]+)"',
        re.DOTALL,
    ),
)
ANTIGRAVITY_TRANSIENT_ERROR_MARKERS = (
    "connection refused",
    "connection reset",
    "connection timed out",
    "deadline_exceeded",
    "gateway timeout",
    "i/o timeout",
    "internal server error",
    "network is unreachable",
    "no route to host",
    "no such host",
    "resource_exhausted",
    "service unavailable",
    "temporarily unavailable",
    "the stream was interrupted",
    "tls handshake timeout",
    "user location is not supported for the api use",
    "unavailable",
)
ANTIGRAVITY_TRANSPORT_ERROR_MARKERS = (
    "connection refused",
    "connection reset",
    "i/o timeout",
    "network is unreachable",
    "no route to host",
    "no such host",
    "tls handshake timeout",
)
ANTIGRAVITY_PRINT_TIMEOUT_MARKER = "Print mode: timed out after"
# Print mode cannot prompt, so a tool call the sandbox does not pre-approve is
# auto-denied. The CLI then exits zero with an empty response and explains itself
# on standard error alone, which is the only place this failure is legible.
ANTIGRAVITY_HEADLESS_DENIAL_MARKER = "headless mode cannot prompt for"
ANTIGRAVITY_SOFT_DENY_PATTERN = re.compile(
    r'soft-denying tool confirmation "([A-Za-z]+)"(?: at step (\d+))?'
)
# The same envelope also lists what it refused, as
# `[{"action": "read_file", "display_name": "ListDir"}]`. That survives a reworded
# diagnostic and a lost standard error, so it is read before either.
ANTIGRAVITY_DENIED_ACTIONS_FIELD = "denied_actions"
ANTIGRAVITY_REFUSED_CALL_MARKER = "refused tool call"
ANTIGRAVITY_MAX_TOOL_FAULT_RETRIES = 1
ANTIGRAVITY_TOOL_FAULT_MARKERS = (
    ANTIGRAVITY_HEADLESS_DENIAL_MARKER,
    ANTIGRAVITY_REFUSED_CALL_MARKER,
    "soft-denied tool call",
    "command timed out due to the size",
    "failed to read file",
    "invalid tool call",
    "matches user-configured deny rule",
    "missing properties",
    "permission check failed",
    "permission denied for",
)
ANTIGRAVITY_ENVELOPE_ERROR_FIELDS = (
    "error",
    "error_message",
    "message",
    "detail",
)


_review_slots_guard = threading.Lock()
_review_slots: "dict[int, int]" = {}


def _review_slot() -> int:
    """Return a stable slot index for the calling thread.

    A run uses one bounded pool, so the number of threads that ever review is
    at most the configured concurrency and the slots stay few.
    """
    ident = threading.get_ident()
    with _review_slots_guard:
        slot = _review_slots.get(ident)
        if slot is None:
            slot = len(_review_slots)
            _review_slots[ident] = slot
        return slot


class _EnvelopeFailure(EngineError):
    """Antigravity returned a result envelope reporting an unsuccessful run."""

    def __init__(self, message: str, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


def _read_log(log_path: Path) -> str:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _condensed_detail(text: str) -> str:
    return " ".join(text.split())[:ANTIGRAVITY_ERROR_DETAIL_LIMIT]


def _condensed_log_line(line: str) -> str:
    """Condense one log line, dropping its leading severity and source header."""
    header_end = line.find("] ")
    if header_end >= 0:
        line = line[header_end + 2 :]
    return _condensed_detail(line)


def _discard_log(log_path: Path) -> None:
    try:
        log_path.unlink(missing_ok=True)
    except OSError:
        pass


def _retained_log_suffix(log_path: Path) -> str:
    try:
        if log_path.stat().st_size <= 0:
            return ""
    except OSError:
        return ""
    return f"; diagnostic log retained at {log_path}"


def _extract_model_selection_error(log_text: str) -> str:
    """Return the CLI's refusal of the configured model or effort, if it logged one.

    The models it lists instead are display names, not the IDs REVIEW_MODEL takes, so
    the detail says where the IDs are rather than suggesting one.
    """

    lines = log_text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        match = ANTIGRAVITY_MODEL_SELECTION_PATTERN.search(lines[index])
        if match is None:
            continue
        detail = match.group(0)
        offered = []
        for following in lines[index + 1 :]:
            if ANTIGRAVITY_LOG_LINE_PREFIX.match(following):
                break
            name = following.strip()
            if name and not name.endswith(":"):
                offered.append(name)
        if offered:
            detail += (
                f"; it offers {', '.join(offered)} (`agy models` lists their IDs)"
            )
        return detail[:ANTIGRAVITY_ERROR_DETAIL_LIMIT]
    return ""


def _extract_backend_error(log_path: Path) -> str:
    log_text = _read_log(log_path)

    model_selection = _extract_model_selection_error(log_text)
    if model_selection:
        return model_selection

    matches = ANTIGRAVITY_BACKEND_ERROR_PATTERN.findall(log_text)
    if matches:
        detail = matches[-1]
        status = detail.split(":", 1)[0]
        duplicate = detail.find(f": {status}", len(status))
        if duplicate >= 0:
            detail = detail[:duplicate]
        return detail[:ANTIGRAVITY_ERROR_DETAIL_LIMIT]

    for pattern_index, pattern in enumerate(ANTIGRAVITY_JSON_ERROR_PATTERNS):
        matches = pattern.findall(log_text)
        if matches:
            first, second = matches[-1]
            message, status = (
                (first, second) if pattern_index == 0 else (second, first)
            )
            return f"{status}: {message}"[:ANTIGRAVITY_ERROR_DETAIL_LIMIT]

    for line in reversed(log_text.splitlines()):
        marker = "Print mode: run ended with error"
        if marker in line:
            return line[line.index(marker) :][:ANTIGRAVITY_ERROR_DETAIL_LIMIT]
    return ""


def _extract_transport_failure(log_path: Path) -> str:
    """Return the last network-transport failure the CLI logged, if any.

    Go transport errors carry no gRPC status token, so the provider-diagnostic
    pattern cannot see them. Surfacing them keeps a network blip retryable
    instead of spending the fallback engine on it.
    """

    for line in reversed(_read_log(log_path).splitlines()):
        normalized = line.casefold()
        if any(
            marker in normalized for marker in ANTIGRAVITY_TRANSPORT_ERROR_MARKERS
        ):
            return _condensed_log_line(line)
    return ""


def _extract_print_timeout(log_path: Path) -> str:
    """Return the CLI's own diagnostic for hitting the print timeout."""
    for line in reversed(_read_log(log_path).splitlines()):
        if ANTIGRAVITY_PRINT_TIMEOUT_MARKER in line:
            return _condensed_detail(line[line.index(ANTIGRAVITY_PRINT_TIMEOUT_MARKER) :])
    return ""


def _denied_actions_detail(envelope: dict) -> str:
    """Return what the envelope says it refused, in this review's own words.

    The CLI's sentence tells an operator to add an allow-rule to settings.json,
    which is advice neither the model nor this controller can act on: the roots
    are deliberate. So the structured field is rendered as the fact the model
    needs, that a named tool call was refused for want of a permission.
    """

    actions = envelope.get(ANTIGRAVITY_DENIED_ACTIONS_FIELD)
    if not isinstance(actions, list):
        return ""
    described = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        tool = action.get("display_name") or action.get("tool")
        permission = action.get("action") or action.get("permission")
        if isinstance(tool, str) and tool.strip():
            named = repr(tool.strip())
        elif isinstance(permission, str) and permission.strip():
            named = "a tool call"
        else:
            continue
        wanted = (
            f" for want of the {permission.strip()} permission"
            if isinstance(permission, str) and permission.strip()
            else ""
        )
        entry = f"{named}{wanted}"
        if entry not in described:
            described.append(entry)
    if not described:
        return ""
    return (
        f"Print mode {ANTIGRAVITY_REFUSED_CALL_MARKER} "
        f"{', '.join(described)}, and an unattended review cannot approve one"
    )


def _extract_headless_denial(error_output: str, log_path: Path) -> str:
    """Explain an empty successful run that a refused tool call caused.

    The CLI's own sentence on standard error names the permission it wanted and
    how to grant it, so it is quoted rather than paraphrased. Its log names the
    tool instead, which is the better fact when the sentence is absent.
    """

    for line in error_output.splitlines():
        if ANTIGRAVITY_HEADLESS_DENIAL_MARKER in line:
            return _condensed_detail(line)
    match = ANTIGRAVITY_SOFT_DENY_PATTERN.search(_read_log(log_path))
    if match:
        step = f" at step {match.group(2)}" if match.group(2) else ""
        return (
            f"Print mode soft-denied tool call {match.group(1)!r}{step}, which "
            "this review does not pre-approve"
        )
    return ""


def _envelope_error_detail(envelope: dict) -> str:
    """Return the failure message an unsuccessful result envelope carries."""
    for field in ANTIGRAVITY_ENVELOPE_ERROR_FIELDS:
        value = envelope.get(field)
        if isinstance(value, dict):
            value = value.get("message")
        if isinstance(value, str) and value.strip():
            return _condensed_detail(value)
    response = envelope.get("response")
    if isinstance(response, str) and response.strip():
        return _condensed_detail(response)
    return ""


def _opaque_failure_detail(log_path: Path) -> str:
    """Describe a failure whose log holds nothing machine-readable.

    The log is kept for inspection precisely because no pattern could explain
    the failure from it.
    """

    return (
        "Antigravity exited without a usable provider diagnostic"
        f"{_retained_log_suffix(log_path)}"
    )


def _is_tool_fault(detail: str) -> bool:
    """Report whether a detail names a tool call the model can avoid repeating."""
    normalized = detail.casefold()
    return any(marker in normalized for marker in ANTIGRAVITY_TOOL_FAULT_MARKERS)


def _missing_tool_path(detail: str) -> str:
    """Return the path a failed read names, or an empty string.

    Antigravity reports the fault as ``open <path>: no such file or directory``,
    so the path can be quoted back to the model as a fact rather than left for it
    to find in a long diagnostic sentence.
    """

    match = re.search(r"open (\S+): no such file or directory", detail)
    return match.group(1) if match else ""


def _tool_fault_feedback(detail: str) -> str:
    """Tell the model which tool fault discarded its previous attempt.

    Antigravity aborts a print-mode review when a tool call cannot be completed,
    whether the sandbox denied it or the target does not exist, so the reason is
    quoted back rather than interpreted. The absent path, when the fault names
    one, is called out separately: repeating or neighbouring that guess is the
    way this failure keeps recurring.
    """

    if (
        ANTIGRAVITY_HEADLESS_DENIAL_MARKER in detail
        or ANTIGRAVITY_REFUSED_CALL_MARKER in detail
        or "soft-denied" in detail
    ):
        return f"""

Antigravity discarded your previous attempt at this review, and every finding it
had already verified, because it made a tool call this review cannot approve:
- The Antigravity CLI reported this refusal: {detail}
- This review runs unattended, so nothing can be approved while it runs. A call
  the sandbox does not already permit is refused, and the refusal ends the review
  with no result at all.
- Read and list only paths under the workspace root and the dependency
  directories named above, each of which is readable at and below itself alone.
  Stepping up out of an installed package into the checkout that holds it is
  refused, and so is any sibling of that package directory.
- Only one more attempt is available. Verify what those paths can show you, and
  treat anything else as unverifiable rather than reaching for it: an omitted
  finding costs one finding, and a refused call costs the entire review.
"""

    missing = _missing_tool_path(detail)
    specifics = (
        f"""- This path does not exist in this workspace: {missing}
- Do not read it again, and do not try a path near it. Whatever you meant to
  confirm there is unverifiable, so omit that finding and review the rest.
"""
        if missing
        else ""
    )
    return f"""

Antigravity discarded your previous attempt at this review, and every finding it
had already verified, because one tool call failed:
- The Antigravity CLI reported this fault: {detail}
{specifics}- Only one more attempt is available. A second failed tool call ends the review
  with no result at all, so make no call you are not certain of.
- Read only paths you have seen in the diff, the eligible-line map, or a listing
  or search result from this workspace. Never guess a path, and list a directory
  before reading a file inside it. Stay inside the roots this review named as
  readable.
- No terminal command, script, or test file will ever run: writing one, including
  with a shell redirect, is denied and would end this review.
"""


def _attempt_budget(deadline: float, delay: float) -> Optional[int]:
    """Return the seconds another attempt may run for, or None if none should.

    Measured after the backoff, because the wait spends the same budget the review
    does.
    """

    remaining = int(deadline - time.monotonic() - delay)
    return remaining if remaining >= ANTIGRAVITY_MIN_ATTEMPT_SECONDS else None


def _is_command_timeout(detail: str) -> bool:
    return detail.casefold().startswith("command timed out")


def _spent_budget_detail(detail: str, timeout: int) -> str:
    """Say why a retryable failure was not retried, so it does not read as final."""

    return (
        f"{detail} (not retried: the {timeout}s review budget is spent, and it "
        "bounds every attempt together)"
    )


def _is_retryable_antigravity_failure(detail: str) -> bool:
    normalized = detail.casefold()
    if is_exhausted_quota(detail):
        # RESOURCE_EXHAUSTED covers both momentary pressure and a spent daily quota,
        # and only the message separates them. Retrying a spent quota cost 8m34s per
        # pull request on 2026-09-22 — three attempts of 2m30s, because the CLI itself
        # retries internally before reporting — and delayed every fallback by that
        # much, which is how a slate of reviews ran into the fallback's own limit.
        return False
    return (
        ANTIGRAVITY_WARNING_ONLY_FAILURE in normalized
        or _is_command_timeout(normalized)
        or any(
        marker in normalized for marker in ANTIGRAVITY_TRANSIENT_ERROR_MARKERS
        )
    )


def _dependency_read_targets(material: ReviewMaterial) -> tuple[str, ...]:
    """Return linked dependency targets that the sandbox must allow reading.

    The controller links installed dependencies into the workspace so a
    dependency-API claim can be verified. Antigravity resolves symlinks before
    applying its read policy, so each link target needs an explicit allowance.
    """

    targets: list[str] = []
    for link in material.dependency_links:
        target = str(Path(link.target).resolve())
        if target not in targets:
            targets.append(target)
    return tuple(targets)


class AntigravityEngine:
    """Translate the common review contract to Antigravity CLI arguments."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _prepare_app_data(
        self,
        material: ReviewMaterial,
    ) -> tuple[Path, Path, str]:
        """Create reviewer-owned CLI settings without changing global preferences."""
        home = os.environ.get("HOME")
        if not home:
            raise EngineError("Antigravity requires HOME to locate its saved login")

        gemini_dir = (Path(home).expanduser() / ".gemini").resolve()
        base_dir = (self.settings.state_file.parent / "antigravity-cli").resolve()
        app_data_dir = base_dir / self._review_id()
        review_workspaces = (
            self.settings.state_file.parent / "worktrees"
        ).resolve()
        try:
            app_data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            settings_path = app_data_dir / "settings.json"
            # Reviews run in parallel and each one allows only its own PR's
            # dependency directories, so a shared settings file cannot describe
            # them all. Any file in the base directory is a read-only template:
            # its keys carry over, and this review never writes back to it.
            cli_settings: dict[str, Any] = {}
            template_path = base_dir / "settings.json"
            if template_path.exists():
                loaded = json.loads(template_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("top-level value must be an object")
                cli_settings.update(loaded)
            cli_settings.update(
                {
                    "toolPermission": "request-review",
                    "enableTerminalSandbox": True,
                    "allowNonWorkspaceAccess": False,
                    "permissions": {
                        "allow": [
                            f"read_file({review_workspaces})",
                            *(
                                f"read_file({target})"
                                for target in _dependency_read_targets(material)
                            ),
                        ],
                        "deny": [
                            "write_file(*)",
                            "read_url(*)",
                            "execute_url(*)",
                            "command(*)",
                            "unsandboxed(*)",
                            "mcp(*)",
                        ],
                        "ask": [],
                    },
                }
            )
            temporary_path = settings_path.with_suffix(".json.tmp")
            temporary_path.write_text(
                json.dumps(cli_settings, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary_path.chmod(0o600)
            temporary_path.replace(settings_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise EngineError(
                f"Cannot prepare isolated Antigravity settings: {error}"
            ) from error

        return gemini_dir, app_data_dir, os.path.relpath(app_data_dir, gemini_dir)

    @staticmethod
    def _review_id() -> str:
        """Return this review's app-data directory name.

        Antigravity populates a full profile there, tens of megabytes of
        binaries and caches, so the name must be stable across runs: a name
        carrying the process or thread identity would leak a fresh copy every
        run. Slots are small indexes instead, so concurrent reviews stay
        isolated while repeated runs reuse the same warm directories.
        """
        return f"review-{_review_slot()}"

    @staticmethod
    def _new_log_path(app_data_dir: Path) -> Path:
        log_dir = app_data_dir / "log"
        try:
            log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=log_dir,
                prefix="review-",
                suffix=".log",
                delete=False,
            ) as log_file:
                return Path(log_file.name)
        except OSError as error:
            raise EngineError(
                f"Cannot prepare Antigravity diagnostic log: {error}"
            ) from error

    def review(
        self,
        repository: Repository,
        pull_request: PullRequest,
        material: ReviewMaterial,
        timeout_seconds: Optional[int] = None,
    ) -> Any:
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.settings.review_timeout_seconds
        )
        if repository.path is None:
            raise EngineError(
                f"Repository {repository.slug} needs a local path for review"
            )
        if not repository.path.is_dir():
            raise EngineError(
                f"Configured repository path does not exist: {repository.path}"
            )
        refuse_host_configuration(
            repository.path, "Antigravity", ANTIGRAVITY_DOCUMENT_DIRECTORIES
        )

        schema = build_schema(self.settings.max_findings, material)
        prompt = build_prompt(
            repository,
            pull_request,
            material,
            self.settings.max_findings,
        )
        workspace_root = str(repository.path.resolve())
        gemini_dir, app_data_dir, app_data_relative = self._prepare_app_data(
            material,
        )
        readable_roots = "\n".join(
            f"  {root}"
            for root in (workspace_root, *_dependency_read_targets(material))
        )
        prompt += host_loaded_instructions(
            repository.path, "Antigravity", ANTIGRAVITY_LOADED_INSTRUCTIONS
        )
        prompt += f"""

Antigravity tool-path requirement:
- The absolute workspace root is: {workspace_root}
- Every file read, directory listing, glob, and search tool call MUST use an
  absolute path inside that workspace root.
- Never use `.`, a relative path, a parent directory, the user home directory,
  or Antigravity configuration directories as a tool path.
- These are the only roots this review can read, listings included:
{readable_roots}
- A path outside them is refused before it is read, and the refusal cannot be
  approved because no human is watching this review. Each root above is readable
  at and below itself and nowhere else. A dependency root is the installed
  package directory of a checkout that this review cannot read, so the directory
  that holds it and every sibling of it are refused, however ordinary they look.
- Following the workspace symlink into installed packages is expected and allowed.
  Stepping up out of it to see the checkout around it is the way this review gets
  discarded: read the package paths the material gave you and go no further up or
  across.

Antigravity sandbox requirement:
- Terminal commands are denied for this review. The sandbox rejects every one,
  including a read-only `grep`, `rg`, `ls`, `cat`, `find`, or `git` invocation,
  and a shell redirect or heredoc that would write a file.
- A rejected command aborts this entire review and wastes the whole invocation,
  so never attempt one, and never retry a command that was already rejected.
- Use only the file read, directory listing, glob, and search tools. They can
  answer every question a command would, and they are the sole way to verify a
  candidate here.

Antigravity tool-fault requirement:
- Any tool call that cannot be completed ends this review outright and discards
  every finding already verified, so no single check is worth risking one.
- A refused tool call is such a fault, and so is reading a path that does not
  exist. Listing a directory needs the same permission as reading a file in it,
  so a listing outside the roots above costs the review exactly as much.
- List a directory before reading a file inside it, especially inside an
  installed dependency, where layout differs between versions. Prefer the
  shallowest listing that answers the question.
- Never retry or vary a call that already failed. If what you meant to inspect is
  absent, treat only that piece of evidence as unavailable and carry on with the
  rest of the pass.
"""
        args = [
            *self.settings.review_engine_command,
            "--gemini_dir",
            str(gemini_dir),
            "--app_data_dir",
            app_data_relative,
            "--sandbox",
            "--disable-slash-commands",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
        ]
        if self.settings.review_model:
            args.extend(["--model", self.settings.review_model])
        effort = effort_for(self.settings, material)
        if effort:
            args.extend(["--effort", effort])

        attempt_prompt = prompt
        fault_retries = 0
        deadline = time.monotonic() + timeout
        attempt_timeout = timeout
        for attempt in range(ANTIGRAVITY_MAX_RETRIES + 1):
            log_path = self._new_log_path(app_data_dir)
            invocation_args = [
                *args,
                "--print-timeout",
                f"{attempt_timeout}s",
                "--log-file",
                str(log_path),
                "--print",
                attempt_prompt,
            ]
            try:
                output, error_output = run_cmd_streams(
                    invocation_args,
                    cwd=repository.path,
                    timeout_seconds=attempt_timeout + 30,
                    environment=build_engine_environment(self.settings),
                    inherit_environment=False,
                )
            except CommandError as error:
                timeout_detail = _extract_print_timeout(log_path)
                if timeout_detail:
                    _discard_log(log_path)
                    raise EngineError(
                        "Antigravity stopped at the "
                        f"{attempt_timeout}s print timeout "
                        f"before completing the review: {timeout_detail}"
                    ) from error
                backend_detail = _extract_backend_error(log_path)
                if not backend_detail:
                    backend_detail = _extract_transport_failure(log_path)
                detail = backend_detail or str(error)
                retryable = attempt < ANTIGRAVITY_MAX_RETRIES and (
                    _is_retryable_antigravity_failure(detail)
                )
                delay = ANTIGRAVITY_RETRY_DELAY_SECONDS * (2**attempt)
                budget = _attempt_budget(deadline, delay) if retryable else None
                if budget is None:
                    if backend_detail:
                        _discard_log(log_path)
                    elif _is_command_timeout(detail):
                        # A hang is its own diagnosis, and its log is the record of
                        # where the CLI stopped, so it is kept and named rather than
                        # replaced with "no usable diagnostic".
                        detail = f"{detail}{_retained_log_suffix(log_path)}"
                    else:
                        detail = _opaque_failure_detail(log_path)
                    if retryable:
                        detail = _spent_budget_detail(detail, timeout)
                    raise EngineError(detail) from error
                _discard_log(log_path)
                progress.notify(
                    "Transient Antigravity failure on attempt "
                    f"{attempt + 1}/{ANTIGRAVITY_MAX_RETRIES + 1}; "
                    f"retrying in {delay}s with {budget}s of the {timeout}s "
                    f"budget left: {backend_detail or error}"
                )
                time.sleep(delay)
                attempt_timeout = budget
                continue

            try:
                structured_output = self._structured_output(
                    output,
                    log_path,
                    error_output,
                )
            except EngineError as error:
                detail = error.detail if isinstance(error, _EnvelopeFailure) else ""
                if attempt >= ANTIGRAVITY_MAX_RETRIES:
                    raise
                if _is_tool_fault(detail):
                    if fault_retries >= ANTIGRAVITY_MAX_TOOL_FAULT_RETRIES:
                        raise
                    budget = _attempt_budget(deadline, 0)
                    if budget is None:
                        raise EngineError(
                            _spent_budget_detail(str(error), timeout)
                        ) from error
                    _discard_log(log_path)
                    fault_retries += 1
                    attempt_prompt = prompt + _tool_fault_feedback(detail)
                    progress.notify(
                        "Antigravity discarded its own review with a failed tool "
                        f"call; reviewing again with that feedback: {detail}"
                    )
                    attempt_timeout = budget
                    continue
                if not _is_retryable_antigravity_failure(detail):
                    raise
                delay = ANTIGRAVITY_RETRY_DELAY_SECONDS * (2**attempt)
                budget = _attempt_budget(deadline, delay)
                if budget is None:
                    raise EngineError(
                        _spent_budget_detail(str(error), timeout)
                    ) from error
                _discard_log(log_path)
                progress.notify(
                    "Transient Antigravity failure on attempt "
                    f"{attempt + 1}/{ANTIGRAVITY_MAX_RETRIES + 1}; "
                    f"retrying in {delay}s with {budget}s of the {timeout}s "
                    f"budget left: {detail}"
                )
                time.sleep(delay)
                attempt_timeout = budget
                continue
            _discard_log(log_path)
            return structured_output

        raise EngineError("Antigravity exhausted every review attempt")

    def _structured_output(
        self,
        output: str,
        log_path: Path,
        error_output: str = "",
    ) -> Any:
        """Return the reviewed payload an Antigravity result envelope carries."""
        try:
            envelope = json.loads(output)
        except json.JSONDecodeError as error:
            raise EngineError(f"Antigravity returned invalid JSON: {error}") from error
        if not isinstance(envelope, dict):
            raise EngineError("Antigravity returned an unexpected response")
        if envelope.get("status") != "SUCCESS":
            detail = _envelope_error_detail(envelope)
            reported = f"Antigravity review failed with status {envelope.get('status')!r}"
            if detail:
                reported += f": {detail}"
            raise _EnvelopeFailure(
                f"{reported}{_retained_log_suffix(log_path)}",
                detail,
            )
        if "structured_output" in envelope:
            return envelope["structured_output"]
        response = envelope.get("response")
        if isinstance(response, str):
            if not response.strip():
                denial = _denied_actions_detail(envelope) or _extract_headless_denial(
                    error_output, log_path
                )
                if denial:
                    raise _EnvelopeFailure(
                        "Antigravity abandoned the review over a tool call it "
                        f"could not have approved: {denial}",
                        denial,
                    )
                raise EngineError(
                    "Antigravity reported SUCCESS but returned an empty response"
                )
            try:
                payload = json.loads(response)
            except json.JSONDecodeError as error:
                raise EngineError(
                    "Antigravity response is missing structured_output and its "
                    f"response field is invalid JSON: {error}"
                ) from error
            if isinstance(payload, dict):
                return payload
        raise EngineError(
            "Antigravity response is missing usable structured_output"
        )
