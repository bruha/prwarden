"""Replay recorded pull requests so a change to the reviewer can be measured.

    python3 -m prwarden.evaluate capture              # pin the open pull requests
    python3 -m prwarden.evaluate replay --metrics before.json
    # change a prompt, or pick another arm
    python3 -m prwarden.evaluate replay --arm claude --metrics after.json
    python3 -m prwarden.compare_metrics before.json after.json

`--metrics` on a normal run measures what that run decided, but it cannot compare the
same work twice: pull requests merge, heads move, and a set of open pull requests
decays within days. A case here pins the head and the merge base it was reviewed
against, so the input never changes again and a difference between two replays belongs
to the reviewer rather than to the work.

The diff is rebuilt from the local checkout rather than from GitHub, because GitHub's
pull-request files API only ever reports the current head; a merged or re-pushed pull
request cannot be replayed through it. Fetching a merged head still works, including
after its branch was deleted, because GitHub keeps the commit reachable through the
pull-request ref, so the existing worktree preparation needs no special case.

Three things a replay deliberately leaves out, because none of them belong to a frozen
head: eligibility and mergeability, which are properties of a live pull request; prior
review comments, which keep changing, so duplicate suppression is not exercised here;
and delivery, Slack, and state, so a replay can never post anything.

This module reaches into `cli` for the level entry points on purpose. The levels are
the thing being measured, and reimplementing them here would measure a copy.
"""

import argparse
import importlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, NamedTuple, Optional, Sequence

from .commands import CommandError, run_cmd
from .config import (
    MAX_REVIEW_CONCURRENCY,
    ConfigError,
    Settings,
    load_settings,
    parse_effort,
)
from .engine import DESIGN_LEVEL, DOCS_LEVEL, EngineError, is_exhausted_quota
from .github import list_open_prs, pin_github_account, reviewer_account
from . import progress
from .cli import RUN_LOG_DIRECTORY_NAME
from .metrics import ReviewMetrics
from .models import Finding, FindingEvidence, PullRequest, Repository
from .review import ReviewError, build_review_material, read_background
from .reviewer_identity import format_review_attribution
from .state import JsonStateStore, StateError
from .workspace import WorkspaceError


DEFAULT_CASES_FILE = Path("evals/cases.json")
DEFAULT_ARMS_FILE = Path("evals/arms.json")
DEFAULT_LABELS_FILE = Path("evals/labels.json")
DEFAULT_CANDIDATES_FILE = Path("evals/candidates.json")
# Where this reviewer's own package lives in a commit: prwarden/ from 2.0.0, src/
# before it.
PACKAGE_FOLDERS = ("prwarden", "src")
# Each pass is another full set of model invocations, and one pass of seventeen cases
# already spends about a provider quota window.
MAX_REPLAY_PASSES = 5
# Scalar settings an arm may override. Anything that needs a matching command, such as
# the verification engine, is deliberately absent: an arm must not be able to describe
# a configuration the loader would refuse.
OVERRIDABLE_SETTINGS = frozenset(
    {
        "review_model",
        "review_timeout_seconds",
        "max_diff_bytes",
        "max_findings",
        # How many reviews one oversized change is read in changes what was read at
        # all, so a measurement that leaves it out cannot be compared.
        "max_review_bundles",
        "design_level",
        "docs_level",
        "max_design_findings",
        "max_docs_findings",
        # Effort changes what a model does with the same prompt, so a measurement
        # that leaves it out cannot be compared, and a verifier's effort is exactly
        # the kind of question verify mode exists to answer cheaply. Each is checked
        # against the engine it would reach when the arm is applied.
        "review_effort",
        "review_code_effort",
        "verify_effort",
    }
)
# Which engine each effort an arm may set reaches, and how a refusal names it.
_ARM_EFFORT_ENGINES = (
    ("review_effort", "review_engine", "a review engine"),
    ("review_code_effort", "review_engine", "a review engine"),
    ("verify_effort", "verify_engine", "a verification engine"),
)


@dataclass(frozen=True)
class ReplayCase:
    """One pinned pull request, as it stood when it was captured."""

    slug: str
    number: int
    head: str
    base: str
    title: str = ""
    head_ref: str = ""
    base_ref: str = ""
    captured: str = ""
    # What the author wrote, which a live review reads and which states intent: a
    # replay without it confirmed a regression the description named as the change.
    body: str = ""

    @property
    def label(self) -> str:
        return f"{self.slug}#{self.number}"

    @property
    def pinned(self) -> str:
        """Return the label with the head it is pinned to.

        A pull request reviewed at several heads is several cases, and the label
        alone renders them identically: a transcript listing two of them twice, and
        a set of measured cases silently holding one. The head is what makes a case
        a case, so anything that identifies one carries it.
        """

        return f"{self.label}@{self.head[:12]}"


@dataclass(frozen=True)
class ReplayArm:
    """A named set of settings one replay runs under."""

    name: str
    engine: Optional[str] = None
    overrides: "dict[str, Any]" = None  # type: ignore[assignment]

    def applied_to(self, settings: Settings) -> Settings:
        overrides = dict(self.overrides or {})
        for name, engine_field, engine_name in _ARM_EFFORT_ENGINES:
            if name not in overrides:
                continue
            value = overrides[name]
            if value is not None and not isinstance(value, str):
                raise ConfigError(f"Arm {self.name!r} sets {name} to {value!r}")
            # The loader refuses a misspelt level or one for an adapter that cannot
            # pass it; an arm is held to the same rule, or it would measure a
            # setting nobody can configure.
            overrides[name] = parse_effort(
                value,
                f"Arm {self.name!r} {name}",
                getattr(settings, engine_field),
                engine_name,
            )
        return replace(settings, **overrides) if overrides else settings

    def describe(self) -> "dict[str, Any]":
        described = {"name": self.name}
        if self.engine:
            described["engine"] = self.engine
        described.update(self.overrides or {})
        return described


BASELINE_ARM = ReplayArm(name="baseline")


def _git(checkout: Path, *args: str, timeout_seconds: int = 180) -> str:
    return run_cmd(
        ["git", "-C", str(checkout), *args],
        timeout_seconds=timeout_seconds,
    )


def merge_base(checkout: Path, head: str, base_ref: str) -> str:
    """Return the commit a pull request's diff was computed against.

    The merge base of the head and the base branch's current tip, which is what
    GitHub compares a pull request against, rather than the base commit recorded when
    the pull request was opened, which GitHub never advances.
    """

    # The head has to be local before it can be compared: a pull request opened
    # since the last fetch is otherwise "not a valid commit name", which silently
    # cost one pull request [pr:4c7v] its case on the first capture.
    _git(
        checkout,
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-write-fetch-head",
        "origin",
        head,
    )
    _git(checkout, "fetch", "--quiet", "--no-tags", "origin", base_ref)
    return _git(checkout, "merge-base", head, "FETCH_HEAD").strip()


def _numstat_records(checkout: Path, base: str, head: str) -> "list[dict[str, Any]]":
    """Return one record per changed path, with its line counts.

    Parsed from the NUL-separated form, because a path with a space, a quote, or a
    newline in it would otherwise be unreadable, and a rename emits three fields
    rather than one.
    """

    output = _git(checkout, "diff", "-M", "--numstat", "-z", base, head)
    fields = [field for field in output.split("\0") if field != ""]
    records: "list[dict[str, Any]]" = []
    index = 0
    while index < len(fields):
        counts = fields[index].split("\t")
        if len(counts) < 3:
            index += 1
            continue
        added, deleted, path = counts[0], counts[1], counts[2]
        previous_path = None
        if path == "":
            # A rename: the old and new paths follow as their own fields.
            if index + 2 >= len(fields):
                break
            previous_path, path = fields[index + 1], fields[index + 2]
            index += 3
        else:
            index += 1
        binary = added == "-" or deleted == "-"
        record: "dict[str, Any]" = {
            "filename": path,
            "additions": 0 if binary else int(added),
            "deletions": 0 if binary else int(deleted),
        }
        record["changes"] = record["additions"] + record["deletions"]
        if previous_path is not None:
            record["previous_filename"] = previous_path
            record["status"] = "renamed"
        records.append(record)
    return records


def _hunks(patch: str) -> Optional[str]:
    """Return the hunks of one file's diff, as GitHub's patch field carries them.

    Everything before the first hunk header is dropped. It has to be: the controller
    reads a line starting with `+` as an added line, and `+++ b/path` would otherwise
    become a legal anchor for a finding.
    """

    marker = patch.find("@@")
    if marker == -1:
        return None
    return patch[marker:].rstrip("\n") or None


def changed_files(checkout: Path, base: str, head: str) -> "list[dict[str, Any]]":
    """Return the change between two commits, shaped as GitHub reports a diff."""

    records = _numstat_records(checkout, base, head)
    for record in records:
        if not record["changes"]:
            # No changed line to diff, which is what GitHub reports for a binary and
            # for a rename that moved nothing.
            continue
        patch = _git(
            checkout,
            "diff",
            "-M",
            base,
            head,
            "--",
            record.get("previous_filename") or record["filename"],
            record["filename"],
        )
        hunks = _hunks(patch)
        if hunks is not None:
            record["patch"] = hunks
    return records


ACTED_ON = "acted_on"
ACTED_ON_AFTER_REBASE = "acted_on_after_rebase"
NOT_ACTED_ON = "not_acted_on"
CHANGED_ELSEWHERE = "changed_elsewhere"
NOTHING_PUSHED = "nothing_pushed_after"
ANCHOR_TOLERANCE = 2
# The delivery formatter's own wording, so a label records whether a verification pass
# stood behind the finding it was mined from.
VERIFIED_ATTRIBUTION = ", verified by "


def classify_outcome(diff: str, line: int, *, rebased: bool = False) -> str:
    """Return what a later push did to the lines one comment pointed at.

    The strongest evidence a review comment was worth posting is the author editing
    exactly what it pointed at afterwards. Hunk ranges are read on the old side,
    because the comment's line number belongs to the commit it was posted against,
    and a small tolerance covers a fix landing a line above or below the anchor.

    A rebase rewrites everything, so a region that changed across one cannot be
    credited to the comment; it is reported apart rather than counted.
    """

    if not diff.strip():
        return NOT_ACTED_ON
    for entry in diff.splitlines():
        if not entry.startswith("@@"):
            continue
        parts = entry.split(" ")
        if len(parts) < 2 or not parts[1].startswith("-"):
            continue
        old_range = parts[1][1:]
        start_text, _, count_text = old_range.partition(",")
        try:
            start = int(start_text)
            count = int(count_text) if count_text else 1
        except ValueError:
            continue
        if start - ANCHOR_TOLERANCE <= line <= start + max(count, 1) + ANCHOR_TOLERANCE:
            return ACTED_ON_AFTER_REBASE if rebased else ACTED_ON
    return CHANGED_ELSEWHERE


def _required(payload: Any, field: str, subject: str) -> Any:
    if not isinstance(payload, dict) or field not in payload:
        raise ConfigError(f"{subject} is missing {field!r}")
    return payload[field]


def load_cases(path: Path) -> "tuple[ReplayCase, ...]":
    """Return the pinned cases, refusing anything a replay could not reproduce."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"Cannot read the case set {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON: {error}") from error
    raw_cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ConfigError(f"{path} holds no cases")
    cases = []
    for raw_case in raw_cases:
        subject = f"A case in {path}"
        slug = str(_required(raw_case, "slug", subject))
        number = _required(raw_case, "number", subject)
        head = str(_required(raw_case, "head", subject))
        base = str(_required(raw_case, "base", subject))
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ConfigError(f"{subject} has an unusable number: {number!r}")
        for name, value in (("head", head), ("base", base)):
            if len(value) < 7 or any(
                character not in "0123456789abcdef" for character in value.lower()
            ):
                raise ConfigError(
                    f"{subject} has an unusable {name} commit: {value!r}"
                )
        cases.append(
            ReplayCase(
                slug=slug,
                number=number,
                head=head,
                base=base,
                title=str(raw_case.get("title") or ""),
                head_ref=str(raw_case.get("head_ref") or ""),
                base_ref=str(raw_case.get("base_ref") or ""),
                captured=str(raw_case.get("captured") or ""),
                body=str(raw_case.get("body") or ""),
            )
        )
    return tuple(cases)


def cases_fingerprint(cases: "Sequence[ReplayCase]") -> str:
    """Return a fingerprint of the input, so two replays can prove they match.

    Comparing measurements taken over different work compares nothing, and a report
    that looks tidy while doing so is worse than no report.
    """

    import hashlib

    payload = json.dumps(
        sorted(f"{case.slug}#{case.number}@{case.head}:{case.base}" for case in cases)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_arm(path: Path, name: str) -> ReplayArm:
    """Return one named arm, or the baseline when nothing is configured."""

    if name == BASELINE_ARM.name and not path.is_file():
        return BASELINE_ARM
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"Cannot read the arm set {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON: {error}") from error
    raw_arms = payload.get("arms") if isinstance(payload, dict) else payload
    if not isinstance(raw_arms, list):
        raise ConfigError(f"{path} holds no arms")
    for raw_arm in raw_arms:
        if not isinstance(raw_arm, dict) or raw_arm.get("name") != name:
            continue
        engine = raw_arm.get("engine")
        overrides = {
            key: value
            for key, value in raw_arm.items()
            if key not in ("name", "engine")
        }
        unknown = sorted(set(overrides) - OVERRIDABLE_SETTINGS)
        if unknown:
            raise ConfigError(
                f"Arm {name!r} sets what an arm may not override: "
                f"{', '.join(unknown)}"
            )
        return ReplayArm(
            name=name,
            engine=str(engine) if engine else None,
            overrides=overrides,
        )
    if name == BASELINE_ARM.name:
        return BASELINE_ARM
    raise ConfigError(f"{path} has no arm named {name!r}")


def measured_engine(
    settings: Settings, *, verifying: bool
) -> "tuple[str, Optional[str]]":
    """Return the engine and model that actually served a pass's invocations.

    A verification pass never invokes the review engine, so naming that engine
    describes a provider the measurement never called: the two passes archived on
    2026-09-24 were stamped antigravity and gemini-3.1-pro-high while every one of
    their thirteen invocations went to kiro. It also silenced the warning that a
    comparison is unsafe, because both sides named the same engine they had not run.
    When no separate verifier is configured the primary engine really is the one that
    ran, since verification falls back to it.
    """

    if not verifying:
        return settings.review_engine, settings.review_model
    from . import cli

    verify_engine, verify_model = cli._verifier_identity(settings)
    if verify_engine is None:
        return settings.review_engine, settings.review_model
    return verify_engine, verify_model


def reviewer_provenance() -> "dict[str, Any]":
    """Return which build of this reviewer produced a measurement.

    A prompt is code, so the arm cannot describe it. The commit can, and a dirty tree
    is worth knowing about because it means the measurement describes something that
    is not committed anywhere.
    """

    from . import __version__

    provenance: "dict[str, Any]" = {"version": __version__}
    try:
        root = Path(__file__).resolve().parent.parent
        provenance["commit"] = run_cmd(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            timeout_seconds=30,
        ).strip()
        provenance["dirty"] = bool(
            run_cmd(
                ["git", "-C", str(root), "status", "--porcelain"],
                timeout_seconds=30,
            ).strip()
        )
    except CommandError:
        pass
    return provenance


def capture(
    settings: Settings,
    path: Path,
    selected_slugs: "Sequence[str]" = (),
) -> "tuple[ReplayCase, ...]":
    """Pin the currently open pull requests, so they can be replayed later."""

    repositories = [
        repository
        for repository in settings.repositories
        if not selected_slugs or repository.slug in set(selected_slugs)
    ]
    if not repositories:
        raise ConfigError("No configured repository matched the requested slugs")
    from datetime import date

    today = date.today().isoformat()
    cases: "list[ReplayCase]" = []
    for repository in repositories:
        for pull_request in list_open_prs(repository):
            try:
                base = merge_base(
                    repository.path,
                    pull_request.head_sha,
                    pull_request.base_ref or "HEAD",
                )
            except CommandError as error:
                print(
                    f"  skipped {repository.slug}#{pull_request.number}: {error}",
                    file=sys.stderr,
                )
                continue
            cases.append(
                ReplayCase(
                    slug=repository.slug,
                    number=pull_request.number,
                    head=pull_request.head_sha,
                    base=base,
                    title=pull_request.title,
                    head_ref=pull_request.head_ref,
                    base_ref=pull_request.base_ref,
                    captured=today,
                    body=pull_request.body,
                )
            )
            print(
                f"  pinned {repository.slug}#{pull_request.number} at "
                f"{pull_request.head_sha[:12]} against {base[:12]}"
            )
    if not cases:
        raise ConfigError("Found no open pull request to pin")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "captured": today,
                "cases": [
                    {
                        "slug": case.slug,
                        "number": case.number,
                        "head": case.head,
                        "base": case.base,
                        "title": case.title,
                        "head_ref": case.head_ref,
                        "base_ref": case.base_ref,
                        "captured": case.captured,
                        "body": case.body,
                    }
                    for case in cases
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Pinned {len(cases)} cases to {path}")
    return tuple(cases)


def _measured_verification(verification, measured):
    """Return the verification pass with its engine measured, when there is one."""

    if verification is None:
        return None
    return replace(verification, engine=measured(verification.engine, "verify"))


def _object_present(checkout: Path, sha: str) -> bool:
    try:
        _git(checkout, "cat-file", "-e", f"{sha}^{{commit}}", timeout_seconds=30)
        return True
    except CommandError:
        return False


def _ensure_object(checkout: Path, sha: str) -> bool:
    if _object_present(checkout, sha):
        return True
    try:
        _git(
            checkout,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-write-fetch-head",
            "origin",
            sha,
        )
    except CommandError:
        return False
    return _object_present(checkout, sha)


def mine_labels(
    settings: Settings,
    cases_path: Path,
    labels_path: Path,
    selected_slugs: "Sequence[str]" = (),
) -> "dict[str, Any]":
    """Derive cases and labels from what authors did after our review comments.

    A finding whose lines the author then edited is a finding that was worth posting.
    That is evidence nobody has to be asked for, so the corpus builds itself from the
    reviewer's own delivered history.

    Each case is pinned at the commit the comments were posted against, not at the
    pull request's latest head, because a label is a line number and a line number
    only means something at the commit it was read from.

    The honest limit, which the report states: this measures whether the reviewer
    still finds what it already found and authors acted on. It cannot measure what
    the reviewer never found, so it is a regression set rather than a ground truth.
    """

    from .github import list_review_comments_by_reviewer

    slugs = set(selected_slugs)
    grouped: "dict[tuple[str, int, str], list[dict[str, Any]]]" = {}
    # Mining one repository must not discard the rest of the corpus. Scoping the work
    # is not the same as scoping the file, and a truncated corpus compares nothing.
    kept_cases = [
        case
        for case in _existing_cases(cases_path)
        if slugs and case.get("slug") not in slugs
    ]
    kept_labels = {
        key: entry
        for key, entry in load_labels(labels_path).items()
        if slugs and entry.get("slug") not in slugs
    }
    pull_requests: "dict[tuple[str, int], dict[str, Any]]" = {}
    for repository in settings.repositories:
        if slugs and repository.slug not in slugs:
            continue
        for comment in list_review_comments_by_reviewer(
            repository, reviewer_account(repository, settings.reviewer_username)
        ):
            key = (repository.slug, comment["number"], comment["anchor"])
            grouped.setdefault(key, []).append(comment)
            pull_requests.setdefault(
                (repository.slug, comment["number"]),
                {
                    "base_ref": comment["base_ref"],
                    "base_sha": comment.get("base_sha", ""),
                    "head": comment["head"],
                    "title": comment.get("pr_title", ""),
                    "body": comment.get("pr_body", ""),
                },
            )

    cases: "list[dict[str, Any]]" = list(kept_cases)
    labels: "dict[str, Any]" = dict(kept_labels)
    tally: "dict[str, int]" = {}
    if kept_cases:
        print(f"  keeping {len(kept_cases)} cases mined from other repositories")
    for (slug, number, anchor), comments in sorted(grouped.items()):
        repository = _repository_for(settings, slug)
        details = pull_requests[(slug, number)]
        if not _ensure_object(repository.path, anchor):
            print(f"  skipped {slug}#{number}: {anchor[:12]} is unreachable")
            continue
        head = details["head"]
        _ensure_object(repository.path, head)
        # Against the commit the pull request was opened from, never its base branch:
        # a merged pull request's base branch contains the reviewed commit, so git
        # would answer with that commit and the case would review nothing at all.
        base_ref = details.get("base_sha") or details["base_ref"] or "HEAD"
        if details.get("base_sha"):
            _ensure_object(repository.path, details["base_sha"])
        try:
            base = merge_base(repository.path, anchor, base_ref)
        except CommandError as error:
            print(f"  skipped {slug}#{number}: {error}")
            continue
        if base == anchor:
            print(
                f"  skipped {slug}#{number}: {anchor[:12]} has no diff against "
                f"{base_ref[:12]}"
            )
            continue
        rebased = True
        try:
            _git(repository.path, "merge-base", "--is-ancestor", anchor, head)
            rebased = False
        except CommandError:
            rebased = anchor != head
        expected: "list[dict[str, Any]]" = []
        for comment in comments:
            if anchor == head:
                outcome = NOTHING_PUSHED
            else:
                try:
                    diff = _git(
                        repository.path,
                        "diff",
                        "--unified=0",
                        f"{anchor}..{head}",
                        "--",
                        comment["path"],
                    )
                except CommandError:
                    diff = ""
                outcome = classify_outcome(diff, comment["line"], rebased=rebased)
            tally[outcome] = tally.get(outcome, 0) + 1
            expected.append(
                {
                    "path": comment["path"],
                    "line": comment["line"],
                    "outcome": outcome,
                    "comment_id": comment["comment_id"],
                    "title": comment["title"],
                    "attribution": comment.get("attribution", ""),
                    "created": comment.get("created", ""),
                }
            )
        label = f"{slug}#{number}"
        cases.append(
            {
                "slug": slug,
                "number": number,
                "head": anchor,
                "base": base,
                "title": details.get("title", ""),
                "base_ref": details["base_ref"],
                "captured": "mined",
                "body": details.get("body", ""),
            }
        )
        labels[f"{label}@{anchor[:12]}"] = {
            "slug": slug,
            "number": number,
            "head": anchor,
            # Where the pull request ended, so a replay can ask whether the author
            # later changed lines it confirmed a finding on that nobody commented on.
            "final_head": head,
            "expected": expected,
        }
        print(
            f"  {label} at {anchor[:12]}: "
            + ", ".join(
                f"{item['path'].split('/')[-1]}:{item['line']} {item['outcome']}"
                for item in expected
            )[:120]
        )

    controls = mine_controls(settings, slugs, set(pull_requests))
    for case, label in controls:
        cases.append(case)
        labels[f"{case['slug']}#{case['number']}@{case['head'][:12]}"] = label
    if controls:
        tally["control"] = len(controls)

    for path, payload in (
        (cases_path, {"captured": "mined", "cases": cases}),
        (labels_path, {"labels": labels}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    cases.sort(key=lambda case: (case["slug"], case["number"], case["head"]))
    findings = sum(len(entry["expected"]) for entry in labels.values())
    print(f"Mined {len(cases)} cases and {findings} labelled findings")
    for outcome, count in sorted(tally.items(), key=lambda item: -item[1]):
        print(f"  {count:3}  {outcome}")
    print(f"Cases written to {cases_path}, labels to {labels_path}")
    return {"cases": cases, "labels": labels, "tally": tally}


def mine_controls(
    settings: Settings,
    slugs: "set[str]",
    commented: "set[tuple[str, int]]",
) -> "list[tuple[dict[str, Any], dict[str, Any]]]":
    """Return the pull requests that needed nothing, as cases and their labels.

    A control is a head the reviewer approved on GitHub that then merged with no
    further push, on a pull request the reviewer never commented on. Recall alone
    cannot tell a better reviewer from a noisier one; a case that should simply be
    approved again can.

    Each condition is checked against GitHub rather than trusted from local state:
    a delivered record does not say whether it was an approval, so the record's
    receipt must be the approving review at that exact head. Merged unchanged is not
    proof the code had no defect, only that nobody found one before it shipped.
    """

    from .github import get_pull_request_refs, list_reviews

    configured = {repository.slug for repository in settings.repositories}
    try:
        with JsonStateStore(settings.state_file, lock_enabled=False) as store:
            records = dict(store.data["records"])
    except StateError as error:
        print(f"  no control cases mined: {error}")
        return []
    delivered: "dict[tuple[str, int], dict[str, str]]" = {}
    for key, record in records.items():
        parts = key.split("#")
        if len(parts) != 4 or parts[3] != "github":
            continue
        slug, number_text, head, _ = parts
        if slug not in configured or (slugs and slug not in slugs):
            continue
        if not isinstance(record, dict) or record.get("status") != "delivered":
            continue
        receipt = record.get("receipt_id")
        if not number_text.isdigit() or not receipt:
            continue
        number = int(number_text)
        if (slug, number) in commented:
            continue
        delivered.setdefault((slug, number), {})[head] = str(receipt)

    mined: "list[tuple[dict[str, Any], dict[str, Any]]]" = []
    for (slug, number), receipts in sorted(delivered.items()):
        repository = _repository_for(settings, slug)
        refs = get_pull_request_refs(repository, number)
        head = refs.get("head")
        if not refs.get("merged") or head not in receipts:
            continue
        try:
            reviews = list_reviews(
                repository, PullRequest(number, "", "", "", head, "")
            )
        except CommandError as error:
            print(f"  skipped control {slug}#{number}: {error}")
            continue
        receipt = receipts[head]
        if not any(
            str(review.get("id")) == receipt
            and str(review.get("state") or "").upper() == "APPROVED"
            and review.get("commit_id") == head
            for review in reviews
        ):
            continue
        if not _ensure_object(repository.path, head):
            print(f"  skipped control {slug}#{number}: {head[:12]} is unreachable")
            continue
        base_ref = refs.get("base_sha") or refs.get("base_ref") or "HEAD"
        if refs.get("base_sha"):
            _ensure_object(repository.path, refs["base_sha"])
        try:
            base = merge_base(repository.path, head, base_ref)
        except CommandError as error:
            print(f"  skipped control {slug}#{number}: {error}")
            continue
        if base == head:
            continue
        mined.append(
            (
                {
                    "slug": slug,
                    "number": number,
                    "head": head,
                    "base": base,
                    "title": str(refs.get("title") or ""),
                    "base_ref": str(refs.get("base_ref") or ""),
                    "captured": "mined",
                    "body": str(refs.get("body") or ""),
                },
                {
                    "slug": slug,
                    "number": number,
                    "head": head,
                    "final_head": head,
                    "expected": [],
                    "control": {"approved_review": receipt, "merged_unchanged": True},
                },
            )
        )
        print(f"  {slug}#{number} at {head[:12]}: control, approved and merged as is")
    return mined


def _finding_document(finding: Finding) -> "dict[str, Any]":
    """Return one candidate as plain data, citations included."""

    return {
        "severity": finding.severity,
        "title": finding.title,
        "path": finding.path,
        "line": finding.line,
        "problem": finding.problem,
        "suggestion": finding.suggestion,
        "trigger": finding.trigger,
        "impact": finding.impact,
        "counterevidence_reason": finding.counterevidence_reason,
        "rule": finding.rule,
        "rule_source": finding.rule_source,
        "evidence": [
            {
                "path": item.path,
                "line": item.line,
                "excerpt": item.excerpt,
                "relevance": item.relevance,
            }
            for item in finding.evidence
        ],
        "counterevidence": [
            {
                "path": item.path,
                "line": item.line,
                "excerpt": item.excerpt,
                "relevance": item.relevance,
            }
            for item in finding.counterevidence
        ],
    }


def _finding_from_document(payload: "dict[str, Any]") -> Finding:
    """Rebuild a candidate the reviewer proposed in an earlier run."""

    def citations(key: str) -> "tuple[FindingEvidence, ...]":
        raw = payload.get(key) or []
        return tuple(
            FindingEvidence(
                path=str(item.get("path", "")),
                line=int(item.get("line", 0)),
                excerpt=str(item.get("excerpt", "")),
                relevance=str(item.get("relevance", "")),
            )
            for item in raw
            if isinstance(item, dict)
        )

    return Finding(
        severity=str(payload["severity"]),
        title=str(payload["title"]),
        path=str(payload["path"]),
        line=int(payload["line"]),
        problem=str(payload.get("problem", "")),
        suggestion=str(payload.get("suggestion", "")),
        trigger=str(payload.get("trigger", "")),
        impact=str(payload.get("impact", "")),
        evidence=citations("evidence"),
        counterevidence=citations("counterevidence"),
        counterevidence_reason=str(payload.get("counterevidence_reason", "")),
        rule=str(payload.get("rule", "")),
        rule_source=str(payload.get("rule_source", "")),
    )


def save_candidates(
    path: Path,
    case: ReplayCase,
    candidates: "Sequence[Finding]",
    attribution: str,
) -> None:
    """Record what a review proposed, so the verifier can be judged on its own.

    A reviewer pass is the expensive half of a measurement — it is what spends the
    daily provider budget — while verification runs on another provider entirely.
    Keeping the candidates lets every verifier question be asked against the same
    proposals: it costs no reviewer budget, and holding the candidates fixed removes
    the reviewer's own run-to-run variance from the comparison, which is what made a
    one-finding difference unreadable before.
    """

    record = {
        "slug": case.slug,
        "number": case.number,
        "head": case.head,
        "base": case.base,
        "reviewer": attribution,
        "recorded": datetime.now().isoformat(timespec="seconds"),
        "candidates": [_finding_document(finding) for finding in candidates],
    }
    # Cases replayed in parallel each read, extend, and rewrite this one file, so the
    # whole exchange is serialized; unguarded, two finishing together would each
    # write back a copy missing the other's case. The write is a rename, so a reader
    # never sees half a file.
    with _CANDIDATES_GUARD:
        stored = load_candidates(path)
        stored[case.pinned] = record
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps({"candidates": stored}, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)


_CANDIDATES_GUARD = threading.Lock()


def load_candidates(path: Path) -> "dict[str, Any]":
    """Return recorded candidates by case, or nothing when none were recorded."""

    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"Cannot read the candidates {path}: {error}") from error
    stored = payload.get("candidates") if isinstance(payload, dict) else None
    return dict(stored) if isinstance(stored, dict) else {}


def candidates_for(
    stored: "dict[str, Any]",
    case: ReplayCase,
) -> "Optional[tuple[list[Finding], str]]":
    """Return the candidates recorded for one pinned case and who proposed them."""

    entry = stored.get(case.pinned)
    if not isinstance(entry, dict):
        return None
    raw = entry.get("candidates")
    if not isinstance(raw, list):
        return None
    try:
        findings = [
            _finding_from_document(item) for item in raw if isinstance(item, dict)
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise ConfigError(
            f"Recorded candidates for {case.label} are unusable: {error}"
        ) from error
    return findings, str(entry.get("reviewer", ""))


def _existing_cases(path: Path) -> "list[dict[str, Any]]":
    """Return the raw cases already pinned, or nothing when none are."""

    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    cases = payload.get("cases") if isinstance(payload, dict) else None
    return [case for case in cases if isinstance(case, dict)] if cases else []


def load_labels(path: Path) -> "dict[str, Any]":
    """Return mined labels, or nothing when none were mined."""

    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"Cannot read the labels {path}: {error}") from error
    labels = payload.get("labels") if isinstance(payload, dict) else None
    return labels if isinstance(labels, dict) else {}


def expected_for(labels: "dict[str, Any]", case: ReplayCase) -> "list[dict[str, Any]]":
    """Return the labelled findings recorded for one pinned case."""

    entry = labels.get(case.pinned)
    if not isinstance(entry, dict):
        return []
    expected = entry.get("expected")
    return expected if isinstance(expected, list) else []


def control_for(
    labels: "dict[str, Any]", case: ReplayCase
) -> "Optional[dict[str, Any]]":
    """Return why a case is a control, or None when it is not one.

    A control is a pull request that needed nothing: the reviewer approved its head
    and it then merged with no further push. It is held with the labels rather than
    with the cases, because like a label it records what happened in production, and
    the cases' fingerprint must keep naming only what is replayed.
    """

    entry = labels.get(case.pinned)
    control = entry.get("control") if isinstance(entry, dict) else None
    return control if isinstance(control, dict) else None


def final_head_for(labels: "dict[str, Any]", case: ReplayCase) -> Optional[str]:
    """Return the commit a case's pull request ended at, when mining recorded it."""

    entry = labels.get(case.pinned)
    final = entry.get("final_head") if isinstance(entry, dict) else None
    return final if isinstance(final, str) and final else None


def changed_later(
    checkout: Path,
    head: str,
    final_head: Optional[str],
    anchors: "Sequence[tuple[str, int]]",
) -> "list[str]":
    """Return the anchors whose lines the author changed after ``head``.

    The same evidence a label is built from, applied to a finding nobody posted: a
    replay confirmed three findings nobody had commented on, and each author had
    changed exactly those lines before merge, while the noise count called all three
    noise. Like a label it is a proxy, since
    a line can change for another reason, and a rebase proves nothing, so a head the
    final commit does not descend from changes nothing here.
    """

    if not final_head or final_head == head or not anchors:
        return []
    _ensure_object(checkout, final_head)
    try:
        _git(checkout, "merge-base", "--is-ancestor", head, final_head)
    except CommandError:
        return []
    changed: "list[str]" = []
    for path, line in anchors:
        try:
            diff = _git(
                checkout, "diff", "--unified=0", f"{head}..{final_head}", "--", path
            )
        except CommandError:
            continue
        if classify_outcome(diff, line) == ACTED_ON:
            changed.append(f"{path}:{line}")
    return changed


def current_attribution(settings: Settings) -> str:
    """Describe the pipeline this run would deliver a finding with."""

    from . import cli

    verify_engine, verify_model = cli._verifier_identity(settings)
    return format_review_attribution(
        settings.review_engine,
        settings.review_model,
        verify_engine,
        verify_model,
    )


def stability(
    hits: "dict[str, int]",
    known: "set[str]",
    pass_rates: "Sequence[float]",
) -> "dict[str, Any]":
    """Describe how the same corpus behaved across repeated passes.

    ``always`` is the part of the corpus this reviewer reproduces dependably, and it
    is the only part a single pass could have measured honestly. ``sometimes`` is the
    reviewer choosing differently between runs over identical input, and ``spread``
    is how far two passes of the very same thing can sit apart — the floor any claimed
    improvement has to clear.
    """

    total = len(pass_rates)
    always = sorted(anchor for anchor in known if hits.get(anchor, 0) == total)
    never = sorted(anchor for anchor in known if not hits.get(anchor))
    sometimes = sorted(
        anchor for anchor in known if 0 < hits.get(anchor, 0) < total
    )
    return {
        "passes": total,
        "rates": list(pass_rates),
        "moved": len(sometimes),
        "spread": round(max(pass_rates) - min(pass_rates), 3),
        "always": always,
        "sometimes": sometimes,
        "never": never,
        "stable_rate": round(len(always) / len(known), 3) if known else 0.0,
    }


def describe_stability(summary: "dict[str, Any]") -> "list[str]":
    """Return the lines a multi-pass measurement reports."""

    rates = ", ".join(f"{rate:.3f}" for rate in summary["rates"])
    lines = [
        f"Across {summary['passes']} passes the reproduction rate was {rates}, "
        f"a spread of {summary['spread']}",
        f"  {len(summary['always'])} findings reproduced in every pass, "
        f"{len(summary['sometimes'])} in some, {len(summary['never'])} in none",
    ]
    if summary["sometimes"]:
        # Two passes can report the same rate over a different set, which is how a
        # measurement looks stable while measuring nothing. The count of findings that
        # moved is the variance; the spread of the rates can hide all of it.
        lines.append(
            f"  {len(summary['sometimes'])} findings moved between passes, so a rate "
            "alone cannot tell a change from this:"
        )
        for anchor in summary["sometimes"][:5]:
            lines.append(f"    unstable  {anchor}")
    return lines


def _matches(item: "dict[str, Any]", anchors: "set[tuple[str, int]]") -> bool:
    return any(
        item["path"] == path and abs(item["line"] - line) <= ANCHOR_TOLERANCE
        for path, line in anchors
    )


def score_against_labels(
    expected: "Sequence[dict[str, Any]]",
    candidates: "Sequence[Any]",
    confirmed: "Sequence[Any]",
) -> "dict[str, Any]":
    """Return how much of what authors acted on this review found again, and what
    else it confirmed.

    Counted twice on purpose. `raised` is what the review proposed and `reproduced`
    is what survived verification, and the gap between them says which half of the
    reviewer changed: a finding the review still raises but the verifier now throws
    out is a verification regression, not a weaker review, and the two need different
    fixes.

    Only findings authors acted on are scored as found. A comment nobody acted on is
    not evidence of anything: the team may have disagreed with it, deferred it, or
    simply merged past it, and counting it either way would invent a fact. That is
    why a confirmed finding on such a comment is reported apart, as `unacted`, from
    one on no comment at all, and neither is called a false positive.

    Every acted-on finding is scored, whichever pipeline first posted it. A defect an
    author fixed is real whoever found it, and it is exactly what a new reviewer has
    to find again; scoring only the current pipeline's own findings scored nothing at
    all once the reviewer model changed. What the origin still decides is how a
    rejection reads. Sixteen of this corpus's first thirty-nine acted-on findings were
    posted before any verification pass existed, and one of them read as a verifier
    regression until its comment was checked: today's verifier was not throwing out
    what it once confirmed, it was seeing that claim for the first time. Such a
    finding is named in ``first_judged`` rather than read as a stricter verifier.
    """

    raised_anchors = {(finding.path, finding.line) for finding in candidates}
    confirmed_anchors = {(finding.path, finding.line) for finding in confirmed}
    acted_on = [item for item in expected if item.get("outcome") == ACTED_ON]
    raised = [item for item in acted_on if _matches(item, raised_anchors)]
    reproduced = [item for item in acted_on if _matches(item, confirmed_anchors)]
    unverified = [item for item in acted_on if not _verified_at_delivery(item)]
    others = [item for item in expected if item.get("outcome") != ACTED_ON]
    # The other half of a review: what it confirmed that no author acted on. Only an
    # upper bound on noise, because an unlabelled finding may be a real defect nobody
    # had found yet, but a change that raises recall by raising this as fast is not
    # the improvement it looks like, and recall alone could not say so.
    unacted: "list[str]" = []
    unlabelled: "list[str]" = []
    for finding in confirmed:
        anchor = {(finding.path, finding.line)}
        if any(_matches(item, anchor) for item in acted_on):
            continue
        named = f"{finding.path}:{finding.line}"
        if any(_matches(item, anchor) for item in others):
            unacted.append(named)
        else:
            unlabelled.append(named)
    return {
        "acted_on": len(acted_on),
        "unverified_origin": len(unverified),
        "raised": len(raised),
        "reproduced": len(reproduced),
        "confirmed": len(confirmed),
        "unacted": len(unacted),
        "unlabelled": len(unlabelled),
        # Which anchors, not just how many. A rate cannot say whether two passes
        # reproduced the same findings or a different subset of the same size, and
        # that distinction is the difference between a property and a coin toss.
        "anchors": [f"{item['path']}:{item['line']}" for item in acted_on],
        "reproduced_anchors": [
            f"{item['path']}:{item['line']}" for item in reproduced
        ],
        "missed": [
            f"{item['path']}:{item['line']}"
            for item in acted_on
            if item not in reproduced
        ],
        "first_judged": [
            f"{item['path']}:{item['line']}"
            for item in raised
            if item not in reproduced and item in unverified
        ],
        "unacted_anchors": unacted,
        "unlabelled_anchors": unlabelled,
    }


def _verified_at_delivery(item: "dict[str, Any]") -> bool:
    """Report whether a labelled finding had a verification pass behind it."""

    return VERIFIED_ATTRIBUTION in str(item.get("attribution") or "")


def _repository_for(settings: Settings, slug: str) -> Repository:
    for repository in settings.repositories:
        if repository.slug == slug:
            return repository
    raise ConfigError(
        f"{slug} is not a configured repository, so it cannot be replayed"
    )


class _PreparedCase(NamedTuple):
    """One pinned case checked out and read exactly as a live review reads it."""

    repository: Repository
    pull_request: PullRequest
    checkout: Repository
    material: Any
    extra_bundles: "tuple[Any, ...]"


@contextmanager
def _prepared_case(
    settings: Settings, case: ReplayCase
) -> "Iterator[Optional[_PreparedCase]]":
    """Check out one pinned case and build what every level reviews.

    Yields None when the pinned range changes nothing. Shared by the full replay and
    the advisory one, so a level measured alone reads exactly what it reads beside
    the others.
    """

    from . import cli

    repository = _repository_for(settings, case.slug)
    pull_request = PullRequest(
        number=case.number,
        title=case.title or f"replay of {case.label}",
        body=case.body,
        author="replay",
        head_sha=case.head,
        url="",
        head_ref=case.head_ref,
        base_ref=case.base_ref,
    )
    print(f"    - {case.pinned} against {case.base[:12]}")
    files = changed_files(repository.path, case.base, case.head)
    if not files:
        print("      -> Skipped: the pinned range changes nothing")
        yield None
        return
    # Bundled exactly as a live review bundles, or this harness could not measure
    # the one change it was built to measure.
    material, extra_bundles = cli._bundled_material(files, settings, ())
    if extra_bundles:
        print(
            f"      -> Reviewing in {len(extra_bundles) + 1} bundles: the change is "
            "larger than one review can read"
        )
    workspace_root = settings.state_file.parent / "worktrees"
    workspace = cli.review_workspace(repository, pull_request, workspace_root)
    with workspace as exact_repository:
        # A review's dependency evidence is part of what it is judged on. Without
        # these links the verifier cannot check a claim about an installed package
        # and rejects it as unevidenced, which is exactly what the first labelled
        # replay did to two findings production had delivered.
        if workspace.dependency_links:
            material = replace(
                material, dependency_links=workspace.dependency_links
            )
            for note in workspace.dependency_notes:
                print(f"      -> Dependency evidence: {note}")
        background = read_background(exact_repository.path)
        if background:
            material = replace(material, background=background)
        history = cli._read_file_history(repository, pull_request, files, case.base)
        if history:
            material = replace(material, file_history=history)
        # The rules the pinned base held, so a replay judges what production judged.
        review_rules = cli.read_review_rules(repository, pull_request, case.base)
        if review_rules.sections:
            material = replace(material, review_rules=review_rules)
            cli._report_review_rules(review_rules)
        cli._report_diff_budget(material)
        yield _PreparedCase(
            repository, pull_request, exact_repository, material, tuple(extra_bundles)
        )


def replay_case(
    settings: Settings,
    case: ReplayCase,
    metrics: ReviewMetrics,
    expected: "Sequence[dict[str, Any]]" = (),
    candidates_path: Optional[Path] = None,
    recorded: "Optional[tuple[list[Finding], str]]" = None,
    final_head: Optional[str] = None,
) -> "dict[str, Any]":
    """Review one pinned case, recording what every level decided.

    With ``recorded`` the review itself is skipped and those candidates are verified
    instead. That measures the verifier alone, against proposals held fixed, for the
    price of the verification provider rather than a day of the reviewer's budget.
    ``final_head`` is where the pull request ended, used to ask whether a confirmed
    finding nobody commented on sat on lines its author later changed.
    """

    from . import cli

    with _prepared_case(settings, case) as prepared:
        if prepared is None:
            return {}
        repository, pull_request, exact_repository, material, extra_bundles = prepared
        engine = cli.create_review_engine(settings)
        verification = cli._create_verification_pass(settings, engine)
        fallback = cli._create_fallback_reviewer(settings)
        usage = cli._ModelUsage()

        def measured(inner, role):
            return cli._MeasuredEngine(inner, usage, role, metrics, repository)

        # Wrapped once, so every level's fallback calls are measured the same way;
        # the bundle path wrapped the reviewer itself and so measured nothing.
        measured_fallback = (
            replace(fallback, engine=measured(fallback.engine, "fallback"))
            if fallback is not None
            else None
        )
        # The advisory levels are reviewer work, so verifying recorded candidates
        # skips them: their whole point is costing no reviewer budget.
        if settings.design_level and recorded is None:
            try:
                assessment = cli._design_level(
                    settings,
                    measured(engine, "review"),
                    _measured_verification(verification, measured),
                    exact_repository,
                    pull_request,
                    material,
                    fallback=measured_fallback,
                )
                print(f"      -> Solution design judged {assessment.verdict}")
            except (ReviewError, CommandError, EngineError) as error:
                print(f"      -> Design level failed: {error}")
        if settings.docs_level and recorded is None:
            try:
                docs = cli._docs_level(
                    settings,
                    measured(engine, "review"),
                    _measured_verification(verification, measured),
                    exact_repository,
                    pull_request,
                    material,
                    fallback=measured_fallback,
                )
                print(f"      -> Documentation judged {docs.verdict}")
            except (ReviewError, CommandError, EngineError) as error:
                print(f"      -> Documentation level failed: {error}")
        if recorded is not None:
            findings, proposer = recorded
            review_settings, review_engine = settings, engine
            # Verifying held proposals never re-asks the reviewer for a verdict, so
            # this mode has no approval to report and must not invent one.
            approve: Optional[bool] = None
            approval_reason = ""
            print(
                f"      -> Verifying {len(findings)} recorded "
                f"candidate{'s' if len(findings) != 1 else ''} "
                f"proposed by {proposer or 'an unrecorded reviewer'}",
                flush=True,
            )
        else:
            try:
                (
                    structured_output,
                    findings,
                    approve,
                    approval_reason,
                    blocking_comment_ids,
                    review_settings,
                    review_engine,
                ) = cli._review_with_engine_fallback(
                    settings,
                    measured(engine, "review"),
                    measured_fallback,
                    exact_repository,
                    pull_request,
                    material,
                )
            except (ReviewError, CommandError, EngineError) as error:
                # Raised, not swallowed: an empty result here was scored as a clean
                # review, and an error must never become one.
                print(f"      -> Code level failed: {error}")
                raise
        candidates = list(findings)
        if candidates_path is not None:
            save_candidates(
                candidates_path, case, candidates, current_attribution(settings)
            )
        if findings:
            (
                _,
                findings,
                verified_approve,
                verified_reason,
                _,
            ) = cli._verify_code_candidates(
                settings,
                _measured_verification(verification, measured),
                review_settings,
                measured(review_engine, "verify"),
                exact_repository,
                pull_request,
                material,
                findings,
            )
            if recorded is None:
                approve, approval_reason = verified_approve, verified_reason
        for index, bundle in enumerate(extra_bundles, start=2):
            bundle_material = cli._bundle_material_from(material, bundle)
            print(
                f"      -> Reviewing bundle {index} of {len(extra_bundles) + 1}: "
                f"{bundle.diff_budget.reviewed_files} more files",
                flush=True,
            )
            try:
                (
                    _,
                    bundle_findings,
                    _,
                    _,
                    _,
                    bundle_settings,
                    bundle_engine,
                ) = cli._review_with_engine_fallback(
                    settings,
                    measured(engine, "review"),
                    measured_fallback,
                    exact_repository,
                    pull_request,
                    bundle_material,
                )
            except (ReviewError, CommandError, EngineError) as error:
                print(f"      -> Bundle {index} failed: {error}")
                # The live review would have failed whole here, so no approval can
                # be claimed for files nobody read.
                if approve:
                    approve, approval_reason = None, f"bundle {index} failed"
                continue
            if bundle_findings:
                (_, bundle_findings, _, _, _) = cli._verify_code_candidates(
                    settings,
                    _measured_verification(verification, measured),
                    bundle_settings,
                    measured(bundle_engine, "verify"),
                    exact_repository,
                    pull_request,
                    bundle_material,
                    bundle_findings,
                )
            candidates = list(candidates) + list(bundle_findings)
            findings = list(findings) + list(bundle_findings)
            if bundle_findings and approve is not None:
                # As live: a bundle may take an approval away but never grant one.
                approve = False
                approval_reason = f"bundle {index} confirmed a finding"
        budget = material.diff_budget
        if approve is not None and budget is not None and budget.is_partial:
            approve = False
            approval_reason = (
                f"the review read {budget.reviewed_files} of {budget.total_files} "
                "changed files"
            )

    for finding in findings:
        # Named because a replay delivers nothing, so this transcript is the only
        # record of what the review actually held. The rejected candidates were
        # already named with the verifier's reason; without this the evidence is
        # one-sided, and reading it back cost a second run.
        print(
            f"      -> Confirmed finding [{finding.severity.upper()}] "
            f"{finding.path}:{finding.line} {finding.title}"
        )

    spend = usage.describe()
    if spend:
        print(f"      -> Spent {spend}")
    if approve is not None:
        # The decision a live run would have delivered, after the bundles and the
        # budget had their say, because that is what a control case is judged on.
        print(
            "      -> Would approve"
            if approve
            else f"      -> Would withhold approval: {approval_reason or 'no reason'}"
        )
    elif recorded is None:
        print(f"      -> Approval undecided: {approval_reason}")
    score = score_against_labels(expected, candidates, findings)
    score["approved"] = approve
    score["approval_reason"] = approval_reason or ""
    if score["acted_on"]:
        print(
            f"      -> Raised {score['raised']} and confirmed {score['reproduced']} "
            f"of {score['acted_on']} findings authors acted on"
            + (f"; missed {', '.join(score['missed'])}" if score["missed"] else "")
        )
    unmatched_anchors = score["unacted_anchors"] + score["unlabelled_anchors"]
    score["fixed_later_anchors"] = changed_later(
        repository.path,
        case.head,
        final_head,
        [
            (finding.path, finding.line)
            for finding in findings
            if f"{finding.path}:{finding.line}" in unmatched_anchors
        ],
    )
    score["fixed_later"] = len(score["fixed_later_anchors"])

    def tagged(anchor: str, kind: str) -> str:
        later = ", fixed later" if anchor in score["fixed_later_anchors"] else ""
        return f"{anchor} ({kind}{later})"

    unmatched = [
        tagged(anchor, "unacted") for anchor in score["unacted_anchors"]
    ] + [tagged(anchor, "unlabelled") for anchor in score["unlabelled_anchors"]]
    if unmatched:
        plural = "s" if len(unmatched) != 1 else ""
        print(
            f"      -> {len(unmatched)} confirmed finding{plural} match nothing "
            f"authors acted on: {', '.join(unmatched)}"
        )
    if score["first_judged"]:
        print(
            f"      -> First judged here: {', '.join(score['first_judged'])} "
            "reached the author with no verification pass, so throwing it out is "
            "not a stricter verifier"
        )
    return score


def _replay_wave(
    settings: Settings,
    pending: "Sequence[ReplayCase]",
    metrics: ReviewMetrics,
    labels: "Optional[dict[str, Any]]",
    candidates_path: Optional[Path],
    recorded: "Optional[dict[str, Any]]",
    concurrency: int,
) -> "list[tuple[ReplayCase, str, Any]]":
    """Replay one wave of cases, returning each outcome in the order given.

    Each outcome is ``skipped``, ``failed`` with the error, or ``scored`` with the
    score. Several cases may run at once, because a pass of sixty cases one at a time
    is most of a working day; each one's report is held until it finishes and then
    printed whole, so the transcript reads case by case rather than interleaved. The
    tally stays with the caller, in case order, so running in parallel changes when
    a case is measured and never what the measurement says.
    """

    def attempt(case: ReplayCase) -> "tuple[ReplayCase, str, Any]":
        case_recorded = (
            candidates_for(recorded, case) if recorded is not None else None
        )
        if recorded is not None and case_recorded is None:
            print(f"    - {case.pinned}: no recorded candidates; skipped")
            return case, "skipped", None
        try:
            score = replay_case(
                settings,
                case,
                metrics,
                expected_for(labels or {}, case),
                candidates_path=candidates_path,
                recorded=case_recorded,
                final_head=final_head_for(labels or {}, case),
            )
        except (
            ConfigError,
            CommandError,
            ReviewError,
            EngineError,
            WorkspaceError,
        ) as error:
            # One engine failure must not cost the fifteen cases behind it. A
            # provider that refuses a call, most often on quota, is a fact about the
            # run and is recorded as one, so the measurement reports what it managed.
            # A workspace that cannot be prepared is the same kind of fact: one git
            # fetch that timed out ended a whole two-pass run nine cases in.
            print(f"      -> ERROR: {error}", file=sys.stderr)
            return case, "failed", error
        return case, "scored", score

    if concurrency <= 1 or len(pending) <= 1:
        return [attempt(case) for case in pending]

    report_guard = threading.Lock()

    def attempt_recorded(case: ReplayCase) -> "tuple[ReplayCase, str, Any]":
        with progress.recording() as recorder:
            outcome = attempt(case)
        with report_guard:
            recorder.replay()
        return outcome

    with progress.routed_output():
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="prwarden-replay",
        ) as pool:
            return list(pool.map(attempt_recorded, pending))


def replay(
    settings: Settings,
    cases: "Sequence[ReplayCase]",
    arm: ReplayArm,
    metrics_path: Optional[str],
    labels: "Optional[dict[str, Any]]" = None,
    passes: int = 1,
    candidates_path: Optional[Path] = None,
    recorded: "Optional[dict[str, Any]]" = None,
    concurrency: int = 1,
) -> int:
    """Replay every case, then write the measurement.

    More than one pass replays the same cases again from the same pins. The reviewer
    is nondeterministic, so a single pass reports one draw from what it might have
    said: the first labelled baseline reproduced 8 of 21 findings, and nothing in that
    number said whether a second pass would reproduce those same 8 or a different 8.
    Passes answer that, and a difference smaller than their spread is not a finding.
    """

    pace = f", {concurrency} at a time" if concurrency > 1 else ""
    print(f"Replaying {len(cases)} cases under arm {arm.name!r}{pace}:")
    metrics = ReviewMetrics()
    failures = 0
    # Which cases a pass actually measured. Reporting only the totals let two runs
    # over different subsets be compared as though they covered the same corpus.
    scored_labels: "set[str]" = set()
    failed_labels: "set[str]" = set()
    skipped_labels: "set[str]" = set()
    scored: "dict[str, Any]" = {
        "acted_on": 0,
        "unverified_origin": 0,
        "raised": 0,
        "reproduced": 0,
        "missed": [],
        "first_judged": [],
        "confirmed": 0,
        "unacted": [],
        "unlabelled": [],
        "fixed_later": [],
    }
    # A control case needed nothing: approved by the reviewer, then merged without
    # another push. The one thing to measure there is whether it is approved again.
    controls: "dict[str, list[str]]" = {
        "approved": [],
        "withheld": [],
        "undecided": [],
    }
    hits: "dict[str, int]" = {}
    known: "set[str]" = set()
    pass_rates: "list[float]" = []
    for attempt in range(1, max(passes, 1) + 1):
        if passes > 1:
            print(f"  pass {attempt} of {passes}:")
        reproduced_here = 0
        acted_on_here = 0
        pending = list(cases)
        # A provider that cuts a response mid-stream takes the whole case with it. On
        # 2026-09-23 that shrank a pass from seventeen cases to thirteen, and because
        # only the totals were reported the arm looked like a drop rather than a
        # smaller sample. Two of five such failures recovered on an immediate retry,
        # so one more attempt after the other cases have run is worth its invocation.
        for wave in (1, 2):
            if wave == 2 and pending:
                print(
                    f"  Re-attempting {len(pending)} case(s) that failed, now the "
                    "rest of the corpus has run:"
                )
            deferred: "list[ReplayCase]" = []
            outcomes = _replay_wave(
                settings,
                pending,
                metrics,
                labels,
                candidates_path,
                recorded,
                concurrency,
            )
            for case, outcome, value in outcomes:
                if outcome == "skipped":
                    skipped_labels.add(case.pinned)
                    continue
                if outcome == "failed":
                    failed_labels.add(case.pinned)
                    if wave == 1 and not is_exhausted_quota(str(value)):
                        deferred.append(case)
                    else:
                        # A spent quota will refuse the retry too, so it is not one
                        # the second wave can do anything about.
                        failures += 1
                    continue
                score = value
                scored_labels.add(case.pinned)
                failed_labels.discard(case.pinned)
                acted_on_here += score.get("acted_on", 0)
                reproduced_here += score.get("reproduced", 0)
                known.update(score.get("anchors", ()))
                for anchor in score.get("reproduced_anchors", ()):
                    hits[anchor] = hits.get(anchor, 0) + 1
                if attempt == 1:
                    for name in ("acted_on", "unverified_origin", "raised"):
                        scored[name] += score.get(name, 0)
                    scored["reproduced"] += score.get("reproduced", 0)
                    scored["missed"].extend(score.get("missed", []))
                    scored["first_judged"].extend(score.get("first_judged", []))
                    scored["confirmed"] += score.get("confirmed", 0)
                    for name in ("unacted", "unlabelled", "fixed_later"):
                        scored[name].extend(
                            f"{case.pinned} {anchor}"
                            for anchor in score.get(f"{name}_anchors", ())
                        )
                    if control_for(labels or {}, case) and "approved" in score:
                        decision = score["approved"]
                        if decision is None:
                            controls["undecided"].append(
                                f"{case.pinned}: {score.get('approval_reason')}"
                            )
                        elif decision:
                            controls["approved"].append(case.pinned)
                        else:
                            controls["withheld"].append(
                                f"{case.pinned}: {score.get('approval_reason')}"
                            )
            pending = deferred
            if not pending:
                break
        failures += len(pending)
        if acted_on_here:
            pass_rates.append(round(reproduced_here / acted_on_here, 3))
    engine, model = measured_engine(settings, verifying=recorded is not None)
    document = metrics.document(engine=engine, model=model)
    if recorded is None:
        # An arm names the settings a replay ran under, and a verification pass takes
        # none: no arm may describe the verifier. Stamping "baseline" on it therefore
        # claimed the baseline for whatever tree was being measured, which is how two
        # opposing passes came to archive the same arm name. The reviewer commit is
        # what tells them apart.
        document["arm"] = arm.describe()
    document["reviewer"] = reviewer_provenance()
    document["cases"] = {
        "count": len(cases),
        "fingerprint": cases_fingerprint(cases),
        # Named, not just counted: a comparison has to be able to restrict itself to
        # the cases both sides measured, and a total cannot say which those were.
        "scored": sorted(scored_labels),
        "failed": sorted(failed_labels),
        "skipped": sorted(skipped_labels),
    }
    document["settings"] = {
        name: getattr(settings, name) for name in sorted(OVERRIDABLE_SETTINGS)
    }
    if len(pass_rates) > 1:
        document["labels_passes"] = stability(hits, known, pass_rates)
        for line in describe_stability(document["labels_passes"]):
            print(line)
    if scored["acted_on"]:
        document["labels"] = {
            "acted_on": scored["acted_on"],
            "unverified_origin": scored["unverified_origin"],
            "raised": scored["raised"],
            "reproduced": scored["reproduced"],
            "raise_rate": round(scored["raised"] / scored["acted_on"], 3),
            "reproduction_rate": round(
                scored["reproduced"] / scored["acted_on"], 3
            ),
            # Of the real defects the verifier was shown, the share it kept. This is
            # the verifier's number, where the confirmation rate is not: a verifier
            # arm that kept 4 of 5 labelled defects had the lower confirmation rate
            # of the two, because it also threw out more of everything else.
            "retention_rate": (
                round(scored["reproduced"] / scored["raised"], 3)
                if scored["raised"]
                else None
            ),
        }
        document["labels_missed"] = sorted(scored["missed"])
        document["labels_first_judged"] = sorted(scored["first_judged"])
        print(
            f"Raised {scored['raised']} and confirmed {scored['reproduced']} of "
            f"{scored['acted_on']} findings authors acted on"
        )
        if scored["raised"]:
            print(
                f"The verifier kept {scored['reproduced']} of the {scored['raised']} "
                "labelled defects it was shown"
            )
        if scored["first_judged"]:
            print(
                f"  {len(scored['first_judged'])} of those it threw out reached their "
                "author with no verification pass, so are not a stricter verifier: "
                + ", ".join(sorted(scored["first_judged"]))
            )
    if scored["confirmed"]:
        unmatched = len(scored["unacted"]) + len(scored["unlabelled"])
        unexplained = unmatched - len(scored["fixed_later"])
        document["confirmed"] = {
            "total": scored["confirmed"],
            "matched": scored["confirmed"] - unmatched,
            "unacted": len(scored["unacted"]),
            "unlabelled": len(scored["unlabelled"]),
            # An upper bound on noise, never a count of false positives: what no
            # author acted on may still be real. It is the number that says whether
            # a rise in recall was bought by confirming more of everything.
            "unmatched_rate": round(unmatched / scored["confirmed"], 3),
            # Of those, the ones on lines their author changed before the pull
            # request ended, which is the evidence a label itself rests on. What is
            # left is the tighter bound.
            "fixed_later": len(scored["fixed_later"]),
            "unexplained": unexplained,
            "unexplained_rate": round(unexplained / scored["confirmed"], 3),
        }
        document["confirmed_unacted"] = sorted(scored["unacted"])
        document["confirmed_unlabelled"] = sorted(scored["unlabelled"])
        document["confirmed_fixed_later"] = sorted(scored["fixed_later"])
        print(
            f"{unmatched} of {scored['confirmed']} confirmed findings match nothing "
            f"authors acted on ({len(scored['unacted'])} on a comment nobody acted "
            f"on, {len(scored['unlabelled'])} on no comment at all)"
        )
        if scored["fixed_later"]:
            print(
                f"  {len(scored['fixed_later'])} of those sit on lines their author "
                f"changed later in the same pull request, leaving {unexplained} "
                "unexplained"
            )
    measured_controls = sum(len(names) for names in controls.values())
    if measured_controls:
        decided = len(controls["approved"]) + len(controls["withheld"])
        document["controls"] = {
            "cases": measured_controls,
            "approved": len(controls["approved"]),
            "withheld": len(controls["withheld"]),
            "undecided": len(controls["undecided"]),
            "approval_rate": (
                round(len(controls["approved"]) / decided, 3) if decided else None
            ),
        }
        document["controls_withheld"] = sorted(controls["withheld"])
        document["controls_undecided"] = sorted(controls["undecided"])
        print(
            f"Approved {len(controls['approved'])} of {measured_controls} control "
            "cases, which needed nothing"
            + (
                f"; {len(controls['undecided'])} undecided"
                if controls["undecided"]
                else ""
            )
        )
        for line in controls["withheld"]:
            print(f"  withheld on {line}")
    if metrics_path:
        destination = Path(metrics_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"Measurement written to {metrics_path}")
    else:
        print(json.dumps(document["summary"], indent=2, sort_keys=True))
    return 1 if failures else 0


# An advisory level measured alone. The design and documentation levels each run once
# per pull request, so their measurement needs the pull request pinned at the head the
# level actually saw, and its verifier compared on the very proposals it judged.

# owner/repository#number, optionally pinned to one of its heads by sha.
PULL_REQUEST_SPEC = re.compile(
    r"(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)"
    r"(?:@(?P<head>[0-9a-fA-F]{7,40}))?"
)
ADVISORY_LEVEL_NOUNS = {DESIGN_LEVEL: "design", DOCS_LEVEL: "documentation"}
_VERIFICATION_PROMPT_BUILDERS = {
    DESIGN_LEVEL: "build_design_verification_prompt",
    DOCS_LEVEL: "build_docs_verification_prompt",
}


def parse_pull_request_spec(spec: str) -> "tuple[str, int, Optional[str]]":
    """Return the repository, number, and optional head one ``--pr`` names."""

    match = PULL_REQUEST_SPEC.fullmatch(spec.strip())
    if match is None:
        raise ConfigError(
            f"--pr {spec!r} must look like owner/repository#123, optionally followed "
            "by @ and the sha of the head to pin"
        )
    return match.group("slug"), int(match.group("number")), match.group("head")


def _full_commit(checkout: Path, sha: str) -> Optional[str]:
    """Return the full sha a possibly abbreviated one names in this checkout."""

    if len(sha) == 40:
        _ensure_object(checkout, sha)
    try:
        resolved = _git(
            checkout,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{sha}^{{commit}}",
            timeout_seconds=30,
        ).strip()
    except CommandError:
        return None
    return resolved or None


def case_for_pull_request(settings: Settings, spec: str) -> ReplayCase:
    """Pin one pull request named on the command line, at the head it names.

    An advisory level runs once per pull request, at whichever head it first saw,
    and that is rarely the head a mined case is pinned at. Read from GitHub and the
    local checkout only: nothing is written to the case set, which mining and
    capture rewrite.
    """

    from .github import get_pull_request_refs

    slug, number, wanted = parse_pull_request_spec(spec)
    repository = _repository_for(settings, slug)
    refs = get_pull_request_refs(repository, number)
    if not isinstance(refs.get("head"), str):
        raise ConfigError(f"GitHub could not describe {slug}#{number}")
    checkout = repository.path
    base_ref = refs.get("base_ref") or "HEAD"
    # The pull request's own ref carries every head it was pushed at, short of a
    # force push, so an earlier head can be named by an abbreviated sha.
    _git(
        checkout,
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-write-fetch-head",
        "origin",
        f"refs/pull/{number}/head",
    )
    head = _full_commit(checkout, wanted or refs["head"])
    if head is None:
        raise ConfigError(
            f"{wanted} is not a commit {slug}#{number} was pushed at; a head a force "
            "push removed can still be named by its full sha"
        )
    merge_commit = refs.get("merge_commit")
    if refs.get("merged") and isinstance(merge_commit, str):
        _git(checkout, "fetch", "--quiet", "--no-tags", "origin", base_ref)
        if not _ensure_object(checkout, merge_commit):
            raise ConfigError(
                f"Cannot fetch {slug}#{number}'s merge commit {merge_commit[:12]}"
            )
        # Merged, the head is an ancestor of its base branch, so the merge base with
        # the branch's tip is the head itself and the pinned diff would be empty. The
        # branch as it stood before the merge is what the review was compared with.
        base = _git(checkout, "merge-base", head, f"{merge_commit}^1").strip()
    else:
        base = merge_base(checkout, head, base_ref)
    return ReplayCase(
        slug=slug,
        number=number,
        head=head,
        base=base,
        title=refs.get("title") or "",
        head_ref=refs.get("head_ref") or "",
        base_ref=refs.get("base_ref") or "",
        captured="named",
        body=refs.get("body") or "",
    )


@contextmanager
def baseline_engine(
    ref: str, root: Optional[Path] = None
) -> "Iterator[tuple[ModuleType, str]]":
    """Import this reviewer's engine module as it was committed at ``ref``.

    Loaded from that commit's files as a package of its own, beside the running one,
    so a verifier prompt from ``ref`` can be judged against today's on the same
    proposals without checking anything out. Yields the module and the full commit.
    """

    root = root or Path(__file__).resolve().parent.parent

    def git(*args: str) -> str:
        return run_cmd(["git", "-C", str(root), *args], timeout_seconds=60)

    try:
        commit = git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
    except CommandError as error:
        raise ConfigError(
            f"--baseline-verifier {ref} names no commit of this reviewer"
        ) from error
    package = f"_baseline_{commit[:12]}"
    with tempfile.TemporaryDirectory(prefix="baseline-verifier-") as directory:
        # The package lives in prwarden/ from 2.0.0 on and in src/ before it, and a
        # baseline worth comparing against is as likely to be either.
        for folder in PACKAGE_FOLDERS:
            names = [
                name
                for name in git(
                    "ls-tree", "-r", "--name-only", commit, "--", folder
                ).split("\n")
                if name.endswith(".py")
            ]
            if names:
                break
        for name in names:
            target = Path(directory, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(git("show", f"{commit}:{name}"), encoding="utf-8")
        source = Path(directory, folder)
        spec = importlib.util.spec_from_file_location(
            package, source / "__init__.py", submodule_search_locations=[str(source)]
        )
        if spec is None or spec.loader is None:
            raise ConfigError(f"{ref} holds no importable reviewer package")
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        try:
            spec.loader.exec_module(module)
            yield importlib.import_module(f"{package}.engine"), commit
        finally:
            for name in [
                name
                for name in sys.modules
                if name == package or name.startswith(f"{package}.")
            ]:
                del sys.modules[name]


@contextmanager
def verification_prompts_from(baseline: ModuleType, level: str) -> "Iterator[None]":
    """Have the verifier read the baseline's prompt for one level while inside.

    Every adapter renders its prompt through ``engine.build_prompt``, which looks
    this builder up by name when it runs, so swapping it changes what the verifier
    reads and nothing else: the adapter, schema, model, workspace, and proposals stay
    today's. It is module state, which is why advisory replays run one at a time.
    """

    from . import engine

    name = _VERIFICATION_PROMPT_BUILDERS[level]
    builder = getattr(baseline, name, None)
    if builder is None:
        raise ConfigError(
            f"The baseline reviewer has no {level} verification prompt to compare"
        )
    current = getattr(engine, name)
    setattr(engine, name, builder)
    try:
        yield
    finally:
        setattr(engine, name, current)


class _PairedVerifier:
    """The verification engine of one advisory case, which can also ask a baseline.

    It keeps the proposals the level hands its verifier, so the replay can report
    every one of them, and given a baseline it first has the same engine judge the
    same proposals under the baseline's prompt. Only the first set is asked about:
    a second call is the level's correction retry over the same proposals.
    """

    def __init__(self, inner: Any, level: str, baseline: Optional[ModuleType]):
        self._inner = inner
        self._level = level
        self._baseline = baseline
        self.candidates: "tuple[Any, ...]" = ()
        self.baseline_judged: "Optional[tuple[tuple[Any, ...], tuple[Any, ...]]]" = None
        self.baseline_error: Optional[str] = None

    def __getattr__(self, name: str) -> Any:
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    def review(self, repository, pull_request, material, **kwargs):
        candidates = (
            material.design_candidates
            if self._level == DESIGN_LEVEL
            else material.docs_candidates
        )
        if candidates and not self.candidates:
            self.candidates = tuple(candidates)
            if self._baseline is not None:
                self._ask_baseline(repository, pull_request, material, kwargs)
        return self._inner.review(repository, pull_request, material, **kwargs)

    def _ask_baseline(self, repository, pull_request, material, kwargs) -> None:
        from .review import validate_design_judgments, validate_docs_judgments

        validate = (
            validate_design_judgments
            if self._level == DESIGN_LEVEL
            else validate_docs_judgments
        )
        try:
            with verification_prompts_from(self._baseline, self._level):
                output = self._inner.review(
                    repository, pull_request, material, **kwargs
                )
            self.baseline_judged = validate(output, self.candidates)
        except (ReviewError, CommandError, EngineError) as error:
            # Recorded apart: a baseline that could not answer judged nothing, and
            # reading its silence as a rejection would invent a difference.
            self.baseline_error = str(error)


def _verdict(
    candidate: Any, confirmed: Any, rejected: Any
) -> "Optional[dict[str, str]]":
    if any(candidate == item for item in confirmed):
        return {"verdict": "confirmed", "reason": ""}
    for item in rejected:
        if item.finding == candidate:
            return {"verdict": "rejected", "reason": item.reason}
    return None


def _describe_verdict(judged: "dict[str, str]") -> str:
    if judged["verdict"] == "rejected":
        return f"rejected because {judged['reason']}"
    return judged["verdict"]


def advisory_case(
    settings: Settings,
    case: ReplayCase,
    level: str,
    metrics: ReviewMetrics,
    baseline: "Optional[tuple[ModuleType, str]]" = None,
) -> "dict[str, Any]":
    """Run one advisory level alone on one pinned case and judge its proposals.

    The level runs exactly as live, review then verification, and every proposal is
    reported with its category and today's verdict. Given a baseline, the same
    proposals are also judged under the baseline's verifier prompt, so a difference
    between the two is the verifier and not the reviewer's run-to-run variance.
    """

    from . import cli

    noun = ADVISORY_LEVEL_NOUNS[level]
    record: "dict[str, Any]" = {"case": case.pinned, "proposals": []}
    assessment = None
    with _prepared_case(settings, case) as prepared:
        if prepared is None:
            record["skipped"] = "the pinned range changes nothing"
            return record
        repository, pull_request, checkout, material, _ = prepared
        engine = cli.create_review_engine(settings)
        verification = cli._create_verification_pass(settings, engine)
        fallback = cli._create_fallback_reviewer(settings)
        usage = cli._ModelUsage()

        def measured(inner, role):
            return cli._MeasuredEngine(inner, usage, role, metrics, repository)

        # With no separate verifier a level verifies with its reviewer, which is the
        # primary engine unless the primary failed; the pairing needs it named.
        if verification is None:
            verification = cli.VerificationPass(engine=engine, settings=settings)
        paired = _PairedVerifier(
            measured(verification.engine, "verify"),
            level,
            baseline[0] if baseline else None,
        )
        run_level = cli._design_level if level == DESIGN_LEVEL else cli._docs_level
        try:
            assessment = run_level(
                settings,
                measured(engine, "review"),
                replace(verification, engine=paired),
                checkout,
                pull_request,
                material,
                fallback=(
                    replace(fallback, engine=measured(fallback.engine, "fallback"))
                    if fallback is not None
                    else None
                ),
            )
        except (ReviewError, CommandError, EngineError) as error:
            print(f"      -> {noun.capitalize()} level failed: {error}")
            record["failed"] = str(error)
    if paired.baseline_error:
        record["baseline_error"] = paired.baseline_error
        print(
            "      -> The baseline verifier failed, so nothing is compared: "
            f"{paired.baseline_error}"
        )
    label = baseline[1][:12] if baseline else ""
    for candidate in paired.candidates:
        now = (
            _verdict(candidate, assessment.findings, assessment.rejected)
            if assessment is not None
            else None
        ) or {"verdict": "unjudged", "reason": ""}
        then = (
            _verdict(candidate, *paired.baseline_judged)
            if paired.baseline_judged is not None
            else None
        )
        record["proposals"].append(
            {"proposal": asdict(candidate), "now": now, "baseline": then}
        )
        print(
            f"      -> [{candidate.severity.upper()}] {candidate.category}: "
            f"{candidate.title}"
        )
        print(f"         now: {_describe_verdict(now)}")
        if then is not None:
            print(f"         at {label}: {_describe_verdict(then)}")
    if not paired.candidates and "failed" not in record:
        print(f"      -> Proposed no {noun} candidates")
    spend = usage.describe()
    if spend:
        record["spend"] = spend
        print(f"      -> Spent {spend}")
    return record


def _advisory_summary(records: "Sequence[dict[str, Any]]") -> "dict[str, int]":
    proposals = [item for record in records for item in record["proposals"]]
    compared = [item for item in proposals if item["baseline"] is not None]

    def confirmed(item: "dict[str, Any]", side: str) -> bool:
        return item[side] is not None and item[side]["verdict"] == "confirmed"

    return {
        "cases": len(records),
        "failed": sum(1 for record in records if "failed" in record),
        "proposed": len(proposals),
        "confirmed_now": sum(1 for item in proposals if confirmed(item, "now")),
        "compared": len(compared),
        "confirmed_at_baseline": sum(
            1 for item in compared if confirmed(item, "baseline")
        ),
        "only_now": sum(
            1
            for item in compared
            if confirmed(item, "now") and not confirmed(item, "baseline")
        ),
        "only_at_baseline": sum(
            1
            for item in compared
            if confirmed(item, "baseline") and not confirmed(item, "now")
        ),
    }


def advisory(
    settings: Settings,
    level: str,
    cases: "Sequence[ReplayCase]",
    arm: ReplayArm,
    baseline_ref: Optional[str] = None,
    metrics_path: Optional[str] = None,
) -> int:
    """Replay one advisory level on each case in turn and report what it decided.

    One case at a time, because a baseline verifier is a swapped module function.
    Returns 1 when any case's level failed, so an error never reads as a clean run.
    """

    noun = ADVISORY_LEVEL_NOUNS[level]
    metrics = ReviewMetrics()
    records: "list[dict[str, Any]]" = []
    context = baseline_engine(baseline_ref) if baseline_ref else nullcontext(None)
    with context as baseline:
        compared = f", judged now and at {baseline[1][:12]}" if baseline else ""
        print(
            f"Replaying the {noun} level alone on {len(cases)} "
            f"case{'s' if len(cases) != 1 else ''}{compared}"
        )
        for case in cases:
            records.append(advisory_case(settings, case, level, metrics, baseline))
        commit = baseline[1] if baseline else None
    summary = _advisory_summary(records)
    plural = "s" if summary["proposed"] != 1 else ""
    print(
        f"Proposed {summary['proposed']} {noun} candidate{plural} across "
        f"{summary['cases']} case{'s' if summary['cases'] != 1 else ''}; confirmed "
        f"{summary['confirmed_now']} now"
    )
    if summary["compared"]:
        print(
            f"Judged {summary['compared']} at {commit[:12]} too: confirmed "
            f"{summary['confirmed_at_baseline']} there, {summary['only_now']} only "
            f"now and {summary['only_at_baseline']} only there"
        )
    if summary["failed"]:
        print(f"The {noun} level failed on {summary['failed']} case(s)")
    if metrics_path:
        review_engine, review_model = measured_engine(settings, verifying=False)
        verify_engine, verify_model = measured_engine(settings, verifying=True)
        document = {
            "command": "advisory",
            "level": level,
            "reviewer": reviewer_provenance(),
            "arm": arm.describe(),
            "engines": {
                "review": {"engine": review_engine, "model": review_model},
                "verify": {"engine": verify_engine, "model": verify_model},
            },
            "baseline": (
                {"ref": baseline_ref, "commit": commit} if baseline_ref else None
            ),
            "summary": summary,
            "cases": records,
        }
        destination = Path(metrics_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"Measurement written to {metrics_path}")
    return 1 if summary["failed"] else 0


def _run_advisory(args: argparse.Namespace, settings: Settings) -> int:
    if not args.case and not args.pr:
        raise ConfigError(
            "Name what to replay: --pr owner/repository#123[@head], or --case from "
            f"{args.cases}"
        )
    cases: "list[ReplayCase]" = []
    if args.case:
        selected = set(args.case)
        cases.extend(
            case
            for case in load_cases(args.cases)
            if case.label in selected or case.pinned in selected
        )
        if not cases:
            raise ConfigError("No pinned case matched --case")
    cases.extend(case_for_pull_request(settings, spec) for spec in args.pr)
    arm = load_arm(args.arms, args.arm)
    return advisory(
        arm.applied_to(settings),
        args.level,
        cases,
        arm,
        args.baseline_verifier,
        args.metrics,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m prwarden.evaluate",
        description="Replay recorded pull requests to measure a change to the reviewer",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--repositories-file", type=Path)
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES_FILE,
        help=f"pinned case set, default {DEFAULT_CASES_FILE}",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=DEFAULT_CANDIDATES_FILE,
        help=f"recorded review candidates, default {DEFAULT_CANDIDATES_FILE}",
    )
    parser.add_argument(
        "--labels",
        type=Path,
        default=DEFAULT_LABELS_FILE,
        help=f"mined labels, default {DEFAULT_LABELS_FILE}",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify_parser = subcommands.add_parser(
        "verify",
        help="re-judge recorded candidates, measuring the verifier on its own",
    )
    for target in ("--arm",):
        verify_parser.add_argument(
            target,
            default=BASELINE_ARM.name,
            help="named settings from the arms file",
        )
    verify_parser.add_argument(
        "--arms",
        type=Path,
        default=DEFAULT_ARMS_FILE,
        help=f"experiment arms, default {DEFAULT_ARMS_FILE}",
    )
    verify_parser.add_argument(
        "--metrics",
        metavar="PATH",
        help="write the measurement to PATH instead of printing it summary",
    )
    verify_parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="verify only this owner/repository#number, repeatable",
    )
    verify_parser.add_argument(
        "--passes",
        type=int,
        default=1,
        help="re-judge the same candidates this many times",
    )
    label_parser = subcommands.add_parser(
        "label",
        help="derive cases and labels from what authors did after our comments",
    )
    label_parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="limit mining to this owner/repository, repeatable",
    )
    capture_parser = subcommands.add_parser(
        "capture", help="pin the currently open pull requests as replayable cases"
    )
    capture_parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="limit the capture to this owner/repository, repeatable",
    )
    replay_parser = subcommands.add_parser(
        "replay", help="review every pinned case and measure what it decided"
    )
    replay_parser.add_argument(
        "--arm",
        default=BASELINE_ARM.name,
        help=f"named arm from {DEFAULT_ARMS_FILE}, default baseline",
    )
    replay_parser.add_argument(
        "--arms",
        type=Path,
        default=DEFAULT_ARMS_FILE,
        help=f"arm definitions, default {DEFAULT_ARMS_FILE}",
    )
    replay_parser.add_argument(
        "--metrics",
        metavar="PATH",
        help="write the measurement to PATH instead of printing its summary",
    )
    replay_parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="replay only this owner/repository#number, repeatable",
    )
    # Scoring a case that carries no finding authors acted on spends an invocation to
    # learn nothing, and over a third of the corpus is such cases.
    replay_parser.add_argument(
        "--passes",
        type=int,
        default=1,
        help="replay the cases this many times to measure the reviewer's spread",
    )
    replay_parser.add_argument(
        "--labelled-only",
        action="store_true",
        help="replay only cases carrying a finding authors acted on",
    )
    replay_parser.add_argument(
        "--controls",
        action="store_true",
        help=(
            "replay only control cases, which were approved and merged unchanged; "
            "with --labelled-only, replay both"
        ),
    )
    advisory_parser = subcommands.add_parser(
        "advisory",
        help=(
            "run the design or documentation level alone and judge its proposals, "
            "optionally beside the verifier of another commit"
        ),
    )
    advisory_parser.add_argument(
        "--level",
        required=True,
        choices=sorted(ADVISORY_LEVEL_NOUNS),
        help="the advisory level to run",
    )
    advisory_parser.add_argument(
        "--pr",
        action="append",
        default=[],
        metavar="OWNER/REPO#N[@HEAD]",
        help="pin this pull request from GitHub, at HEAD if named, repeatable",
    )
    advisory_parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="replay this pinned owner/repository#number, repeatable",
    )
    advisory_parser.add_argument(
        "--baseline-verifier",
        metavar="REF",
        help="also judge every proposal with the verifier prompt committed at REF",
    )
    advisory_parser.add_argument(
        "--arm",
        default=BASELINE_ARM.name,
        help=f"named arm from {DEFAULT_ARMS_FILE}, default baseline",
    )
    advisory_parser.add_argument(
        "--arms",
        type=Path,
        default=DEFAULT_ARMS_FILE,
        help=f"arm definitions, default {DEFAULT_ARMS_FILE}",
    )
    advisory_parser.add_argument(
        "--metrics",
        metavar="PATH",
        help="write every proposal and both verdicts to PATH as JSON",
    )
    for subcommand in (replay_parser, verify_parser):
        subcommand.add_argument(
            "--concurrency",
            type=int,
            default=1,
            help=(
                "cases to replay at the same time, 1 to "
                f"{MAX_REVIEW_CONCURRENCY}, default 1"
            ),
        )
    return parser


def transcript_name(command: str) -> str:
    """Name one harness transcript.

    Named apart from a review's ``run-`` transcripts because these two things are
    read for different reasons: a review transcript is history of what was delivered,
    an evaluation transcript is the working record of a measurement. Both are pruned
    by the same retention.
    """

    started = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"eval-{command}-{started}-{os.getpid()}.log"


def _run_command(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "capture":
        capture(settings, args.cases, args.repo)
        return 0
    if args.command == "label":
        mine_labels(settings, args.cases, args.labels, args.repo)
        return 0
    if args.command == "advisory":
        return _run_advisory(args, settings)
    cases = load_cases(args.cases)
    if args.command == "verify":
        recorded = load_candidates(args.candidates)
        if not recorded:
            raise ConfigError(
                f"No recorded candidates in {args.candidates}; run a replay first, "
                "which records what its review proposed"
            )
    else:
        recorded = None
    if args.case:
        selected = set(args.case)
        cases = tuple(
            case
            for case in cases
            if case.label in selected
            or case.pinned in selected
        )
        if not cases:
            raise ConfigError("No pinned case matched --case")
    arm = load_arm(args.arms, args.arm)
    settings = arm.applied_to(settings)
    labels = load_labels(args.labels)
    labelled_only = getattr(args, "labelled_only", False)
    controls_only = getattr(args, "controls", False)
    if labelled_only or controls_only:
        cases = tuple(
            case
            for case in cases
            if (
                labelled_only
                and any(
                    item.get("outcome") == ACTED_ON
                    for item in expected_for(labels, case)
                )
            )
            or (controls_only and control_for(labels, case) is not None)
        )
        if not cases:
            raise ConfigError(
                "No pinned case carries a finding authors acted on or is a control; "
                "mine labels first or drop --labelled-only and --controls"
            )
        print(f"Replaying the {len(cases)} cases that carry a scoreable label")
    passes = getattr(args, "passes", 1)
    if passes < 1 or passes > MAX_REPLAY_PASSES:
        raise ConfigError(f"--passes must be between 1 and {MAX_REPLAY_PASSES}")
    concurrency = getattr(args, "concurrency", 1)
    if not 1 <= concurrency <= MAX_REVIEW_CONCURRENCY:
        raise ConfigError(
            f"--concurrency must be between 1 and {MAX_REVIEW_CONCURRENCY}"
        )
    if recorded is not None:
        # Judge only what there is something to judge. Sweeping the whole corpus
        # printed a skip line for every case without a recording — thirty-four of
        # forty-seven — which buried the thirteen that were the point.
        judgeable = tuple(
            case for case in cases if candidates_for(recorded, case) is not None
        )
        if not judgeable:
            raise ConfigError(
                "No recorded candidates match the selected cases; run a replay "
                "over them first, which records what its review proposed"
            )
        left_out = len(cases) - len(judgeable)
        tail = f", leaving out {left_out} with nothing recorded" if left_out else ""
        print(
            f"Re-judging the {len(judgeable)} case(s) that carry recorded "
            f"candidates{tail}; no review runs"
        )
        cases = judgeable
        return replay(
            settings,
            cases,
            arm,
            args.metrics,
            labels,
            passes,
            recorded=recorded,
            concurrency=concurrency,
        )
    return replay(
        settings,
        cases,
        arm,
        args.metrics,
        labels,
        passes,
        candidates_path=args.candidates,
        concurrency=concurrency,
    )


def main(argv: "Optional[Sequence[str]]" = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        arm = (
            load_arm(args.arms, args.arm)
            if args.command in ("replay", "advisory")
            else BASELINE_ARM
        )
        settings = load_settings(
            args.env_file,
            args.repositories_file,
            review_engine_override=arm.engine,
        )
        pin_github_account(settings.reviewer_username, settings.repositories)
    except (ConfigError, CommandError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2 if isinstance(error, ConfigError) else 1
    log_directory = settings.state_file.parent / RUN_LOG_DIRECTORY_NAME
    # A measurement is worth nothing if its evidence scrolled past. The verifier's
    # reason for throwing out a candidate is the diagnosis, and reading it should not
    # cost another hour of model time.
    with progress.transcript(log_directory / transcript_name(args.command)):
        try:
            progress.prune_transcripts(
                log_directory, settings.run_log_retention_days
            )
        except (OSError, ValueError) as error:
            print(f"Note: old transcripts were kept: {error}")
        try:
            return _run_command(args, settings)
        except (ConfigError, CommandError) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2 if isinstance(error, ConfigError) else 1
        except KeyboardInterrupt:
            print("\nInterrupted.")
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
