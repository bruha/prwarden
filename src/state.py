import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


TERMINAL_STATUSES = {
    "delivered",
    "no_findings",
    "approval_withheld",
    "delivery_uncertain",
    "skipped_oversized",
    # Keyed by pull request rather than by head: one manual-review message per
    # pull request, however many times its author pushes to it.
    "oversized_notified",
    # The same request for a repository kept out of Slack: there is nowhere to send
    # it, and retrying every run would never change that.
    "oversized_not_notified",
}


class StateError(RuntimeError):
    """Raised when the local state file or process lock cannot be used."""


class StateLockedError(StateError):
    """Raised when another live run already holds the process lock.

    This is the lock working, not a fault: the other run is reviewing the same
    pull requests against the same state, so there is nothing for this one to
    do. Callers report it as a safe skip.
    """


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Optional[Any] = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                if self.handle.read(1) == b"":
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            self.handle.close()
            self.handle = None
            raise StateLockedError(
                f"Another reviewer process holds {self.path}"
            ) from error
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.handle is None:
            return
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


class JsonStateStore:
    """Atomic delivery state, safe for concurrent reviews in one process.

    ``FileLock`` keeps other hosts and processes out for the whole run. Reviews
    running in parallel threads share this instance, so every read and write of
    the in-memory records is serialized here as well; a record must never be
    saved from a dictionary another thread is mutating.
    """

    def __init__(self, path: Path, *, lock_enabled: bool = True):
        self.path = path
        self.lock = (
            FileLock(path.with_suffix(path.suffix + ".lock"))
            if lock_enabled
            else None
        )
        self.data: dict[str, Any] = {"version": 1, "records": {}}
        self._records_lock = threading.RLock()

    def __enter__(self) -> "JsonStateStore":
        if self.lock is not None:
            self.lock.__enter__()
        try:
            self._load()
        except Exception:
            if self.lock is not None:
                self.lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        if self.lock is not None:
            self.lock.__exit__(exc_type, exc_value, traceback)

    @staticmethod
    def key(repository: str, pr_number: int, head_sha: str, destination: str) -> str:
        return f"{repository}#{pr_number}#{head_sha}#{destination}"

    @staticmethod
    def pull_request_key(repository: str, pr_number: int, destination: str) -> str:
        """Return a key for work done once per pull request rather than per head.

        The code level is keyed by head because a new head is a new set of lines to
        judge. Advisory work that reads the whole change rather than its lines is
        settled for the pull request itself.
        """

        return f"{repository}#{pr_number}#{destination}"

    def is_settled_for_pull_request(
        self,
        repository: str,
        pr_number: int,
        destination: str,
    ) -> bool:
        """Return whether this destination is settled for the pull request.

        Any head counts, which is what makes the change to per-pull-request keying
        safe on existing state: a pull request that already received this work under
        a head key is settled, so switching keys cannot deliver a second time.
        """

        prefix = "{0}#{1}#".format(repository, pr_number)
        suffix = "#{0}".format(destination)
        with self._records_lock:
            records = dict(self.data["records"])
        for key, record in records.items():
            if not key.startswith(prefix) or not key.endswith(suffix):
                continue
            if isinstance(record, dict) and record.get("status") in TERMINAL_STATUSES:
                return True
        return False

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StateError(f"Cannot read state file {self.path}: {error}") from error
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("records"), dict)
        ):
            raise StateError(f"Unsupported state structure in {self.path}")
        self.data = payload

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(self.data, temporary, indent=2, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        except OSError as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StateError(f"Cannot write state file {self.path}: {error}") from error

    def get(self, key: str) -> Optional[dict[str, Any]]:
        with self._records_lock:
            record = self.data["records"].get(key)
            return dict(record) if isinstance(record, dict) else None

    def is_terminal(self, key: str) -> bool:
        record = self.get(key)
        return bool(record and record.get("status") in TERMINAL_STATUSES)

    def set_status(self, key: str, status: str, **details: Any) -> None:
        record = {
            "status": status,
            "updated_at": _timestamp(),
            **details,
        }
        with self._records_lock:
            self.data["records"][key] = record
            self._save()
