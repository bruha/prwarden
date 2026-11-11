from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Repository:
    name: str
    slug: str
    path: Optional[Path] = None
    # Whether anything about this repository may reach the Slack webhook. A webhook
    # belongs to one workspace, and a project outside it must not appear there: the
    # reviewer's own pull requests are then reviewed on GitHub instead.
    slack: bool = True
    # The GitHub account this repository is read and reviewed as, when it is not the
    # reviewer's. A private repository can be visible to its owner alone, and the
    # alternative, adding the reviewer as a collaborator, also gives it push access.
    # Who counts as the reviewer here follows it: the account's own pull requests
    # are its own, never approved.
    github_account: Optional[str] = None
    # Whether `path` is a clone PR Warden makes and keeps itself, because the entry
    # named no local checkout. Such a clone is fetched as the account above, with
    # GitHub CLI's credentials, since it has none of its own.
    managed_checkout: bool = False
    # The Jira project keys this repository's pull requests name work items in.
    # Only these are looked up: a bare pattern also matches `UTF-8` and `ISO-8601`.
    jira_projects: "tuple[str, ...]" = ()


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    author: str
    head_sha: str
    url: str
    head_ref: str = ""
    base_ref: str = ""


@dataclass(frozen=True)
class LinkedIssue:
    """An issue a pull request names, read as the intent behind the change.

    A GitHub issue the pull request closes, or a Jira work item it names. Its text
    is untrusted: anyone who can edit the issue wrote it.
    """

    source: str
    reference: str
    title: str
    state: str = ""
    kind: str = ""
    body: str = ""


@dataclass(frozen=True)
class HistoryCommit:
    """An earlier commit on the base branch that touched a file the change edits.

    Its message often says why the code is as it is. The message is untrusted text,
    written by whoever committed, and describes an earlier change, not this one.
    """

    commit: str
    date: str
    subject: str
    body: str = ""
    paths: "tuple[str, ...]" = ()


@dataclass(frozen=True)
class PullRequestSnapshot:
    head_sha: str
    state: str
    is_draft: bool
    labels: frozenset[str]
    changed_files: int
    mergeable: Optional[bool]
    base_ref: str = ""


@dataclass(frozen=True)
class DeferredFile:
    """A changed file this review did not read.

    ``reason`` is empty when the file simply did not fit the budget, which its
    size already explains. It is set when the file could not be read at all, and
    then the size is unknown rather than zero: GitHub omits the patch of a diff it
    will not inline, so there is nothing local to measure.
    """

    path: str
    size_bytes: int
    reason: str = ""


@dataclass(frozen=True)
class DiffBudget:
    """What a change measured against what one review could read.

    Recorded for every review, so the size of a change is a fact the run reports
    rather than something an operator has to reconstruct from GitHub. When
    ``deferred`` is non-empty the review read part of the change, which withholds
    the approval: a review that did not see every line cannot certify the whole.
    """

    limit_bytes: int
    total_bytes: int
    reviewed_bytes: int
    reviewed_files: int
    deferred: "tuple[DeferredFile, ...]" = ()

    @property
    def is_partial(self) -> bool:
        return bool(self.deferred)

    @property
    def oversized(self) -> "tuple[DeferredFile, ...]":
        """Return the files that were readable but did not fit."""
        return tuple(file for file in self.deferred if not file.reason)

    @property
    def unreadable(self) -> "tuple[DeferredFile, ...]":
        """Return the files no budget could have helped with."""
        return tuple(file for file in self.deferred if file.reason)

    @property
    def exceeds_limit(self) -> bool:
        """Return whether the readable diff was itself too large.

        A change can be partial without exceeding anything, when one file's patch
        was unavailable, and reporting a ratio then would invent a cause.
        """
        return self.total_bytes > self.limit_bytes

    @property
    def total_files(self) -> int:
        return self.reviewed_files + len(self.deferred)

    @property
    def limit_ratio(self) -> float:
        """Return how many times the whole change exceeds the budget."""

        if self.limit_bytes <= 0:
            return 0.0
        return self.total_bytes / self.limit_bytes


@dataclass(frozen=True)
class FindingEvidence:
    path: str
    line: int
    excerpt: str
    relevance: str


@dataclass(frozen=True)
class Finding:
    severity: str
    title: str
    path: str
    line: int
    problem: str
    suggestion: str
    trigger: str = ""
    impact: str = ""
    evidence: tuple[FindingEvidence, ...] = ()
    counterevidence: tuple[FindingEvidence, ...] = ()
    counterevidence_reason: str = ""
    # The repository code review rule the change breaks, quoted exactly from the
    # base branch, and the file it is in; empty for a defect that breaks no rule.
    rule: str = ""
    rule_source: str = ""


@dataclass(frozen=True)
class ReviewRuleSection:
    """One "Code review" section of an agent instruction file on the base branch."""

    path: str
    heading: str
    text: str


@dataclass(frozen=True)
class ReviewRules:
    """The base branch's code review rules, which the pull request cannot change."""

    base_ref: str
    base_sha: str
    sections: tuple[ReviewRuleSection, ...] = ()
    # Whether rules beyond the prompt's budget were left out, and so not enforced.
    truncated: bool = False

    def source_of(self, quote: str) -> Optional[str]:
        """Return the file whose rules contain ``quote`` exactly, if any does."""

        for section in self.sections:
            if quote in section.text:
                return section.path
        return None


@dataclass(frozen=True)
class DesignFinding:
    """A flaw in how a change is built rather than in one of its lines.

    A design flaw is usually a property of the whole change, or of an absence, so
    it carries no single anchor line and can never become an inline comment. Its
    citations still have to resolve in the reviewed workspace: `citations` holds
    the line showing the structure and, where the claim is that the workspace
    already solves this, the existing code it is compared against.
    """

    severity: str
    title: str
    category: str
    problem: str
    consequence: str
    why_not_preference: str
    alternative: str
    citations: tuple[FindingEvidence, ...] = ()


@dataclass(frozen=True)
class RejectedDesignCandidate:
    """A design candidate the separate design verifier threw out, and why."""

    finding: DesignFinding
    reason: str


@dataclass(frozen=True)
class DesignAssessment:
    """One design level's judgment of a pull request, after verification."""

    understanding: str
    verdict: str
    findings: tuple[DesignFinding, ...] = ()
    rejected: tuple[RejectedDesignCandidate, ...] = ()
    # The engine that actually reviewed, which is the fallback's when the primary
    # failed. None means the configured primary.
    reviewer_engine: Optional[str] = None
    reviewer_model: Optional[str] = None
    # The engine that verified, set only when the verifier's fallback judged it.
    # None means the configured verifier.
    verifier_engine: Optional[str] = None
    verifier_model: Optional[str] = None


@dataclass(frozen=True)
class DocumentationFile:
    """A documentation file found in the reviewed head, and who it is written for.

    The audience comes from where the file lives rather than from the model, so a
    proposal cannot claim to be about agent instructions when it cites a README.
    """

    path: str
    size_bytes: int
    audience: str


# The documentation category for an entry a list or table lacks. The controller
# accepts it only as new entries shaped like the one it cites, which is what lets
# the verifier and the delivered comment treat it differently from a correction.
MISSING_LIST_ENTRY = "missing_list_entry"


@dataclass(frozen=True)
class DocsFinding:
    """Documentation this change leaves untrue, and the update it needs.

    This is not a defect in the change: the code may be exactly right and the
    document simply out of date. So it carries no anchor line and never becomes an
    inline comment. `documentation` cites the line that is now wrong and
    `citations` the changed lines that made it wrong, both held to the same
    exact-excerpt standard as code evidence.
    """

    severity: str
    title: str
    category: str
    audience: str
    documented: str
    behavior: str
    proposed_update: str
    documentation: FindingEvidence
    citations: tuple[FindingEvidence, ...] = ()

    @property
    def adds_list_entries(self) -> bool:
        """Whether the update is only new entries for the list it cites.

        Such a proposal holds just the rows or items to insert, the controller
        having refused anything else, so `documentation` is where they go.
        """

        return self.category == MISSING_LIST_ENTRY


@dataclass(frozen=True)
class RejectedDocsCandidate:
    """A documentation candidate the separate verifier threw out, and why."""

    finding: DocsFinding
    reason: str


@dataclass(frozen=True)
class DocsAssessment:
    """One documentation level's judgment of a pull request, after verification."""

    understanding: str
    verdict: str
    findings: tuple[DocsFinding, ...] = ()
    rejected: tuple[RejectedDocsCandidate, ...] = ()
    # The engine that actually reviewed, which is the fallback's when the primary
    # failed. None means the configured primary.
    reviewer_engine: Optional[str] = None
    reviewer_model: Optional[str] = None
    # The engine that verified, set only when the verifier's fallback judged it.
    # None means the configured verifier.
    verifier_engine: Optional[str] = None
    verifier_model: Optional[str] = None


@dataclass(frozen=True)
class PriorReviewComment:
    comment_id: str
    kind: str
    author: str
    body: str
    path: Optional[str] = None
    line: Optional[int] = None
    resolved: Optional[bool] = None


@dataclass(frozen=True)
class PullRequestCheck:
    name: str
    status: str
    conclusion: Optional[str] = None


@dataclass(frozen=True)
class RemovedFile:
    path: str
    deletions: Optional[int] = None


@dataclass(frozen=True)
class MovedFile:
    """A file GitHub reports as renamed without any content change."""

    path: str
    previous_path: Optional[str] = None


@dataclass(frozen=True)
class DependencyLink:
    """An installed dependency directory made readable inside the workspace.

    The controller links the whole directory when the reviewed head and the
    configured checkout resolve dependencies identically, so the linked contents
    are authoritative evidence for the versions the lockfile pins. When a lockfile
    differs, only packages both trees resolve to the same version are linked
    individually, and ``packages`` counts them; it is zero when the directory was
    linked whole. Either way a linked path holds the version the reviewed head
    resolves, and a package that is absent is simply not evidence.
    """

    name: str
    target: str
    lockfiles: tuple[str, ...] = ()
    packages: int = 0


@dataclass(frozen=True)
class ReviewMaterial:
    diff: str
    added_lines: dict[str, frozenset[int]]
    prior_comments: tuple[PriorReviewComment, ...] = ()
    removed_files: tuple[RemovedFile, ...] = ()
    moved_files: tuple[MovedFile, ...] = ()
    ignored_missing_patch_files: tuple[str, ...] = ()
    textless_files: tuple[str, ...] = ()
    checks: tuple[PullRequestCheck, ...] = ()
    dependency_links: tuple[DependencyLink, ...] = ()
    verification_candidates: tuple[Finding, ...] = ()
    validation_error: Optional[str] = None
    level: str = "code"
    diff_budget: Optional[DiffBudget] = None
    design_candidates: tuple[DesignFinding, ...] = ()
    max_design_findings: int = 4
    documentation_files: tuple[DocumentationFile, ...] = ()
    background: str = ""
    docs_candidates: tuple[DocsFinding, ...] = ()
    max_docs_findings: int = 3
    # Read from the base branch, so the change under review cannot write its own.
    review_rules: Optional[ReviewRules] = None
    # The issues the pull request names, as the intent behind it: GitHub issues it
    # closes and Jira work items it names. Read once per pull request and shared by
    # every level and bundle, like the background note.
    linked_issues: "tuple[LinkedIssue, ...]" = ()
    # The base branch's recent commits on the files this change edits, read once per
    # pull request and shared by every level and bundle.
    file_history: "tuple[HistoryCommit, ...]" = ()
