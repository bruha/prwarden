"""Structured measurement of what a run's reviews decided.

Every prompt this project has changed was justified by reasoning and confirmed only
by watching production afterwards. Alibaba's reviewer can answer "did that change
help" with a benchmark; this module is the first half of the same ability for us: the
numbers a run produces, written where a later run can be compared against them.

The observation point is deliberate. Every level's decision is derived from one model
response, and the engine wrapper the controller already puts around each invocation
sees the material going in and the payload coming back, so recording there needs no
level to remember to report anything and cannot miss a level added later. What it
cannot see, and so is out of scope here, is the controller's own arithmetic
afterwards: withheld approvals, delivery, and skips.

Nothing here may change a review. Every read is defensive and every failure is
silent, because a measurement that can fail a review would be worse than no
measurement at all.
"""

import json
import threading
from typing import Any, Mapping, Optional

from .review import UNCLASSIFIED_REJECTION


SCHEMA_VERSION = 1


def _finding_count(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    findings = payload.get("findings")
    return len(findings) if isinstance(findings, list) else None


def _verdict(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    verdict = payload.get("verdict")
    return verdict if isinstance(verdict, str) else None


def _judgments(payload: Any) -> "tuple[int, int, dict[str, int]]":
    """Return confirmed, rejected, and the rejection classes of one pass."""

    confirmed = 0
    rejected = 0
    classes: "dict[str, int]" = {}
    if not isinstance(payload, dict):
        return confirmed, rejected, classes
    raw_judgments = payload.get("judgments")
    if not isinstance(raw_judgments, list):
        return confirmed, rejected, classes
    for judgment in raw_judgments:
        if not isinstance(judgment, dict):
            continue
        if judgment.get("verdict") == "confirmed":
            confirmed += 1
        elif judgment.get("verdict") == "rejected":
            rejected += 1
            raw_class = judgment.get("rejection_class")
            name = (
                raw_class.strip().lower()
                if isinstance(raw_class, str) and raw_class.strip()
                else UNCLASSIFIED_REJECTION
            )
            classes[name] = classes.get(name, 0) + 1
    return confirmed, rejected, classes


class ReviewMetrics:
    """Collects one record per model invocation, keyed by pull request.

    Thread-safe: reviews run concurrently, and two of them recording at once must
    not lose either observation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._invocations: "list[dict[str, Any]]" = []

    def record(
        self,
        *,
        slug: str,
        number: int,
        head_sha: str,
        label: str,
        seconds: float,
        level: str,
        corrected: bool,
        payload: Any = None,
        error: Optional[str] = None,
        credits: Optional[float] = None,
    ) -> None:
        """Record one invocation. Never raises: measurement must not gate a review."""

        try:
            entry: "dict[str, Any]" = {
                "pull_request": f"{slug}#{number}",
                "head": head_sha[:12],
                "label": label,
                "level": level,
                "seconds": round(seconds, 3),
                # A retry the controller asked for after rejecting the last answer.
                # A rising count means the prompt and the schema disagree somewhere.
                "corrected": corrected,
            }
            if credits is not None:
                entry["credits"] = round(credits, 4)
            if error is not None:
                entry["error"] = error[:300]
            else:
                verdict = _verdict(payload)
                if verdict is not None:
                    entry["verdict"] = verdict
                candidates = _finding_count(payload)
                if candidates is not None:
                    entry["findings"] = candidates
                confirmed, rejected, classes = _judgments(payload)
                if confirmed or rejected:
                    entry["confirmed"] = confirmed
                    entry["rejected"] = rejected
                    if classes:
                        entry["rejection_classes"] = classes
            with self._lock:
                self._invocations.append(entry)
        except Exception:  # pragma: no cover - measurement must never propagate
            return

    @property
    def invocations(self) -> "list[dict[str, Any]]":
        with self._lock:
            return [dict(entry) for entry in self._invocations]

    def summary(self) -> "dict[str, Any]":
        """Return the aggregate numbers two runs are compared on."""

        invocations = self.invocations
        pull_requests = sorted({entry["pull_request"] for entry in invocations})
        by_label: "dict[str, int]" = {}
        seconds_by_label: "dict[str, float]" = {}
        classes: "dict[str, int]" = {}
        verdicts: "dict[str, int]" = {}
        corrected = 0
        errors = 0
        candidates = 0
        confirmed = 0
        credits_by_label: "dict[str, float]" = {}
        for entry in invocations:
            label = entry["label"]
            by_label[label] = by_label.get(label, 0) + 1
            seconds_by_label[label] = round(
                seconds_by_label.get(label, 0.0) + entry["seconds"], 3
            )
            if isinstance(entry.get("credits"), (int, float)):
                credits_by_label[label] = round(
                    credits_by_label.get(label, 0.0) + entry["credits"], 4
                )
            if entry.get("corrected"):
                corrected += 1
            if entry.get("error"):
                errors += 1
            verdict = entry.get("verdict")
            if isinstance(verdict, str):
                verdicts[verdict] = verdicts.get(verdict, 0) + 1
            if "confirmed" in entry:
                confirmed += entry["confirmed"]
                candidates += entry["confirmed"] + entry["rejected"]
            for name, count in (entry.get("rejection_classes") or {}).items():
                classes[name] = classes.get(name, 0) + count
        return {
            "pull_requests": len(pull_requests),
            "invocations": len(invocations),
            "invocations_by_label": by_label,
            "seconds_by_label": seconds_by_label,
            "seconds": round(sum(seconds_by_label.values()), 3),
            # None when no engine reported a cost, which is not the same as free.
            "credits": (
                round(sum(credits_by_label.values()), 2) if credits_by_label else None
            ),
            "credits_by_label": credits_by_label,
            "corrected_invocations": corrected,
            "failed_invocations": errors,
            "candidates": candidates,
            "confirmed": confirmed,
            "confirmation_rate": (
                round(confirmed / candidates, 3) if candidates else None
            ),
            "rejection_classes": classes,
            "verdicts": verdicts,
        }

    def document(self, *, engine: str, model: Optional[str]) -> "dict[str, Any]":
        """Return the whole measurement, ready to write."""

        return {
            "schema": SCHEMA_VERSION,
            "engine": engine,
            "model": model,
            "summary": self.summary(),
            "invocations": self.invocations,
        }


def _flat(summary: Mapping[str, Any], prefix: str = "") -> "dict[str, Any]":
    flattened: "dict[str, Any]" = {}
    for key, value in summary.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flattened.update(_flat(value, f"{name}."))
        elif isinstance(value, (int, float)) or value is None:
            flattened[name] = value
    return flattened


# What a replay measured against production, beside what its invocations decided.
# Left out, a comparison reported every engine number and none of the ones a change to
# the reviewer is judged on: how much it found again, and what else it confirmed.
COMPARED_SECTIONS = ("labels", "confirmed", "controls")


def _compared_numbers(document: Mapping[str, Any]) -> "dict[str, Any]":
    numbers = _flat(document.get("summary", {}))
    for section in COMPARED_SECTIONS:
        value = document.get(section)
        if isinstance(value, Mapping):
            numbers.update(_flat(value, f"{section}."))
    return numbers


def compare(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """Return a per-number report of what changed between two measurements.

    Sorted with the numbers that moved first, because a comparison exists to answer
    what changed and a wall of unchanged rows buries it.
    """

    left = _compared_numbers(before)
    right = _compared_numbers(after)
    names = sorted(set(left) | set(right))
    moved: "list[str]" = []
    same: "list[str]" = []
    for name in names:
        old = left.get(name)
        new = right.get(name)
        if old == new:
            same.append(f"  = {name}: {_render(old)}")
            continue
        moved.append(f"  {_arrow(old, new)} {name}: {_render(old)} -> {_render(new)}")
    lines = [
        f"before: {_side(before)}",
        f"after:  {_side(after)}",
    ]
    lines.extend(_provenance_warnings(before, after))
    lines.extend(
        ["", f"{len(moved)} number{'s' if len(moved) != 1 else ''} moved:"]
    )
    lines.extend(moved or ["  (none)"])
    if same:
        lines.extend(["", f"{len(same)} unchanged:", *same])
    return "\n".join(lines)


def _side(document: Mapping[str, Any]) -> str:
    """Return one side's identity: engine, model, arm, and reviewer build."""

    parts = [str(document.get("engine")), str(document.get("model") or "")]
    arm = document.get("arm")
    if isinstance(arm, Mapping) and arm.get("name"):
        parts.append(f"arm={arm['name']}")
    reviewer = document.get("reviewer")
    if isinstance(reviewer, Mapping):
        build = str(reviewer.get("commit") or reviewer.get("version") or "")
        if build:
            parts.append(build + ("+dirty" if reviewer.get("dirty") else ""))
    return " ".join(part for part in parts if part).strip()


def _provenance_warnings(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> "list[str]":
    """Return what makes this comparison unsafe to read, loudly.

    A comparison exists to attribute a difference to a change. Two runs over
    different work attribute nothing, and two runs that changed several things at once
    cannot say which one moved the numbers. Both are reported rather than prevented:
    the operator may have a reason, but they should not learn of it by accident.
    """

    warnings: "list[str]" = []
    left_cases = before.get("cases")
    right_cases = after.get("cases")
    if isinstance(left_cases, Mapping) and isinstance(right_cases, Mapping):
        if left_cases.get("fingerprint") != right_cases.get("fingerprint"):
            warnings.append(
                "  !! DIFFERENT CASE SETS: these runs reviewed different work, so "
                "no difference below is attributable"
            )
        else:
            # The same corpus is not the same coverage. A provider that cut a
            # response mid-stream cost four of seventeen cases on 2026-09-23, and
            # because only totals were compared the smaller sample read as a drop.
            left_scored = set(left_cases.get("scored") or ())
            right_scored = set(right_cases.get("scored") or ())
            if (left_scored or right_scored) and left_scored != right_scored:
                shared = left_scored & right_scored
                warnings.append(
                    "  !! DIFFERENT COVERAGE: "
                    f"{len(left_scored)} case(s) measured before, "
                    f"{len(right_scored)} after, {len(shared)} in both. The totals "
                    "below count different work; compare only the shared cases"
                )
                for side, missing in (
                    ("before", right_scored - left_scored),
                    ("after", left_scored - right_scored),
                ):
                    if missing:
                        warnings.append(
                            f"     not measured {side}: "
                            + ", ".join(sorted(missing))
                        )
    elif isinstance(left_cases, Mapping) != isinstance(right_cases, Mapping):
        warnings.append(
            "  !! one side is a replay and the other is a live run, so the inputs "
            "are not the same work"
        )
    changed = [
        f"{name} {_render(before.get('settings', {}).get(name))} -> "
        f"{_render(after.get('settings', {}).get(name))}"
        for name in sorted(
            set(before.get("settings") or {}) | set(after.get("settings") or {})
        )
        if (before.get("settings") or {}).get(name)
        != (after.get("settings") or {}).get(name)
    ]
    if before.get("engine") != after.get("engine"):
        changed.append(f"engine {before.get('engine')} -> {after.get('engine')}")
    if before.get("model") != after.get("model"):
        changed.append(f"model {before.get('model')} -> {after.get('model')}")
    before_reviewer = before.get("reviewer") or {}
    after_reviewer = after.get("reviewer") or {}
    if isinstance(before_reviewer, Mapping) and isinstance(after_reviewer, Mapping):
        if before_reviewer.get("commit") != after_reviewer.get("commit"):
            changed.append(
                f"reviewer {before_reviewer.get('commit')} -> "
                f"{after_reviewer.get('commit')}"
            )
        if before_reviewer.get("dirty") or after_reviewer.get("dirty"):
            warnings.append(
                "  !! one side ran from an uncommitted tree, so it cannot be "
                "reproduced"
            )
    if len(changed) > 1:
        warnings.append(
            f"  !! {len(changed)} things differ, so a movement cannot be attributed "
            f"to one of them: {'; '.join(changed)}"
        )
    return warnings


def _arrow(old: Any, new: Any) -> str:
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        return "+" if new > old else "-"
    return "*"


def _render(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def load(path: str) -> "dict[str, Any]":
    """Return a measurement written by an earlier run."""

    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} does not hold a measurement")
    return payload
