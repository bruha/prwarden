"""Coordination for the terminal's in-place progress line and captured output.

An in-place progress line owns the last terminal row and rewrites it without a
newline. Any other message written while it is displayed lands on the same row
and leaves the progress text stranded in the scrollback, so long-running code
that reports a retry must be able to interrupt the line first.

Reviews also run in parallel, and their reports would interleave into an
unreadable stream. Rather than thread an output object through every call, this
module can install thread-aware ``sys.stdout`` and ``sys.stderr`` routers: a
thread that binds a recorder keeps its own output, and every other thread still
writes straight to the terminal.

A run can also keep a copy of everything it reports, so output that scrolled
past, or that a scheduled run never wrote anywhere, can be read afterwards. The
copy holds the text a run produced, not the frames it drew: an in-place line and
the review board resolve the terminal behind any wrapper and write only there.

The controller renders the line and owns the phases; adapters and command
helpers only report around them. This module holds the small amount of shared
state that makes both safe from different threads, so neither side has to
import the other.
"""

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Protocol, TextIO, Tuple


class ProgressLine(Protocol):
    """The rendering behavior this module coordinates."""

    def suspend(self) -> None:
        """Erase the displayed line so other output can start on a clean row."""


TRANSCRIPT_NAME_ATTEMPTS = 20
TRANSCRIPT_FILE_MODE = 0o600
SECONDS_PER_DAY = 86400

_render_lock = threading.RLock()
_active: Optional[ProgressLine] = None


@contextmanager
def rendering() -> Iterator[None]:
    """Hold the terminal row so a frame and a message cannot interleave."""
    with _render_lock:
        yield


def set_active(line: ProgressLine) -> None:
    """Register the progress line that currently owns the terminal row."""
    global _active
    with _render_lock:
        _active = line


def release_active(line: ProgressLine) -> None:
    """Unregister ``line``, ignoring a line that is no longer active."""
    global _active
    with _render_lock:
        if _active is line:
            _active = None


def notify(message: str, *, stream: Optional[TextIO] = None) -> None:
    """Report ``message`` on its own row, erasing any displayed progress line.

    The progress line redraws itself on its next frame, so an interruption
    scrolls the message into place without stopping the elapsed display.
    """
    target = stream if stream is not None else sys.stderr
    with _render_lock:
        if _active is not None:
            _active.suspend()
        print(message, file=target, flush=True)


class Recorder:
    """Collect one review's output, keeping the order of both streams."""

    def __init__(self, label: str = "") -> None:
        self._chunks: List[Tuple[bool, str]] = []
        self._lock = threading.Lock()
        self._label = label
        self._pending = ""

    def record(self, text: str, *, error: bool) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append((error, text))
            steps = self._steps_in(text) if self._label else ()
        # Journaled outside the lock: the sink is a file shared by every review,
        # and holding two locks in one order here and the other order elsewhere
        # is how a run would deadlock instead of reporting.
        stamp = time.strftime("%H:%M:%S")
        for step in steps:
            journal(f"{stamp}  {self._label}  {step}")

    def _steps_in(self, text: str) -> "tuple[str, ...]":
        """Return the complete step reports this chunk finished.

        A recorded chunk is whatever a ``print`` produced, so a line can arrive
        in pieces; the remainder is held until its newline rather than journaled
        as a truncated step.
        """

        lines = (self._pending + text).split("\n")
        self._pending = lines.pop()
        return tuple(
            line.strip() for line in lines if line.strip().startswith("-> ")
        )

    def last_step(self) -> str:
        """Return the latest reported step, for a caller rendering a status.

        Only the controller's own ``-> step`` reports qualify, so a status never
        shows a wrapped diagnostic or the pull request's own header line.
        """
        with self._lock:
            joined = "".join(text for _, text in self._chunks)
        for line in reversed(joined.splitlines()):
            stripped = line.strip()
            if stripped.startswith("-> "):
                return stripped[3:].strip()
        return ""

    def text(self) -> str:
        """Return every recorded chunk as one block, both streams merged."""
        with self._lock:
            return "".join(text for _, text in self._chunks)

    def replay(
        self,
        out: Optional[TextIO] = None,
        err: Optional[TextIO] = None,
    ) -> None:
        """Write the recording out, sending error chunks back to ``err``.

        Both streams often lead to one redirected log file, and each keeps its
        own buffer, so a chunk is flushed as soon as it is written. Without that
        the whole recording would reach the file in stream order rather than the
        order the review reported it.
        """
        out_stream = out if out is not None else sys.stdout
        err_stream = err if err is not None else sys.stderr
        with self._lock:
            chunks = list(self._chunks)
        for error, text in chunks:
            stream = err_stream if error else out_stream
            stream.write(text)
            stream.flush()


class _RoutedStream:
    """A stdout or stderr stand-in that honors the calling thread's recorder."""

    def __init__(self, base: TextIO, *, error: bool) -> None:
        self._base = base
        self._error = error
        self._local = threading.local()

    def bind(self, recorder: Recorder) -> None:
        self._local.recorder = recorder

    def unbind(self) -> None:
        self._local.recorder = None

    def _recorder(self) -> Optional[Recorder]:
        return getattr(self._local, "recorder", None)

    def write(self, data: str) -> int:
        recorder = self._recorder()
        if recorder is None:
            return self._base.write(data)
        recorder.record(data, error=self._error)
        return len(data)

    def flush(self) -> None:
        if self._recorder() is None:
            self._base.flush()

    def isatty(self) -> bool:
        # Recorded output is not a terminal, which keeps in-place rendering out
        # of a capture buffer without every caller having to ask.
        if self._recorder() is not None:
            return False
        try:
            return self._base.isatty()
        except (AttributeError, OSError):
            return False

    def fileno(self) -> int:
        return self._base.fileno()

    @property
    def base(self) -> TextIO:
        """Return the real stream, for output that must reach the terminal."""
        return self._base

    def __getattr__(self, name: str) -> object:
        return getattr(self._base, name)


@contextmanager
def routed_output() -> Iterator[None]:
    """Install thread-aware stdout and stderr for the duration of the block."""
    original_out, original_err = sys.stdout, sys.stderr
    sys.stdout = _RoutedStream(original_out, error=False)  # type: ignore[assignment]
    sys.stderr = _RoutedStream(original_err, error=True)  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.stdout, sys.stderr = original_out, original_err


@contextmanager
def recording(label: str = "") -> Iterator[Recorder]:
    """Keep this thread's output in a recorder instead of the terminal.

    A label makes the recording report its steps to the run journal as they
    happen, which is the only live view of a parallel run: the board draws on the
    terminal alone, and a recorded report is printed after every review ends.
    """
    recorder = Recorder(label)
    routed = [
        stream
        for stream in (sys.stdout, sys.stderr)
        if isinstance(stream, _RoutedStream)
    ]
    for stream in routed:
        stream.bind(recorder)
    try:
        yield recorder
    finally:
        for stream in routed:
            stream.unbind()


class _TeeStream:
    """Write to the terminal and to a run's transcript at the same time.

    Only text a run reports is copied. Progress frames reach the terminal
    through :func:`terminal_stream`, which unwraps this object, so the
    transcript never fills with redrawn rows.
    """

    def __init__(self, base: TextIO, transcript: TextIO) -> None:
        self._base = base
        self._transcript = transcript

    @property
    def base(self) -> TextIO:
        return self._base

    def write(self, text: str) -> int:
        written = self._base.write(text)
        try:
            self._transcript.write(text)
            # Both streams share one file, so a chunk is flushed as it is
            # written to keep the transcript in reported order.
            self._transcript.flush()
        except (OSError, ValueError):
            pass
        return written

    def flush(self) -> None:
        self._base.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._base.isatty())
        except (AttributeError, OSError, ValueError):
            return False

    def fileno(self) -> int:
        return self._base.fileno()

    def __getattr__(self, name: str) -> object:
        return getattr(self._base, name)


class _ProgressStream:
    """Draw in-place frames on the terminal, and only where they belong.

    Rendering has two independent requirements that no single existing stream
    satisfies. Frames must never enter a run transcript or a captured recording,
    so they are written past both wrappers to the terminal itself. Yet a thread
    whose output is being recorded must not draw at all, because its report is
    printed later and the board owns the live row meanwhile. So writes bypass the
    wrappers while the interactivity question is still put to the stream the
    caller was given, which answers no while that thread is recording.
    """

    def __init__(self, source: TextIO) -> None:
        self._source = source

    @property
    def base(self) -> TextIO:
        return terminal_stream(self._source)

    def write(self, text: str) -> int:
        return self.base.write(text)

    def flush(self) -> None:
        self.base.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._source.isatty())
        except (AttributeError, OSError, ValueError):
            return False

    def fileno(self) -> int:
        return self.base.fileno()

    def __getattr__(self, name: str) -> object:
        return getattr(terminal_stream(self._source), name)


def progress_stream(stream: TextIO) -> TextIO:
    """Return the stream an in-place progress renderer should draw on."""
    return _ProgressStream(stream)


def terminal_stream(stream: TextIO) -> TextIO:
    """Return the real terminal behind any output wrappers this module installs."""
    while isinstance(stream, (_RoutedStream, _TeeStream, _ProgressStream)):
        stream = stream.base
    return stream


def prune_transcripts(directory: Path, retention_days: int) -> None:
    """Delete transcripts in ``directory`` last written over ``retention_days`` ago.

    Age rather than count decides, so how far back a run can be read does not
    depend on how often the tool happens to have run. Pruning is best-effort:
    losing an old transcript is not worth failing a review over, and a run that
    deletes one another run is still writing simply misses it next time.
    """
    if retention_days < 1:
        raise ValueError("Transcript retention must be at least one day")
    cutoff = time.time() - retention_days * SECONDS_PER_DAY
    try:
        transcripts = list(directory.glob("run-*.log"))
    except OSError:
        return
    for path in transcripts:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


_journal_guard = threading.Lock()
_journal_sink: Optional[TextIO] = None


def journal(line: str) -> None:
    """Write one timestamped line to the active transcript and flush it.

    The line reaches the transcript only, never the terminal, because the board
    already owns the terminal while reviews run. Flushing every line is the point
    of the journal: a run in progress can be followed with ``tail -f`` instead of
    being diagnosed from worktree timestamps. A run without a transcript, or one
    whose transcript is already closed, simply has nowhere to journal.
    """

    with _journal_guard:
        sink = _journal_sink
        if sink is None:
            return
        try:
            sink.write(line if line.endswith("\n") else line + "\n")
            sink.flush()
        except (OSError, ValueError):
            pass


@contextmanager
def transcript(path: Path) -> Iterator[Optional[Path]]:
    """Copy everything the run reports into ``path`` while it runs.

    Runs can overlap: only a live run takes the process lock, and it opens its
    transcript before trying. The file is therefore created exclusively and a
    taken name is stepped past, because truncating another run's transcript
    would destroy the record both runs need.

    A run that cannot open a transcript still has to review, so the failure is
    reported once and the run continues without a copy.
    """
    handle: Optional[TextIO] = None
    resolved = path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(TRANSCRIPT_NAME_ATTEMPTS):
            resolved = (
                path
                if attempt == 0
                else path.with_name(f"{path.stem}-{attempt + 1}{path.suffix}")
            )
            try:
                # A transcript carries repository names, pull-request titles, and
                # finding text, so it is created readable only by its owner
                # whatever umask the run inherited from launchd or a shell.
                descriptor = os.open(
                    str(resolved),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    TRANSCRIPT_FILE_MODE,
                )
            except FileExistsError:
                continue
            handle = os.fdopen(descriptor, "w", encoding="utf-8")
            break
        if handle is None:
            raise OSError(f"{path.name} and every alternative name are taken")
    except OSError as error:
        print(f"Note: no run transcript ({error})", file=sys.stderr)
        yield None
        return
    global _journal_sink
    out, err = sys.stdout, sys.stderr
    sys.stdout = _TeeStream(out, handle)
    sys.stderr = _TeeStream(err, handle)
    with _journal_guard:
        _journal_sink = handle
    try:
        yield resolved
    finally:
        with _journal_guard:
            _journal_sink = None
        sys.stdout, sys.stderr = out, err
        handle.close()
