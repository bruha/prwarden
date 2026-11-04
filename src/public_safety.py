"""Keep the names of reviewed projects out of what this repository publishes.

The reviewer is developed against private repositories, and what it learns there is
easy to write down by accident: a repository name in a comment, a colleague's login
in a fixture, a real pull-request title in an example, a file path from someone
else's codebase. This module checks staged changes and commit messages against a
word list built from the reviewed repositories themselves, and against a private map
of the `[pr:xxxx]` references that stand in for real pull requests.

Both lists live in `.private/`, which is never committed, so this file names nothing
private. Without them there is nothing to check against, and the check says so and
fails rather than passing quietly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import string
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PRIVATE_DIRECTORY = ".private"
TERMS_FILE = "terms.json"
REFS_FILE = "pr-refs.json"
DEFAULT_REPOSITORIES_FILE = "repositories.json"

# Shorter words and titles are too common to tell a project apart, and flagging them
# would teach people to bypass the hook.
MIN_WORD_LENGTH = 4
MIN_TITLE_LENGTH = 16
MIN_BRANCH_LENGTH = 8
GENERIC_BRANCHES = frozenset(
    {"main", "master", "develop", "development", "staging", "release", "head"}
)
GENERIC_BASENAME = re.compile(
    r"(?i)^(readme|license|licence|changelog|contributing|notice|package|"
    r"package-lock|tsconfig|index|main|app|setup|config|\.gitignore|\.gitkeep|"
    r"\.env|\.eslintrc|\.prettierrc|vite\.config|jest\.config)(\.[\w.-]+)?$"
)
CONVENTIONAL_PREFIX = re.compile(r"^\w+(\([^)]*\))?!?:\s*")
PATH_TOKEN = re.compile(r"[\w.@+-]+(?:/[\w.@+-]+){2,}")
REF_PATTERN = re.compile(r"\[pr:([^\]]*)\]")
REF_ALPHABET = string.ascii_lowercase + "234567"
REF_FORMAT = re.compile(r"\A[a-z2-7]{4}\Z")
# Documentation shows the format with this placeholder, which no reference can be.
REF_PLACEHOLDER = "xxxx"
# This module and its tests spell out the reference syntax and malformed examples of
# it. They are exempt from the reference check only; names are still checked there.
REF_CHECK_EXEMPT = ("src/public_safety.py", "tests/test_public_safety.py")
HOOK_MARKER = "# Installed by: python3 -m src.public_safety install-hooks"
HOOKS = {
    "pre-commit": "python3 -m src.public_safety check",
    "commit-msg": 'python3 -m src.public_safety check-message "$1"',
}

# Called with the command, and with ``environment`` only for a repository read as
# its own account, so a runner that never sees one need not accept it.
Runner = Callable[..., str]


class SafetyError(RuntimeError):
    """A check could not run, as distinct from a check that found something."""


@dataclass(frozen=True)
class Finding:
    location: str
    kind: str
    text: str

    def render(self) -> str:
        return f"{self.location}: {self.kind}: {self.text}"


@dataclass
class Terms:
    words: "list[str]" = field(default_factory=list)
    titles: "list[str]" = field(default_factory=list)
    branches: "list[str]" = field(default_factory=list)
    paths: "set[str]" = field(default_factory=set)
    allow: "list[str]" = field(default_factory=list)

    def __post_init__(self) -> None:
        self._word = _alternation(self.words, r"(?<![\w-])", r"(?![\w-])")
        self._title = _alternation(self.titles, "", "")
        self._branch = _alternation(self.branches, r"(?<![\w/.-])", r"(?![\w/.-])")
        self._allow = _alternation(self.allow, "", "")


def _alternation(
    items: Iterable[str], before: str, after: str
) -> "Optional[re.Pattern]":
    escaped = sorted({re.escape(item) for item in items if item}, key=len, reverse=True)
    if not escaped:
        return None
    return re.compile(before + "(?:" + "|".join(escaped) + ")" + after, re.IGNORECASE)


def scan_line(text: str, terms: Terms) -> "list[tuple[str, str]]":
    """Return the private terms one line contains, as (kind, matched text)."""

    allowed = [m.span() for m in terms._allow.finditer(text)] if terms._allow else []

    def kept(span: "tuple[int, int]") -> bool:
        return not any(start <= span[0] and span[1] <= end for start, end in allowed)

    found = []
    for kind, pattern in (
        ("name", terms._word),
        ("pull-request title", terms._title),
        ("branch", terms._branch),
    ):
        if pattern is None:
            continue
        found.extend(
            (kind, m.group(0)) for m in pattern.finditer(text) if kept(m.span())
        )
    for m in PATH_TOKEN.finditer(text):
        token = m.group(0)
        candidates = [token] + [token[i + 1 :] for i, c in enumerate(token) if c == "/"]
        if any(candidate in terms.paths for candidate in candidates) and kept(m.span()):
            found.append(("file path from a reviewed repository", token))
    return found


def scan_refs(text: str, refs: "Optional[dict[str, dict]]") -> "list[tuple[str, str]]":
    """Return references that are malformed or missing from the private map."""

    found = []
    for m in REF_PATTERN.finditer(text):
        ref = m.group(1)
        if ref == REF_PLACEHOLDER:
            continue
        if not REF_FORMAT.match(ref):
            found.append(("malformed reference", m.group(0)))
        elif refs is not None and ref not in refs:
            found.append(("reference missing from the private map", m.group(0)))
    return found


def scan(
    lines: "Iterable[tuple[str, str]]",
    terms: Terms,
    refs: "Optional[dict[str, dict]]",
) -> "list[Finding]":
    findings = []
    for location, text in lines:
        found = scan_line(text, terms)
        if not location.startswith(tuple(f"{path}:" for path in REF_CHECK_EXEMPT)):
            found += scan_refs(text, refs)
        for kind, matched in found:
            findings.append(Finding(location, kind, matched))
    return findings


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=str(root),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SafetyError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def staged_lines(root: Path) -> "list[tuple[str, str]]":
    """Return every line the staged change adds, and the names of its files."""

    lines = []
    names = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    for name in filter(None, names.split("\0")):
        lines.append((f"{name} (file name)", name))
    diff = _git(
        root, "diff", "--cached", "--unified=0", "--no-color", "--no-ext-diff",
        "--diff-filter=ACMR",
    )
    current = None
    number = 0
    for entry in diff.splitlines():
        if entry.startswith("+++ "):
            current = entry[6:] if entry.startswith("+++ b/") else None
        elif entry.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)", entry)
            number = int(m.group(1)) if m else 0
        elif current and entry.startswith("+"):
            lines.append((f"{current}:{number}", entry[1:]))
            number += 1
    return lines


def tracked_lines(root: Path) -> "list[tuple[str, str]]":
    """Return every line of every tracked text file, and every tracked file name."""

    lines = []
    for name in filter(None, _git(root, "ls-files", "-z").split("\0")):
        lines.append((f"{name} (file name)", name))
        try:
            text = (root / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines.extend(
            (f"{name}:{i}", line) for i, line in enumerate(text.splitlines(), 1)
        )
    return lines


def message_lines(path: Path) -> "list[tuple[str, str]]":
    """Return a commit message's lines, without the comments Git adds below it."""

    text = path.read_text(encoding="utf-8")
    return [
        (f"commit message:{i}", line)
        for i, line in enumerate(text.splitlines(), 1)
        if not line.startswith("#")
    ]


def _private(root: Path) -> Path:
    return root / PRIVATE_DIRECTORY


def load_terms(root: Path) -> Terms:
    path = _private(root) / TERMS_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SafetyError(
            f"No word list at {PRIVATE_DIRECTORY}/{TERMS_FILE}, so nothing can be "
            "checked; build it with `python3 -m src.public_safety refresh-terms`"
        ) from None
    except (OSError, json.JSONDecodeError) as error:
        raise SafetyError(f"Cannot read {path}: {error}") from error
    return Terms(
        words=list(payload.get("words", [])) + list(payload.get("extra", [])),
        titles=list(payload.get("titles", [])),
        branches=list(payload.get("branches", [])),
        paths=set(payload.get("paths", [])),
        allow=list(payload.get("allow", [])),
    )


def load_refs(root: Path) -> "Optional[dict[str, dict]]":
    path = _private(root) / REFS_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise SafetyError(f"Cannot read {path}: {error}") from error
    refs = payload.get("refs") if isinstance(payload, dict) else None
    if not isinstance(refs, dict):
        raise SafetyError(f"{path} holds no refs object")
    return refs


def _write_private(root: Path, name: str, payload: dict) -> Path:
    """Write one private file atomically, readable by its owner only."""

    directory = _private(root)
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    with tempfile.NamedTemporaryFile(
        "w", dir=directory, prefix=f".{name}.", suffix=".tmp", delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(path)
    return path


@dataclass(frozen=True)
class RepositorySource:
    """What one reviewed repository contributes to the word list."""

    slug: str
    logins: "frozenset[str]"
    titles: "tuple[str, ...]"
    branches: "frozenset[str]"
    paths: "frozenset[str]"


def collect_repository(
    slug: str, checkout: Optional[Path], run: Runner
) -> RepositorySource:
    """Read one reviewed repository's names, people, titles, branches, and paths."""

    pulls = json.loads(
        run(
            [
                "gh", "pr", "list", "-R", slug, "--state", "all", "--limit", "10000",
                "--json", "title,author,headRefName",
            ]
        )
        or "[]"
    )
    logins = {p["author"]["login"] for p in pulls if p.get("author")}
    for endpoint in ("pulls/comments", "issues/comments"):
        output = run(
            [
                "gh", "api", "--paginate", f"repos/{slug}/{endpoint}?per_page=100",
                "-q", ".[].user.login",
            ]
        )
        logins.update(output.split())
    paths: "frozenset[str]" = frozenset()
    if checkout is not None and checkout.is_dir():
        listed = run(["git", "-C", str(checkout), "ls-files"])
        paths = frozenset(filter(None, listed.splitlines()))
    return RepositorySource(
        slug=slug,
        logins=frozenset(logins),
        titles=tuple(p.get("title", "") for p in pulls),
        branches=frozenset(p.get("headRefName", "") for p in pulls),
        paths=paths,
    )


def build_terms(
    sources: "Sequence[RepositorySource]",
    own_login: str,
    own_paths: "Iterable[str]",
    previous: "Optional[dict]" = None,
) -> dict:
    """Return the word list, keeping the hand-maintained `extra` and `allow` entries."""

    previous = previous or {}
    own = set(own_paths)
    words = set()
    titles = set()
    branches = set()
    paths = set()
    for source in sources:
        owner, name = source.slug.split("/", 1)
        words.update({owner, name, source.slug})
        words.update(
            login for login in source.logins
            if not login.endswith("[bot]") and login.lower() != own_login.lower()
        )
        for title in source.titles:
            core = CONVENTIONAL_PREFIX.sub("", title).strip()
            for candidate in (title.strip(), core):
                if len(candidate) >= MIN_TITLE_LENGTH:
                    titles.add(candidate)
        branches.update(
            branch for branch in source.branches
            if len(branch) >= MIN_BRANCH_LENGTH
            and branch.lower() not in GENERIC_BRANCHES
            and ("/" in branch or "-" in branch)
        )
        paths.update(
            path for path in source.paths
            if path.count("/") >= 2
            and path not in own
            and not GENERIC_BASENAME.match(path.rsplit("/", 1)[-1])
        )
    return {
        "version": 1,
        "generated": date.today().isoformat(),
        "words": sorted(word for word in words if len(word) >= MIN_WORD_LENGTH),
        "titles": sorted(titles),
        "branches": sorted(branches),
        "paths": sorted(paths),
        "extra": list(previous.get("extra", [])),
        "allow": list(previous.get("allow", [])),
    }


def _run(
    args: Sequence[str], environment: "Optional[dict[str, str]]" = None
) -> str:
    completed = subprocess.run(
        list(args), check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        env=None if environment is None else {**os.environ, **environment},
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise SafetyError(f"{' '.join(args[:3])} failed: {detail}")
    return completed.stdout


def _as_account(run: Runner, account: Optional[str]) -> Runner:
    """Return ``run``, with its GitHub CLI calls made as ``account`` when one is named.

    A repository can be visible to its own account alone, and then the active login
    cannot list it. The token travels in the environment, never in the arguments,
    because the arguments are visible to every process on the machine.
    """

    if not account:
        return run
    token = run(["gh", "auth", "token", "--user", account]).strip()
    if not token:
        raise SafetyError(f"GitHub CLI returned an empty token for {account}")

    def as_account(args: Sequence[str]) -> str:
        if args[0] != "gh":
            return run(args)
        return run(args, environment={"GH_TOKEN": token})

    return as_account


def refresh_terms(root: Path, repositories_file: Path, run: Runner = _run) -> dict:
    from .config import ConfigError, load_repositories

    try:
        repositories = load_repositories(repositories_file)
    except ConfigError as error:
        raise SafetyError(str(error)) from error
    own_login = run(["gh", "api", "user", "-q", ".login"]).strip()
    sources = [
        collect_repository(r.slug, r.path, _as_account(run, r.github_account))
        for r in repositories
    ]
    own_paths = filter(None, _git(root, "ls-files").splitlines())
    previous_path = _private(root) / TERMS_FILE
    previous = None
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SafetyError(f"Cannot read {previous_path}: {error}") from error
    terms = build_terms(sources, own_login, own_paths, previous)
    _write_private(root, TERMS_FILE, terms)
    return terms


def new_ref(root: Path, url: str, note: str = "") -> str:
    """Return the reference standing in for one pull request, creating it if needed."""

    refs = load_refs(root) or {}
    for ref, entry in refs.items():
        if isinstance(entry, dict) and entry.get("url") == url:
            return ref
    while True:
        ref = "".join(secrets.choice(REF_ALPHABET) for _ in range(4))
        # A digit keeps a reference from reading as a word someone might search for.
        if ref not in refs and any(character.isdigit() for character in ref):
            break
    refs[ref] = {"url": url, "note": note}
    _write_private(root, REFS_FILE, {"version": 1, "refs": refs})
    return ref


def install_hooks(root: Path, force: bool = False) -> "list[Path]":
    hooks_directory = Path(_git(root, "rev-parse", "--git-path", "hooks").strip())
    if not hooks_directory.is_absolute():
        hooks_directory = root / hooks_directory
    hooks_directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, command in HOOKS.items():
        path = hooks_directory / name
        existing = (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )
        if path.exists() and HOOK_MARKER not in existing:
            if not force:
                raise SafetyError(
                    f"{path} already exists and was not installed here; "
                    "rerun with --force to replace it"
                )
        path.write_text(f"#!/bin/sh\n{HOOK_MARKER}\nexec {command}\n", encoding="utf-8")
        path.chmod(0o755)
        written.append(path)
    return written


def _report(findings: "Sequence[Finding]") -> int:
    if not findings:
        return 0
    print(
        f"Public-safety check found {len(findings)} private reference(s); replace "
        "them, or cite a pull request as [pr:xxxx] from "
        f"`python3 -m src.public_safety new-ref <url>`:",
        file=sys.stderr,
    )
    for finding in findings:
        print(f"  {finding.render()}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Keep reviewed projects' names out of this repository."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="check the staged change")
    check.add_argument(
        "--all", action="store_true", help="check every tracked file instead"
    )
    message = commands.add_parser("check-message", help="check a commit message file")
    message.add_argument("path", type=Path)
    refresh = commands.add_parser(
        "refresh-terms", help="rebuild the word list from the reviewed repositories"
    )
    refresh.add_argument(
        "--repositories-file", type=Path, default=Path(DEFAULT_REPOSITORIES_FILE)
    )
    ref = commands.add_parser("new-ref", help="create a [pr:xxxx] reference for a URL")
    ref.add_argument("url")
    ref.add_argument("--note", default="")
    hooks = commands.add_parser("install-hooks", help="install the Git hooks")
    hooks.add_argument("--force", action="store_true")
    return parser


def main(argv: "Optional[Sequence[str]]" = None, root: Path = PROJECT_ROOT) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            lines = tracked_lines(root) if args.all else staged_lines(root)
            return _report(scan(lines, load_terms(root), load_refs(root)))
        if args.command == "check-message":
            lines = message_lines(args.path)
            return _report(scan(lines, load_terms(root), load_refs(root)))
        if args.command == "refresh-terms":
            repositories_file = args.repositories_file
            if not repositories_file.is_absolute():
                repositories_file = root / repositories_file
            terms = refresh_terms(root, repositories_file)
            print(
                f"Wrote {PRIVATE_DIRECTORY}/{TERMS_FILE}: {len(terms['words'])} names, "
                f"{len(terms['titles'])} titles, {len(terms['branches'])} branches, "
                f"{len(terms['paths'])} paths"
            )
            return 0
        if args.command == "new-ref":
            print(f"[pr:{new_ref(root, args.url, args.note)}]")
            return 0
        for path in install_hooks(root, force=args.force):
            print(f"Installed {path}")
        return 0
    except SafetyError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
