import fnmatch
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, NamedTuple, Optional, Sequence

from .engine import (
    DESIGN_CATEGORIES,
    DESIGN_FIELD_LIMITS,
    DOCS_CATEGORIES,
    DOCS_FIELD_LIMITS,
    REJECTION_CLASSES,
    RESPONSE_FIELD_LIMITS,
    code_candidate_refs,
    design_candidate_refs,
    docs_candidate_refs,
    prior_comment_refs,
)
from .models import (
    MISSING_LIST_ENTRY,
    DeferredFile,
    DesignFinding,
    DiffBudget,
    DocsFinding,
    DocumentationFile,
    Finding,
    FindingEvidence,
    MovedFile,
    PriorReviewComment,
    PullRequestCheck,
    RejectedDesignCandidate,
    RejectedDocsCandidate,
    RemovedFile,
    ReviewMaterial,
    ReviewRules,
    ReviewRuleSection,
)


HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
ALLOWED_SEVERITIES = {"critical", "high", "medium", "low"}
# A check whose conclusion says this head did not pass it. `cancelled`, `skipped`,
# and `neutral` are not here: a run cancelled because no runner came, or skipped by
# its own conditions, says nothing about the code.
FAILED_CHECK_CONCLUSIONS = frozenset(
    ("failure", "timed_out", "startup_failure", "action_required")
)
BUILD_CHECK_NAME = re.compile(r"build|compile|typecheck|type[-\s]check|tsc\b", re.I)
BUILD_FAILURE_CLAIM = re.compile(
    r"\bTS\d{4}\b"
    r"|\btsc\b"
    r"|(?:does not|cannot|will not|won't|fails? to|failing to) compile"
    r"|compilation (?:error|failure|fails|will fail|breaks)"
    r"|(?:build|type[-\s]?check|typecheck) (?:error|errors|failure|fails|will fail"
    r"|breaks|is broken)"
    r"|breaks? the build"
    r"|build (?:pipeline|step)? ?(?:will )?fail",
    re.I,
)
SUCCESSFUL_CHECK_CONCLUSION = "success"
COMPLETED_CHECK_STATUS = "completed"


class ReviewError(RuntimeError):
    """Raised when review input or model output cannot be used safely."""


class DiffTooLargeError(ReviewError):
    """Raised when a complete PR diff cannot fit within the configured limit."""


def _added_lines_from_patch(patch: str) -> frozenset[int]:
    added: set[int] = set()
    new_line: Optional[int] = None

    for line in patch.splitlines():
        header = HUNK_HEADER.match(line)
        if header:
            new_line = int(header.group(1))
            continue
        if new_line is None or line.startswith("\\"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1
    return frozenset(added)


def _optional_count(value: Any) -> Optional[int]:
    """Return a non-negative file-statistic count, or None when unusable."""

    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _content_free_rename(path: str, file_data: dict[str, Any]) -> Optional[MovedFile]:
    """Return move metadata when GitHub reports a rename with no content change.

    GitHub omits the patch for a pure rename because there is no textual diff to
    send, so such a file must not be mistaken for a withheld binary or oversized
    patch. Absent or unusable counts are treated as a real change, keeping the
    caller's fail-closed behavior for anything that is not provably content-free.
    """

    if file_data.get("status") != "renamed":
        return None
    if _optional_count(file_data.get("additions")) != 0:
        return None
    if _optional_count(file_data.get("deletions")) != 0:
        return None
    previous = file_data.get("previous_filename")
    return MovedFile(
        path=path,
        previous_path=previous if isinstance(previous, str) and previous else None,
    )


def _has_no_changed_lines(file_data: dict[str, Any]) -> bool:
    """Report whether GitHub counted no changed lines in a patchless file.

    A binary file has no text to diff, so GitHub sends no patch and counts no
    added, deleted, or changed line. An oversized text patch is withheld too, but
    its lines are still counted, so requiring all three counts to be zero
    separates a file with nothing to review from a genuinely incomplete diff.
    Absent or unusable counts are treated as a real change, keeping the caller's
    fail-closed behavior for anything that is not provably textless.
    """

    return all(
        _optional_count(file_data.get(field)) == 0
        for field in ("additions", "deletions", "changes")
    )


def format_byte_size(size_bytes: int) -> str:
    """Return a byte count a human reads at a glance."""

    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


OMITTED_PATCH_REASON = "GitHub omitted its patch"


def dominant_directories(
    deferred: "tuple[DeferredFile, ...]",
    limit: int = 3,
) -> "tuple[tuple[str, int], ...]":
    """Return the directories holding the most deferred bytes, largest first.

    A list of sixty-five paths tells an operator nothing; three directories tell
    them which part of the change nobody read, and usually where to split it.
    """

    totals: "dict[str, int]" = {}
    for file in deferred:
        directory = str(PurePosixPath(file.path).parent)
        if directory == ".":
            # A bare "." tells a reader nothing about where to look.
            directory = "(repository root)"
        totals[directory] = totals.get(directory, 0) + file.size_bytes
    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return tuple(ranked[:limit])


def _fit_diff_to_budget(
    order: "list[str]",
    sizes: "dict[str, int]",
    max_diff_bytes: int,
) -> "tuple[list[str], tuple[DeferredFile, ...]]":
    """Return the paths that fit the budget and the files left for a human.

    Filled largest change first: the most changed lines carry the most risk, and
    the tail of a large pull request is usually one-line edits. A file that cannot
    fit the remaining budget is skipped rather than ending the fill, so one huge
    file does not defer everything smaller than itself.
    """

    remaining = max_diff_bytes
    kept = set()
    for path in sorted(order, key=lambda path: (-sizes[path], path)):
        if sizes[path] <= remaining:
            kept.add(path)
            remaining -= sizes[path]
    included = [path for path in order if path in kept]
    deferred = tuple(
        DeferredFile(path=path, size_bytes=sizes[path])
        for path in order
        if path not in kept
    )
    return included, deferred


def ignores_omitted_patch(
    path: str, patterns: "Sequence[str]", mark: Optional[bool]
) -> bool:
    """Return whether a file whose patch GitHub omitted needs no review.

    The base branch's own mark decides first: generated needs none, and not
    generated keeps the file unread whatever the patterns say. Without a mark the
    patterns decide. The skip gate asks the same question of a recorded file, so
    both places read one answer.
    """

    if mark is not None:
        return mark
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def failed_checks(checks: "Sequence[PullRequestCheck]") -> "tuple[str, ...]":
    """Return the name of each check that failed at this head, sorted, once each.

    A check still running has no conclusion yet, so only a finished one can fail.
    """

    return tuple(
        sorted(
            {
                check.name
                for check in checks
                if (check.conclusion or "").lower() in FAILED_CHECK_CONCLUSIONS
            }
        )
    )


def omitted_patch_paths(files: "list[dict[str, Any]]") -> "tuple[str, ...]":
    """Return the changed files GitHub has text for but will not show.

    These are the files `build_review_material` can only read around: not a
    removal, a rename with no content change, or a file with no changed line, each
    of which needs no patch. Asking anything about generated files is only worth it
    for these, and most pull requests have none.
    """

    paths = []
    for file_data in files:
        path = file_data.get("filename")
        if not isinstance(path, str) or not path:
            continue
        if isinstance(file_data.get("patch"), str):
            continue
        if file_data.get("status") == "removed" and file_data.get("additions") == 0:
            continue
        if _content_free_rename(path, file_data) is not None:
            continue
        if _has_no_changed_lines(file_data):
            continue
        paths.append(path)
    return tuple(paths)


def build_review_material(
    files: list[dict[str, Any]],
    max_diff_bytes: int,
    prior_comments: tuple[PriorReviewComment, ...] = (),
    ignored_missing_patch_patterns: tuple[str, ...] = (),
    generated_marks: "Optional[Mapping[str, Optional[bool]]]" = None,
    marks_unreadable: bool = False,
) -> ReviewMaterial:
    """Return the reviewable material for one pull request.

    The whole change is measured before anything is judged too large, so the size
    and the paths that dominate it are facts the run can report. When the complete
    diff does not fit ``max_diff_bytes``, the review reads what fits and names the
    rest instead of reviewing nothing: a pull request nobody reviewed catches no
    defects at all.

    ``generated_marks`` is how the base branch's `.gitattributes` mark each omitted
    file: True says it is generated, so it needs no review like a file the patterns
    name, and False says it is not, which keeps it unread even when a pattern names
    it. A repository can widen review by unmarking a file, never narrow it.
    ``marks_unreadable`` says the marks were needed and could not be read, so any
    of them might be an unmark: every omitted file then stays unread, and failing
    to read the marks can only withhold an approval, never grant one.
    """

    marks = generated_marks or {}
    sections: dict[str, str] = {}
    order: list[str] = []
    added_lines: dict[str, frozenset[int]] = {}
    removed_files: list[RemovedFile] = []
    moved_files: list[MovedFile] = []
    ignored_missing_patch_files: list[str] = []
    unreadable_files: list[str] = []
    textless_files: list[str] = []
    patches: dict[str, str] = {}

    for file_data in files:
        path = file_data.get("filename")
        if not isinstance(path, str) or not path:
            raise ReviewError("GitHub returned a changed file without a filename")

        if file_data.get("status") == "removed" and file_data.get("additions") == 0:
            removed_files.append(
                RemovedFile(
                    path=path,
                    deletions=_optional_count(file_data.get("deletions")),
                )
            )
            added_lines[path] = frozenset()
            continue

        patch = file_data.get("patch")
        if not isinstance(patch, str):
            moved_file = _content_free_rename(path, file_data)
            if moved_file is not None:
                moved_files.append(moved_file)
                added_lines[path] = frozenset()
                continue
            if _has_no_changed_lines(file_data):
                # GitHub reports the file changed but counts no changed line, so
                # there is no text any reviewer could read. Naming it beats
                # requiring a glob for every binary extension in existence,
                # where a missing one costs the whole pull request's review.
                textless_files.append(path)
                added_lines[path] = frozenset()
                continue
            if not marks_unreadable and ignores_omitted_patch(
                path, ignored_missing_patch_patterns, marks.get(path)
            ):
                ignored_missing_patch_files.append(path)
                continue
            # GitHub reports changed lines here but will not inline them, so this
            # file holds text nobody local can read. That is the same position as
            # a file the budget could not fit, and it takes the same route: the
            # rest of the change is reviewed, this path is named, and the approval
            # is withheld. Failing the pull request instead reviewed nothing and
            # needed a glob for every generated file anybody might commit.
            unreadable_files.append(path)
            added_lines[path] = frozenset()
            continue

        section = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{patch}\n"
        sections[path] = section
        order.append(path)
        patches[path] = patch

    sizes = {path: len(section.encode("utf-8")) for path, section in sections.items()}
    total_bytes = sum(sizes.values())
    deferred: "tuple[DeferredFile, ...]" = ()
    included = order
    if total_bytes > max_diff_bytes:
        included, deferred = _fit_diff_to_budget(order, sizes, max_diff_bytes)
        if not included:
            smallest = min(sizes.values())
            raise DiffTooLargeError(
                f"No changed file fits MAX_DIFF_BYTES ({max_diff_bytes}); the "
                f"complete diff is {total_bytes} bytes and its smallest changed "
                f"file alone is {smallest} bytes"
            )
    for path in included:
        added_lines[path] = _added_lines_from_patch(patches[path])

    deferred += tuple(
        DeferredFile(path=path, size_bytes=0, reason=OMITTED_PATCH_REASON)
        for path in unreadable_files
    )

    if not included and not removed_files:
        raise ReviewError("Pull request contains no reviewable changed files")
    return ReviewMaterial(
        diff="\n".join(sections[path] for path in included),
        added_lines=added_lines,
        diff_budget=DiffBudget(
            limit_bytes=max_diff_bytes,
            total_bytes=total_bytes,
            reviewed_bytes=sum(sizes[path] for path in included),
            reviewed_files=len(included),
            deferred=deferred,
        ),
        prior_comments=prior_comments,
        removed_files=tuple(removed_files),
        moved_files=tuple(moved_files),
        ignored_missing_patch_files=tuple(ignored_missing_patch_files),
        textless_files=tuple(textless_files),
    )


def build_bundled_review_material(
    files: "list[dict[str, Any]]",
    max_diff_bytes: int,
    max_bundles: int = 1,
    prior_comments: "tuple[PriorReviewComment, ...]" = (),
    ignored_missing_patch_patterns: "tuple[str, ...]" = (),
    generated_marks: "Optional[Mapping[str, Optional[bool]]]" = None,
    marks_unreadable: bool = False,
) -> "tuple[tuple[ReviewMaterial, ...], tuple[DeferredFile, ...]]":
    """Return the material for each bundle, and the files still nobody read.

    A change too large for one review is read in several, each one a complete review
    of a smaller set of whole files. Only a change that does not fit is bundled, so
    the ordinary path is untouched: the first bundle is exactly the material a single
    review would have received, and a pull request that fits produces one bundle.

    Bundling costs an invocation per bundle, so ``max_bundles`` caps it. Whatever the
    cap leaves over stays deferred exactly as before, which means the worst outcome of
    this is today's outcome rather than a new one.

    Files are never split. A finding cites a line and a reviewer that saw half a
    function would cite it from a diff nobody can reproduce, so a file too large for
    the budget on its own is left unread and named, as it is today.
    """

    first = build_review_material(
        files,
        max_diff_bytes,
        prior_comments,
        ignored_missing_patch_patterns,
        generated_marks,
        marks_unreadable,
    )
    bundles = [first]
    remaining = first.diff_budget.oversized
    if max_bundles <= 1 or not remaining:
        return tuple(bundles), remaining

    by_path = {
        file_data.get("filename"): file_data
        for file_data in files
        if isinstance(file_data.get("filename"), str)
    }
    while remaining and len(bundles) < max_bundles:
        subset = [
            by_path[deferred.path] for deferred in remaining if deferred.path in by_path
        ]
        if not subset:
            break
        try:
            bundle = build_review_material(
                subset,
                max_diff_bytes,
                prior_comments,
                ignored_missing_patch_patterns,
                generated_marks,
                marks_unreadable,
            )
        except (DiffTooLargeError, ReviewError):
            # Nothing here fits alone, so another bundle would read nothing. The
            # files stay deferred and named, which is what a single review does.
            break
        bundles.append(bundle)
        remaining = bundle.diff_budget.oversized
    return tuple(bundles), remaining


def _required_string(
    item: dict[str, Any],
    field: str,
    maximum_length: int,
    owner: str = "Finding",
) -> str:
    """Return a bounded non-blank string, naming what carries the field on failure.

    ``owner`` reaches the engine verbatim on a correction retry, so a judgment
    field reported as a finding field sends the retry looking for the wrong value.
    """

    value = item.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ReviewError(f"{owner} field {field!r} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum_length:
        raise ReviewError(
            f"{owner} field {field!r} exceeds {maximum_length} characters"
        )
    return value


def _resolve_finding_path(
    reported_path: str,
    line: int,
    added_lines: dict[str, frozenset[int]],
) -> str:
    normalized_path = reported_path.replace("\\", "/")
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    if normalized_path.startswith("a/"):
        normalized_path = normalized_path[2:]

    if normalized_path in added_lines:
        return normalized_path

    filename = PurePosixPath(normalized_path).name
    candidates = [
        candidate_path
        for candidate_path, candidate_lines in added_lines.items()
        if PurePosixPath(candidate_path).name == filename and line in candidate_lines
    ]
    if len(candidates) == 1:
        return candidates[0]
    return normalized_path


def _passing_build_checks(material: ReviewMaterial) -> tuple[str, ...]:
    """Return exact-head checks that prove the reviewed head builds and typechecks."""

    return tuple(
        check.name
        for check in material.checks
        if isinstance(check.name, str)
        and BUILD_CHECK_NAME.search(check.name)
        and (check.status or "").casefold() == COMPLETED_CHECK_STATUS
        and (check.conclusion or "").casefold() == SUCCESSFUL_CHECK_CONCLUSION
    )


def _contradicting_build_check(
    claim_fields: tuple[str, ...],
    passing_build_checks: tuple[str, ...],
) -> Optional[str]:
    """Return the successful check that disproves a build-breakage claim, if any.

    A claim that the reviewed head cannot compile is a claim about the whole head,
    which a successful exact-head build or typecheck check already answers. The
    controller rejects the response instead of posting it, so the engine must
    either withdraw the claim or report a defect the passing check cannot cover.
    """

    if not passing_build_checks:
        return None
    if not any(BUILD_FAILURE_CLAIM.search(field) for field in claim_fields):
        return None
    return passing_build_checks[0]


def _within_root(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _evidence_roots(material: ReviewMaterial) -> tuple[Path, ...]:
    """Return extra roots that hold verifiable evidence outside the worktree.

    Installed dependency directories are linked into the workspace, so citations
    that resolve into a linked target are inside the reviewed workspace even
    though the resolved path leaves the worktree directory.
    """

    roots: list[Path] = []
    for link in material.dependency_links:
        try:
            roots.append(Path(link.target).resolve())
        except OSError:
            continue
    return tuple(roots)


def _quote_line(text: str) -> str:
    """Render one line of untrusted model or source text for a diagnostic.

    A rejected citation is only actionable when the message shows the submitted
    excerpt next to the real line, because a mismatch is usually a re-quoted
    template literal, a normalized quote character, or collapsed indentation.
    The text is shown whole: an excerpt cut short hid exactly the part that
    differed, which made a report of the mismatch unreadable to the person asked
    to judge it and to the model asked to correct it. Whitespace is collapsed so a
    line keeps to one line, and quoting makes the boundaries unambiguous.
    """

    return repr(" ".join(text.split()))


def _required_evidence(
    item: dict[str, Any],
    field: str,
    repository_root: Optional[Path],
    evidence_roots: tuple[Path, ...] = (),
) -> tuple[FindingEvidence, ...]:
    raw_evidence = item.get(field)
    if not isinstance(raw_evidence, list):
        raise ReviewError(f"Finding field {field!r} must be an array")
    if field == "evidence" and not raw_evidence:
        raise ReviewError("Every finding must contain repository evidence")
    if len(raw_evidence) > 6:
        raise ReviewError(f"Finding field {field!r} exceeds 6 citations")

    evidence_items: list[FindingEvidence] = []
    for raw_item in raw_evidence:
        if not isinstance(raw_item, dict):
            raise ReviewError(f"Every {field} citation must be an object")
        path = _required_string(raw_item, "path", RESPONSE_FIELD_LIMITS["path"]).replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        line = raw_item.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ReviewError(f"Finding {field} line must be a positive integer")
        excerpt = _required_string(raw_item, "excerpt", RESPONSE_FIELD_LIMITS["excerpt"])
        relevance = _required_string(raw_item, "relevance", RESPONSE_FIELD_LIMITS["relevance"])
        if "\n" in excerpt or "\r" in excerpt:
            raise ReviewError(f"Finding {field} excerpt must be single-line")

        if repository_root is not None:
            root = repository_root.resolve()
            if PurePosixPath(path).is_absolute():
                # An absolute path inside the workspace names the file a relative one
                # would, so it can be rewritten rather than refused: the file is the
                # same and the relative form is what an inline comment needs. Worth
                # rewriting because the retry made this worse, not better — told only
                # that a path did not exist, the model answered with the full sandbox
                # path and lost the whole level ([pr:ud7g], 4 levels in one week).
                # A path outside the workspace is a different matter and still fails.
                absolute = Path(path).resolve()
                if _within_root(absolute, root):
                    path = absolute.relative_to(root).as_posix()
                else:
                    raise ReviewError(
                        f"Finding {field} path must be workspace-relative, like "
                        f"src/module.py, not {path}"
                    )
            allowed_roots = (root, *evidence_roots)
            evidence_path = (root / path).resolve()
            if not any(_within_root(evidence_path, allowed) for allowed in allowed_roots):
                raise ReviewError(
                    f"Finding {field} path escapes the review workspace: {path}"
                )
            if not evidence_path.is_file():
                raise ReviewError(
                    f"Finding {field} path does not exist in the reviewed head: {path}"
                )
            try:
                lines = evidence_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError) as error:
                raise ReviewError(
                    f"Cannot verify finding {field} citation {path}:{line}: {error}"
                ) from error
            if line > len(lines) or excerpt not in lines[line - 1]:
                matching_lines = [
                    candidate_line
                    for candidate_line, source_line in enumerate(lines, start=1)
                    if excerpt in source_line
                ]
                if len(matching_lines) == 1:
                    line = matching_lines[0]
                else:
                    cited = _quote_line(excerpt)
                    if line <= len(lines):
                        found = f"that line is {_quote_line(lines[line - 1])}"
                    else:
                        found = f"the file ends at line {len(lines)}"
                    # Where the text actually is decides what to do about it, and
                    # the search above has just established it. Without that, a
                    # correction retry can only guess: on [pr:djf5] the excerpt
                    # sat on three identical assertion lines and the retry, told
                    # only that the cited line was blank, failed the same way and
                    # cost the whole review.
                    if matching_lines:
                        places = ", ".join(
                            str(found_at) for found_at in matching_lines
                        )
                        plural = "s" if len(matching_lines) > 1 else ""
                        detail = (
                            f"; that text is on line{plural} {places}, so cite "
                            "the one this finding means"
                        )
                    else:
                        detail = (
                            "; that text is on no line of this file, so quote a line "
                            "that exists or omit the finding"
                        )
                    raise ReviewError(
                        f"Finding {field} excerpt does not match {path}:{line}: "
                        f"cited {cited} but {found}{detail}"
                    )

        evidence_items.append(FindingEvidence(path, line, excerpt, relevance))
    return tuple(evidence_items)


def validate_findings(
    structured_output: Any,
    material: ReviewMaterial,
    max_findings: int,
    repository_root: Optional[Path] = None,
) -> list[Finding]:
    if not isinstance(structured_output, dict):
        raise ReviewError("Review engine did not return a JSON object")
    raw_findings = structured_output.get("findings")
    if not isinstance(raw_findings, list):
        raise ReviewError("Review engine response must contain a findings array")
    if len(raw_findings) > max_findings:
        raise ReviewError(f"Review engine returned more than {max_findings} findings")

    findings: list[Finding] = []
    seen_locations: set[tuple[str, int]] = set()
    evidence_roots = _evidence_roots(material)
    passing_build_checks = _passing_build_checks(material)
    for raw_finding in raw_findings:
        if not isinstance(raw_finding, dict):
            raise ReviewError("Every finding must be an object")
        severity = _required_string(raw_finding, "severity", RESPONSE_FIELD_LIMITS["severity"]).lower()
        if severity not in ALLOWED_SEVERITIES:
            raise ReviewError(f"Unsupported finding severity: {severity}")
        title = _required_string(raw_finding, "title", RESPONSE_FIELD_LIMITS["title"])
        reported_path = _required_string(raw_finding, "path", RESPONSE_FIELD_LIMITS["path"])
        line = raw_finding.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ReviewError("Finding line must be a positive integer")
        path = _resolve_finding_path(reported_path, line, material.added_lines)
        if path not in material.added_lines or line not in material.added_lines[path]:
            raise ReviewError(
                f"Finding {title!r} location is not an added diff line: "
                f"{reported_path}:{line}"
            )
        location = (path, line)
        if location in seen_locations:
            raise ReviewError(f"Duplicate finding location: {path}:{line}")
        seen_locations.add(location)

        problem = _required_string(raw_finding, "problem", RESPONSE_FIELD_LIMITS["problem"])
        suggestion = _required_string(raw_finding, "suggestion", RESPONSE_FIELD_LIMITS["suggestion"])
        trigger = _required_string(raw_finding, "trigger", RESPONSE_FIELD_LIMITS["trigger"])
        impact = _required_string(raw_finding, "impact", RESPONSE_FIELD_LIMITS["impact"])
        rule, rule_source = _broken_rule(raw_finding, title, material.review_rules)
        contradicting_check = _contradicting_build_check(
            (title, problem, trigger, impact),
            passing_build_checks,
        )
        if contradicting_check is not None:
            raise ReviewError(
                f"Finding {title!r} claims a build or typecheck failure that the "
                f"successful exact-head check {contradicting_check!r} disproves; "
                "omit it or report a defect that the passing check cannot cover"
            )

        findings.append(
            Finding(
                severity=severity,
                title=title,
                path=path,
                line=line,
                problem=problem,
                suggestion=suggestion,
                trigger=trigger,
                impact=impact,
                evidence=_required_evidence(
                    raw_finding,
                    "evidence",
                    repository_root,
                    evidence_roots,
                ),
                counterevidence=_required_evidence(
                    raw_finding,
                    "counterevidence",
                    repository_root,
                    evidence_roots,
                ),
                counterevidence_reason=_required_string(
                    raw_finding,
                    "counterevidence_reason",
                    RESPONSE_FIELD_LIMITS["counterevidence_reason"],
                ),
                rule=rule,
                rule_source=rule_source,
            )
        )
    return findings


def _broken_rule(
    raw_finding: "dict[str, Any]", title: str, rules: Optional[ReviewRules]
) -> "tuple[str, str]":
    """Return the repository rule a finding says the change breaks, and its file.

    The quote must be the base branch's own text, so a finding cannot invent a rule,
    or borrow one from the reviewed head, to dress a preference up as a defect.
    Without rules the field is not asked for, and anything sent anyway is ignored.
    """

    if rules is None or not rules.sections:
        return "", ""
    value = raw_finding.get("rule", "")
    if not isinstance(value, str):
        raise ReviewError(f"Finding {title!r} field 'rule' must be a string")
    quote = value.strip()
    if not quote:
        return "", ""
    limit = RESPONSE_FIELD_LIMITS["rule"]
    if len(quote) > limit:
        raise ReviewError(f"Finding {title!r} field 'rule' exceeds {limit} characters")
    source = None if "\n" in quote or "\r" in quote else rules.source_of(quote)
    if source is None:
        raise ReviewError(
            f"Finding {title!r} quotes a rule that is not in the repository's code "
            f"review rules: {quote[:120]!r}. Copy a fragment of one line of a rule "
            "exactly, or leave `rule` empty for a defect that breaks no rule"
        )
    return quote, source


DESIGN_SEVERITIES = ("LOW", "MEDIUM", "HIGH")

HUMAN_DOCUMENTATION_PATTERNS = (
    "README.md",
    "README.*.md",
    "CONTRIBUTING.md",
    "ARCHITECTURE.md",
    "docs/**/*.md",
    "doc/**/*.md",
    # Two levels at most: a package README documents that package, while a deep
    # search would collect every dependency's own README.
    "*/README.md",
    "*/*/README.md",
)
AI_DOCUMENTATION_PATTERNS = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".cursorrules",
    ".cursor/rules/**/*.md",
    ".cursor/rules/**/*.mdc",
    # Steering only. A `.kiro/specs` plan describes work to do rather than how the
    # project behaves, so a change is meant to leave it behind.
    ".kiro/steering/**/*.md",
    ".github/copilot-instructions.md",
    ".github/instructions/**/*.md",
)
# A changelog records what happened rather than promising how the code behaves, so
# it cannot be contradicted by a change and is not documentation for this purpose.
EXCLUDED_DOCUMENTATION_NAMES = frozenset(
    {"changelog.md", "license.md", "license", "code_of_conduct.md", "security.md"}
)
EXCLUDED_DOCUMENTATION_DIRECTORIES = frozenset(
    {"node_modules", "vendor", "dist", "build", "coverage", ".next", ".venv"}
)
MAX_DOCUMENTATION_FILES = 60
# A document this large is generated, or a book; either way the level cannot read it
# in one pass and listing it would spend the model's attention on the wrong file.
MAX_DOCUMENTATION_FILE_BYTES = 200_000


BACKGROUND_FILE = ".prwarden/background.md"
# The same ceiling the pull-request body gets in the prompt: this is context, and a
# document long enough to compete with the diff would be answering a different
# question than "what is this product for".
MAX_BACKGROUND_BYTES = 4000


def read_background(root: Optional[Path]) -> str:
    """Return the reviewed head's own description of what this codebase is for.

    A diff says what changed and a pull-request body says what the author meant; a
    reviewer new to the product still lacks what everybody on the team knows. This
    file is where a team writes that down once, and it travels with the code rather
    than living in the operator's configuration, so it stays true as the product
    moves.

    It is content from the reviewed head, so it is untrusted: capped here and framed
    as data in the prompt. It may add context, never authority.
    """

    if root is None:
        return ""
    path = root / BACKGROUND_FILE
    try:
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    text = text.strip()
    if len(text) <= MAX_BACKGROUND_BYTES:
        return text
    return text[:MAX_BACKGROUND_BYTES].rstrip() + "\n[truncated]"


def discover_documentation(root: Optional[Path]) -> "tuple[DocumentationFile, ...]":
    """Return the documentation the reviewed head carries, human and agent alike.

    Found by looking rather than by configuration: a repository that keeps its
    agent instructions in `.kiro` and its prose in `docs` should need no setting,
    and a repository with no documentation should cost no invocation at all. The
    audience is decided by location, so it is a fact about the file rather than a
    claim the model can make.

    Root-level files sort first and the list is capped, so a repository with a
    hundred pages of documentation still shows the reviewer its README and its
    agent instructions rather than a hundred deep pages.
    """

    if root is None:
        return ()
    found: "dict[str, DocumentationFile]" = {}
    for audience, patterns in (
        ("ai", AI_DOCUMENTATION_PATTERNS),
        ("human", HUMAN_DOCUMENTATION_PATTERNS),
    ):
        for pattern in patterns:
            for path in sorted(root.glob(pattern)):
                if not path.is_file():
                    continue
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    continue
                parts = relative.parts
                if any(part in EXCLUDED_DOCUMENTATION_DIRECTORIES for part in parts):
                    continue
                if parts[-1].casefold() in EXCLUDED_DOCUMENTATION_NAMES:
                    continue
                try:
                    size_bytes = path.stat().st_size
                except OSError:
                    continue
                if not size_bytes or size_bytes > MAX_DOCUMENTATION_FILE_BYTES:
                    continue
                key = relative.as_posix()
                if key in found:
                    continue
                found[key] = DocumentationFile(
                    path=key,
                    size_bytes=size_bytes,
                    audience=audience,
                )
    ordered = sorted(found.values(), key=lambda file: (file.path.count("/"), file.path))
    return tuple(ordered[:MAX_DOCUMENTATION_FILES])


def _glob_matches(parts: "Sequence[str]", pattern: "Sequence[str]") -> bool:
    """Return whether path segments match glob segments, `**` spanning any depth.

    The semantics of `Path.glob`, which finds the documents, for a path that may no
    longer exist: `PurePath.match` reads `**` as one segment before Python 3.13.
    """

    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(
            _glob_matches(parts[index:], pattern[1:])
            for index in range(len(parts) + 1)
        )
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) and (
        _glob_matches(parts[1:], pattern[1:])
    )


def is_documentation_path(path: str) -> bool:
    """Return whether a repository path is documentation the docs level reads.

    The same patterns and exclusions as `discover_documentation`, decided from the
    path alone, so a document a pull request deleted or renamed away still counts.
    """

    parts = tuple(part for part in PurePosixPath(path).parts if part not in ("", "."))
    if not parts:
        return False
    if any(part in EXCLUDED_DOCUMENTATION_DIRECTORIES for part in parts):
        return False
    if parts[-1].casefold() in EXCLUDED_DOCUMENTATION_NAMES:
        return False
    return any(
        _glob_matches(parts, pattern.split("/"))
        for pattern in AI_DOCUMENTATION_PATTERNS + HUMAN_DOCUMENTATION_PATTERNS
    )


# A repository states what its reviews must hold the code to in a "Code review"
# section of its agent instructions, the files `AI_DOCUMENTATION_PATTERNS` names.
REVIEW_RULE_HEADING = re.compile(
    r"code[ -]?reviews?(?:\s+(?:guidelines|rules|instructions|checklist|standards))?",
    re.IGNORECASE,
)
ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
CODE_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# What one prompt can carry without crowding out the diff it is about.
MAX_REVIEW_RULES_CHARACTERS = 6000


def is_review_rule_source(path: str) -> bool:
    """Return whether a repository path is an agent instruction file."""

    parts = tuple(part for part in PurePosixPath(path).parts if part not in ("", "."))
    return bool(parts) and any(
        _glob_matches(parts, pattern.split("/"))
        for pattern in AI_DOCUMENTATION_PATTERNS
    )


def review_rule_pathspecs() -> "tuple[str, ...]":
    """Return the directories and files an instruction file can be found under."""

    prefixes = []
    for pattern in AI_DOCUMENTATION_PATTERNS:
        literal = []
        for part in pattern.split("/"):
            if any(character in part for character in "*?["):
                break
            literal.append(part)
        if literal:
            prefixes.append("/".join(literal))
    return tuple(dict.fromkeys(prefixes))


def _is_review_heading(text: str) -> bool:
    title = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", text.strip())
    title = title.strip("*_` \t").rstrip(":").strip("*_` \t")
    return bool(REVIEW_RULE_HEADING.fullmatch(title))


def code_review_sections(text: str) -> "list[tuple[str, str]]":
    """Return the heading and body of each "Code review" section of a document.

    A section runs from its Markdown heading to the next heading of the same or a
    higher level, so its own subsections belong to it. A heading inside a fenced
    code block is code, not a heading.
    """

    sections: "list[tuple[str, str]]" = []
    current: "Optional[tuple[int, str, list[str]]]" = None
    fence = ""

    def close() -> None:
        if current is not None:
            body = "\n".join(current[2]).strip()
            if body:
                sections.append((current[1], body))

    for line in text.splitlines():
        if fence:
            stripped = line.strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = ""
            if current is not None:
                current[2].append(line)
            continue
        opening = CODE_FENCE.match(line)
        if opening:
            fence = opening.group(1)
            if current is not None:
                current[2].append(line)
            continue
        heading = ATX_HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            title = (heading.group(2) or "").strip()
            if current is not None and level <= current[0]:
                close()
                current = None
            if current is None:
                if _is_review_heading(title):
                    current = (level, title, [])
                continue
        if current is not None:
            current[2].append(line)
    close()
    return sections


def collect_review_rules(
    documents: "Sequence[tuple[str, str]]", base_ref: str, base_sha: str
) -> ReviewRules:
    """Return every "Code review" section the base branch's instruction files hold.

    Root-level files come first, and a section repeated word for word in two files,
    as `CLAUDE.md` often repeats `AGENTS.md`, is kept once. Past the budget the rest
    is left out and marked truncated, so nobody mistakes it for enforced.
    """

    sections: "list[ReviewRuleSection]" = []
    seen: "set[str]" = set()
    remaining = MAX_REVIEW_RULES_CHARACTERS
    truncated = False
    ordered = sorted(documents, key=lambda item: (item[0].count("/"), item[0]))
    for path, text in ordered:
        for heading, body in code_review_sections(text):
            if body in seen:
                continue
            seen.add(body)
            if remaining <= 0:
                truncated = True
                break
            if len(body) > remaining:
                body = body[:remaining].rstrip()
                truncated = True
            remaining -= len(body)
            sections.append(ReviewRuleSection(path=path, heading=heading, text=body))
    return ReviewRules(
        base_ref=base_ref,
        base_sha=base_sha,
        sections=tuple(sections),
        truncated=truncated,
    )


def documentation_change_fingerprint(
    files: "Sequence[dict[str, Any]]",
) -> Optional[str]:
    """Return a digest of what a pull request does to its documentation.

    None when it changes no documentation. Built from the lines it adds and removes
    rather than from blobs or hunk positions, so a rebase or a merge from the base
    branch, which moves hunks and can change the files around them, leaves it alone;
    only a different documentation edit by this pull request changes it. A file
    GitHub sends no patch for is represented by its blob, the only evidence left.
    """

    entries = []
    for item in files:
        path = item.get("filename")
        previous = item.get("previous_filename")
        if not isinstance(path, str):
            continue
        if not is_documentation_path(path) and not (
            isinstance(previous, str) and is_documentation_path(previous)
        ):
            continue
        patch = item.get("patch")
        if isinstance(patch, str):
            content = "\n".join(
                line for line in patch.splitlines() if line.startswith(("+", "-"))
            )
        else:
            content = f"blob {item.get('sha') or ''}"
        entries.append(
            json.dumps(
                [path, previous or "", item.get("status") or "", content],
                ensure_ascii=False,
            )
        )
    if not entries:
        return None
    digest = hashlib.sha256("\n".join(sorted(entries)).encode("utf-8"))
    return digest.hexdigest()


def validate_design_findings(
    structured_output: Any,
    material: ReviewMaterial,
    repository_root: Optional[Path],
    max_findings: int,
) -> "tuple[str, str, list[DesignFinding]]":
    """Return the understanding, verdict, and findings of a design-level response.

    Design findings carry no anchor line, which is what lets them describe a whole
    change, so nothing here checks one. Their citations are held to the same
    standard as code evidence: the path must resolve inside the reviewed workspace
    and the excerpt must be that line's exact text.
    """

    if not isinstance(structured_output, dict):
        raise ReviewError("Design review response must be a JSON object")
    understanding = _required_string(
        structured_output, "understanding", DESIGN_FIELD_LIMITS["understanding"]
    )
    verdict = structured_output.get("verdict")
    if verdict not in ("sound", "flawed"):
        raise ReviewError(
            "Design review verdict must be 'sound' or 'flawed', not "
            f"{verdict!r}"
        )
    raw_findings = structured_output.get("findings")
    if not isinstance(raw_findings, list):
        raise ReviewError("Design review findings must be an array")
    if len(raw_findings) > max_findings:
        raise ReviewError(
            f"Design review returned {len(raw_findings)} findings, "
            f"exceeding the configured maximum of {max_findings}"
        )
    if raw_findings and verdict != "flawed":
        raise ReviewError(
            "Design review returned findings with a 'sound' verdict; a reported "
            "design flaw contradicts a sound approach"
        )

    evidence_roots = _evidence_roots(material)
    findings: list[DesignFinding] = []
    for raw_finding in raw_findings:
        if not isinstance(raw_finding, dict):
            raise ReviewError("Each design finding must be a JSON object")
        severity = raw_finding.get("severity")
        if severity not in DESIGN_SEVERITIES:
            raise ReviewError(
                f"Design finding severity must be one of "
                f"{', '.join(DESIGN_SEVERITIES)}, not {severity!r}"
            )
        category = raw_finding.get("category")
        if category not in DESIGN_CATEGORIES:
            raise ReviewError(
                f"Design finding category is not one of the supplied "
                f"categories: {category!r}"
            )
        citations = _required_evidence(
            raw_finding, "citations", repository_root, evidence_roots
        )
        if not citations:
            raise ReviewError("Every design finding must cite the workspace")
        findings.append(
            DesignFinding(
                severity=severity,
                title=_required_string(
                    raw_finding, "title", DESIGN_FIELD_LIMITS["title"]
                ),
                category=category,
                problem=_required_string(
                    raw_finding, "problem", DESIGN_FIELD_LIMITS["problem"]
                ),
                consequence=_required_string(
                    raw_finding, "consequence", DESIGN_FIELD_LIMITS["consequence"]
                ),
                why_not_preference=_required_string(
                    raw_finding,
                    "why_not_preference",
                    DESIGN_FIELD_LIMITS["why_not_preference"],
                ),
                alternative=_required_string(
                    raw_finding, "alternative", DESIGN_FIELD_LIMITS["alternative"]
                ),
                citations=citations,
            )
        )
    return understanding, verdict, findings


def _changed_paths(material: ReviewMaterial) -> "frozenset[str]":
    """Return every path this pull request changed, readable or not.

    A file the budget deferred, or whose patch GitHub omitted, is still part of the
    change and can be read in the workspace, so a proposal may cite it.
    """

    paths = set(material.added_lines)
    paths.update(material.ignored_missing_patch_files)
    paths.update(material.textless_files)
    paths.update(file.path for file in material.removed_files)
    paths.update(file.path for file in material.moved_files)
    if material.diff_budget is not None:
        paths.update(file.path for file in material.diff_budget.deferred)
    return frozenset(paths)


# A Markdown table row is the text between unescaped pipes; the header rule under a
# table's first row is a row of dashes, which starts a table rather than extending
# one. A list item is a marker followed by text, at some indent.
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_TABLE_RULE_CELL = re.compile(r"\s*:?-{3,}:?\s*")
_LIST_ITEM = re.compile(r"( *)([-*+]|\d+[.)])[ \t]+\S")


def _list_entry_kind(line: str) -> "Optional[tuple[Any, ...]]":
    """Return what kind of list entry one Markdown line is, or None if it is none.

    Two lines are entries of one list when their kinds are equal: table rows with
    the same number of cells, or items with the same marker at the same indent.
    """

    stripped = line.strip()
    if len(stripped) > 1 and stripped.startswith("|") and stripped.endswith("|"):
        cells = _UNESCAPED_PIPE.split(stripped[1:-1])
        if all(_TABLE_RULE_CELL.fullmatch(cell) for cell in cells):
            return None
        return ("row", len(cells))
    item = _LIST_ITEM.match(line.replace("\t", "    "))
    if item:
        marker = item.group(2)
        style = f"n{marker[-1]}" if marker[0].isdigit() else marker
        return ("item", len(item.group(1)), style)
    return None


def _describe_entry_kind(kind: "tuple[Any, ...]") -> str:
    if kind[0] == "row":
        return f"a table row of {kind[1]} cells"
    style = kind[2]
    marker = f"a number and {style[1]!r}" if style.startswith("n") else repr(style)
    return f"a list item marked {marker} at indent {kind[1]}"


def _check_list_entry_shape(
    repository_root: Optional[Path],
    cited: FindingEvidence,
    proposed_update: str,
) -> None:
    """Refuse a missing-entry proposal that is anything but new entries for a list.

    The verifier may treat a list that lacks an entry as incomplete rather than
    silent only for this category, so the category has to mean exactly that.
    Worded as a prompt rule for every candidate, the same exception let a verifier
    pass a clarification of a sentence that was still true and a whole new section;
    held to this shape, a proposal can be neither.
    """

    if repository_root is None:
        raise ReviewError(
            "A missing_list_entry proposal cannot be checked without the workspace"
        )
    try:
        lines = (repository_root / cited.path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ReviewError(
            f"Cannot check missing_list_entry citation {cited.path}:{cited.line}: "
            f"{error}"
        ) from error
    line = lines[cited.line - 1] if cited.line <= len(lines) else ""
    kind = _list_entry_kind(line)
    if kind is None:
        raise ReviewError(
            "A missing_list_entry proposal must cite an existing list item or table "
            f"row, the entry the new one belongs after; {cited.path}:{cited.line} is "
            f"{_quote_line(line)}. Cite that entry, or use another category."
        )
    in_item = False
    for number, text in enumerate(proposed_update.strip("\n").splitlines(), start=1):
        entry = _list_entry_kind(text)
        if entry == kind:
            in_item = kind[0] == "item"
            continue
        indent = len(text) - len(text.lstrip(" "))
        if entry is None and in_item and text.strip() and indent > kind[1]:
            continue
        raise ReviewError(
            "A missing_list_entry proposal must hold only new entries formatted like "
            f"{cited.path}:{cited.line}, {_describe_entry_kind(kind)}, with no "
            f"heading, new table, or sentence around them; its line {number} is "
            f"{_quote_line(text)}. Drop what is not an entry, or use another category."
        )


def validate_docs_findings(
    structured_output: Any,
    material: ReviewMaterial,
    repository_root: Optional[Path],
    max_findings: int,
) -> "tuple[str, str, list[DocsFinding]]":
    """Return the understanding, verdict, and proposals of a docs-level response.

    A proposal must have both sides on record: the documentation line it claims is
    now wrong, which has to be one of the documents the controller found in this
    head, and at least one changed line that made it wrong. Without the first, the
    level could invent a document; without the second, it could report drift that
    this pull request did not cause.
    """

    if not isinstance(structured_output, dict):
        raise ReviewError("Documentation review response must be a JSON object")
    understanding = _required_string(
        structured_output, "understanding", DOCS_FIELD_LIMITS["understanding"]
    )
    verdict = structured_output.get("verdict")
    if verdict not in ("consistent", "stale"):
        raise ReviewError(
            "Documentation review verdict must be 'consistent' or 'stale', not "
            f"{verdict!r}"
        )
    raw_findings = structured_output.get("findings")
    if not isinstance(raw_findings, list):
        raise ReviewError("Documentation review findings must be an array")
    if len(raw_findings) > max_findings:
        raise ReviewError(
            f"Documentation review returned {len(raw_findings)} proposals, "
            f"exceeding the configured maximum of {max_findings}"
        )
    if raw_findings and verdict != "stale":
        raise ReviewError(
            "Documentation review returned proposals with a 'consistent' verdict; "
            "documentation that needs an update is not consistent"
        )

    documentation = {file.path: file for file in material.documentation_files}
    changed = _changed_paths(material)
    evidence_roots = _evidence_roots(material)
    findings: "list[DocsFinding]" = []
    for raw_finding in raw_findings:
        if not isinstance(raw_finding, dict):
            raise ReviewError("Each documentation proposal must be a JSON object")
        severity = raw_finding.get("severity")
        if severity not in DESIGN_SEVERITIES:
            raise ReviewError(
                f"Documentation proposal severity must be one of "
                f"{', '.join(DESIGN_SEVERITIES)}, not {severity!r}"
            )
        category = raw_finding.get("category")
        if category not in DOCS_CATEGORIES:
            raise ReviewError(
                f"Documentation proposal category is not one of the supplied "
                f"categories: {category!r}"
            )
        raw_documentation = raw_finding.get("documentation")
        if not isinstance(raw_documentation, dict):
            raise ReviewError(
                "Every documentation proposal must cite the documentation line it "
                "reports as out of date"
            )
        cited_document = dict(raw_documentation)
        if not cited_document.get("relevance"):
            cited_document["relevance"] = "the documentation this change outdates"
        cited = _required_evidence(
            {"documentation": [cited_document]},
            "documentation",
            repository_root,
            evidence_roots,
        )[0]
        if cited.path not in documentation:
            raise ReviewError(
                f"Documentation proposal cites {cited.path}, which is not one of "
                "this workspace's documentation files"
            )
        citations = _required_evidence(
            raw_finding, "citations", repository_root, evidence_roots
        )
        if not citations:
            raise ReviewError(
                "Every documentation proposal must cite the changed lines that "
                "outdated the document"
            )
        if not any(citation.path in changed for citation in citations):
            raise ReviewError(
                "Documentation proposal cites no file this pull request changed: "
                f"{', '.join(sorted({citation.path for citation in citations}))}"
            )
        proposed_update = _required_string(
            raw_finding,
            "proposed_update",
            DOCS_FIELD_LIMITS["proposed_update"],
        )
        if category == MISSING_LIST_ENTRY:
            _check_list_entry_shape(repository_root, cited, proposed_update)
        findings.append(
            DocsFinding(
                severity=severity,
                title=_required_string(
                    raw_finding, "title", DOCS_FIELD_LIMITS["title"]
                ),
                category=category,
                audience=documentation[cited.path].audience,
                documented=_required_string(
                    raw_finding, "documented", DOCS_FIELD_LIMITS["documented"]
                ),
                behavior=_required_string(
                    raw_finding, "behavior", DOCS_FIELD_LIMITS["behavior"]
                ),
                proposed_update=proposed_update,
                documentation=cited,
                citations=citations,
            )
        )
    return understanding, verdict, findings


def validate_docs_judgments(
    structured_output: Any,
    candidates: Sequence[DocsFinding],
) -> "tuple[tuple[DocsFinding, ...], tuple[RejectedDocsCandidate, ...]]":
    """Return the confirmed and rejected candidates of a docs verification pass.

    Judged by reference, so a confirmed proposal keeps the replacement text the
    controller validated, and every supplied reference must be judged exactly once.
    """

    if not isinstance(structured_output, dict):
        raise ReviewError(
            "Documentation verification response must be a JSON object"
        )
    raw_judgments = structured_output.get("judgments")
    if not isinstance(raw_judgments, list):
        raise ReviewError("Documentation verification judgments must be an array")

    supplied = docs_candidate_refs(candidates)
    confirmed: "list[DocsFinding]" = []
    rejected: "list[RejectedDocsCandidate]" = []
    seen: set = set()
    for raw_judgment in raw_judgments:
        if not isinstance(raw_judgment, dict):
            raise ReviewError("Each documentation judgment must be a JSON object")
        raw_reference = raw_judgment.get("ref")
        if not isinstance(raw_reference, str) or not raw_reference.strip():
            raise ReviewError(
                "Documentation judgment reference must be a non-empty string"
            )
        reference = raw_reference.strip().upper()
        if reference not in supplied:
            raise ReviewError(
                "Documentation judgment names a candidate that was not supplied: "
                f"{reference}; supplied references: {', '.join(supplied)}"
            )
        if reference in seen:
            raise ReviewError(
                f"Documentation candidate {reference} was judged twice"
            )
        seen.add(reference)
        verdict = raw_judgment.get("verdict")
        if verdict not in ("confirmed", "rejected"):
            raise ReviewError(
                "Documentation judgment verdict must be 'confirmed' or 'rejected', "
                f"not {verdict!r}"
            )
        reason = _required_string(
            raw_judgment,
            "reason",
            DOCS_FIELD_LIMITS["reason"],
            owner="Documentation judgment",
        )
        if verdict == "confirmed":
            confirmed.append(supplied[reference])
        else:
            rejected.append(
                RejectedDocsCandidate(finding=supplied[reference], reason=reason)
            )
    unjudged = [reference for reference in supplied if reference not in seen]
    if unjudged:
        raise ReviewError(
            "Documentation verification left candidates unjudged: "
            f"{', '.join(unjudged)}"
        )
    return tuple(confirmed), tuple(rejected)


def validate_design_judgments(
    structured_output: Any,
    candidates: Sequence[DesignFinding],
) -> "tuple[tuple[DesignFinding, ...], tuple[RejectedDesignCandidate, ...]]":
    """Return the confirmed and rejected candidates of a design verification pass.

    The verifier judges by reference and never restates a candidate, so a confirmed
    finding is the controller's own validated object and the verifier cannot edit
    what it approves. Every supplied reference must be judged exactly once: a
    candidate nobody judged is neither confirmed nor rejected, and silently
    dropping it would turn an unanswered question into an approval.
    """

    if not isinstance(structured_output, dict):
        raise ReviewError("Design verification response must be a JSON object")
    raw_judgments = structured_output.get("judgments")
    if not isinstance(raw_judgments, list):
        raise ReviewError("Design verification judgments must be an array")

    supplied = design_candidate_refs(candidates)
    confirmed: list[DesignFinding] = []
    rejected: list[RejectedDesignCandidate] = []
    seen: set = set()
    for raw_judgment in raw_judgments:
        if not isinstance(raw_judgment, dict):
            raise ReviewError("Each design judgment must be a JSON object")
        raw_reference = raw_judgment.get("ref")
        if not isinstance(raw_reference, str) or not raw_reference.strip():
            raise ReviewError("Design judgment reference must be a non-empty string")
        reference = raw_reference.strip().upper()
        if reference not in supplied:
            raise ReviewError(
                "Design judgment names a candidate that was not supplied: "
                f"{reference}; supplied references: {', '.join(supplied)}"
            )
        if reference in seen:
            raise ReviewError(f"Design candidate {reference} was judged twice")
        seen.add(reference)
        verdict = raw_judgment.get("verdict")
        if verdict not in ("confirmed", "rejected"):
            raise ReviewError(
                "Design judgment verdict must be 'confirmed' or 'rejected', not "
                f"{verdict!r}"
            )
        reason = _required_string(
            raw_judgment,
            "reason",
            DESIGN_FIELD_LIMITS["reason"],
            owner="Design judgment",
        )
        if verdict == "confirmed":
            confirmed.append(supplied[reference])
        else:
            rejected.append(
                RejectedDesignCandidate(finding=supplied[reference], reason=reason)
            )
    unjudged = [
        reference for reference in supplied if reference not in seen
    ]
    if unjudged:
        raise ReviewError(
            "Design verification left candidates unjudged: "
            f"{', '.join(unjudged)}"
        )
    return tuple(confirmed), tuple(rejected)


def validate_code_judgments(
    structured_output: Any,
    candidates: "Sequence[Finding]",
) -> "list[Finding]":
    """Return the candidates a verification pass confirmed.

    Each confirmed finding is the controller's own validated object, so the pass
    cannot alter a path, a line, or an excerpt it was given: judging by reference
    removes the transcription step that produced seven rejected verification
    responses. Severity is the single correction the verifier may make, because only
    this pass has weighed the established impact. Every supplied reference must be
    judged exactly once, since a candidate nobody answered for is neither confirmed
    nor rejected and dropping it silently would turn an unanswered question into a
    clean review.
    """

    if not isinstance(structured_output, dict):
        raise ReviewError("Verification response must be a JSON object")
    raw_judgments = structured_output.get("judgments")
    if not isinstance(raw_judgments, list):
        raise ReviewError("Verification judgments must be an array")

    supplied = code_candidate_refs(candidates)
    confirmed: list[Finding] = []
    seen: set = set()
    for raw_judgment in raw_judgments:
        if not isinstance(raw_judgment, dict):
            raise ReviewError("Each verification judgment must be a JSON object")
        raw_reference = raw_judgment.get("ref")
        if not isinstance(raw_reference, str) or not raw_reference.strip():
            raise ReviewError(
                "Verification judgment reference must be a non-empty string"
            )
        reference = raw_reference.strip().upper()
        if reference not in supplied:
            raise ReviewError(
                "Verification judged a candidate that was not supplied: "
                f"{reference}; supplied references: {', '.join(supplied)}"
            )
        if reference in seen:
            raise ReviewError(f"Candidate {reference} was judged twice")
        seen.add(reference)
        verdict = raw_judgment.get("verdict")
        if verdict not in ("confirmed", "rejected"):
            raise ReviewError(
                "Verification judgment verdict must be 'confirmed' or 'rejected', "
                f"not {verdict!r}"
            )
        _required_string(
            raw_judgment, "reason", RESPONSE_FIELD_LIMITS["rejection_reason"]
        )
        if verdict != "confirmed":
            continue
        candidate = supplied[reference]
        raw_severity = raw_judgment.get("severity")
        if raw_severity is None:
            confirmed.append(candidate)
            continue
        if (
            not isinstance(raw_severity, str)
            or raw_severity.strip().lower() not in ALLOWED_SEVERITIES
        ):
            raise ReviewError(
                f"Verification corrected {reference} to an unsupported severity: "
                f"{raw_severity!r}"
            )
        confirmed.append(
            replace(candidate, severity=raw_severity.strip().lower())
        )
    unjudged = [reference for reference in supplied if reference not in seen]
    if unjudged:
        raise ReviewError(
            f"Verification left candidates unjudged: {', '.join(unjudged)}"
        )
    return confirmed


def validate_approval_verdict(
    structured_output: Any,
    findings: list[Finding],
) -> bool:
    if not isinstance(structured_output, dict):
        raise ReviewError("Review engine did not return a JSON object")
    verdict = structured_output.get("verdict")
    if verdict not in {"approve", "do_not_approve"}:
        raise ReviewError(
            "Review engine response must contain verdict 'approve' or "
            "'do_not_approve'"
        )
    if verdict == "approve" and findings:
        raise ReviewError("Review engine cannot approve a response with findings")
    return verdict == "approve"


def validate_approval_reason(structured_output: Any) -> str:
    if not isinstance(structured_output, dict):
        raise ReviewError("Review engine did not return a JSON object")
    reason = structured_output.get("approval_reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ReviewError(
            "Review engine response must contain a non-empty approval_reason"
        )
    reason = reason.strip()
    approval_limit = RESPONSE_FIELD_LIMITS["approval_reason"]
    if len(reason) > approval_limit:
        raise ReviewError(
            f"Review engine approval_reason exceeds {approval_limit} characters"
        )
    return reason


UNCLASSIFIED_REJECTION = "unclassified"


class CandidateRejection(NamedTuple):
    """Why the verifier threw a candidate out, as it said and as it classified it."""

    reason: str
    rejection_class: str


def collect_rejection_reasons(
    structured_output: Any,
    candidates: "Sequence[Finding]" = (),
) -> "dict[str, CandidateRejection]":
    """Read the verifier's reason for each candidate it did not confirm.

    These reasons are diagnostic: they are reported on the terminal and in the
    run transcript so a strict verifier can be told from a weak review, and they
    are never delivered anywhere. A malformed or unmatched entry therefore must
    not fail a review that is otherwise sound, so unusable entries are dropped
    rather than raised, and a reason longer than the response limit is cut to it.
    """
    if not isinstance(structured_output, dict):
        return {}
    raw_judgments = structured_output.get("judgments")
    if not isinstance(raw_judgments, list):
        return {}
    supplied = code_candidate_refs(candidates)
    limit = RESPONSE_FIELD_LIMITS["rejection_reason"]
    # Keyed by the reference the verifier answered, not by the line it points at.
    # Two candidates can share a line — a review that raises two defects in one
    # expression does — and keying by position kept the first reason and dropped the
    # second, which is exactly the evidence this exists to preserve.
    reasons: "dict[str, CandidateRejection]" = {}
    for raw_judgment in raw_judgments:
        if not isinstance(raw_judgment, dict):
            continue
        raw_reference = raw_judgment.get("ref")
        raw_reason = raw_judgment.get("reason")
        if not isinstance(raw_reference, str) or not raw_reference.strip():
            continue
        if not isinstance(raw_reason, str) or not raw_reason.strip():
            continue
        candidate = supplied.get(raw_reference.strip().upper())
        if candidate is None:
            continue
        reason = " ".join(raw_reason.split())[:limit]
        raw_class = raw_judgment.get("rejection_class")
        # An off-enum or absent class counts as unclassified rather than failing a
        # sound review: this field is measurement, and measurement must not gate.
        rejection_class = (
            raw_class.strip().lower()
            if isinstance(raw_class, str)
            and raw_class.strip().lower() in REJECTION_CLASSES
            else UNCLASSIFIED_REJECTION
        )
        reasons.setdefault(
            raw_reference.strip().upper(),
            CandidateRejection(reason, rejection_class),
        )
    return reasons


def validate_blocking_comment_ids(
    structured_output: Any,
    findings: list[Finding],
    approve: bool,
    prior_comments: tuple[PriorReviewComment, ...],
) -> tuple[str, ...]:
    if not isinstance(structured_output, dict):
        raise ReviewError("Review engine did not return a JSON object")
    raw_ids = structured_output.get("blocking_comment_ids", [])
    if not isinstance(raw_ids, list):
        raise ReviewError("Review engine blocking_comment_ids must be an array")

    references: list[str] = []
    for raw_id in raw_ids:
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ReviewError(
                "Every review engine blocking comment ID must be a non-empty string"
            )
        # The prompt supplies short references, so case is the only variation
        # worth accepting; anything else has to match what was given.
        reference = raw_id.strip().upper()
        if len(reference) > 200:
            raise ReviewError(
                "Review engine blocking comment ID exceeds 200 characters"
            )
        if reference in references:
            raise ReviewError(f"Duplicate blocking comment ID: {reference}")
        references.append(reference)

    known_comments = prior_comment_refs(prior_comments)
    blocking_ids: list[str] = []
    for reference in references:
        comment = known_comments.get(reference)
        if comment is None:
            supplied = ", ".join(known_comments) or "none"
            raise ReviewError(
                "Blocking comment reference is not one of the prior comments "
                f"supplied: {reference}; supplied references: {supplied}"
            )
        if comment.resolved is True:
            raise ReviewError(
                f"Blocking comment reference is already resolved: {reference}"
            )
        blocking_ids.append(comment.comment_id)

    if approve and blocking_ids:
        raise ReviewError("An approval cannot contain blocking comment IDs")
    if not approve and not findings and not blocking_ids:
        raise ReviewError(
            "Withholding approval without new findings requires at least one "
            "currently unresolved blocking comment ID"
        )
    return tuple(blocking_ids)
