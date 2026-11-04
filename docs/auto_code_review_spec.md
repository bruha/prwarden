# Specification: Automated GitHub and Slack Code Review Workflow

## Objective

Periodically inspect ready-for-review open pull requests in user-configured
GitHub repositories, review each eligible head commit through a configured
review-engine adapter, and route confirmed correctness or security findings:

- Team pull requests with findings: one GitHub review containing inline comments.
- Team pull requests without findings: one `APPROVE` review without a body;
  `LGTM` is terminal output only.
- Configured reviewer's pull requests: one private Slack message.
- Configured reviewer's pull requests without findings: after final head and
  mergeability checks, send an `LGTM` Slack message stating that the PR can be
  merged once required checks and branch protections pass. Never approve it on
  GitHub because authors cannot approve their own pull requests.
- Configured reviewer's pull requests in a repository with `"slack": false`: the
  same results on the pull request instead, because the webhook belongs to a
  workspace that project is not part of. Findings are one GitHub review with inline
  comments and event `COMMENT`. A clean result, after the same checks, is one
  pull-request comment that says `LGTM`, names the reviewed head, and attributes
  the review and its verification. It is never an approval, and these results are
  recorded under their own `github-own` destination, apart from a teammate's
  `github` review of the same head. After either result for a later head is
  delivered, the reviewer's earlier `LGTM` comments on that pull request are
  minimized as `OUTDATED` through GraphQL: an earlier one speaks for code that is
  gone, or contradicts the findings the new head received. They are hidden rather
  than deleted, so anyone can expand them and the record of which heads passed
  survives. Only comments by the reviewing account that carry the `LGTM` marker are
  touched, never the one just posted, never one already hidden, and never a
  comment the same person typed by hand. Hiding runs after delivery, so the pull
  request is never left without a verdict, and a failure to hide is reported and
  left alone: the current result is already delivered and recorded, and the write
  is not retried blindly.

## Configuration

Private configuration remains outside Git:

- `.env`: delivery gates, engine selection, Slack secret, and limits.
- `repositories.json`: GitHub slugs and matching local Git checkout paths used
  to fetch immutable PR head commits.
- `.state/review-state.json`: delivery idempotency records.

These live in the configuration and state directories resolved at startup by
`prwarden/paths.py`, before `.env` is read. `PRWARDEN_HOME`, when set, must be
absolute and holds both, laid out as above. Otherwise a source checkout, recognised
by `repositories.example.json` beside the package, keeps them in its own root as
listed. An installed copy has no such root and uses `~/.config/prwarden` for
configuration and `~/.local/state/prwarden` for state, or the absolute
`XDG_CONFIG_HOME` and `XDG_STATE_HOME` directories when set; it never writes beside
the installed package. Relative paths in `.env` resolve from the configuration
directory. The default GitHub comment template ships in `prwarden/templates/`, and
a setting that still names the pre-2.1.0 `templates/` defaults selects them.

Safe templates are committed as `.env.example` and
`repositories.example.json`. The configured reviewer identity is the current
authenticated GitHub CLI user, resolved once at startup from
`GITHUB_REVIEWER_USERNAME` when set, otherwise from the REST current-user
endpoint with the GraphQL `viewer` query as a fallback. Startup fails only when
every source fails, and the resulting error names each attempt.

Every GitHub CLI call then acts as that identity, not as whichever of the CLI's
logged-in accounts happens to be active. At startup the controller takes the
reviewer's token from the CLI's own stored login with `gh auth token --user` and
passes it as `GH_TOKEN` to its GitHub CLI calls only; the review engines run with a
scrubbed environment that never receives it. A run started while another account
was active otherwise failed on every repository, and with an account that could
see the repositories it would have read and posted as that account instead. A
reviewer the CLI holds no login for stops the run at startup with an error naming
the account. A `GH_TOKEN` or `GITHUB_TOKEN` already in the environment is the
operator's explicit choice and is left in charge.

A repository entry may name its own `github_account`, for a private repository that
only its owner can see. The reviewer could instead be added as a collaborator, but
that also grants push access. Its token is taken the same way at startup and carried
by every call for that repository, including when the operator set a token in the
environment, because the entry names an account for that repository alone. That
account is then the reviewer identity for the repository. Its own pull requests
take the own-pull-request routes, and nothing it authored is ever approved; anyone
else's, the global reviewer's included, are reviewed and approved as that account.
A missing login for it does not stop the run. It is kept as that repository's
failure, which every call for the repository raises, and the other repositories
are still reviewed. A repository whose account was never pinned is refused rather
than read as the reviewer. Authors are compared with the account without regard to
case, as GitHub compares logins, so a case slip in the entry cannot turn the
account's own pull request into a teammate's, which is the route that approves.
`python3 -m prwarden.public_safety refresh-terms` reads such a repository as its account
too, because the active login cannot list it.

## Review Scheduling

Discovery completes for every configured repository before the first review
starts, so the run reports the whole plan up front and draws work from all
repositories rather than emptying one at a time.

`REVIEW_CONCURRENCY`, overridable with `--concurrency`, bounds how many pull
requests are reviewed at the same time between 1 and 8, defaulting to 2. The
bound exists because capacity, not local CPU, limits a run.

Concurrency changes scheduling only. Every guarantee elsewhere in this
specification continues to hold per pull request:

- Each review fetches its own immutable head into its own detached worktree.
- Git operations against one shared local checkout are serialized, because Git
  refuses concurrent index, ref, and worktree updates in one repository.
- Delivery state remains atomic: the in-memory records are serialized in the
  process and the file lock still excludes other processes.
- Each engine invocation that needs per-review configuration receives its own,
  so one review's permissions cannot narrow another's.

Reviews running together record their own output instead of writing to the
terminal, and the recordings print in the planned order after the last review
finishes. A run with a single review, or `REVIEW_CONCURRENCY=1`, reports each
step as it happens.

The board is drawn by moving the cursor over the rows it already wrote, so no
row may reach the last column and the whole board must stay on screen: rows are
built for the current terminal size, and rows a short window cannot show are
replaced by a single summary row. An interrupt stops the reviews with the run,
because the review commands share its process group, and the recordings are
reported before the run exits with the signalled exit code.

An in-place progress line and the review board write frames to the terminal
itself, past any transcript or capture wrapper, but decide whether to render at
all from the stream the caller holds. A review whose output is being captured
therefore draws nothing and leaves the live row to the board, while the same
phase in a one-at-a-time run animates as usual.

Every run copies its reported output to a transcript file beside the state file
and deletes transcripts older than a configured number of days, so retention is
bounded by age rather than by how often the tool ran. Runs can overlap, because only
a live run takes the process lock, so a transcript name identifies the process as
well as the moment it started and is created exclusively: a run steps past a taken
name rather than truncating another run's record. It is created readable only by
its owner whatever umask the run inherited, because it carries repository names,
pull-request titles, and finding text. The transcript holds reported
text only: progress rendering resolves the terminal behind the copy, so redrawn
frames stay out of the file.

A parallel run reports its steps twice over, to two different readers. The board
owns the terminal while reviews run and each review's report is held until every
review finishes, so the transcript would otherwise show a run's plan and nothing
else until the last review ended, leaving a run in progress diagnosable only from
worktree timestamps and child processes. So each recorded step is also journaled
to the transcript as it happens, labeled with its pull request and stamped with
the time it was reported, and flushed line by line so the file can be followed
while the run continues. The journal never reaches the terminal, which the board
is drawing, and the grouped per-pull-request reports still close the file.

Each candidate the verification pass rejected is reported on the terminal and in
the run transcript with its severity, location, claim, trigger, and the reason the
verifier gave for that candidate. The response schema requires one reason per
candidate the pass does not confirm, because the approval reason explains the
verdict and is frequently about a different subject. Those reasons are diagnostic:
an unusable or unmatched entry is dropped rather than failing an otherwise sound
review, and a candidate whose reason is missing is reported as unexplained. A rejected candidate is unconfirmed by definition, so it is never
delivered to GitHub or Slack, and the report exists to make the verification pass
auditable rather than to raise the defect.

A clean Slack result reports the number of candidate findings the verification
pass rejected, when there was at least one. The count describes work the pipeline
performed and never implies the rejected candidates were defects; a review that
raised nothing reports no count.

A recording keeps both output streams in the order the review reported them, and
replays each line to the stream it came from. Because a scheduled run usually
redirects both streams into one log file, every line is flushed as it is
replayed, so a retry notice stays inside its own pull request's block instead of
drifting to the top of the log.

## Eligibility and Idempotency

A pull-request head is skipped when either condition is true:

1. The configured GitHub reviewer already submitted a review for the current
   head commit, unless it is a delivered comment review whose inline findings
   have all been resolved.
2. JSON state contains a terminal record for the code level's tuple
   `(repository, PR number, head SHA, destination)`. The advisory design level is
   keyed by pull request instead, as described under its own heading below. An `approval_withheld` record is skipped only while
   at least one controller-validated blocking comment remains unresolved and its
   feedback fingerprint remains unchanged. A `delivered` comment
   review is skipped only while at least one exact inline finding from its
   review receipt remains unresolved.

Terminal JSON states are `delivered`, `no_findings`, `approval_withheld`,
`delivery_uncertain`, and `skipped_oversized`. `approval_withheld` means there
was no new finding to post but an existing qualifying issue still blocks
approval. It stores the engine's reason and is reconsidered when review feedback
changes without a new commit, when every recorded blocker is resolved, or when a
legacy record has no validated blocker IDs. Resolved and unresolved inline
review threads are included in engine context, and each thread's resolution
state participates in the feedback fingerprint. Resolved feedback provides
duplicate, intent, and counterevidence context but does not itself block
approval. A delivered GitHub approval remains terminal, while a delivered
comment review is reconsidered after all of its inline findings are resolved.
`skipped_oversized` means not one changed file fitted
`MAX_DIFF_BYTES`, so nothing could be reviewed, and a manual-review Slack
notification was confirmed; it does not fail the run and requires `--force` to
reconsider the same head after configuration or implementation changes. The request
for a human is recorded separately under a pull-request-scoped `oversized` key as
terminal `oversized_notified`, so pushing to a pull request that is too large does
not send the same message again; a request that could not be sent is nonterminal
`oversized_notification_pending` on that key, and the head that needed it stays
nonterminal too rather than settling without it. In a repository kept out of Slack
the request has nowhere to go, so it is recorded as terminal `oversized_not_notified`
and appears only in the run log. Failed review generation is retryable. An uncertain
review delivery requires an explicit forced retry after manual destination
inspection. Nonterminal `superseded` records that an attempt was safely
abandoned because GitHub reported a newer head SHA; the newer SHA has its own
state key. If a selected pull request becomes a draft or gains an
excluded label before delivery, the attempt is safely skipped, recorded as
nonterminal `skipped_ineligible`, and omitted from the run error count. No
review, approval, or Slack finding is delivered for that attempt, and the same
head can be reconsidered if it later becomes eligible again. A draft or a label is
the author asking for no review, which delivery respects.

A pull request that closes or merges before delivery is treated differently,
because closing ends the approval decision and nothing else. Confirmed findings are
still delivered, as inline comments or to Slack, and recorded `delivered` with
`closed_during_review`; the approval or the clean-result `LGTM` is not sent, and a
review that confirmed nothing is skipped as `skipped_ineligible` exactly as before.
A merge does not unmake a defect: the finding is already verified against the exact
head, that code is now in the base branch, and discarding it would lose the whole
review to timing. This is the one eligibility change that still delivers.

The state file is atomically replaced, and a process lock prevents overlapping
runs using the same state path from racing. This does not coordinate independent
hosts unless they share a locking-capable filesystem.

Losing that lock to another live run is a safe skip reported on the terminal with
exit code `0` and no failure notification. The lock did its job, the run holding it
covers the same work, and an operator who started a manual run does not need to be
told their scheduled run stood down for it. Every other way the state file can fail
remains an error.

### Changes too large to review completely

The complete diff is measured before anything is judged too large, and the
measurement is recorded on the review material as a diff budget: the limit, the total
bytes, the bytes and files reviewed, and every deferred file with its size. A change
within the budget defers nothing.

A change over the budget is reviewed in part rather than skipped, because a pull
request nobody reviewed catches no defects at all. The budget is filled largest
changed file first: the most changed lines carry the most risk, and the tail of a
large pull request is mostly one-line edits. A file that cannot fit the remaining
budget is skipped rather than ending the fill, so one very large file does not defer
everything smaller than itself.

A change over the budget is first read in bundles, up to `MAX_REVIEW_BUNDLES`. Each
bundle is a complete review of whole files that fit the budget, and the files no bundle
had room for stay deferred exactly as a single review would have left them. Bundling is
confined to the oversized path: a change within the budget produces one bundle, which is
byte for byte the material a single review received, and `MAX_REVIEW_BUNDLES=1` disables
bundling entirely. This matters for about one pull request in a hundred, measured across
394, which is the reason it changes nothing outside that path.

A change that fits the budget is not split into smaller bundles, because doing so was
measured and did not pay. Sixteen pull requests of 60 to 200 KB were each reviewed
twice both ways, and every confirmed finding was judged by hand. Reading them in 60 KB
bundles found 18 real defects where one review found 16, at 1.75 times the credits. The
two arms also swapped places between passes, 4 against 12 and then 12 against 6, so the
difference is smaller than the reviewer's own spread from one pass to the next.

Four rules keep bundling honest. A file is never split across bundles, because a
reviewer shown half a function would cite lines nobody can reproduce; a file too large
for the budget alone is named and left unread. Bundles hold disjoint files, so no two
bundles can raise a finding on the same line and no cross-bundle duplicate handling is
required. Each bundle's findings are validated against that bundle's own added lines, so
a bundle cannot raise a finding on a file it did not read. And a bundle speaks for its
own files only: it may add findings, and confirmed findings in any bundle withhold the
approval, but no bundle may grant one, because an approval speaks for the whole pull
request. A change every bundle covered is a complete review and may be approved; a
change with anything left over remains a partial review under the rules below.

A partial review is constrained in four ways, and all four are required for it to be
honest. Deferred files are absent from the added-line index, so no finding can be
raised against a file the reviewer never read. Both levels are told which files were
withheld and that their absence proves nothing, so neither can conclude that a
caller, definition, or test is missing when it was simply not shown. The approval is
withheld whatever the reviewer concluded, because a review that did not read every
line cannot certify the whole change. And the delivered GitHub review carries a body
stating what it did not read, because inline comments alone imply the whole change
was read.

A pull request where not one changed file fits the budget is not reviewed at all and
is recorded `skipped_oversized`, with the measurement in the reported reason.

## Review Levels

A review runs in levels. Each level is one reviewer invocation followed by one
adversarial verifier invocation, and the levels run sequentially. Every level is
reviewed by `REVIEW_ENGINE` at `REVIEW_MODEL` and verified by
`REVIEW_VERIFY_ENGINE` at `REVIEW_VERIFY_MODEL`, so a level is defined by its
prompt, response schema, validation, and delivery route, never by its own provider
settings. Reasoning effort is the one setting a level may have of its own:
`REVIEW_CODE_EFFORT` overrides `REVIEW_EFFORT` for the code level, which alone can
approve, and the verifier and fallback each run at their own effort on every level.
The adapters are unaware of levels: the level travels on the review
material they are handed, and prompt, schema, and effort selection stays in the
provider-neutral core.

The **solution-design level** runs first when `REVIEW_DESIGN_LEVEL` is enabled. It
judges the approach: where the change puts responsibility, what contracts it
establishes, how state and data flow, which failure modes the structure gives no
place to handle, and whether this workspace already provides what the change
re-implements. A design finding must name a consequence that is reachable in this
workspace and a cost that does not depend on taste, and it must cite the line that
shows the structure. It carries no anchor line, which is what lets it describe a
whole change and also why it can never become an inline comment. At most
`MAX_DESIGN_FINDINGS` are reported, deliberately fewer than the code level allows.

The level runs on a pull request whose diff was too large to read completely, since
the shape of a change is what nobody can read all of, and it does not run at all on a
release promotion. `DESIGN_EXCLUDED_HEAD_BRANCHES` holds case-insensitive globs
matched against the whole head branch name, `develop` and `release/*` by default: a
promotion carries no new design, because every change in it was designed and reviewed
on the feature pull request that put it on the promoted branch. The match is on whole
names, so a topic branch called `feat/developer-docs` is still reviewed.

Its delivery state is keyed by pull request rather than by head, so the level runs
at most once per pull request. The code level is keyed by head because a new head is
a new set of lines to judge; the design level reads the shape of the whole change,
where a second pass on a later head mostly repeats itself at full cost and leaves a
second advisory comment beside the first. Any head counts as settled, which makes
the keying change safe on state written before it: a pull request that already
received design notes under a head key cannot receive them a second time. A settled
level is reported and skipped, a failed one is attempted again, and `--force`
reconsiders a named pull request.

Prior feedback never includes the reviewer's own comments, so a level that runs
again, under `--force` here or after a documentation edit at the documentation
level, would not know what it already said. Such a run is therefore also handed the
level's own earlier notes, those posted by the reviewing account that carry that
level's marker, both to the review and to its verifier, within the same byte limit
as prior feedback. A first run spends no lookup on notes it cannot have posted,
unless the level's state shows a delivery that started and never recorded its
outcome. The code level never receives them, because a note it could cite would
then be able to withhold an approval. A failed lookup fails that level alone. Notes
delivered to Slack cannot be read back, so on a repository that routes them there a
repeat is possible.

The design level is advisory by construction. Its confirmed findings post as one
pull-request conversation comment, or go to Slack for the reviewer's own pull
requests, and they never block a merge, withhold an approval, enter a review, or
change the run's exit code. Approval authority belongs to the code level alone. A
design-level fault is reported as a failure of that level and the code review
continues unaffected; it never becomes "no design issues found", because an error
presented as a clean result is a lie the reader cannot detect. A rejected design
response is retried once with the controller's reason, and so is a design judgment
the controller cannot use, because an over-long field would otherwise discard a
completed design review and its verification pass. That retry quotes the controller's
own rejection and requires every supplied reference to be judged again, since a
verifier that dropped a candidate to satisfy the limit would fail for leaving it
unjudged. Both prompts state each field's hard maximum, so a limit is never a rule
the engine can only discover by breaking it. Unverified candidates are never
delivered: when design verification fails, nothing is posted.

The design verifier judges supplied candidates under short references and returns
one verdict and reason per reference, never restating a candidate, so a confirmed
finding is the controller's own validated object and its text cannot change on the
way back. Every supplied reference must be judged exactly once; an unjudged
candidate fails the level rather than passing silently. Rejected candidates are
reported with the verifier's reason on the terminal and in the run transcript only.

Design delivery is recorded in the same state file under its own destination for
the reviewed head, so a re-run neither reposts the notes nor spends quota
re-reviewing the design. A head that produced no confirmed findings is recorded
too. Delivery revalidates the mutable pull request first, and happens as soon as
the level completes rather than with the code-level result, so notes survive a
later code-level failure.

The **documentation level** runs next when `REVIEW_DOCS_LEVEL` is enabled. It asks
one question: does a document in this repository now say something the change made
untrue, and what should it say instead. Both audiences count, the README and the
docs directory that people read, and the agent instruction files that coding agents
read, which are the ones nobody notices going stale.

The documents are discovered in the reviewed head rather than configured, so a
repository needs no setting to be checked and a repository with no documentation
costs no invocation at all. Human documentation is `README.md`, `CONTRIBUTING.md`,
`ARCHITECTURE.md`, `docs/**`, and a `README.md` up to two directories down, which
covers a monorepo package without collecting a dependency's own README. Agent
documentation is `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.cursorrules`,
`.cursor/rules/**`, `.kiro/steering/**`, and the Copilot instruction files. A
changelog, a licence, and a `.kiro/specs` plan are excluded: a changelog records what
happened rather than promising how the code behaves, and a plan describes work a
change is meant to leave behind. An empty document, one over 200 KB, and anything
inside `node_modules` or a build directory are left out, and at most 60 documents are
listed, root-level ones first.

The prompt lists those documents with their size and audience and does not paste
them. This project's own documentation is 119 KB against a typical 43 KB diff, so
including it would make the documents the review and the change a footnote; the
engine opens the ones the change could affect. The audience is decided by where a
document lives, so it is a fact the controller owns rather than a claim the model
can make.

A proposal must put both sides on record: the documentation line it reports as out of
date, which has to be one of the documents the controller found, and at least one
changed line that outdated it. Without the first the level could invent a document,
and without the second it could report drift this pull request did not cause. Both
citations are held to the same exact-excerpt standard as code evidence.
`proposed_update` is the replacement text, ready to paste in that document's own
voice, rather than an instruction to somebody to go and update the file. The level
may not propose a code change: where the document is right and the code is wrong,
that is the code level's business. Documentation that is merely silent about an
internal detail is not stale, and a rewording of documentation that is still true is
not a finding. The one exception is a list or table that enumerates one kind of
entry, such as scripts or guides, and now lacks an entry of that kind the change
adds. That is reported in its own category, `missing_list_entry`, which the controller
accepts only in one shape: `documentation` cites an existing entry of that list, and
`proposed_update` holds nothing but new entries formatted like it, table rows with
the same number of cells or items with the same marker at the same indent, with no
heading, new table, or sentence around them. For that category alone the verifier
treats the missing entry as incomplete rather than silent, and decides it by whether
the list already holds the new entry's siblings, not by whether the list claims to be
complete; every other candidate is judged as before. Its delivered note names the
cited entry as the place the new entries go, since the proposal holds only them. A
prompt rule that loosened
silence for every candidate was tried twice and each time passed a neighbouring
proposal, a clarification of a sentence that was still true and then a whole new
section, which is why the exception is held to a shape the controller can check. At
most `MAX_DOCS_FINDINGS` are reported.

Everything else matches the design level, and for the same reasons: keyed by pull
request, skipped on a release
promotion, reviewed then adversarially verified by reference with one judgment per
supplied candidate, unverified proposals never delivered, a rejected response
retried once with the controller's reason, delivered as its own pull-request
conversation comment or to Slack for the reviewer's own pull requests, and advisory
throughout: it cannot block a merge, withhold an approval, enter a review, or change
the run's exit code, and a fault in it is reported as a failure of that level while
the code review continues.

One thing differs. A settled documentation level is checked again when a push changes
what the pull request does to its documentation, because that is text the level has
never read, and in a repository that is mostly documentation it is nearly every push.
Every record the level writes carries a fingerprint of the documentation edits it
judged: the lines the pull request adds and removes in documentation files, the same
patterns and exclusions as discovery decided from the path alone, so a document
deleted or renamed away still counts, with a file GitHub sends no patch for
represented by its blob. Hunk positions and context lines are left out, so a rebase
or a merge from the base branch that moves the same edit does not recheck, and
neither does a push that changes only code. That is the one gap: a code-only push can
still make a document wrong, and `--force` remains the way to check again. The
design level stays once per pull request, since the shape of a change rarely moves
with a documentation edit. A record written before fingerprints existed reads as a
check of no documentation edits, so a pull request that edits none stays settled and
one that does is checked once more. The verifier, like the design verifier, reads the
prior feedback and rejects a proposal that feedback already asks for, which is the
second check against repeating an earlier note.

The **code level** then reports line-level defects and owns approval, exactly as
described below.

What identifies a response also belongs to the level, not to an adapter. An adapter
reading a CLI transcript has to find the response among the provider's own tool
narration, and the keys that mark it differ per level, so the neutral core supplies
them. An adapter that carried that knowledge would have to learn about every level
added later.

### Reporting what a review spent

Every pull request reports the model invocations it caused and the wall-clock they
took, and the run reports the total with a per-label breakdown. Skips report nothing,
which is the point: a run that spent nothing must be visibly different from a run
that spent half an hour reaching a recorded answer.

Measured by wrapping the engine rather than by counting inside each level, so an
invocation cannot be missed and a level added later needs no accounting code. The
wrapper counts a failed invocation too, because a review that failed after four
minutes still spent them, and it labels each one by the material it was handed:
`code`, `design`, `docs`, their verification passes, and each level's fallback, such as
`design fallback`, with `verification` appended when the fallback also judged what it
found. Tokens are
deliberately absent: the engines are coding-agent CLIs that do not report them. Credits
are present for an engine that reports them: Kiro ends every headless call with its
cost on standard error, the adapter reads it, and the spend line adds the total of the
calls that reported one. The cost travels on the calling thread rather than on the
engine object, because one engine serves every parallel review at once.

### Measuring a run

`--metrics PATH` writes one record per model invocation and the run's aggregate:
level, wall-clock, credits when the engine reported them, verdict, candidate count, what
verification confirmed and rejected
with its classes, whether the invocation was a correction retry the controller asked
for, and the failure when one happened. `python3 -m prwarden.compare_metrics before after`
reports what moved.

Recorded from the engine wrapper rather than from each level, because every level's
decision is derived from one model response and the wrapper is the only place that
sees both the material going in and the payload coming back. A level added later is
measured without being changed. What the wrapper cannot see is out of scope by
construction: the controller's own arithmetic afterwards, its withheld approvals, its
deliveries, and its skips.

Measurement may never change a review. Every field is read defensively, an unusable
payload is recorded without a verdict rather than raised, and a measurement that
cannot be written reports the reason and leaves the run's exit code alone.

### Replaying pinned work

`python3 -m prwarden.evaluate` pins pull requests and reviews them again, so a change to
the reviewer can be attributed to the reviewer. A case records the slug, the number,
the head, and the merge base that head was reviewed against; pinning the merge base
rather than the base branch is what keeps a case the same case after the base moves.
The diff is rebuilt with `git diff <base> <head>`, per file, each patch trimmed to
its first hunk header, because a `+++ b/path` line reaching the parser would be read
as an added line and become a legal finding anchor. Line counts come from `--numstat`
in its NUL-separated form, so a path containing a space or a quote survives, and a
rename reports where it came from.

A merged pull request stays replayable and needs no special handling: GitHub keeps the
commit reachable through its pull-request ref, so fetching the pinned head by object
name still succeeds after the branch is deleted, which was confirmed against a merged
pull request whose branch no longer exists.

A replay omits what belongs to a live pull request, deliberately: eligibility and
mergeability, prior review comments, and delivery, Slack, and state. Duplicate
suppression is therefore not exercised by a replay, and a replay can post nothing.

An arm is a named set of overridable scalar settings plus an optional engine. An arm
may not set anything that needs a matching command, such as the verification engine,
because an arm must not be able to describe a configuration the loader would refuse.
The measurement records the arm, the reviewer's version, commit, and dirty flag, the
effective settings, and a fingerprint of the case set; `compare_metrics` reports
loudly when the fingerprints differ, when one side is a replay and the other a live
run, when more than one thing changed, and when either side ran from an uncommitted
tree. Those are reported rather than prevented: the operator may have a reason, but
should never learn of it by accident.

Labels are mined from delivered history rather than authored by hand. For every inline
finding the reviewer has posted, `evaluate label` asks what the author did next, by
diffing the commit the finding was anchored to against the pull request's later head
and reading the hunk ranges on the old side: a finding whose lines were then edited was
worth posting. Findings are selected by the comment template's own marker, never by
author, because the reviewer posts under a human account and that person comments too;
selecting by author collected human review remarks as though the reviewer had written
them. A rebase is reported as its own outcome, since it rewrites everything and proves
nothing about one comment, and only findings authors acted on are scored, because a
comment nobody acted on is evidence of neither correctness nor error.

Each label records the attribution its finding was delivered with, produced by the same
formatter the comment carries. A replay scores every acted-on label whichever pipeline
delivered it: a defect an author fixed is real whoever found it, and it is what a new
reviewer model has to find again, while scoring only the running pipeline's own labels
scored nothing once the reviewer model changed. The attribution decides how a rejection
reads instead. A finding delivered with no verification pass that the replay raises
and its verifier throws out is reported as first judged, not as a stricter verifier,
because reading one of those as a verifier regression is a mistake this corpus has
already invited once. `--labelled-only` replays only cases carrying an acted-on label.

A scored replay reports what it raised and what survived verification as two numbers.
The gap identifies which half of the reviewer moved: a finding still raised but no
longer confirmed is a verification change, not a weaker review. Their ratio is the
verifier's retention of real defects, reported as its own number, and it is what a
verifier change is judged on rather than the confirmation rate: in one measured pair the
arm that kept more labelled defects had the lower confirmation rate, because it threw
out more of everything else as well. This is a regression set and not a ground truth:
it can say the reviewer stopped finding something it used to find, and can never say
what it has always missed.

A replay also reports every confirmed finding that matches no acted-on label, split
into those on a comment nobody acted on and those on no comment at all, as an upper
bound on noise rather than a count of false positives, since an unlabelled finding may
be a real defect nobody had found. Mining records the head each pull request ended at,
and a replay marks an unmatched finding as fixed later when its author changed those
lines before that head, by the same rule a label uses and never across a rebase; the
remainder is reported as unexplained, the tighter bound. The bound is loose: of 34
unmatched confirmed findings from four replays of large pull requests, judged by hand,
31 were real defects, so a pass rule must never count unexplained findings as false. A
replay gives the review the
pull request's title and description, recorded by mining and capture, because a live
review reads them and they state intent; the description is the current one, which may
have been edited since the pinned commit. Mining pins control cases beside the labels: heads
the reviewer approved on GitHub, verified by matching the delivered receipt to an
approving review at that exact head, on pull requests that then merged with no further
push and that the reviewer never commented on. `--controls` replays them, alone or with
`--labelled-only`, and a control is scored on the decision a live run would deliver:
a bundle that confirms a finding withdraws the approval, a partial review never
approves, a failed bundle leaves the decision undecided, and a code-level failure is a
failed case rather than a clean one. `compare_metrics` compares the label, confirmed,
and control numbers as well as the engine summary.

`--concurrency N` replays up to eight cases at a time. Each case's report is held until
it finishes and printed whole, the recorded-candidates file is rewritten under a lock
and by rename, and the tally is taken in case order, so running in parallel changes
when a case is measured and never what the measurement says.

Model output is not deterministic, so a single before-and-after pair proves nothing.
Two baseline replays of one pull request produced two candidates and then one, moving
the confirmation rate from 0.5 to 1.0 with no change to the reviewer, so a comparison
is read against that spread rather than against zero.

`evaluate advisory --level design|docs` runs one advisory level alone, review then
verification exactly as live, and reports every proposal with its category and its
verdict. A case comes from the case set with `--case`, or from GitHub with
`--pr owner/repository#123[@head]`: an advisory level runs once per pull request, at
whichever head it first saw, which is rarely the head a mined case is pinned at. A
named merged pull request is pinned against its merge commit's first parent, since its
head is an ancestor of the base branch and the merge base with that branch's tip would
be the head itself. Nothing named this way is written to the case set, which mining
and capture rewrite.

`--baseline-verifier REF` has the same engine judge the same proposals a second time,
under the verifier prompt committed at REF, before today's verifier judges them. REF's
package, `prwarden` from 2.0.0 and `src` before it, is imported from that commit's
files under a name of its own, and its
verification prompt builder is swapped into `engine` for that one call: every adapter
renders its prompt through `engine.build_prompt`, which looks the builder up when it
runs, so the adapter, schema, model, workspace, and proposals stay today's and only the
words the verifier reads differ. Holding the proposals fixed is the point, since the
reviewer's run-to-run variance would otherwise hide the verifier's difference. The swap
is module state, so advisory cases run one at a time. A baseline that fails to answer
judged nothing and is recorded as an error, never as a rejection, and a level that
fails makes the run exit 1.

### Repository background

Every level is given the reviewed head's own `.prwarden/background.md` when it
exists: what the product is, what its units mean, what everybody on the team already
knows and no diff states. Alibaba's reviewer asks the calling agent to summarise
business context and pass it in on every invocation; the same context committed to
the repository stays true as the product moves and needs no operator to remember it.

It is content from the reviewed head, so it is untrusted exactly like the diff:
capped at 4000 characters with the remainder marked truncated, delimited in the
prompt, and named in the sentence that forbids following instructions found in
reviewed content. It may add context, never authority, and never permission to
relax a rule the prompt states. A repository without the file costs nothing and the
prompts say nothing about one.

## Review Engine Contract

The core owns the provider-neutral prompt, output schema, and post-generation
validation. It calls a `ReviewEngine` interface with repository metadata, pull
request metadata, the exact GitHub patch, added-line locations, and compact
metadata for fully removed and purely renamed files. The engine returns a JSON
object containing a
`findings` array, an explicit `approve` or `do_not_approve` verdict, and a
concise `approval_reason` explaining that verdict. Each finding also contains a
concrete trigger, observable impact, exact repository evidence citations,
optional counterevidence citations, and a required explanation of the
counterevidence considered. The response also contains `blocking_comment_ids`.

The verification pass judges candidates by reference and never restates one. Each
candidate is supplied under a short reference, and the pass returns one verdict and
one reason for every reference, plus the approval verdict, its reason, and any
blocking prior-comment references. A confirmed finding is therefore the controller's
own validated object: its path, line, excerpt, and prose are the ones already checked
against the worktree, and the pass cannot alter them. Severity is the single
correction available, because only this pass has weighed the impact it established.

Restating was the source of a whole failure class. Seven verification responses were
rejected for excerpts that were textually right but reformatted, and because a
rejected confirmation looked like a defect the verifier believed but could not
evidence, the controller re-ran the entire review to recover it. That recovery cycle
no longer exists: a judgment cannot be right in substance and wrong in evidence, so a
malformed verification response is retried once with the controller's reason and then
fails the pull request like any other validation error. Every supplied reference must
be judged exactly once, and a reference that was never supplied is refused rather
than resolved, so the pass can neither invent a finding nor quietly drop one.

A rejected judgment may also carry a classification: `not_a_defect` when the claim
is wrong about the code, `already_handled` when the concern is real but the branch
deals with it, `wrong_anchor` when the defect is real but the candidate points at or
cites the wrong line, and `insufficient_evidence` when the claim could not be
established either way. Nothing reads it to decide anything. It is reported with the
rejection on the terminal and in the transcript, and an absent or unrecognized value
counts as unclassified rather than failing a sound review, because a measurement that
can lose a review is not worth taking.

It is recorded because judging by reference gave something up. A pass that restated
findings could also silently re-anchor a sound one, and no transcript distinguishes a
salvaged finding from an ordinary delivery, so the frequency of that rescue is
unknown. Counted `wrong_anchor` rejections measure it. Until they are counted, no
re-anchoring mechanism is justified.

Mergeability gates an approval, not a review. The controller consults it only when
the outcome would be an approval, because until verification finishes it does not
know whether it will approve or report findings, and a conflict does not invalidate
a line-level defect: confirmed findings are delivered on a conflicted pull request.
GitHub also computes mergeability lazily and reports it as unknown on a cold read,
so it is resolved by bounded polling immediately before delivery rather than gating
the review.

A withheld approval caused by a conflict is recorded with the base commit the
conflict was measured against, read from the base branch tip rather than the pull
request's own base SHA, which GitHub does not advance as the branch moves. While
that base and the reviewed head are both unchanged, a later run skips the pull
request rather than reviewing it again, because the same conflict can only produce
the same discarded result: one pull request cost five full reviews that way. The
skip rewrites the same record, so the decision survives the in-progress marker.

A base-branch commit does not reopen the review on its own either. The conflict is
recorded only for a head the review found nothing in, and the diff is taken from
the merge base, which the branch advancing leaves where it was, so the code under
review is unchanged and the only open question is whether the conflict is gone.
GitHub answers that without a model: the controller polls mergeability with the
same bounded rechecks used before delivery, and while GitHub still reports a
conflict it records the conflict against the new tip and skips. One unchanged
conflicted head was otherwise reviewed eight times in a week as a busy base branch
moved under it. Mergeability still indeterminate after the last recheck keeps the
old record, so the next run asks again, rather than spending a review that would end
at the same unanswered question. Only a cleared conflict, a new head, an unreadable
base branch, or a record written before the base was tracked leads to a normal
review, and `--force` overrides the skip.

An evidence excerpt is compared against the cited line after JSON decoding, so
the prompt states exactly how to copy one: character for character, without
reformatting, ellipsis, or escaping beyond what the JSON string requires, and
short enough to be unique in that file. Eight recorded rejections were excerpts
that were textually right but reformatted, doubled a backslash, dropped the
backticks of a template literal, or named a fragment occurring on several lines.
Each cost the review that produced it, so the requirement is stated where the
model writes the citation rather than left to the validation message.

A verification pass is budgeted by how much work it was given. Its timeout is a
configured base plus an allowance for each candidate in the pass, because the
verifier checks every candidate against the workspace in turn while a budget fixed
at construction cannot express that. Recorded passes carrying one or two candidates
finished within about two minutes; the only passes that ever exhausted a flat ten
minutes carried three and four candidates, and each lost its entire review with the
timeout. The budget travels with the invocation rather than with the engine, so an
adapter uses the budget it is handed and falls back to the configured
per-invocation timeout when it is handed none. A longer ceiling costs more wall
clock when a provider genuinely hangs, which is the deliberate trade for not
discarding completed reviews.

Feedback that changes between the start of a review and its delivery is compared
comment by comment rather than as one set. A comment that vanished, or whose only
change is that it became resolved, removes an objection: a result reached without
knowing that is still sound and is delivered. A comment that arrived, reopened, or
was edited may carry words this review never read, and what it can invalidate
depends on what is being delivered. An approval speaks for the whole pull request,
so any such comment withholds it. A finding speaks for one line, so it is
delivered unless one of those comments shares its path and line. A shared line is
not proof of a duplicate, which is why the reviewer is told not to treat it as
one, but it is proof that the controller cannot tell them apart, and it holds the
whole result rather than posting next to a human who may have said the same thing
first.

Holding a result that way is a skip and not a run failure. Nothing local went
wrong: somebody is reviewing the same pull request and their comment is newer than
this judgment. So the exit code is untouched, the state stays nonterminal for that
head, and a Slack notification names where the feedback landed, never what it
said, because whether to run the reviewer again is an operator decision. Delivery
is never attempted against feedback the review has not read.

Prior review comments are supplied to the engine under short references, `C1`,
`C2` and so on, and their real GitHub IDs are withheld. A blocking citation names
a reference, and the controller resolves it back to the comment it stands for
before recording or comparing anything. A ten-digit GitHub comment ID that a model
has to copy is a transcription hazard: dropping one digit makes the citation match
no current feedback, which rejects the entire review and spends a second pass
correcting it. A reference cannot be mistyped into a different valid comment, and a
rejection names every reference that was supplied so the correction retry can
succeed. Case is normalized; nothing else about a reference is guessed.

Every validated text field has one maximum length, defined once and applied in
both directions: the generated JSON schema publishes it as `maxLength`, and
controller validation enforces the identical value, so the schema and the
validator cannot drift apart. The prompt also states the limits for the fields a
model is most likely to overrun, with the instruction to write a shorter sentence
rather than truncate one, because an over-long field rejects the whole response
and costs a correction retry.
When the engine withholds approval without a new finding, the controller requires
at least one exact prior-comment ID that GitHub currently reports as unresolved.
Unknown and resolved IDs are rejected through the normal correction-retry path.

Those two rules can close on a correction retry from both sides: a resolved thread
may not block approval, and a withheld approval must be supported by something the
response carries. A retry told only that its reference was resolved dropped it,
withheld approval anyway, and was rejected again for having nothing left to
withhold on, which lost a complete review after two full invocations. So the
prompt states what supports withholding before the first attempt, and a rejection
naming the verdict or a blocking comment restates every valid response: approve
with nothing, withhold citing an unresolved reference, or withhold with a finding
that carries its own evidence. Approving is named explicitly, because a concern
that cannot be established here is not grounds to withhold. A rejection about a
citation is not given that guidance, so a retry stays about the field that was
actually rejected.

Built-in adapters:

- `antigravity`: translates the common contract to Antigravity CLI structured
  output and unwraps its response envelope. It refuses a head whose `.agents/` or
  `.agent/` holds anything but rule and workflow documents, because Antigravity
  runs the MCP servers and hooks a head names there outside its sandbox.
- `codex`: uses non-interactive, ephemeral, read-only Codex execution with a
  required output schema. It never reads the user configuration, where trusted
  projects are recorded, so a head's `.codex/config.toml` is never loaded.
- `claude`: uses Claude Code print and safe modes with read-only tools and a
  required output schema. It reads only the operator's own settings and no MCP
  configuration, because safe mode still loads a head's project settings.
- `kiro`: uses Kiro CLI headless mode with read/search tools and strict JSON
  parsing before common finding validation. It refuses a head that ships Kiro
  configuration other than documents, because Kiro would load and could execute
  it, and gives the worktree its own setting that keeps the answer unrendered.
- `command`: runs an arbitrary command without a shell, sends the prompt on
  stdin, supplies schema and limits through environment variables, and expects
  the common JSON object on stdout.

New provider adapters may translate the same interface without modifying
discovery, validation, state, or delivery code.

For each engine invocation, terminal output reports the configured timeout and
elapsed monotonic time in compact forms such as `5m` and `1m26s`. Failed engine
attempts report their elapsed time before the error is handled. The configured
timeout applies separately to candidate generation, adversarial verification,
and each validation-correction invocation. In an interactive terminal, an
in-place progress line appears after five seconds and continuously shows the
elapsed time until that invocation finishes. The line carries no loader glyph.
It is drawn faint, and a fixed-width window of its characters is drawn bold and
sweeps repeatedly from the start of the line to its end, so at least one
character is highlighted in every frame. Both are styles of the terminal's own
text colour rather than palette colours, because a theme may draw any palette
colour as its background: Solarized Dark does that with bright black, which once
made the line unreadable. Redirected output, cron, and
LaunchAgent logs do not receive progress frames or terminal control codes.
Once per run, startup states the engines the run intends to use, in the future
tense and one per line: the reviewer, the fallback engine and the failure that
would invoke it, and the verifier. Only configured engines appear, and an engine
matching the primary engine and model is omitted rather than repeated.

Delivered attribution is separate and reports completed work. Slack messages and
every GitHub inline finding name the verifying engine and model only when a
separate verification engine actually judged candidate findings, because
verification runs solely for a review that produced candidates: a clean review, a
failed review, and a run that verifies with its own engine all report a single
reviewer. The one exception is the `LGTM` comment on the reviewer's own pull request
in a repository kept out of Slack: it names the configured verifier as having had
nothing to check, never as having verified, so the comment shows the whole pipeline
while crediting the verifier with nothing it did not do.
Delivered feedback names the engine that actually reviewed, which is
the fallback engine when the primary failed. The aggregated Slack error
notification follows the same rule, which distinguishes a review that failed
before verification from one that failed after it. GitHub approval reviews remain
bodyless.

The common prompt treats repository and pull-request content as untrusted data.
The code-level prompt places each block of that data inside one of its own tags, and
every prompt neutralises any text in untrusted data that would open or close one of
those tags, writing `&lt;` for its `<`, and says so once. Only the registered tag
names are touched, so markup in a diff reads as written, and a test fails if a prompt
gains a tag that is not registered.
It permits only read-only local file search and reading for semantic
verification, and prohibits edits, command execution, networking, browsing, and
external actions. It requests only correctness, reliability, data-loss,
concurrency, and security defects introduced by added lines.
Changed test code is held to the same rules, with the test run as its runtime. A
changed test, fixture, or test helper is defective when it fails against correct
code, reports a pass or a skip without making the check it exists to make, or
exercises a route, selector, or symbol the workspace does not contain; its trigger
is running the suite that contains it. A missing test, a weaker assertion, or added
coverage is not a defect, and product code whose only callers are tests still has no
user-facing trigger. The rule exists because a review once approved a pull request
after skipping its changed end-to-end suite as test harness code.
The diff limits finding scope and inline locations but is not treated as complete
branch context. For every candidate, the engine must read complete involved files
from the exact PR worktree, inspect enclosing control flow above and below the
hunk, search relevant definitions and call sites, check whether equivalent
protection already exists elsewhere, and verify presence or absence claims
against the current head. Diff omission alone is never evidence that code is
absent, and deleted/base-branch code is never evidence that it still exists.
Dependency and framework claims require exact manifest and lockfile versions and
installed-package evidence. A claim that a dependency does not export a symbol,
or that an API is missing, renamed, removed, or deprecated, counts as confirmed
only when the engine cites the installed package declaration at the resolved
version. When the installed package is not present in the workspace, such a
claim is unverifiable and the finding must be omitted, so remembered behavior
from another version cannot become a reported defect.
Exact-head check runs are controller-fetched GitHub facts: a successful check
proves its commands passed on the reviewed head, while its coverage remains
unknown. An engine may argue that a passing check does not cover a defect only by
citing the workflow or configuration files in the workspace that define what it
runs. Dismissing a passing check as misconfigured, cached, stale, or bypassed
without that citation is not permitted, and the controller independently rejects
any finding claiming that the reviewed head fails to compile, typecheck, or build
while a matching exact-head check succeeded. Such a rejection uses the bounded
correction retry; if it recurs, that pull request ends as a retryable failure
without delivery.
The controller validates every evidence path, line, and excerpt against the exact
worktree before a finding can proceed.
Before analysis, the controller retrieves existing human submitted review
bodies, resolved and unresolved inline review threads, human pull-request
conversation comments, and exact-head check runs. Each inline comment includes
its resolution state. Resolved feedback supplies context but cannot block
approval. Bot-authored review summaries and conversation comments are excluded
as noise; inline bot findings remain available. The common prompt requires every
engine to omit a finding when prior
feedback already describes the same underlying issue, including when the
wording or reported location differs. If the complete prior-feedback text
exceeds the bounded context limit, review stops instead of silently ignoring
feedback.
Duplicate findings are omitted from new comments but still require a
`do_not_approve` verdict while the underlying defect remains on the current
head.

When candidate findings are generated, the controller invokes a fresh engine
session as an adversarial verifier. The verifier may only confirm or reject the
supplied candidates and must actively test them against dependency versions,
targeted tests, check results, project patterns, and prior discussion. It cannot
introduce new finding locations, and only confirmed candidates can be delivered.
`REVIEW_VERIFY_ENGINE` and `REVIEW_VERIFY_MODEL` optionally run that pass on a
different authenticated adapter or model, so a candidate produced from one
model's priors is judged by another. A verification engine that matches the
primary engine and model is treated as unconfigured. All controller validation,
routing, state, and delivery rules apply identically regardless of which engine
verified.

`REVIEW_EFFORT`, `REVIEW_CODE_EFFORT`, `REVIEW_VERIFY_EFFORT`,
`REVIEW_FALLBACK_EFFORT`, and `REVIEW_VERIFY_FALLBACK_EFFORT` optionally set a
reasoning effort of `low`, `medium`, `high`, `xhigh`, or `max` for the reviewer, the
reviewer's code level, the verifier, the fallback, and the verifier's fallback.
Startup refuses an unknown level, an effort without its engine, and an effort for
an adapter that cannot pass one, because each would otherwise run at a level nobody
chose without saying so. An engine replaced on the command line does not keep the
configured effort.

`REVIEW_FALLBACK_ENGINE` and `REVIEW_FALLBACK_MODEL` optionally review a pull
request when the primary engine fails at the engine level, which covers an
unavailable, unauthenticated, or refusing provider, an exhausted adapter retry
budget, and a timeout. Every level hands over the same way: the design and
documentation levels as well as the code level, each at most once per pull request.
The advisory levels stay advisory when the fallback carries them, and a failure of
both engines at an advisory level remains contained to that level. A fallback
engine matching the primary engine and model is treated as unconfigured
because repeating the same provider call cannot recover from that provider being
unavailable. A controller validation failure never hands over: the review phase
already retries it with the exact diagnostic, and trying a second engine for a
compliant answer would weaken validation rather than recover from an outage. Only
the primary engine's refusals are recorded as the primary's spent quota.

The fallback reviewer is a reviewer, not an exception to any rule. Its response
passes through identical schema, evidence, added-line, dependency, and build
validation, its candidates go through the same adversarial verification, and
delivered feedback names the engine that actually reviewed instead of the
configured primary. When no separate verification engine is configured, the
verification pass runs on the engine that produced the candidates, because the
primary engine is the one that just failed. When both engines fail, the reported
error names both so the outage is not mistaken for a single provider's fault.

`REVIEW_VERIFY_FALLBACK_ENGINE`, `_MODEL`, and `_EFFORT` optionally name a verifier
that judges when the verification engine fails at the engine level, under the same
rules: an engine failure hands over and a validation failure does not, a fallback
matching the verifier's engine and model is unconfigured, and a failure of both is
reported naming both. It requires `REVIEW_VERIFY_ENGINE`. The hand-over is made per
pull request and holds for the rest of it, so a correction retry reaches the engine
whose answer it corrects, and the next pull request starts with the verifier again.
Delivered feedback names the verifier that actually judged, each advisory note and
each inline finding alike, and the spend line counts its calls apart. Replays never
use it, because a measurement that changed verifier mid-run would compare nothing.
Without it, a verifier on the reviewer's provider left every finding the review
fallback raised undeliverable while that provider was down.

Candidate generation and adversarial verification each permit one correction
retry when the controller rejects an otherwise completed engine response for a
schema or semantic validation error. The fresh retry receives the exact
controller diagnostic, recreates the complete response, and passes through all
validation again.

A verification response can no longer confirm a defect it fails to evidence. The
pass judges references and the controller supplies the evidence from its own
validated candidate, so a confirmed finding either exists as the controller already
checked it or the reference does not resolve at all. A response the controller
cannot use is therefore a malformed response, not a disputed defect: it takes the
one correction retry above and then ends that pull request as a retryable failure,
while processing continues with other pull requests. Engine command
failures and timeouts do not use this correction path. An adapter may separately
retry a recognized pre-delivery provider failure when its attempts are bounded
and cannot create an external review delivery. The Antigravity adapter retries
recognized transient failures twice and surfaces a bounded backend status from
a per-attempt diagnostic log. It also retries a warning-only failed exit, which
can occur when the CLI records the provider failure only in internal state, and
a logged network-transport fault, which carries no provider status. A logged
print-timeout expiry is reported as the configured timeout and is not retried.
The configured timeout bounds every attempt of one invocation together, not each
attempt alone: a retry runs only for what remains after the elapsed attempts and
the backoff, and is skipped when under a minute would remain, with the failure
reported as not retried because the budget is spent. A subprocess timeout keeps
its own wording and names its retained diagnostic log rather than being reported
as a failure without a diagnostic. The CLI's refusal of the configured model or of
its effort, which it logs without a provider status, is reported by its own words
with the models the CLI offers instead, and is not retried, since the same
configuration fails the same way.
An unsuccessful result envelope reports the message it carries rather than only
its status. When that message reports a failed tool call, such as a
denied command or a read of a path that does not exist, the adapter re-reviews
once with the reported reason quoted back as feedback, within the same bounded
invocation budget, because such a fault discards an otherwise complete review.
A successful envelope carrying an empty response is treated the same way when
either stream explains it: an unattended review cannot approve a tool call the
sandbox does not already permit, so the call is refused and the review ends with
nothing, which is a local fault the model can be told about rather than a reason
to spend the fallback engine. A final non-empty log that no diagnostic explains is retained with
owner-only permissions so the reported local path can be inspected without
reducing the error to unrelated stderr noise. If the log is empty, the final
error states that no usable provider diagnostic was available.

## Exact Review Workspace

Before invoking an engine, the controller verifies that the configured
checkout is a Git worktree and that its `origin` URL ends with the configured
GitHub slug. It fetches the selected PR head SHA with `--no-write-fetch-head`,
creates a unique detached temporary worktree under the ignored state directory,
and passes that path to the engine. Cleanup runs after successful and failed
engine calls. The configured checkout's branch, index, and working files are
not changed.

Installed dependencies are not tracked by Git, so the controller links the
configured checkout's dependency directories into the temporary worktree. The
whole directory is linked when, and only when, every known lockfile declared by
the reviewed head is byte-identical to the checkout's, which establishes that the
linked contents are the versions the reviewed head resolves and makes them valid
evidence for dependency API claims.

A differing lockfile does not change every dependency, and withholding all
dependency evidence from the pull requests that touch a lockfile denies it
exactly where dependency claims arise. So when a lockfile differs, the controller
compares resolved versions per package in the formats it can read and links only
the packages both trees resolve identically, each still at the version the head
resolves. A partial link is declared as partial in the prompt, because absence
then means a version changed rather than that a package does not exist. Formats
the controller cannot read contribute nothing, and a package is linked only when
every readable lockfile agrees on its version, so a link never presents one
version as another.

Whenever linking is skipped or partial, the run reports why, naming the lockfile
that differed or the missing installation, because a stale local checkout and a
dependency-changing pull request produce the same missing evidence and only one
of them is worth fixing locally. Links are symlinks that the engine may
only read, evidence citations inside a linked target are accepted as
in-workspace, and both worktree cleanup paths unlink them without modifying the
checkout's contents. Evidence paths must stay workspace-relative so local
filesystem layout is never published in delivered feedback.

The controller rejects findings with unsupported severity, excessive length,
duplicate location, nonexistent path, unverified evidence, or a line that is
not added by the exact GitHub patch. Evidence citations must identify a current
workspace text file and a single-line exact excerpt. When the reported evidence
line does not contain that excerpt, the controller corrects the line only if the
excerpt occurs on exactly one line in the same file. Missing, multiline, and
ambiguous excerpts remain validation errors, and the rejection quotes the
submitted excerpt beside the real line, bounded and whitespace-collapsed, so the
correction retry and the operator can see the difference instead of receiving
the same location twice. If the engine omits part of a
directory, the controller corrects the path only when the filename and reported
added line identify exactly one changed file. Ambiguous path matches remain
validation errors, and those errors include the finding title for diagnosis. A
first validation failure triggers the bounded correction retry described above;
validation is never bypassed or converted into an empty successful review.

If GitHub reports a new head SHA before delivery, the controller abandons the
stale result and restarts the complete review against the new immutable head.
One pull request may restart at most five times in a single application run,
for at most six total attempts including the original. Each stale attempt is
recorded as `superseded` and cannot deliver. A sixth observed head change ends
that pull request as a retryable final error while processing continues with
other pull requests.

## Delivery

### GitHub

Create and submit one bodyless `COMMENT` review for the exact head SHA. Each
finding is an inline comment rendered from a validated Markdown template and
editable tag registry. The committed default contains severity/title, problem,
trigger, impact, evidence, counterevidence, suggested change, reviewer
attribution, and a hidden reviewer marker. Template tags map only to supported
controller values and cannot execute code. Startup rejects unreadable or
invalid files, unknown tags, unsupported mappings, and templates without
exactly one marker field. Rendered comments are bounded before a GitHub write
begins. Every marker this tool writes starts `<!-- prwarden`; wherever one is read,
the marker the same comment carried before the 1.0.0 rename, starting
`<!-- ai-code-reviewer`, is read as well, so a pull request reviewed under the old
name keeps its notes, its LGTM comments, and its delivered findings. All inline
locations use GitHub's new-file `RIGHT` side. Draft pull
requests and pull requests with a label configured in
the `EXCLUDED_PR_LABELS` JSON array are excluded during discovery; label
matching is case-insensitive.

When a complete review returns no qualifying findings, print `LGTM` to the
terminal. For a team pull request, submit a bodyless `APPROVE` review for the
exact head SHA. For a self-authored pull request, send the clean-review Slack
message without GitHub approval, or, in a repository kept out of Slack, post the
`LGTM` pull-request comment instead. Immediately before any of these deliveries, fetch
GitHub mergeability. A conflicting
pull request is recorded with the nonterminal `merge_conflict` status and is not
approved. GitHub calculates mergeability in a background job and answers with
null until it finishes, so an indeterminate response is rechecked a bounded
number of times with increasing delays, revalidating the snapshot each time so a
head that moves during the wait still restarts the review. Mergeability that is
still indeterminate after the last recheck is a retryable failure.

### Slack

Send one contiguous incoming-webhook message containing a linked PR label and
numbered findings. PR-specific messages include a bold PR title and finish with
an italic reviewer attribution containing the selected review engine, the
explicitly configured model ID, and the verifying engine and model when they
differ. When no model ID is configured, they identify the provider default
without claiming a model name the controller cannot verify. Known Gemini, GPT,
and Claude model IDs are rendered as readable display names; any other ID is
reported verbatim. Finding messages are used only for pull requests authored by the
configured reviewer. A clean self-review sends a separate `LGTM` message only
after GitHub confirms that the exact reviewed head has no merge conflicts. Its
result is formatted as a blockquote and states that required checks and branch
protections must still pass before merging.

A repository configured with `"slack": false` is never named in a Slack message.
Its own pull requests are delivered on GitHub as described above, a run error about
it is left out of the aggregated notification (which is not sent when nothing else
remains), and its manual-review and stale-review notices are printed and logged
only. A run error names its repository as its first word, which is what the filter
matches. On a self-authored pull request there the check for a review this account
already submitted at the head is skipped, as it is on the Slack route: the author
replying to a review thread submits a review, which would otherwise settle a head
nobody had reviewed, so idempotency rests on the local state alone.

When the Slack webhook is configured, a live run with one or more unrecovered
errors sends one bounded, aggregated notification after processing. The
notification says the run completed with errors because unrelated repositories
and pull requests continue processing. Expected eligibility changes are logged
as skips and do not trigger this notification. Startup failures notify when both
live-delivery gates are active and dotenv loading made the webhook available.
Recovered retry attempts do not notify, and a failure to send the alert is
logged without replacing the original exit status.

A defect in this tool is reported the same way rather than ending the run. One that
a review hits is that review's run error, "crashed with" its exception type and
text, so the run carries on with the others, prints its summary, and sends the
notification. One that no review owns ends the run with exit code `1` and, under
the same gates as a startup failure, a notification that carries only the
exception type and the file, line, and function in this tool's code: the text of an
unexpected error could name a repository kept out of Slack. Either way the full
traceback is printed to the terminal, which is the scheduler log for a scheduled
run.

Every outbound Slack message has absolute local filesystem paths reduced to their
final component. Terminal output keeps complete paths for debugging, but a Slack
channel is shared and archived, so a message must not disclose the reviewing
host's directory layout, account name, or install locations. URLs and
repository-relative paths are unaffected.

When a `gh` command fails after its transient retry window is exhausted, the
controller consults GitHub's public status summary once per run and reports any
announced incident, unresolved by GitHub, on the terminal and as one line in the
aggregated Slack notification. The lookup is read-only, sends no repository data,
is bounded by a short timeout, and is attempted only after such a failure. Any
lookup failure, malformed response, or healthy status is silent, so it can never
mask or replace the original command failure.

## Safety

- Live delivery requires `AUTO_REVIEW_DRY_RUN=0` and `--live`.
- On POSIX systems, dotenv configuration must have mode `0600` so webhook and
  provider secrets are not exposed to other local accounts.
- Zero findings never produce a GitHub comment on a team pull request. A clean
  team pull request may receive a bodyless approval, while a clean self-authored
  pull request may produce an `LGTM` Slack message after mergeability checks, or,
  in a repository kept out of Slack, one `LGTM` pull-request comment.
- Fully removed files use compact deletion metadata, do not count toward the
  diff-size limit, and can affect the approval verdict without producing an
  invalid inline finding on a deleted line.
- A patchless added or modified file that GitHub counts as zero additions, zero
  deletions, and zero changes has no line any reviewer could read, which is how a
  binary asset is reported. It is named to the engine as textless, carries no
  eligible finding locations, and does not stop the review, so one image no longer
  costs a pull request its review and no glob has to be maintained for every
  binary extension. A withheld patch whose lines GitHub still counts is a
  genuinely incomplete diff, as is an absent or unusable count, because neither
  proves there is nothing to read.
- Such a file is deferred rather than fatal. It holds real changed lines that
  nothing local can read, which is the position of a file the byte budget could
  not fit, so it takes the same route: the rest of the change is reviewed, the
  path is named on the terminal, in the GitHub review body and in the Slack
  request, no finding can be anchored in it because it contributes no eligible
  line, and the approval is withheld. Stopping the pull request instead reviewed
  none of it and required a glob for every generated file anybody might commit.
  A partial review caused only this way reports no budget ratio, because the
  readable diff never exceeded anything.
- A withheld approval that the controller forced this way is not reconsidered
  while nothing about it can change. The record names the unread paths and
  fingerprints `MAX_DIFF_BYTES` and `IGNORED_MISSING_PATCH_PATTERNS`, the only
  settings that decide what goes unread for an immutable head, and a later run
  skips while both still match. A new head, either setting changing, or `--force`
  reopens it. Reconsideration stays for a withholding the engine chose without
  validated blocking comment IDs, where another attempt can genuinely answer
  differently; a rule the controller applied itself cannot, so re-reviewing only
  spends a model invocation to record the same result.
- `IGNORED_MISSING_PATCH_PATTERNS` therefore no longer decides whether a pull
  request is reviewed at all. It states that a matching path is generated and
  needs no review, which ignores the file outright and leaves the change
  approvable. Widening it is a decision to allow approval over an unread file,
  not a way to unblock a review, and narrowing it costs an approval rather than a
  review. Matching omitted patches are reported in terminal output and ignored by
  the engine. If nothing is reviewable at all, the review stops without approval:
  a pull request carrying only unreadable files is not an approvable clean
  result.
- A file GitHub reports as renamed with zero additions and zero deletions has no
  patch because it has no content change. It is recorded as move metadata, is
  exempt from the missing-patch stop, and carries no eligible finding locations.
  Renames that change content keep their patch and are reviewed normally, and a
  rename reporting changes without a patch still stops review.
- Oversized reviewable diffs send a dedicated manual-review Slack notification
  and are recorded as `skipped_oversized` after confirmed delivery rather than
  silently reducing coverage. They do not fail the overall run or trigger the
  aggregated run-failure notification. Notification failures remain retryable.
- Pull-request metadata and the paginated changed-file response must report the
  same file count. Pull requests above GitHub's 3,000-file response limit stop
  rather than receiving a partial review.
- Errors are not interpreted as empty results.
- Read-only GitHub CLI operations retry recognized transient connectivity,
  timeout, rate-limit, and GitHub 5xx failures three times with bounded
  exponential delays. Each retry is logged. Delivery writes are single-attempt
  because a lost response does not prove that GitHub rejected the write.
- The open state, draft state, excluded labels, and current head SHA are
  rechecked immediately before live delivery. A changed head triggers the
  bounded complete-review restart described above. The exact head SHA is
  embedded in the GitHub review request.
- For live delivery with findings, existing review feedback is fetched again
  immediately before posting. If it changed during analysis, delivery stops and
  the head remains retryable so the next run can evaluate the new comments.
- Inline feedback is fetched through GitHub GraphQL so resolved and unresolved
  review threads can carry explicit resolution state into review context.
  Human review summaries and PR conversation comments remain part of the
  context; bot summaries and bot conversation comments are filtered as noise.
- Engine commands are argument arrays and do not use a shell.
- Engine subprocesses receive a minimal environment plus explicitly allowlisted
  provider variables; delivery secrets are not inherited by default. The account
  name is resolved from the process owner when the parent supplies no `USER` or
  `LOGNAME`, because launchd and cron omit both and an authenticated CLI that
  resolves stored credentials by account name would otherwise report itself as
  logged out only under the scheduler.
- Secrets, real repository lists, state, and logs are ignored by Git.
- Scheduling is deployment-specific and must preserve the single-host process
  lock and explicit live-delivery gates.
