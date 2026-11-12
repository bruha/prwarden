import json
import os
import re
import shlex
import shutil
from typing import Any, Optional, Sequence

from . import commands
from .comment_template import CommentTemplate
from .commands import CommandError
from .models import (
    DesignFinding,
    DocsFinding,
    Finding,
    LinkedIssue,
    PriorReviewComment,
    PullRequest,
    PullRequestCheck,
    PullRequestSnapshot,
    Repository,
)
from .reviewer_identity import (
    format_engine_display_name,
    format_model_display_name,
    format_review_attribution,
    format_reviewer_identity,
)


REVIEW_COMMENT_MARKER = "<!-- prwarden -->"
# Before 1.0.0 this tool was called AI Code Reviewer, and every comment it posted then
# carries that name in its marker. Wherever a marker is read the old one is read too,
# so a pull request reviewed under the old name keeps its history: its earlier notes
# are read back rather than repeated, its earlier LGTM comments can still be hidden,
# and its delivered findings still count as this reviewer's.
MARKER_NAME = "prwarden"
LEGACY_MARKER_NAME = "ai-code-reviewer"
# A commit is 40 hex characters, or 64 once a repository moves to SHA-256. Anything
# else reaching git as an object name is not a commit, and git would read some of it
# as a revision expression or an option instead.
OBJECT_NAME_PATTERN = re.compile(r"\A[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
MAX_PRIOR_REVIEW_BYTES = 100_000
MAX_GITHUB_CHANGED_FILES = 3_000
# A token the operator supplied through the environment is already a deliberate
# choice of account, and the GitHub CLI prefers it over every stored login.
GITHUB_TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN")

# The GitHub CLI acts as whichever of its logged-in accounts is active, so switching
# accounts for unrelated work silently changes who this reviewer reads and posts as.
# A scheduled run made while another account was active failed on every repository
# with "Could not resolve to a Repository". Once pinned, every call in this module
# carries the reviewer's own token, and only this module's calls do: the review
# engines are started with a scrubbed environment that never includes it.
_pinned_account: "dict[str, str]" = {}
# A repository can name its own account, for a private repository its owner alone
# can see. Each such account is pinned the same way, keyed by its login folded to
# one case, and a call for that repository carries that token instead. A login
# that is missing is kept as the reason, so that repository fails and the others
# are still reviewed.
_repository_accounts: "dict[str, dict[str, str]]" = {}
_repository_account_failures: "dict[str, str]" = {}


def marker_variants(marker: str) -> "tuple[str, ...]":
    """Return a comment marker and the one it replaced when this tool was renamed."""

    legacy = marker.replace(f"<!-- {MARKER_NAME}", f"<!-- {LEGACY_MARKER_NAME}", 1)
    return (marker,) if legacy == marker else (marker, legacy)


def carries_marker(body: str, marker: str) -> bool:
    """Return whether a comment carries a marker, under either name of this tool."""

    return any(variant in body for variant in marker_variants(marker))


def _account_token(username: str, role: str) -> str:
    """Return the GitHub CLI's stored token for ``username``, or refuse."""

    try:
        token = commands.run_cmd(
            ["gh", "auth", "token", "--user", username],
            timeout_seconds=30,
        )
    except CommandError as error:
        raise CommandError(
            f"GitHub CLI has no login for {username}, {role}; log it "
            f"in with `gh auth login`, or set GH_TOKEN for it: {error}",
            command="gh",
        ) from error
    if not token:
        raise CommandError(
            f"GitHub CLI returned an empty token for {username}, {role}; "
            "log it in again with `gh auth login`",
            command="gh",
        )
    return token


def pin_github_account(
    username: str, repositories: "Sequence[Repository]" = ()
) -> None:
    """Make every GitHub CLI call in this module act as ``username``.

    The token comes from the GitHub CLI's own stored login for that account, so
    nothing new is stored or configured. An account the CLI holds no login for is a
    configuration error raised here, at startup, because the alternative is reading
    and posting as whichever other account happens to be active.

    A repository in ``repositories`` that names its own account is pinned to that
    account instead, even when the operator set a token in the environment: the
    repository's entry names an account for that repository alone, which is the
    more specific choice. A missing login for it is not raised here. Only that
    repository's calls fail, with the reason, and the others are still reviewed.
    """

    _pinned_account.clear()
    _repository_accounts.clear()
    _repository_account_failures.clear()
    if not any(os.environ.get(name) for name in GITHUB_TOKEN_VARIABLES):
        _pinned_account["GH_TOKEN"] = _account_token(
            username, "the reviewer account"
        )
    accounts = {
        repository.github_account.casefold(): repository.github_account
        for repository in repositories
        if repository.github_account
    }
    for key, account in sorted(accounts.items()):
        try:
            token = _account_token(account, "the account a repository is read as")
        except CommandError as error:
            _repository_account_failures[key] = str(error)
        else:
            _repository_accounts[key] = {"GH_TOKEN": token}


def reviewer_account(repository: Repository, reviewer_username: str) -> str:
    """Return the account that reads, reviews, and posts for ``repository``."""

    return repository.github_account or reviewer_username


def _run_for(repository: Repository, args: Sequence[str], **options: Any) -> str:
    """Run one GitHub CLI call for ``repository``, as the account it is read as.

    A repository whose account was never pinned is refused rather than read as the
    reviewer, because the account it names is the only one configured for it.
    """

    account = repository.github_account
    if account:
        key = account.casefold()
        if key in _repository_account_failures:
            raise CommandError(_repository_account_failures[key], command="gh")
        if key not in _repository_accounts:
            raise CommandError(
                f"{repository.slug} is read as {account}, and no login for that "
                "account was pinned before this GitHub call",
                command="gh",
            )
        environment = dict(options.get("environment") or {})
        environment.update(_repository_accounts[key])
        options["environment"] = environment
    return run_cmd(args, **options)


def run_git_for(
    repository: Repository, git_args: Sequence[str], **options: Any
) -> str:
    """Run one git command that reaches GitHub for a clone PR Warden manages.

    Such a clone has no credentials of its own. Git asks GitHub CLI for them, and
    the token it is given is the one every GitHub call for this repository carries,
    so the clone is read as the same account. Git's other credential helpers are
    switched off for the command, so the token is never written to a keychain, and
    git never prompts, because a scheduled run has nobody to answer.
    """

    gh = shutil.which("gh") or "gh"
    helper = f"!{shlex.quote(gh)} auth git-credential"
    environment = dict(options.get("environment") or {})
    environment["GIT_TERMINAL_PROMPT"] = "0"
    options["environment"] = environment
    return _run_for(
        repository,
        [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            f"credential.https://github.com.helper={helper}",
            *git_args,
        ],
        **options,
    )


def run_cmd(args: Sequence[str], **options: Any) -> str:
    """Run one GitHub CLI call, as the pinned reviewer account once there is one.

    A call that already carries a token keeps it: that is a repository read as its
    own account, and the reviewer's token must not replace it.
    """

    if _pinned_account:
        environment = dict(_pinned_account)
        environment.update(options.get("environment") or {})
        options["environment"] = environment
    return commands.run_cmd(args, **options)


REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          isResolved
          path
          line
          comments(first: 100) {
            pageInfo {
              hasNextPage
            }
            nodes {
              id
              databaseId
              body
              author {
                login
              }
            }
          }
        }
      }
    }
  }
}
"""


def _parse_json(output: str, description: str) -> Any:
    try:
        return json.loads(output)
    except json.JSONDecodeError as error:
        raise CommandError(
            f"Invalid JSON returned while {description}: {error}"
        ) from error


def _flatten_pages(output: str, description: str) -> list[dict[str, Any]]:
    pages = _parse_json(output, description)
    if not isinstance(pages, list):
        raise CommandError(f"Unexpected paginated response while {description}")

    records: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, list):
            raise CommandError(f"Unexpected page while {description}")
        records.extend(record for record in page if isinstance(record, dict))
    return records


def list_open_prs(
    repository: Repository,
    excluded_labels: frozenset[str] = frozenset(),
) -> list[PullRequest]:
    normalized_excluded_labels = {label.strip().casefold() for label in excluded_labels}
    output = _run_for(
        repository,
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repository.slug,
            "--state",
            "open",
            "--limit",
            "1000",
            "--json",
            "number,title,body,author,headRefOid,url,isDraft,labels,"
            "headRefName,baseRefName",
        ],
        retry_transient=True,
    )
    payload = _parse_json(output, f"listing pull requests for {repository.slug}")
    if not isinstance(payload, list):
        raise CommandError(f"Unexpected pull-request response for {repository.slug}")

    pull_requests: list[PullRequest] = []
    for item in payload:
        if not isinstance(item, dict):
            raise CommandError(f"Unexpected pull-request item for {repository.slug}")
        is_draft = item.get("isDraft")
        if not isinstance(is_draft, bool):
            raise CommandError(
                f"GitHub omitted draft status for {repository.slug} pull request"
            )
        labels = item.get("labels")
        if not isinstance(labels, list):
            raise CommandError(
                f"GitHub omitted labels for {repository.slug} pull request"
            )
        label_names: set[str] = set()
        for label in labels:
            if not isinstance(label, dict) or not isinstance(label.get("name"), str):
                raise CommandError(
                    f"GitHub returned an invalid label for "
                    f"{repository.slug} pull request"
                )
            label_names.add(label["name"].strip().casefold())
        if is_draft or label_names.intersection(normalized_excluded_labels):
            continue
        author = item.get("author") or {}
        pull_requests.append(
            PullRequest(
                number=int(item["number"]),
                title=str(item.get("title") or ""),
                body=str(item.get("body") or ""),
                author=str(author.get("login") or "unknown"),
                head_sha=validate_object_name(
                    str(item["headRefOid"]),
                    f"{repository.slug}#{item['number']} head",
                ),
                url=str(item.get("url") or ""),
                head_ref=str(item.get("headRefName") or ""),
                base_ref=str(item.get("baseRefName") or ""),
            )
        )
    return pull_requests


def list_reviews(
    repository: Repository, pull_request: PullRequest
) -> list[dict[str, Any]]:
    output = _run_for(
        repository,
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository.slug}/pulls/{pull_request.number}/reviews?per_page=100",
        ],
        retry_transient=True,
    )
    return _flatten_pages(
        output,
        f"listing reviews for {repository.slug}#{pull_request.number}",
    )


def _list_pull_request_records(
    repository: Repository,
    pull_request: PullRequest,
    endpoint: str,
    description: str,
) -> list[dict[str, Any]]:
    output = _run_for(
        repository,
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository.slug}/{endpoint}?per_page=100",
        ],
        timeout_seconds=120,
        retry_transient=True,
    )
    return _flatten_pages(
        output,
        f"listing {description} for {repository.slug}#{pull_request.number}",
    )


def _list_review_thread_comments(
    repository: Repository,
    pull_request: PullRequest,
) -> list[dict[str, Any]]:
    try:
        owner, name = repository.slug.split("/", 1)
    except ValueError as error:
        raise CommandError(
            f"Invalid GitHub repository slug: {repository.slug}"
        ) from error

    records: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    while True:
        arguments = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={REVIEW_THREADS_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={pull_request.number}",
        ]
        if cursor is not None:
            arguments.extend(["-f", f"cursor={cursor}"])
        output = _run_for(
            repository, arguments, timeout_seconds=120, retry_transient=True
        )
        payload = _parse_json(
            output,
            f"listing review threads for {repository.slug}#{pull_request.number}",
        )
        try:
            connection = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
            threads = connection["nodes"]
            page_info = connection["pageInfo"]
        except (KeyError, TypeError) as error:
            raise CommandError(
                f"GitHub returned invalid review threads for "
                f"{repository.slug}#{pull_request.number}"
            ) from error
        if not isinstance(threads, list) or not isinstance(page_info, dict):
            raise CommandError(
                f"GitHub returned invalid review threads for "
                f"{repository.slug}#{pull_request.number}"
            )

        for thread in threads:
            if not isinstance(thread, dict):
                continue
            resolved = thread.get("isResolved") is True
            comments = thread.get("comments")
            if not isinstance(comments, dict):
                raise CommandError("GitHub returned a review thread without comments")
            comment_page_info = comments.get("pageInfo")
            if not isinstance(comment_page_info, dict):
                raise CommandError("GitHub returned invalid review-thread pagination")
            if comment_page_info.get("hasNextPage") is True:
                raise CommandError(
                    "A review thread exceeds GitHub's 100-comment response limit"
                )
            nodes = comments.get("nodes")
            if not isinstance(nodes, list):
                raise CommandError("GitHub returned invalid review-thread comments")
            for comment in nodes:
                if not isinstance(comment, dict):
                    continue
                author = comment.get("author") or {}
                records.append(
                    {
                        "id": comment.get("databaseId") or comment.get("id"),
                        "user": {"login": author.get("login")},
                        "body": comment.get("body"),
                        "path": thread.get("path"),
                        "line": thread.get("line"),
                        "resolved": resolved,
                    }
                )

        if page_info.get("hasNextPage") is not True:
            return records
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise CommandError("GitHub omitted the next review-thread cursor")
        cursor = next_cursor


def _prior_comment(
    record: dict[str, Any],
    kind: str,
    reviewer_username: str,
) -> Optional[PriorReviewComment]:
    user = record.get("user") or {}
    author = str(user.get("login") or "unknown")
    if author.casefold() == reviewer_username.casefold():
        return None
    body = record.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    if kind in {"review", "conversation"} and author.casefold().endswith("[bot]"):
        return None
    record_id = record.get("id")
    if record_id is None:
        raise CommandError(f"GitHub returned a {kind} without an ID")
    path = record.get("path")
    if not isinstance(path, str) or not path:
        path = None
    line = record.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        line = record.get("original_line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        line = None
    return PriorReviewComment(
        comment_id=f"{kind}:{record_id}",
        kind=kind,
        author=author,
        body=body.strip(),
        path=path,
        line=line,
        resolved=record.get("resolved") if kind == "inline" else None,
    )


def list_prior_review_comments(
    repository: Repository,
    pull_request: PullRequest,
    reviewer_username: str,
) -> tuple[PriorReviewComment, ...]:
    sources = (
        (list_reviews(repository, pull_request), "review"),
        (
            _list_review_thread_comments(repository, pull_request),
            "inline",
        ),
        (
            _list_pull_request_records(
                repository,
                pull_request,
                f"issues/{pull_request.number}/comments",
                "pull-request conversation comments",
            ),
            "conversation",
        ),
    )
    comments = tuple(
        comment
        for records, kind in sources
        for record in records
        if (comment := _prior_comment(record, kind, reviewer_username)) is not None
    )
    total_bytes = sum(len(comment.body.encode("utf-8")) for comment in comments)
    if total_bytes > MAX_PRIOR_REVIEW_BYTES:
        raise CommandError(
            "Existing pull-request feedback exceeds the safe review-context limit "
            f"({MAX_PRIOR_REVIEW_BYTES} bytes)"
        )
    return comments


def list_own_advisory_notes(
    repository: Repository,
    pull_request: PullRequest,
    reviewer_username: str,
    marker: str,
) -> tuple[PriorReviewComment, ...]:
    """Return the notes one advisory level already posted to this pull request.

    Prior feedback leaves out the reviewer's own comments, so a level that runs
    again, at a later head or under --force, would not know what it said before and
    could post the same note twice. Selected by author and by the level's marker
    together: the account is also a person's, and anybody can type a marker.
    """

    notes = []
    for record in _list_pull_request_records(
        repository,
        pull_request,
        f"issues/{pull_request.number}/comments",
        "pull-request conversation comments",
    ):
        author = str((record.get("user") or {}).get("login") or "")
        body = record.get("body")
        if author.casefold() != reviewer_username.casefold():
            continue
        if not isinstance(body, str) or not carries_marker(body, marker):
            continue
        record_id = record.get("id")
        if record_id is None:
            raise CommandError("GitHub returned a conversation comment without an ID")
        notes.append(
            PriorReviewComment(
                comment_id=f"conversation:{record_id}",
                kind="conversation",
                author=author,
                body=body.strip(),
                path=None,
                line=None,
                resolved=None,
            )
        )
    return tuple(notes)


def list_review_comments_by_reviewer(
    repository: Repository,
    reviewer_username: str,
) -> "list[dict[str, Any]]":
    """Return every inline finding this reviewer has posted in a repository.

    Selected by the template's own marker rather than by author. The reviewer posts
    under a human GitHub account, and that person comments too: filtering by author
    alone collected that person's own review remarks as though the reviewer had
    written them, which would have quietly poisoned any corpus built from this. The
    marker is mandatory in the comment template, so it identifies
    a delivered finding and nothing else.

    Read only to build an evaluation corpus from delivered history, never during a
    review. Replies are excluded: a thread reply is a conversation, while the top
    comment is the finding. Each record carries the commit the finding was anchored to,
    which is what makes its line number meaningful later.
    """

    marked = " or ".join(
        f"contains({json.dumps(marker)})"
        for marker in marker_variants(REVIEW_COMMENT_MARKER)
    )
    output = _run_for(
        repository,
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{repository.slug}/pulls/comments?per_page=100",
            "--jq",
            ".[] | select(.in_reply_to_id == null) "
            f"| select(.body | ({marked})) "
            "| {comment_id: .id, number: (.pull_request_url | split(\"/\") | last "
            "| tonumber), path: .path, line: (.original_line // .line), "
            "anchor: (.original_commit_id // .commit_id), created: .created_at, "
            "body: .body}",
        ],
        retry_transient=True,
        timeout_seconds=180,
    )
    records: "list[dict[str, Any]]" = []
    pull_request_details: "dict[int, dict[str, Any]]" = {}
    for entry in output.splitlines():
        if not entry.strip():
            continue
        try:
            record = json.loads(entry)
        except json.JSONDecodeError:
            continue
        number = record.get("number")
        line = record.get("line")
        if not isinstance(number, int) or not isinstance(line, int):
            continue
        if number not in pull_request_details:
            pull_request_details[number] = _pull_request_refs(repository, number)
        details = pull_request_details[number]
        if not details:
            continue
        body = record.get("body") or ""
        title = body.split("**")[1] if body.count("**") >= 2 else body[:80]
        attribution = ""
        for body_line in body.splitlines():
            if body_line.startswith("_Reviewed by ") and body_line.endswith("_"):
                attribution = body_line.strip("_").strip()
                break
        records.append(
            {
                "comment_id": record.get("comment_id"),
                "number": number,
                "path": record.get("path"),
                "line": line,
                "anchor": record.get("anchor"),
                "title": " ".join(title.split())[:120],
                # Which reviewer and which verifier produced this finding. A
                # reproduction rate across two different pipelines measures nothing.
                "attribution": attribution,
                "created": record.get("created", ""),
                "head": details["head"],
                "base_ref": details["base_ref"],
                # The commit the pull request was opened against. Once it is merged,
                # its base branch contains the reviewed commit, so asking git for the
                # merge base of the two answers with the commit itself and the case
                # would review an empty diff.
                "base_sha": details.get("base_sha", ""),
                "merged": details["merged"],
                # The pull request's own title and description, apart from the
                # finding's title above, so a replay reads what a live review read.
                "pr_title": str(details.get("title") or ""),
                "pr_body": str(details.get("body") or ""),
            }
        )
    return records


def _pull_request_refs(repository: Repository, number: int) -> "dict[str, Any]":
    """Return the head, base branch, merge state, title, and description of one
    pull request."""

    try:
        output = _run_for(
            repository,
            [
                "gh",
                "api",
                f"repos/{repository.slug}/pulls/{number}",
                "--jq",
                "{head: .head.sha, head_ref: .head.ref, base_ref: .base.ref, "
                "base_sha: .base.sha, merged: .merged, "
                "merge_commit: .merge_commit_sha, title: .title, body: .body}",
            ],
            retry_transient=True,
        )
    except CommandError:
        return {}
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def get_pull_request_refs(repository: Repository, number: int) -> "dict[str, Any]":
    """Return one pull request's head, base, merge state, and title.

    Read only to build an evaluation corpus, never during a review. An empty mapping
    means GitHub could not say, which the caller treats as not knowing.
    """

    return _pull_request_refs(repository, number)


def list_pull_request_checks(
    repository: Repository,
    pull_request: PullRequest,
) -> tuple[PullRequestCheck, ...]:
    output = _run_for(
        repository,
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository.slug}/commits/{pull_request.head_sha}/check-runs"
            "?per_page=100",
        ],
        timeout_seconds=120,
        retry_transient=True,
    )
    pages = _parse_json(
        output,
        f"listing check runs for {repository.slug}#{pull_request.number}",
    )
    if not isinstance(pages, list):
        raise CommandError("GitHub returned invalid check-run pages")
    records: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("check_runs"), list):
            raise CommandError("GitHub returned an invalid check-run page")
        records.extend(
            record for record in page["check_runs"] if isinstance(record, dict)
        )
    checks: list[PullRequestCheck] = []
    for record in records:
        name = record.get("name")
        status = record.get("status")
        conclusion = record.get("conclusion")
        if not isinstance(name, str) or not isinstance(status, str):
            raise CommandError(
                f"GitHub returned an invalid check run for "
                f"{repository.slug}#{pull_request.number}"
            )
        checks.append(
            PullRequestCheck(
                name=name,
                status=status,
                conclusion=conclusion if isinstance(conclusion, str) else None,
            )
        )
    return tuple(checks) + _list_commit_statuses(repository, pull_request)


# A commit status reports one of four states; the three settled ones read as a
# check run's conclusion, so both kinds of check are judged alike.
STATUS_CONCLUSIONS = {"success": "success", "failure": "failure", "error": "failure"}


def _list_commit_statuses(
    repository: Repository,
    pull_request: PullRequest,
) -> tuple[PullRequestCheck, ...]:
    """Return the commit statuses on the exact head, as checks.

    Many CI services, and branch protection, report through the older status API
    rather than as check runs, and GitHub shows both in a pull request's checks.
    The combined status holds the latest status of each context.
    """

    output = _run_for(
        repository,
        [
            "gh",
            "api",
            f"repos/{repository.slug}/commits/{pull_request.head_sha}/status"
            "?per_page=100",
        ],
        timeout_seconds=120,
        retry_transient=True,
    )
    payload = _parse_json(
        output,
        f"listing commit statuses for {repository.slug}#{pull_request.number}",
    )
    statuses = payload.get("statuses") if isinstance(payload, dict) else None
    if not isinstance(statuses, list):
        raise CommandError("GitHub returned an invalid combined commit status")
    checks = []
    for record in statuses:
        if not isinstance(record, dict):
            continue
        context = record.get("context")
        state = record.get("state")
        if not isinstance(context, str) or not isinstance(state, str):
            raise CommandError(
                f"GitHub returned an invalid commit status for "
                f"{repository.slug}#{pull_request.number}"
            )
        conclusion = STATUS_CONCLUSIONS.get(state)
        checks.append(
            PullRequestCheck(
                name=context,
                status="completed" if conclusion else "in_progress",
                conclusion=conclusion,
            )
        )
    return tuple(checks)


def delivered_review_findings_resolved(
    repository: Repository,
    pull_request: PullRequest,
    review_id: str,
) -> bool:
    if not review_id.isdecimal():
        return False

    output = _run_for(
        repository,
        [
            "gh",
            "api",
            f"repos/{repository.slug}/pulls/{pull_request.number}/reviews/{review_id}",
        ],
        retry_transient=True,
    )
    review = _parse_json(
        output,
        f"reading delivered review for {repository.slug}#{pull_request.number}",
    )
    if not isinstance(review, dict):
        raise CommandError(
            f"GitHub returned an invalid delivered review for "
            f"{repository.slug}#{pull_request.number}"
        )
    if review.get("state") != "COMMENTED":
        return False

    review_comments = _list_pull_request_records(
        repository,
        pull_request,
        f"pulls/{pull_request.number}/reviews/{review_id}/comments",
        "delivered review comments",
    )
    review_comment_ids = {
        str(comment["id"])
        for comment in review_comments
        if comment.get("id") is not None
    }
    if not review_comment_ids:
        return False

    unresolved_comment_ids = {
        str(comment["id"])
        for comment in _list_review_thread_comments(
            repository,
            pull_request,
        )
        if comment.get("id") is not None and comment.get("resolved") is not True
    }
    return review_comment_ids.isdisjoint(unresolved_comment_ids)


def has_current_review(
    repository: Repository,
    pull_request: PullRequest,
    reviewer_username: str,
) -> bool:
    accepted_states = {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}
    for review in list_reviews(repository, pull_request):
        user = review.get("user") or {}
        if (
            str(user.get("login") or "").casefold() == reviewer_username.casefold()
            and review.get("commit_id") == pull_request.head_sha
            and review.get("state") in accepted_states
            and review.get("submitted_at")
        ):
            return True
    return False


def validate_object_name(value: str, subject: str) -> str:
    """Return ``value`` when it is a Git object name, or refuse it.

    Every commit this tool handles arrives from GitHub and is then handed to git as an
    object name: fetched, checked out, diffed against, recorded in state. Git reads an
    object name as a revision expression, so a value that is not one does not simply
    fail to resolve — `HEAD`, `main@{1}`, a path, or a leading dash would each mean
    something, and a leading dash would mean an option.

    Nothing has ever sent such a value: the trust here is inherited from GitHub's API
    and from a checkout the operator already owns. This makes the guarantee local, so
    the boundary holds on its own rather than on an argument about who called it.
    """

    if not OBJECT_NAME_PATTERN.match(value):
        raise CommandError(
            f"{subject} is not a Git object name: {value[:64]!r}"
        )
    return value


def repository_reachable(repository: Repository) -> str:
    """Return the repository's full name as its account sees it, or raise.

    A read that fails here is the failure every review of it would meet: a login
    that cannot see a private repository, a renamed repository, or a typo.
    """

    return _run_for(
        repository,
        ["gh", "api", f"repos/{repository.slug}", "--jq", ".full_name"],
        timeout_seconds=30,
    ).strip()


def repository_size(repository: Repository) -> Optional[int]:
    """Return GitHub's estimate of a repository's size in kilobytes, if it has one."""

    try:
        output = _run_for(
            repository,
            ["gh", "api", f"repos/{repository.slug}", "--jq", ".size"],
            timeout_seconds=30,
        )
        return int(output.strip())
    except (CommandError, ValueError):
        return None


def repository_account_problem(repository: Repository) -> Optional[str]:
    """Return why the account a repository names could not be pinned, if it was not."""

    account = repository.github_account
    if not account:
        return None
    return _repository_account_failures.get(account.casefold())


# A pull request closing more than a few issues is closing a list, and each costs
# prompt room; the body is capped like a Jira work item's.
MAX_CLOSING_ISSUES = 3
MAX_CLOSING_ISSUE_BODY_CHARACTERS = 3000
CLOSING_ISSUES_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) {"
    " pullRequest(number: $number) {"
    f" closingIssuesReferences(first: {MAX_CLOSING_ISSUES}) {{"
    " nodes { number title body state repository { nameWithOwner } }"
    " } } } }"
)


def list_closing_issues(
    repository: Repository, pull_request: PullRequest
) -> "tuple[LinkedIssue, ...]":
    """Return the issues GitHub records this pull request as closing.

    GitHub links an issue when the description says `Fixes #12`, or when someone
    links it by hand, and the link may point at another repository. Only GitHub's
    own record is read, never a number guessed from the text.
    """

    owner, name = repository.slug.split("/", 1)
    output = _run_for(
        repository,
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={CLOSING_ISSUES_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={pull_request.number}",
        ],
        retry_transient=True,
    )
    payload = _parse_json(
        output, f"reading the issues {repository.slug}#{pull_request.number} closes"
    )
    try:
        nodes = payload["data"]["repository"]["pullRequest"][
            "closingIssuesReferences"
        ]["nodes"]
    except (KeyError, TypeError) as error:
        raise CommandError(
            f"GitHub returned no closing issues for {repository.slug}#"
            f"{pull_request.number}"
        ) from error
    issues = []
    for node in nodes if isinstance(nodes, list) else ():
        if not isinstance(node, dict) or not isinstance(node.get("number"), int):
            continue
        source = node.get("repository")
        slug = (
            source.get("nameWithOwner") if isinstance(source, dict) else None
        ) or repository.slug
        body = node.get("body") if isinstance(node.get("body"), str) else ""
        body = body.strip()
        if len(body) > MAX_CLOSING_ISSUE_BODY_CHARACTERS:
            body = body[: MAX_CLOSING_ISSUE_BODY_CHARACTERS - 1].rstrip() + "…"
        issues.append(
            LinkedIssue(
                source="github",
                reference=f"{slug}#{node['number']}",
                title=" ".join(str(node.get("title") or "").split()),
                state=str(node.get("state") or "").lower(),
                body=body,
            )
        )
    return tuple(issues[:MAX_CLOSING_ISSUES])


def get_branch_head_sha(repository: Repository, branch: str) -> str:
    """Return the current tip of ``branch``.

    A pull request's own ``base.sha`` is not this: GitHub records the base commit
    from when the pull request was last synchronised and does not advance it as the
    base branch moves, so it cannot tell whether a recorded conflict is stale.
    """

    output = _run_for(
        repository,
        [
            "gh",
            "api",
            f"repos/{repository.slug}/git/ref/heads/{branch}",
        ],
        retry_transient=True,
    )
    payload = _parse_json(output, f"reading {repository.slug} branch {branch}")
    object_payload = payload.get("object") if isinstance(payload, dict) else None
    sha = object_payload.get("sha") if isinstance(object_payload, dict) else None
    if not isinstance(sha, str) or not sha:
        raise CommandError(
            f"GitHub omitted the tip commit of {repository.slug} branch {branch}"
        )
    return sha


def get_pull_request_snapshot(
    repository: Repository,
    pull_request: PullRequest,
) -> PullRequestSnapshot:
    output = _run_for(
        repository,
        [
            "gh",
            "api",
            f"repos/{repository.slug}/pulls/{pull_request.number}",
        ],
        retry_transient=True,
    )
    payload = _parse_json(
        output,
        f"reading pull-request state for {repository.slug}#{pull_request.number}",
    )
    if not isinstance(payload, dict):
        raise CommandError(
            f"GitHub returned invalid pull-request state for "
            f"{repository.slug}#{pull_request.number}"
        )

    head = payload.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    state = payload.get("state")
    is_draft = payload.get("draft")
    labels = payload.get("labels")
    changed_files = payload.get("changed_files")
    mergeable = payload.get("mergeable")
    base = payload.get("base")
    base_ref = base.get("ref") if isinstance(base, dict) else None
    if not isinstance(head_sha, str) or not head_sha:
        raise CommandError("GitHub omitted the pull-request head SHA")
    validate_object_name(head_sha, f"{repository.slug}#{pull_request.number} head")
    if state not in {"open", "closed"}:
        raise CommandError("GitHub returned invalid pull-request state")
    if not isinstance(is_draft, bool):
        raise CommandError("GitHub omitted pull-request draft status")
    if not isinstance(labels, list):
        raise CommandError("GitHub omitted pull-request labels")
    if (
        not isinstance(changed_files, int)
        or isinstance(changed_files, bool)
        or changed_files < 0
    ):
        raise CommandError("GitHub returned an invalid changed-file count")
    if mergeable is not None and not isinstance(mergeable, bool):
        raise CommandError("GitHub returned invalid pull-request mergeability")
    if not isinstance(base_ref, str) or not base_ref:
        raise CommandError("GitHub omitted the pull-request base branch")

    label_names: set[str] = set()
    for label in labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise CommandError("GitHub returned an invalid pull-request label")
        label_names.add(label["name"].strip().casefold())

    return PullRequestSnapshot(
        head_sha=head_sha,
        state=state,
        is_draft=is_draft,
        labels=frozenset(label_names),
        changed_files=changed_files,
        mergeable=mergeable,
        base_ref=base_ref,
    )


def list_changed_files(
    repository: Repository,
    pull_request: PullRequest,
    expected_count: int,
) -> list[dict[str, Any]]:
    if expected_count > MAX_GITHUB_CHANGED_FILES:
        raise CommandError(
            f"Pull request changes {expected_count} files, exceeding GitHub's "
            f"{MAX_GITHUB_CHANGED_FILES:,}-file API limit"
        )
    output = _run_for(
        repository,
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository.slug}/pulls/{pull_request.number}/files?per_page=100",
        ],
        timeout_seconds=120,
        retry_transient=True,
    )
    files = _flatten_pages(
        output,
        f"listing changed files for {repository.slug}#{pull_request.number}",
    )
    if len(files) != expected_count:
        raise CommandError(
            f"GitHub returned {len(files)} changed files but pull-request metadata "
            f"reports {expected_count}; refusing a partial review"
        )
    return files


def format_github_comment(
    finding: Finding,
    review_engine: str,
    review_model: Optional[str],
    comment_template: CommentTemplate,
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
) -> str:
    def inline_code(value: str) -> str:
        longest_run = max((len(run) for run in re.findall(r"`+", value)), default=0)
        fence = "`" * (longest_run + 1)
        padding = " " if value.startswith("`") or value.endswith("`") else ""
        return f"{fence}{padding}{value}{padding}{fence}"

    evidence = "\n".join(
        f"- {inline_code(f'{item.path}:{item.line}')} — {item.relevance} "
        f"({inline_code(item.excerpt)})"
        for item in finding.evidence
    )
    rule = (
        f"**Repository rule** ({inline_code(finding.rule_source)}):\n> {finding.rule}"
        if finding.rule
        else ""
    )
    return comment_template.render(
        {
            "severity": finding.severity.upper(),
            "title": finding.title,
            "path": finding.path,
            "line": str(finding.line),
            "problem": finding.problem,
            "rule": rule,
            "trigger": finding.trigger,
            "impact": finding.impact,
            "evidence": evidence,
            "counterevidence_reason": finding.counterevidence_reason,
            "suggested_change": finding.suggestion,
            "reviewer_attribution": format_review_attribution(
                review_engine,
                review_model,
                verify_engine,
                verify_model,
            ),
            "reviewer_identity": format_reviewer_identity(
                review_engine,
                review_model,
            ),
            "engine": format_engine_display_name(review_engine),
            "model": format_model_display_name(review_model),
            "verifier_identity": format_reviewer_identity(
                verify_engine if verify_engine is not None else review_engine,
                verify_model if verify_engine is not None else review_model,
            ),
            "verify_engine": format_engine_display_name(
                verify_engine if verify_engine is not None else review_engine
            ),
            "verify_model": format_model_display_name(
                verify_model if verify_engine is not None else review_model
            ),
            "marker": REVIEW_COMMENT_MARKER,
        }
    )


DESIGN_COMMENT_MARKER = "<!-- prwarden:design -->"


def format_design_comment(
    findings: "Sequence[DesignFinding]",
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
) -> str:
    """Render advisory solution-design notes as one pull-request comment.

    The layout is fixed here rather than templated: a design finding has six prose
    fields and its useful presentation is still being learned, and a layout nobody
    has read yet is not worth a configuration surface.
    """

    def inline_code(value: str) -> str:
        longest_run = max((len(run) for run in re.findall(r"`+", value)), default=0)
        fence = "`" * (longest_run + 1)
        padding = " " if value.startswith("`") or value.endswith("`") else ""
        return f"{fence}{padding}{value}{padding}{fence}"

    blocks = []
    for finding in findings:
        citations = "\n".join(
            f"- {inline_code(f'{item.path}:{item.line}')} — {item.relevance} "
            f"({inline_code(item.excerpt)})"
            for item in finding.citations
        )
        searches = "".join(
            f"\n- {inline_code(search.pattern)} in {inline_code(search.scope)} — "
            f"{search.purpose}: "
            + (
                "matched only "
                + ", ".join(inline_code(path) for path in search.matched_files)
                if search.matched_files
                else "no match"
            )
            for search in finding.searches
        )
        blocks.append(
            f"**[{finding.severity.upper()}] {finding.title}**  \n"
            f"_{finding.category.replace('_', ' ')}_\n\n"
            f"{finding.problem}\n\n"
            f"**Consequence:** {finding.consequence}\n\n"
            f"**Not a preference because:** {finding.why_not_preference}\n\n"
            f"**Cheaper structure:** {finding.alternative}\n\n"
            f"**Citations:**\n{citations}"
            + (f"\n\n**Searches:**{searches}" if searches else "")
        )
    attribution = format_review_attribution(
        review_engine,
        review_model,
        verify_engine,
        verify_model,
    )
    return (
        "## Solution design notes\n\n"
        + "\n\n---\n\n".join(blocks)
        + f"\n\n_{attribution}._\n"
        + DESIGN_COMMENT_MARKER
        + "\n"
    )


DOCS_COMMENT_MARKER = "<!-- prwarden:docs -->"


def format_docs_comment(
    findings: "Sequence[DocsFinding]",
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
) -> str:
    """Render proposed documentation updates as one pull-request comment.

    The proposed text is fenced rather than inlined, because it is meant to be
    copied into the document and Markdown in it would otherwise render here.
    """

    def inline_code(value: str) -> str:
        longest_run = max((len(run) for run in re.findall(r"`+", value)), default=0)
        fence = "`" * (longest_run + 1)
        padding = " " if value.startswith("`") or value.endswith("`") else ""
        return f"{fence}{padding}{value}{padding}{fence}"

    def fence(value: str) -> str:
        longest_run = max(
            (len(run) for run in re.findall(r"`{3,}", value)), default=2
        )
        border = "`" * max(3, longest_run + 1)
        return f"{border}\n{value}\n{border}"

    blocks = []
    for finding in findings:
        citations = "\n".join(
            f"- {inline_code(f'{item.path}:{item.line}')} — {item.relevance} "
            f"({inline_code(item.excerpt)})"
            for item in finding.citations
        )
        audience = "coding agents" if finding.audience == "ai" else "people"
        document = inline_code(
            f"{finding.documentation.path}:{finding.documentation.line}"
        )
        update_heading = (
            f"Entries to add after {document}:"
            if finding.adds_list_entries
            else "Proposed documentation update:"
        )
        blocks.append(
            f"**[{finding.severity.upper()}] {finding.title}**  \n"
            f"_{finding.category.replace('_', ' ')}, documentation for "
            f"{audience}_\n\n"
            f"**{document} says today:** {finding.documented}\n\n"
            f"**This change makes true:** {finding.behavior}\n\n"
            f"**{update_heading}**\n{fence(finding.proposed_update)}\n\n"
            f"**Changed lines that outdated it:**\n{citations}"
        )
    attribution = format_review_attribution(
        review_engine,
        review_model,
        verify_engine,
        verify_model,
    )
    return (
        "## Documentation updates this change needs\n\n"
        + "\n\n---\n\n".join(blocks)
        + f"\n\n_{attribution}._\n"
        + DOCS_COMMENT_MARKER
        + "\n"
    )


def submit_docs_notes(
    repository: Repository,
    pull_request: PullRequest,
    body: str,
) -> str:
    """Post proposed documentation updates as one pull-request comment.

    Its own function rather than a shared one so the caller cannot post design
    notes under the documentation marker, and so the reason each comment exists
    stays readable at the call site.
    """

    return _post_conversation_comment(
        repository, pull_request, body, "documentation updates"
    )


def submit_design_notes(
    repository: Repository,
    pull_request: PullRequest,
    body: str,
) -> str:
    """Post one advisory pull-request comment and return its comment ID.

    This is a pull-request conversation comment, not a review: advisory notes must
    not carry review authority, and an approval must stay bodyless.
    """

    return _post_conversation_comment(repository, pull_request, body, "design notes")


LGTM_COMMENT_MARKER = "<!-- prwarden:lgtm -->"


def format_lgtm_comment(
    pull_request: PullRequest,
    review_engine: str,
    review_model: Optional[str],
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
    rejected_candidates: int = 0,
    idle_verifier: "tuple[Optional[str], Optional[str]]" = (None, None),
) -> str:
    """Describe a clean result on the reviewer's own pull request.

    GitHub does not let an author approve their own pull request, so where Slack is
    not used the clean result is said in a comment instead. It names the head it
    speaks for, because unlike an approval a comment is not tied to a commit, and it
    reports the candidates verification rejected for the reason the Slack message
    does: an LGTM reached by rejecting candidates is a different event.

    ``idle_verifier`` is the configured verifier when no candidate reached it. It is
    named as having had nothing to check, never as having verified, because a
    verifier that judged nothing confirmed nothing.
    """

    if rejected_candidates < 0:
        raise ValueError("Rejected candidate count must not be negative")
    lines = [
        "## LGTM",
        "",
        f"AI review found no qualifying issues at `{pull_request.head_sha[:12]}`, "
        "and GitHub reports no merge conflicts. This PR can be merged once "
        "required checks and branch protections pass.",
    ]
    if rejected_candidates:
        issue_word = "issue" if rejected_candidates == 1 else "issues"
        lines.extend(
            [
                "",
                f"Verification confirmed 0 of {rejected_candidates} candidate "
                f"{issue_word} the review raised.",
            ]
        )
    attribution = format_review_attribution(
        review_engine,
        review_model,
        verify_engine,
        verify_model,
    )
    idle_engine, idle_model = idle_verifier
    if verify_engine is None and not rejected_candidates and idle_engine is not None:
        attribution += (
            f"; it raised no candidate findings, so "
            f"{format_reviewer_identity(idle_engine, idle_model)} "
            "had nothing to verify"
        )
    lines.extend(["", f"_{attribution}._", LGTM_COMMENT_MARKER, ""])
    return "\n".join(lines)


def submit_lgtm_comment(
    repository: Repository,
    pull_request: PullRequest,
    body: str,
) -> str:
    """Post the clean result on the reviewer's own pull request as a comment.

    A conversation comment, never a review: the author cannot approve their own pull
    request, and a comment review would read as feedback waiting for an answer.
    """

    return _post_conversation_comment(repository, pull_request, body, "an LGTM")


PULL_REQUEST_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      comments(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          databaseId
          body
          isMinimized
          author {
            login
          }
        }
      }
    }
  }
}
"""
MINIMIZE_OUTDATED_COMMENT_MUTATION = """
mutation($id: ID!) {
  minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) {
    minimizedComment {
      isMinimized
    }
  }
}
"""


def _list_conversation_comment_nodes(
    repository: Repository,
    pull_request: PullRequest,
) -> "list[dict[str, Any]]":
    """Return the pull request's conversation comments with their GraphQL state.

    GraphQL rather than REST, because only GraphQL says whether a comment is
    already hidden and gives the node ID that hiding one needs.
    """

    owner, name = repository.slug.split("/", 1)
    nodes: "list[dict[str, Any]]" = []
    cursor: Optional[str] = None
    while True:
        arguments = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={PULL_REQUEST_COMMENTS_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={pull_request.number}",
        ]
        if cursor is not None:
            arguments.extend(["-f", f"cursor={cursor}"])
        output = _run_for(
            repository, arguments, timeout_seconds=120, retry_transient=True
        )
        what = f"conversation comments for {repository.slug}#{pull_request.number}"
        payload = _parse_json(output, f"listing {what}")
        try:
            connection = payload["data"]["repository"]["pullRequest"]["comments"]
            page = connection["nodes"]
            page_info = connection["pageInfo"]
        except (KeyError, TypeError) as error:
            raise CommandError(f"GitHub returned invalid {what}") from error
        if not isinstance(page, list) or not isinstance(page_info, dict):
            raise CommandError(f"GitHub returned invalid {what}")
        nodes.extend(node for node in page if isinstance(node, dict))
        if page_info.get("hasNextPage") is not True:
            return nodes
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise CommandError(f"GitHub omitted the next cursor for {what}")
        cursor = next_cursor


def hide_earlier_lgtm_comments(
    repository: Repository,
    pull_request: PullRequest,
    reviewer_username: str,
    keep_comment_id: Optional[str] = None,
) -> int:
    """Mark this reviewer's earlier LGTM comments on a pull request as outdated.

    Each push to the reviewer's own pull request earns its own result, and an LGTM
    comment left visible from an earlier head speaks for code that is gone, or
    contradicts the findings the current head received. Hidden rather than deleted:
    GitHub collapses an outdated comment and anyone can expand it again, so the
    record of which heads passed survives and the step can be undone.

    Selected by author and by the LGTM marker together, because the account is also
    a person's, and anybody can type a marker. The comment just posted is kept by
    its ID. Returns how many comments were hidden.
    """

    hidden = 0
    for node in _list_conversation_comment_nodes(repository, pull_request):
        author = str((node.get("author") or {}).get("login") or "")
        body = node.get("body")
        node_id = node.get("id")
        if author.casefold() != reviewer_username.casefold():
            continue
        if not isinstance(body, str) or not carries_marker(body, LGTM_COMMENT_MARKER):
            continue
        if node.get("isMinimized") is True:
            continue
        if keep_comment_id is not None and str(node.get("databaseId")) == str(
            keep_comment_id
        ):
            continue
        if not isinstance(node_id, str) or not node_id:
            raise CommandError("GitHub returned an LGTM comment without a node ID")
        output = _run_for(
            repository,
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={MINIMIZE_OUTDATED_COMMENT_MUTATION}",
                "-f",
                f"id={node_id}",
            ],
            timeout_seconds=60,
        )
        payload = _parse_json(
            output,
            f"hiding an LGTM comment on {repository.slug}#{pull_request.number}",
        )
        try:
            minimized = payload["data"]["minimizeComment"]["minimizedComment"]
        except (KeyError, TypeError) as error:
            raise CommandError("GitHub did not confirm the hidden comment") from error
        if not isinstance(minimized, dict) or minimized.get("isMinimized") is not True:
            raise CommandError("GitHub did not confirm the hidden comment")
        hidden += 1
    return hidden


def _post_conversation_comment(
    repository: Repository,
    pull_request: PullRequest,
    body: str,
    what: str,
) -> str:
    """Post one pull-request conversation comment and return its comment ID."""

    output = _run_for(
        repository,
        [
            "gh",
            "api",
            "--method",
            "POST",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{repository.slug}/issues/{pull_request.number}/comments",
            "--input",
            "-",
        ],
        input_text=json.dumps({"body": body}),
        timeout_seconds=120,
    )
    response = _parse_json(
        output,
        f"posting {what} for {repository.slug}#{pull_request.number}",
    )
    if not isinstance(response, dict) or not response.get("id"):
        raise CommandError(
            f"GitHub did not return a comment ID for {what} on "
            f"{repository.slug}#{pull_request.number}"
        )
    return str(response["id"])


def submit_approval(
    repository: Repository,
    pull_request: PullRequest,
) -> str:
    payload = {
        "commit_id": pull_request.head_sha,
        "event": "APPROVE",
    }
    output = _run_for(
        repository,
        [
            "gh",
            "api",
            "--method",
            "POST",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{repository.slug}/pulls/{pull_request.number}/reviews",
            "--input",
            "-",
        ],
        input_text=json.dumps(payload),
        timeout_seconds=120,
    )
    response = _parse_json(
        output,
        f"approving {repository.slug}#{pull_request.number}",
    )
    if not isinstance(response, dict) or not response.get("id"):
        raise CommandError(
            f"GitHub did not return an approval ID for "
            f"{repository.slug}#{pull_request.number}"
        )
    return str(response["id"])


def submit_review(
    repository: Repository,
    pull_request: PullRequest,
    findings: list[Finding],
    review_engine: str,
    review_model: Optional[str],
    comment_template: CommentTemplate,
    verify_engine: Optional[str] = None,
    verify_model: Optional[str] = None,
    body: Optional[str] = None,
) -> str:
    """Submit one COMMENT review carrying the confirmed findings.

    ``body`` states what the review did not cover. Inline comments alone imply the
    whole change was read, so a partial review has to say so where the author reads
    it rather than only in the operator's transcript.
    """

    pending_payload = {
        "commit_id": pull_request.head_sha,
        "comments": [
            {
                "path": finding.path,
                "line": finding.line,
                "side": "RIGHT",
                "body": format_github_comment(
                    finding,
                    review_engine,
                    review_model,
                    comment_template,
                    verify_engine,
                    verify_model,
                ),
            }
            for finding in findings
        ],
    }
    output = _run_for(
        repository,
        [
            "gh",
            "api",
            "--method",
            "POST",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{repository.slug}/pulls/{pull_request.number}/reviews",
            "--input",
            "-",
        ],
        input_text=json.dumps(pending_payload),
        timeout_seconds=120,
    )
    pending_response = _parse_json(
        output,
        f"creating pending review for {repository.slug}#{pull_request.number}",
    )
    if not isinstance(pending_response, dict) or not pending_response.get("id"):
        raise CommandError(
            f"GitHub did not return a pending review ID for "
            f"{repository.slug}#{pull_request.number}"
        )
    review_id = str(pending_response["id"])

    output = _run_for(
        repository,
        [
            "gh",
            "api",
            "--method",
            "POST",
            "-H",
            "Accept: application/vnd.github+json",
            f"repos/{repository.slug}/pulls/{pull_request.number}/reviews/"
            f"{review_id}/events",
            "--input",
            "-",
        ],
        input_text=json.dumps(
            {"event": "COMMENT", "body": body} if body else {"event": "COMMENT"}
        ),
        timeout_seconds=120,
    )
    submitted_response = _parse_json(
        output,
        f"submitting review for {repository.slug}#{pull_request.number}",
    )
    if not isinstance(submitted_response, dict) or not submitted_response.get("id"):
        raise CommandError(
            f"GitHub did not confirm submitted review for "
            f"{repository.slug}#{pull_request.number}"
        )
    return str(submitted_response["id"])
