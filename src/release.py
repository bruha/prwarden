"""Prepare a Semantic Versioning release from Conventional Commits."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = PROJECT_ROOT / "VERSION"
INIT_FILE = PROJECT_ROOT / "src" / "__init__.py"
CHANGELOG_FILE = PROJECT_ROOT / "CHANGELOG.md"

SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)$"
)
COMMIT_PATTERN = re.compile(
    r"^(?P<type>feat|fix|docs|refactor|perf|test|build|ci|chore|style|revert)"
    r"(?:\((?P<scope>[^()\r\n]+)\))?"
    r"(?P<breaking>!)?: (?P<description>\S.*)$"
)
BASELINE_PATTERN = re.compile(
    r"<!-- release-baseline: (?P<version>\d+\.\d+\.\d+) "
    r"(?P<commit>[0-9a-f]{7,40}) -->"
)
INIT_VERSION_PATTERN = re.compile(
    r'(?m)^__version__ = "(?P<version>\d+\.\d+\.\d+)"$'
)
RELEASE_COMMIT_PATTERN = re.compile(
    r"^chore\(release\): v(?P<version>\d+\.\d+\.\d+)$"
)

TYPE_SECTIONS = {
    "feat": "Added",
    "fix": "Fixed",
    "perf": "Changed",
    "docs": "Documentation",
    "test": "Testing",
    "build": "Maintenance",
    "ci": "Maintenance",
    "chore": "Maintenance",
    "refactor": "Changed",
    "style": "Changed",
    "revert": "Changed",
}
SECTION_ORDER = (
    "Breaking Changes",
    "Added",
    "Fixed",
    "Changed",
    "Documentation",
    "Testing",
    "Maintenance",
)
BUMP_RANK = {"patch": 0, "minor": 1, "major": 2}


class ReleaseError(RuntimeError):
    """Raised when release preparation cannot proceed safely."""


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> "Version":
        match = SEMVER_PATTERN.fullmatch(value.strip())
        if match is None:
            raise ReleaseError(f"Invalid stable SemVer version: {value!r}")
        return cls(*(int(match.group(name)) for name in ("major", "minor", "patch")))

    def bump(self, kind: str) -> "Version":
        if kind == "major":
            return Version(self.major + 1, 0, 0)
        if kind == "minor":
            return Version(self.major, self.minor + 1, 0)
        if kind == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise ReleaseError(f"Unsupported version bump: {kind}")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str
    commit_type: str
    scope: Optional[str]
    description: str
    breaking: bool


@dataclass(frozen=True)
class ReleasePlan:
    current_version: Version
    next_version: Version
    bump: str
    boundary: str
    head: str
    commits: tuple[Commit, ...]
    changelog: str


def _git(*args: str, root: Path = PROJECT_ROOT) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=str(root),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ReleaseError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _read_current_version(root: Path) -> Version:
    version_path = root / "VERSION"
    init_path = root / "src" / "__init__.py"
    version = Version.parse(version_path.read_text(encoding="utf-8").strip())
    match = INIT_VERSION_PATTERN.search(init_path.read_text(encoding="utf-8"))
    if match is None:
        raise ReleaseError("src/__init__.py does not contain a stable __version__")
    init_version = Version.parse(match.group("version"))
    if version != init_version:
        raise ReleaseError(
            f"VERSION ({version}) and src.__version__ ({init_version}) disagree"
        )
    return version


def _latest_tag(root: Path) -> Optional[tuple[Version, str]]:
    tags = _git("tag", "--merged", "HEAD", "--list", "v[0-9]*", root=root).splitlines()
    parsed: list[tuple[Version, str]] = []
    for tag in tags:
        try:
            parsed.append((Version.parse(tag.removeprefix("v")), tag))
        except ReleaseError:
            continue
    return max(parsed, default=None)


def _release_boundary(root: Path, changelog: str, current: Version) -> str:
    latest_tag = _latest_tag(root)
    if latest_tag is not None:
        tag_version, tag = latest_tag
        if tag_version != current:
            raise ReleaseError(
                f"Latest reachable tag {tag} disagrees with current version {current}"
            )
        return tag

    baseline = BASELINE_PATTERN.search(changelog)
    if baseline is None:
        raise ReleaseError("CHANGELOG.md has no release tag or release-baseline marker")
    baseline_version = Version.parse(baseline.group("version"))
    if baseline_version != current:
        raise ReleaseError(
            f"Changelog baseline {baseline_version} disagrees with current version {current}"
        )
    commit = baseline.group("commit")
    _git("rev-parse", "--verify", f"{commit}^{{commit}}", root=root)
    return commit


def _parse_commits(root: Path, boundary: str) -> tuple[Commit, ...]:
    output = _git(
        "log",
        "--reverse",
        # A merge commit records that a branch arrived, not what changed: the commits
        # it brought are in this range on their own and would be counted twice. Its
        # subject is also not a Conventional Commit and never could be, so reading it
        # as one made the first merge on this history refuse to release at all.
        "--no-merges",
        "--format=%H%x00%s%x00%b%x00",
        f"{boundary}..HEAD",
        root=root,
    )
    fields = output.split("\x00")
    if fields and not fields[-1].strip():
        fields.pop()
    if len(fields) % 3:
        raise ReleaseError("Could not parse git log output")

    commits: list[Commit] = []
    invalid: list[str] = []
    for index in range(0, len(fields), 3):
        sha, subject, body = (field.strip() for field in fields[index : index + 3])
        release_match = RELEASE_COMMIT_PATTERN.fullmatch(subject)
        if release_match is not None:
            continue
        match = COMMIT_PATTERN.fullmatch(subject)
        if match is None:
            invalid.append(f"{sha[:7]} {subject}")
            continue
        breaking = bool(match.group("breaking")) or bool(
            re.search(r"(?m)^BREAKING CHANGE:\s+\S", body)
        )
        commits.append(
            Commit(
                sha=sha,
                subject=subject,
                body=body,
                commit_type=match.group("type"),
                scope=match.group("scope"),
                description=match.group("description"),
                breaking=breaking,
            )
        )
    if invalid:
        raise ReleaseError(
            "Commits since the release boundary must use Conventional Commits:\n- "
            + "\n- ".join(invalid)
        )
    if not commits:
        raise ReleaseError("No releasable commits exist after the release boundary")
    return tuple(commits)


def _required_bump(commits: Sequence[Commit]) -> str:
    if any(commit.breaking for commit in commits):
        return "major"
    if any(commit.commit_type == "feat" for commit in commits):
        return "minor"
    return "patch"


def _render_entry(commit: Commit) -> str:
    scope = f"**{commit.scope}:** " if commit.scope else ""
    description = commit.description.rstrip(".")
    return f"- {scope}{description} (`{commit.sha[:7]}`)."


def _render_release(version: Version, commits: Sequence[Commit], released: date) -> str:
    sections: dict[str, list[Commit]] = {}
    for commit in commits:
        section = "Breaking Changes" if commit.breaking else TYPE_SECTIONS[commit.commit_type]
        sections.setdefault(section, []).append(commit)

    lines = [f"## [{version}] - {released.isoformat()}"]
    for section in SECTION_ORDER:
        entries = sections.get(section)
        if not entries:
            continue
        lines.extend(("", f"### {section}", ""))
        lines.extend(_render_entry(commit) for commit in entries)
    return "\n".join(lines) + "\n"


def _updated_changelog(
    changelog: str,
    current: Version,
    next_version: Version,
    head: str,
    commits: Sequence[Commit],
    released: date,
) -> str:
    baseline = BASELINE_PATTERN.search(changelog)
    marker = f"<!-- release-baseline: {next_version} {head} -->"
    if baseline is None:
        raise ReleaseError("CHANGELOG.md has no release-baseline marker")
    if Version.parse(baseline.group("version")) != current:
        raise ReleaseError("CHANGELOG.md baseline does not match VERSION")
    updated = changelog[: baseline.start()] + marker + changelog[baseline.end() :]

    unreleased = "## [Unreleased]\n"
    if updated.count(unreleased) != 1:
        raise ReleaseError("CHANGELOG.md must contain exactly one Unreleased heading")
    release = _render_release(next_version, commits, released)
    return updated.replace(unreleased, f"{unreleased}\n{release}", 1)


def create_plan(
    root: Path = PROJECT_ROOT,
    requested_bump: Optional[str] = None,
    released: Optional[date] = None,
) -> ReleasePlan:
    branch = _git("branch", "--show-current", root=root)
    if branch != "main":
        raise ReleaseError(f"Releases must be prepared on main, not {branch or 'detached HEAD'}")
    if _git("status", "--porcelain", root=root):
        raise ReleaseError("Working tree must be clean before release preparation")

    current = _read_current_version(root)
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    boundary = _release_boundary(root, changelog, current)
    commits = _parse_commits(root, boundary)
    required = _required_bump(commits)
    bump = requested_bump or required
    if BUMP_RANK[bump] < BUMP_RANK[required]:
        raise ReleaseError(
            f"Requested {bump} bump is lower than required {required} bump"
        )
    next_version = current.bump(bump)
    head = _git("rev-parse", "HEAD", root=root)
    updated_changelog = _updated_changelog(
        changelog,
        current,
        next_version,
        head,
        commits,
        released or date.today(),
    )
    return ReleasePlan(
        current_version=current,
        next_version=next_version,
        bump=bump,
        boundary=boundary,
        head=head,
        commits=commits,
        changelog=updated_changelog,
    )


def apply_plan(plan: ReleasePlan, root: Path = PROJECT_ROOT) -> None:
    (root / "VERSION").write_text(f"{plan.next_version}\n", encoding="utf-8")

    init_path = root / "src" / "__init__.py"
    init_text = init_path.read_text(encoding="utf-8")
    updated_init, replacements = INIT_VERSION_PATTERN.subn(
        f'__version__ = "{plan.next_version}"',
        init_text,
    )
    if replacements != 1:
        raise ReleaseError("Expected exactly one src.__version__ assignment")
    init_path.write_text(updated_init, encoding="utf-8")
    (root / "CHANGELOG.md").write_text(plan.changelog, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare VERSION and CHANGELOG.md from Conventional Commits."
    )
    parser.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        help="override the calculated bump; cannot be lower than required",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the release plan without changing files",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = create_plan(requested_bump=args.bump)
        print(
            f"Release {plan.current_version} -> {plan.next_version} "
            f"({plan.bump}; {len(plan.commits)} commits)"
        )
        for commit in plan.commits:
            print(f"- {commit.sha[:7]} {commit.subject}")
        if args.dry_run:
            print("Dry run: no files changed")
        else:
            apply_plan(plan)
            print("Updated VERSION, src/__init__.py, and CHANGELOG.md")
            print("Review the diff before committing and tagging")
        return 0
    except (OSError, ReleaseError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
