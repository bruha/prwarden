import filecmp
import json
import os
import re
import shutil
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Callable, Optional, Sequence, Type

from .commands import CommandError, run_cmd
from .github import run_git_for
from .models import DependencyLink, PullRequest, Repository


class WorkspaceError(RuntimeError):
    """Raised when an exact pull-request workspace cannot be prepared."""


DEPENDENCY_DIRECTORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "node_modules",
        ("bun.lock", "bun.lockb", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"),
    ),
)


CLONE_TIMEOUT_SECONDS = 1800
# The attribute GitHub reads to collapse a machine-made file in a pull request.
GENERATED_ATTRIBUTE = "linguist-generated"
# `git check-attr --source`, which reads attributes from a commit rather than from
# the working tree, arrived in Git 2.40.
ATTRIBUTE_SOURCE_GIT = (2, 40)
GIT_VERSION_PATTERN = re.compile(r"git version (\d+)\.(\d+)")


class UnsupportedGitError(WorkspaceError):
    """Raised when the installed Git cannot read attributes from a commit."""


def _git_version() -> "Optional[tuple[int, int]]":
    """Return the installed Git's major and minor version, or None when unknown."""

    try:
        match = GIT_VERSION_PATTERN.search(run_cmd(["git", "--version"]))
    except CommandError:
        return None
    return (int(match.group(1)), int(match.group(2))) if match else None


def ensure_managed_checkout(repository: Repository) -> Path:
    """Clone a repository PR Warden manages itself the first time it is needed.

    The clone carries Git objects only: every review reads its own detached
    worktree, so the clone's own working tree is never checked out. It is made in a
    temporary directory beside its final place and renamed there once complete, so
    an interrupted clone is never mistaken for a finished one. The caller holds the
    checkout lock, which keeps two reviews in this process from cloning it twice.
    """

    checkout = repository.path
    if checkout is None or not repository.managed_checkout:
        raise WorkspaceError(f"{repository.slug} is not a managed clone")
    if (checkout / ".git").exists():
        return checkout
    checkout.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(
        tempfile.mkdtemp(prefix=f".{checkout.name}.partial-", dir=checkout.parent)
    )
    print(f"    - Cloning {repository.slug} into {checkout}, once", flush=True)
    try:
        run_git_for(
            repository,
            [
                "clone",
                "--quiet",
                "--no-checkout",
                f"https://github.com/{repository.slug}.git",
                str(partial),
            ],
            timeout_seconds=CLONE_TIMEOUT_SECONDS,
        )
        # A run started at the same time may have finished first; its clone wins.
        if not (checkout / ".git").exists():
            os.replace(partial, checkout)
    except (CommandError, OSError) as error:
        raise WorkspaceError(f"Cannot clone {repository.slug}: {error}") from error
    finally:
        if partial.exists():
            shutil.rmtree(partial, ignore_errors=True)
    return checkout


def remote_matches_slug(remote_url: str, slug: str) -> bool:
    pattern = re.compile(rf"(?:[:/]){re.escape(slug)}(?:\.git)?/?$")
    return bool(pattern.search(remote_url.strip()))


def _identical_lockfiles(
    worktree: Path,
    checkout: Path,
    lockfile_names: tuple[str, ...],
) -> "tuple[tuple[str, ...], Optional[str]]":
    """Return the lockfiles proving both trees resolve dependencies identically.

    The second element explains why they do not, so a run can report whether a
    pull request changed its dependencies or the local checkout has drifted from
    the head being reviewed. Both leave installed contents untrustworthy as
    whole-directory evidence, but only one of them is worth fixing locally.
    """

    matched: list[str] = []
    for name in lockfile_names:
        head_lockfile = worktree / name
        if not head_lockfile.is_file():
            continue
        checkout_lockfile = checkout / name
        if not checkout_lockfile.is_file():
            return (), f"{name} is missing from the local checkout"
        try:
            if not filecmp.cmp(head_lockfile, checkout_lockfile, shallow=False):
                return (), f"{name} differs from the local checkout"
        except OSError:
            return (), f"{name} could not be compared with the local checkout"
        matched.append(name)
    if not matched:
        return (), "the reviewed head declares no known lockfile"
    return tuple(matched), None


MAX_LINKED_PACKAGES = 4000


def _npm_resolved_versions(raw: str) -> "Optional[dict[str, str]]":
    """Read top-level resolved versions from an npm lockfile."""
    try:
        document = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(document, dict):
        return None
    versions: "dict[str, str]" = {}
    packages = document.get("packages")
    if isinstance(packages, dict):
        for key, entry in packages.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            if not key.startswith("node_modules/"):
                continue
            name = key[len("node_modules/") :]
            # A nested path describes one dependency's private copy, which is
            # not the version the workspace resolves for that name.
            if "node_modules/" in name:
                continue
            version = entry.get("version")
            if isinstance(version, str) and version:
                versions[name] = version
        return versions
    dependencies = document.get("dependencies")
    if isinstance(dependencies, dict):
        for name, entry in dependencies.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            version = entry.get("version")
            if isinstance(version, str) and version:
                versions[name] = version
        return versions
    return None


_BUN_PACKAGE_LINE = re.compile(r'^\s*"(?P<name>[^"]+)":\s*\[\s*"(?P<spec>[^"]+)"')


def _bun_resolved_versions(raw: str) -> "Optional[dict[str, str]]":
    """Read resolved versions from a bun lockfile.

    Bun writes JSON with trailing commas, which no standard-library parser
    accepts, so each package line is read on its own. A line that does not match
    is skipped: missing a package costs one piece of evidence, while guessing at
    one would present the wrong version as fact.
    """
    versions: "dict[str, str]" = {}
    inside = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not inside:
            inside = stripped.startswith('"packages"')
            continue
        if stripped in ("}", "},"):
            break
        match = _BUN_PACKAGE_LINE.match(line)
        if match is None:
            continue
        name, _, version = match.group("spec").rpartition("@")
        if name and version and name == match.group("name"):
            versions[name] = version
    return versions or None


_LOCKFILE_READERS = {
    "bun.lock": _bun_resolved_versions,
    "package-lock.json": _npm_resolved_versions,
}


def _unchanged_packages(
    worktree: Path,
    checkout: Path,
    lockfile_names: tuple[str, ...],
) -> tuple[str, ...]:
    """Return packages both trees resolve to the same version.

    A lockfile that changed does not change every dependency, and the ones it
    leaves alone are still installed at the version the reviewed head resolves.
    Only formats this module can read contribute, and a package must agree in
    every readable lockfile the head declares.
    """

    agreed: "Optional[set[str]]" = None
    for name in lockfile_names:
        reader = _LOCKFILE_READERS.get(name)
        head_lockfile = worktree / name
        checkout_lockfile = checkout / name
        if reader is None or not head_lockfile.is_file():
            continue
        if not checkout_lockfile.is_file():
            return ()
        try:
            head_versions = reader(head_lockfile.read_text(encoding="utf-8"))
            checkout_versions = reader(
                checkout_lockfile.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError):
            return ()
        if head_versions is None or checkout_versions is None:
            return ()
        matching = {
            package
            for package, version in head_versions.items()
            if checkout_versions.get(package) == version
        }
        agreed = matching if agreed is None else (agreed & matching)
    if not agreed:
        return ()
    return tuple(sorted(agreed)[:MAX_LINKED_PACKAGES])


_checkout_locks_guard = threading.Lock()
_checkout_locks: "dict[Path, threading.RLock]" = {}


def _checkout_lock(checkout: Optional[Path]) -> "threading.RLock":
    """Return the lock that serializes git work against one local checkout.

    Reviews run in parallel, and Git refuses concurrent index, ref, and
    worktree updates in the same repository. Preparing and removing a worktree
    is short, so serializing per checkout costs little; the long model
    invocation happens outside this lock.
    """
    key = Path(checkout) if checkout is not None else Path(".")
    try:
        key = key.resolve()
    except OSError:
        pass
    with _checkout_locks_guard:
        lock = _checkout_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _checkout_locks[key] = lock
        return lock


MAX_BASE_FILE_BYTES = 200_000
MAX_BASE_FILES = 60


def read_base_files(
    repository: Repository,
    commit: str,
    pathspecs: "tuple[str, ...]",
    wanted: "Callable[[str], bool]",
) -> "list[tuple[str, str]]":
    """Return the path and text of each wanted file in ``commit``, a trusted commit.

    The commit is fetched into the local checkout and read from Git's objects, never
    checked out, so nothing in the checkout or the review worktree changes. Only
    regular files under ``pathspecs`` that ``wanted`` accepts are read; one too large
    to be prose, or not UTF-8, is skipped.
    """

    checkout = repository.path
    if checkout is None:
        raise WorkspaceError(
            f"Repository {repository.slug} needs a local checkout path"
        )
    fetch = [
        "-C",
        str(checkout),
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-write-fetch-head",
        "origin",
        commit,
    ]
    documents: "list[tuple[str, str]]" = []
    try:
        with _checkout_lock(checkout):
            _fetch_trusted_commit(repository, fetch)
            listing = run_cmd(
                ["git", "-C", str(checkout), "ls-tree", "-r", "-l", "-z", commit, "--"]
                + list(pathspecs)
            )
            entries = []
            for entry in listing.split("\0"):
                metadata, _, path = entry.partition("\t")
                fields = metadata.split()
                if len(fields) != 4 or not path or not wanted(path):
                    continue
                mode, kind, blob, size = fields
                if kind != "blob" or mode not in ("100644", "100755"):
                    continue
                if not size.isdigit() or not 0 < int(size) <= MAX_BASE_FILE_BYTES:
                    continue
                entries.append((path, blob))
            for path, blob in entries[:MAX_BASE_FILES]:
                try:
                    text = run_cmd(
                        ["git", "-C", str(checkout), "cat-file", "blob", blob]
                    )
                except UnicodeDecodeError:
                    continue
                documents.append((path, text))
    except CommandError as error:
        raise WorkspaceError(
            f"Cannot read {repository.slug} at {commit[:12]}: {error}"
        ) from error
    return documents


def _fetch_trusted_commit(repository: Repository, fetch: "list[str]") -> None:
    """Fetch a trusted commit into the local checkout; the caller holds its lock."""

    if repository.managed_checkout:
        ensure_managed_checkout(repository)
        run_git_for(repository, fetch, timeout_seconds=180)
    else:
        run_cmd(["git", *fetch], timeout_seconds=180)


def read_generated_marks(
    repository: Repository,
    commit: str,
    paths: "Sequence[str]",
) -> "dict[str, Optional[bool]]":
    """Return how ``commit``, a trusted commit, marks each path as generated.

    True where its `.gitattributes` set `linguist-generated`, the mark GitHub reads
    to collapse a machine-made file in a diff, False where they unset it or set it
    false, and None where they say nothing. Git does the matching itself, so nested
    files, later lines overriding earlier ones, and negations mean what they mean
    to Git and GitHub. `--source` reads the commit's own files rather than the
    checkout's, which may sit on any branch; it needs Git 2.40, and an older Git
    fails here like an unreadable commit. The paths go on standard input, so no
    file name is ever read as an option.
    """

    if not paths:
        return {}
    version = _git_version()
    if version is not None and version < ATTRIBUTE_SOURCE_GIT:
        raise UnsupportedGitError(
            f"Git {version[0]}.{version[1]} cannot read attributes from a commit; "
            f"Git {ATTRIBUTE_SOURCE_GIT[0]}.{ATTRIBUTE_SOURCE_GIT[1]} or newer can"
        )
    checkout = repository.path
    if checkout is None:
        raise WorkspaceError(
            f"Repository {repository.slug} needs a local checkout path"
        )
    fetch = [
        "-C",
        str(checkout),
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-write-fetch-head",
        "origin",
        commit,
    ]
    try:
        with _checkout_lock(checkout):
            _fetch_trusted_commit(repository, fetch)
            output = run_cmd(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "check-attr",
                    "-z",
                    "--stdin",
                    f"--source={commit}",
                    GENERATED_ATTRIBUTE,
                ],
                input_text="".join(f"{path}\0" for path in paths),
            )
    except CommandError as error:
        raise WorkspaceError(
            f"Cannot read the {GENERATED_ATTRIBUTE} marks of {repository.slug} at "
            f"{commit[:12]}: {error}"
        ) from error
    fields = output.split("\0")
    marks: "dict[str, Optional[bool]]" = {}
    for index in range(0, len(fields) - 2, 3):
        path, attribute, value = fields[index : index + 3]
        if attribute != GENERATED_ATTRIBUTE:
            continue
        if value in ("set", "true"):
            marks[path] = True
        elif value in ("unset", "false"):
            marks[path] = False
    return {path: marks.get(path) for path in paths}


class ReviewWorkspace:
    def __init__(
        self,
        repository: Repository,
        pull_request: PullRequest,
        workspace_root: Path,
    ):
        self.repository = repository
        self.pull_request = pull_request
        self.workspace_root = workspace_root
        self.path: Optional[Path] = None
        self.dependency_links: tuple[DependencyLink, ...] = ()
        self.dependency_notes: tuple[str, ...] = ()

    def __enter__(self) -> Repository:
        checkout = self.repository.path
        if checkout is None:
            raise WorkspaceError(
                f"Repository {self.repository.slug} needs a local checkout path"
            )
        with _checkout_lock(checkout):
            if self.repository.managed_checkout:
                ensure_managed_checkout(self.repository)
            return self._create(checkout)

    def _create(self, checkout: Path) -> Repository:
        """Fetch the exact head and add its detached worktree."""
        if not checkout.is_dir():
            raise WorkspaceError(f"Configured checkout does not exist: {checkout}")

        try:
            inside_worktree = run_cmd(
                ["git", "-C", str(checkout), "rev-parse", "--is-inside-work-tree"]
            )
            if inside_worktree != "true":
                raise WorkspaceError(f"Configured checkout is not a Git worktree: {checkout}")
            remote_url = run_cmd(
                ["git", "-C", str(checkout), "remote", "get-url", "origin"]
            )
        except CommandError as error:
            raise WorkspaceError(
                f"Cannot validate configured checkout {checkout}: {error}"
            ) from error

        if not remote_matches_slug(remote_url, self.repository.slug):
            raise WorkspaceError(
                f"Checkout origin does not match {self.repository.slug}: {remote_url}"
            )

        self.workspace_root.mkdir(parents=True, exist_ok=True)
        safe_slug = self.repository.slug.replace("/", "-")
        prefix = (
            f"{safe_slug}-pr-{self.pull_request.number}-"
            f"{self.pull_request.head_sha[:12]}-"
        )
        self.path = Path(tempfile.mkdtemp(prefix=prefix, dir=self.workspace_root))
        self.path.rmdir()

        fetch = [
            "-C",
            str(checkout),
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "origin",
            self.pull_request.head_sha,
        ]
        try:
            # A clone of the operator's fetches with the operator's own Git
            # credentials; a managed clone has none, so GitHub CLI supplies them.
            if self.repository.managed_checkout:
                run_git_for(self.repository, fetch, timeout_seconds=180)
            else:
                run_cmd(["git", *fetch], timeout_seconds=180)
            run_cmd(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "cat-file",
                    "-e",
                    f"{self.pull_request.head_sha}^{{commit}}",
                ]
            )
            run_cmd(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "worktree",
                    "add",
                    "--quiet",
                    "--detach",
                    str(self.path),
                    self.pull_request.head_sha,
                ],
                timeout_seconds=180,
            )
        except CommandError as error:
            self._cleanup_failed_setup()
            raise WorkspaceError(
                f"Cannot prepare PR head {self.pull_request.head_sha[:12]} for "
                f"{self.repository.slug}#{self.pull_request.number}: {error}"
            ) from error

        self.dependency_links = self._link_installed_dependencies(checkout)

        return replace(self.repository, path=self.path)

    def _link_installed_dependencies(self, checkout: Path) -> tuple[DependencyLink, ...]:
        """Expose the checkout's installed dependencies for read-only verification.

        The whole directory is linked when every known lockfile declared by the
        reviewed head is byte-identical to the configured checkout. When one
        differs, the dependencies it left alone are still installed at the version
        the head resolves, so those packages are linked individually rather than
        withholding all dependency evidence from a pull request that touched its
        lockfile. Every link is a symlink: cleanup unlinks it and never deletes the
        checkout's contents.
        """

        if self.path is None:
            return ()
        links: list[DependencyLink] = []
        notes: list[str] = []
        for name, lockfile_names in DEPENDENCY_DIRECTORIES:
            source = checkout / name
            destination = self.path / name
            try:
                if not source.is_dir():
                    notes.append(f"{name} is not installed in the local checkout")
                    continue
                if destination.exists():
                    continue
                lockfiles, mismatch = _identical_lockfiles(
                    self.path,
                    checkout,
                    lockfile_names,
                )
                if lockfiles:
                    destination.symlink_to(
                        source.resolve(),
                        target_is_directory=True,
                    )
                    links.append(
                        DependencyLink(
                            name=name,
                            target=str(source.resolve()),
                            lockfiles=lockfiles,
                        )
                    )
                    continue
                packages = _unchanged_packages(self.path, checkout, lockfile_names)
                linked = self._link_packages(source, destination, packages)
                if linked:
                    links.append(
                        DependencyLink(
                            name=name,
                            target=str(source.resolve()),
                            packages=linked,
                        )
                    )
                    counted = (
                        f"{linked} {name} packages resolved to the same version "
                        "are linked"
                        if linked != 1
                        else f"1 {name} package resolved to the same version is "
                        "linked"
                    )
                    notes.append(f"{mismatch}, so only the {counted}")
                else:
                    notes.append(f"no {name} linked because {mismatch}")
            except OSError as error:
                notes.append(f"no {name} linked: {error.strerror or error}")
                continue
        self.dependency_notes = tuple(notes)
        return tuple(links)

    def _link_packages(
        self,
        source: Path,
        destination: Path,
        packages: tuple[str, ...],
    ) -> int:
        """Link each named package, and report how many are readable."""

        if not packages:
            return 0
        linked = 0
        for package in packages:
            installed = source / package
            target = destination / package
            try:
                if not installed.is_dir() or target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(installed.resolve(), target_is_directory=True)
            except OSError:
                continue
            linked += 1
        return linked

    def _cleanup_failed_setup(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            run_cmd(
                [
                    "git",
                    "-C",
                    str(self.repository.path),
                    "worktree",
                    "remove",
                    "--force",
                    str(self.path),
                ]
            )
        except CommandError:
            self._remove_orphaned_path()

    def _remove_orphaned_path(self) -> Optional[BaseException]:
        if self.path is None:
            return None

        cleanup_error: Optional[BaseException] = None
        for _ in range(3):
            if not self.path.exists():
                break
            try:
                shutil.rmtree(self.path)
                cleanup_error = None
            except OSError as error:
                cleanup_error = error

        try:
            run_cmd(
                [
                    "git",
                    "-C",
                    str(self.repository.path),
                    "worktree",
                    "prune",
                ]
            )
        except CommandError as error:
            cleanup_error = error

        if self.path.exists() and cleanup_error is None:
            cleanup_error = OSError(f"temporary path still exists: {self.path}")
        return cleanup_error

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        if self.path is None:
            return
        try:
            with _checkout_lock(self.repository.path):
                run_cmd(
                    [
                        "git",
                        "-C",
                        str(self.repository.path),
                        "worktree",
                        "remove",
                        "--force",
                        str(self.path),
                    ],
                    timeout_seconds=120,
                )
        except CommandError as error:
            cleanup_error = self._remove_orphaned_path()
            if exc_type is None and cleanup_error is not None:
                raise WorkspaceError(
                    f"Cannot remove temporary review workspace {self.path}: {error}; "
                    f"fallback cleanup failed: {cleanup_error}"
                ) from cleanup_error
            if exc_type is None:
                return
        finally:
            try:
                self.workspace_root.rmdir()
            except OSError:
                pass


def review_workspace(
    repository: Repository,
    pull_request: PullRequest,
    workspace_root: Path,
) -> ReviewWorkspace:
    return ReviewWorkspace(repository, pull_request, workspace_root)
