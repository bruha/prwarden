import os
import subprocess
import sys
import time

from . import progress
from pathlib import Path
from typing import Mapping, Optional, Sequence


class CommandError(RuntimeError):
    """Raised when an external command cannot complete successfully.

    ``returncode`` and ``error_output`` are set when the command ran and exited
    non-zero, so a caller can explain the failure from the command's own report
    rather than from the whole of standard error, which some CLIs fill with the
    prompt they were given.
    """

    def __init__(
        self,
        message: str,
        *,
        command: Optional[str] = None,
        output: Optional[str] = None,
        returncode: Optional[int] = None,
        error_output: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.output = output
        self.returncode = returncode
        self.error_output = error_output


TRANSIENT_COMMAND_ERROR_MARKERS = (
    "bad gateway",
    "connection refused",
    "connection reset",
    "connection timed out",
    "could not resolve host",
    "error connecting to",
    "gateway timeout",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "internal server error",
    "network is unreachable",
    "no such host",
    "operation timed out",
    "rate limit",
    "service unavailable",
    "temporary failure",
    "temporarily unavailable",
    "tls handshake timeout",
    "unexpected eof",
)


def _is_transient_command_error(error: CommandError) -> bool:
    detail = str(error).casefold()
    return detail.startswith("command timed out") or any(
        marker in detail for marker in TRANSIENT_COMMAND_ERROR_MARKERS
    )


def run_cmd(
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    input_text: Optional[str] = None,
    timeout_seconds: int = 60,
    environment: Optional[Mapping[str, str]] = None,
    inherit_environment: bool = True,
    retry_transient: bool = False,
    retry_attempts: int = 3,
    retry_delay_seconds: float = 15,
) -> str:
    """Return what a successful command printed to standard output."""

    return run_cmd_streams(
        args,
        cwd=cwd,
        input_text=input_text,
        timeout_seconds=timeout_seconds,
        environment=environment,
        inherit_environment=inherit_environment,
        retry_transient=retry_transient,
        retry_attempts=retry_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )[0]


def run_cmd_streams(
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    input_text: Optional[str] = None,
    timeout_seconds: int = 60,
    environment: Optional[Mapping[str, str]] = None,
    inherit_environment: bool = True,
    retry_transient: bool = False,
    retry_attempts: int = 3,
    retry_delay_seconds: float = 15,
) -> "tuple[str, str]":
    """Return both streams of a successful command.

    A command can exit zero and still explain on standard error that it produced
    nothing useful, which is the only place some CLIs report a refused tool call.
    Callers that must diagnose such a result need the stream that carries it.
    """

    process_environment = None
    if environment is not None or not inherit_environment:
        process_environment = os.environ.copy() if inherit_environment else {}
        if environment is not None:
            process_environment.update(environment)
    for attempt in range(retry_attempts + 1):
        try:
            result = subprocess.run(
                list(args),
                cwd=cwd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=process_environment,
            )
        except FileNotFoundError as error:
            raise CommandError(
                f"Required command not found: {args[0]}",
                command=args[0],
            ) from error
        except subprocess.TimeoutExpired:
            command_error = CommandError(
                f"Command timed out after {timeout_seconds}s: {args[0]}",
                command=args[0],
            )
        else:
            if result.returncode == 0:
                return result.stdout.strip(), result.stderr.strip()
            detail = result.stderr.strip() or "no error output"
            command_error = CommandError(
                f"{args[0]} exited with {result.returncode}: {detail}",
                command=args[0],
                output=result.stdout.strip() or None,
                returncode=result.returncode,
                error_output=result.stderr.strip(),
            )

        if (
            not retry_transient
            or attempt >= retry_attempts
            or not _is_transient_command_error(command_error)
        ):
            raise command_error

        delay = min(retry_delay_seconds * (2**attempt), 60)
        progress.notify(
            f"Transient {args[0]} failure on attempt {attempt + 1}/"
            f"{retry_attempts + 1}; retrying in {delay:g}s: {command_error}"
        )
        time.sleep(delay)

    raise AssertionError("unreachable")
