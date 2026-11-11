# PR Warden

A self-hosted pull-request reviewer with pluggable AI CLI engines. It discovers
pull requests with GitHub CLI, reviews the exact PR head in a temporary
worktree, posts confirmed findings or approvals to GitHub, routes self-review
feedback to Slack, and keeps single-host delivery state in a local JSON file.

Delivery is disabled by default. A live run requires both a configuration gate
and a command-line flag.

## How it works

On every run, for each repository you list, PR Warden:

1. lists the open pull requests with GitHub CLI, skipping drafts, excluded labels,
   and heads it has already reviewed;
2. fetches the exact head of each one into a temporary detached worktree, so the
   review reads the code as it is in that pull request and nothing else;
3. reviews it with your AI CLI in up to three levels: the solution design and the
   documentation, which only ever comment, and the code, which reports line-level
   defects and is the only level that can approve;
4. hands every candidate finding to a verifier, ideally another model, whose job is
   to refute it, while the controller checks that each cited line really says what
   the finding quotes;
5. delivers only what survived: inline comments on lines the pull request added, or
   an approval when the review was complete, found nothing, and the pull request
   can merge. Results on your own pull requests go to Slack, or to the pull request
   as comments, because GitHub does not let you approve your own;
6. records what it delivered for that head in a local state file, so running it
   again never posts twice.

The models propose and the controller decides: eligibility, validation, duplicates,
and delivery are plain code, the same for every engine. Kiro, Claude Code, Codex,
and Antigravity can each review, verify, or stand in when another's provider fails,
and a command of your own can review.

## Install

PR Warden is one command, `prwarden`, with no Python packages of its own to pull
in. Install it with Homebrew on macOS or Linux, or from PyPI anywhere Python 3.9 or
newer runs.

### Homebrew

```sh
brew install bruha/tap/prwarden
```

This adds the [bruha/homebrew-tap](https://github.com/bruha/homebrew-tap) tap and
installs PR Warden together with GitHub CLI and the Python it runs on.

```sh
brew upgrade prwarden      # update to the latest release
brew uninstall prwarden    # remove it
```

### PyPI

The [prwarden package](https://pypi.org/project/prwarden/) runs on any Python 3.9 or
newer, the `python3` that comes with the macOS Command Line Tools included. Install
it with [pipx](https://pipx.pypa.io/), which gives it an environment of its own and
puts `prwarden` on your `PATH`:

```sh
pipx install prwarden      # or: uv tool install prwarden
pipx upgrade prwarden      # update to the latest release
pipx uninstall prwarden    # remove it
```

GitHub CLI is not a Python package, so pipx does not install it; `prwarden setup`
checks for it and offers to.

### Either way

`prwarden --version` shows the installed release. Configuration and review records
live outside the installation, in `~/.config/prwarden` and `~/.local/state/prwarden`
(see [where configuration and state live](#where-configuration-and-state-live)), so
an upgrade keeps them and a schedule keeps working: Homebrew and pipx both keep
`prwarden` at one path. Install it one way only: `prwarden schedule install`
schedules the first `prwarden` on your `PATH`.

Uninstalling leaves the configuration and records behind. To remove everything, run
`prwarden schedule remove` before uninstalling, then delete `~/.config/prwarden`,
`~/.local/state/prwarden`, and `~/.local/share/prwarden`, which holds the clones PR
Warden made.

## Quick start

With PR Warden [installed](#install), run:

```sh
prwarden setup
```

`prwarden setup` first checks GitHub CLI and the AI CLIs PR Warden can drive,
Kiro CLI, Claude Code, Codex, and Antigravity, and shows which are installed and
signed in. It offers to install what is missing, with Homebrew on macOS and the
vendor's own installer otherwise, showing each command and running it only when you
agree, and then runs each tool's own sign-in. Then it asks which GitHub account
reviews, which AI CLI reviews and which verifies, and which repositories to watch. A repository you have no clone of is
cloned by PR Warden on its first review; setup says where, and roughly how large,
before you agree.

It then asks whether the reviews of your own pull requests, which GitHub does not
let you approve, should go to Slack. Without a webhook yet, it opens Slack's page
for a new app already filled in: pick the workspace and the channel, and paste the
webhook URL Slack shows. Setup sends a test message through it before keeping it,
and asks which repositories report there, so a repository whose team is in another
workspace stays out of it. `prwarden setup --slack` does this step alone later.

Next it asks whether your pull requests name Jira work items. If they do, setup
checks for Atlassian's own CLI, `acli`, offers to install it, and signs it in to
Jira, either in the browser or with an API token: setup opens Atlassian's page for
making one and hands the token you paste straight to `acli`, which keeps it. Then
it asks for the project keys your pull requests use and which repositories use
them. `prwarden setup --jira` does this step alone later.

Setup writes `.env` and `repositories.json` to `~/.config/prwarden`, then offers a
dry-run preview of one pull request, which spends credits and posts nothing, live
delivery, and a schedule. `prwarden doctor` checks the result at any time and
spends nothing.

### By hand, or from a clone

An installed copy can also be configured from the two examples:

```sh
mkdir -p ~/.config/prwarden && cd ~/.config/prwarden
base=https://raw.githubusercontent.com/bruha/prwarden/main
(umask 077 && curl -fsSL "$base/.env.example" -o .env)
curl -fsSL "$base/repositories.example.json" -o repositories.json
```

To run from a clone instead, which keeps its configuration in the clone, type
`python3 -m prwarden` in place of `prwarden`:

```sh
git clone https://github.com/bruha/prwarden.git
cd prwarden
install -m 600 .env.example .env
cp repositories.example.json repositories.json
```

`prwarden setup --import <clone>` later moves a clone's configuration and delivery
records to an installed copy, so nothing is reviewed twice.

1. In `.env`, set `REVIEW_ENGINE` to the AI CLI you are signed in to, such as `kiro`
   or `claude`, and optionally `REVIEW_VERIFY_ENGINE` to a second one.
2. In `repositories.json`, list each repository's `owner/name` slug, and the path to
   a local clone of it if you have one; without a path PR Warden clones it itself.
   Add `"slack": false` to an entry if you do not use Slack.
3. Check the setup with `prwarden doctor`, which reads everything and spends
   nothing. Then review one pull request as a dry run. Nothing is posted, but the
   model runs and uses your quota:

   ```sh
   prwarden --repo your-org/example-app --pr 123
   ```

4. When the result looks right, set `AUTO_REVIEW_DRY_RUN=0` in `.env` and add
   `--live` to post it.
5. To review on a schedule, run `prwarden schedule install`, which sets up a
   LaunchAgent on macOS or a systemd user timer on Linux; see
   [scheduling](docs/scheduling.md).

PR Warden posts as the account GitHub CLI is signed in as, and never approves that
account's own pull requests, so a separate reviewer account works best.

## Requirements

- Python 3.9 or newer, which includes the `python3` bundled with the macOS
  Xcode Command Line Tools, so a stock Mac needs no interpreter install
  ([why](docs/code-conventions.md#why-python-39))
- [GitHub CLI](https://cli.github.com/) authenticated for each repository
- An authenticated supported review CLI: Antigravity, Codex, Claude Code, Kiro,
  or a compatible custom command
- A Slack incoming webhook when reviewing your own pull requests or sending
  failure and manual-review notifications, unless every repository is kept out of
  Slack; `prwarden setup` walks you through making one

No third-party Python packages are required.

## Setup

The [quick start](#quick-start) created the two private configuration files from
their examples, in `~/.config/prwarden` for an installed copy or in the clone. If
`install` is unavailable, copy `.env.example` to `.env` and then run
`chmod 600 .env`. Edit both files for your environment. In a clone they are
ignored by Git.

Each `repositories.json` entry needs a GitHub `owner/repository` slug, and may name
the `path` of a matching local Git checkout. The checkout supplies Git objects; its
currently checked-out branch does not need to match or be clean, and dependencies
installed in it, such as `node_modules`, are lent read-only to each review.

An entry with no `path` is cloned by PR Warden itself the first time one of its
pull requests is reviewed, into `~/.local/share/prwarden/checkouts/<owner>/<name>`
for an installed copy or `.state/checkouts/` in a clone. The clone holds Git objects
only, nothing is installed into it, and it is fetched with GitHub CLI's login for
the account the repository is read as, so it needs no Git credentials of its own.
Delete it to reclaim the space; the next review clones it again.

An entry may also set `"slack": false` to keep the repository out of Slack, when
the webhook belongs to a workspace the project is not part of. Your own pull
requests there are then reviewed on GitHub rather than in Slack, and nothing about
the repository is sent to the webhook: its run errors, manual-review requests, and
stale-review notices stay on the terminal and in the run log.

```json
{ "slug": "your-org/side-project", "path": "~/src/side-project", "slack": false }
```

An entry may set `"github_account"` to read and review the repository as another of
the GitHub CLI's logged-in accounts, for a private repository that only its owner
can see. Adding the reviewer as a collaborator would work too, but it also grants
push access. That account's own pull requests there count as yours: they get
comments or an `LGTM` comment, never an approval. Everyone else's, including the
reviewer account's, are reviewed and approved as usual, by that account. A missing
login for it fails only that repository. The local checkout's `origin` must also be
readable over that account's SSH key or credentials.

```json
{ "slug": "your-name/private-app", "path": "~/src/private-app", "slack": false,
  "github_account": "your-name" }
```

An entry may set `"jira_projects"` to the Jira project keys its pull requests name
work items in, such as `["APP", "WEB"]`; see
[what the pull request was asked to do](#what-the-pull-request-was-asked-to-do).

Important `.env` settings:

| Setting | Purpose |
| --- | --- |
| `REVIEW_ENGINE` | `antigravity`, `codex`, `claude`, `kiro`, or `command` |
| `REVIEW_MODEL` | Optional explicit provider model ID |
| `REVIEW_VERIFY_ENGINE` | Optional built-in adapter that verifies candidate findings |
| `REVIEW_VERIFY_MODEL` | Optional model for the verification engine |
| `REVIEW_FALLBACK_ENGINE` | Optional built-in adapter that reviews when the primary engine fails |
| `REVIEW_FALLBACK_MODEL` | Optional model for the fallback engine |
| `REVIEW_VERIFY_FALLBACK_ENGINE` | Optional built-in adapter that verifies when the verification engine fails; requires `REVIEW_VERIFY_ENGINE` |
| `REVIEW_VERIFY_FALLBACK_MODEL` | Optional model for the verification fallback |
| `REVIEW_EFFORT` | Optional reasoning effort for the reviewer: `low`, `medium`, `high`, `xhigh`, or `max` |
| `REVIEW_CODE_EFFORT` | Optional reviewer effort for the code level only, overriding `REVIEW_EFFORT` there |
| `REVIEW_VERIFY_EFFORT` | Optional reasoning effort for the verification engine |
| `REVIEW_FALLBACK_EFFORT` | Optional reasoning effort for the fallback engine |
| `REVIEW_VERIFY_FALLBACK_EFFORT` | Optional reasoning effort for the verification fallback |
| `REVIEW_TIMEOUT_SECONDS` | Timeout for each model invocation, retries included |
| `REVIEW_DESIGN_LEVEL` | Advisory solution-design level before the code review |
| `MAX_DESIGN_FINDINGS` | Findings one design review may report |
| `REVIEW_DOCS_LEVEL` | Advisory level checking the change against this repository's documentation |
| `MAX_DOCS_FINDINGS` | Documentation updates one docs review may propose |
| `REVIEW_VERIFY_TIMEOUT_SECONDS` | Base budget for one verification pass |
| `REVIEW_VERIFY_TIMEOUT_PER_CANDIDATE_SECONDS` | Added to that base for each candidate verified |
| `REVIEW_CONCURRENCY` | Pull requests reviewed at the same time, 1 to 8 |
| `RUN_LOG_RETENTION_DAYS` | Days a run transcript is kept, 1 to 365 |
| `MAX_DIFF_BYTES` | Diff one review reads; a larger change is reviewed in part |
| `MAX_REVIEW_BUNDLES` | Reviews one oversized change may be read in, 1 to 8 |
| `DESIGN_EXCLUDED_HEAD_BRANCHES` | Head branches whose PRs skip the design level |
| `EXCLUDED_PR_LABELS` | Case-insensitive labels treated like draft status |
| `IGNORED_MISSING_PATCH_PATTERNS` | Globs whose omitted patch needs no review, keeping the PR approvable; unset, a built-in list of lockfiles and other generated files |
| `GITHUB_COMMENT_TEMPLATE_FILE` | Markdown layout for GitHub inline findings |
| `GITHUB_COMMENT_TAGS_FILE` | Editable tag aliases for template values |
| `GITHUB_REVIEWER_USERNAME` | Optional reviewer identity override that skips the GitHub user lookup |
| `SLACK_WEBHOOK_URL` | Slack delivery and failure-notification webhook |
| `AUTO_REVIEW_DRY_RUN` | First live-delivery gate; keep `1` while testing |

See [.env.example](.env.example) for every setting and
[Review engine setup](docs/review-engines.md) for provider authentication and
adapter-specific commands.

### Where configuration and state live

| Layout | Configuration (`.env`, `repositories.json`) | State, run logs, worktrees |
| --- | --- | --- |
| Source checkout | The checkout root | `.state/` in the checkout |
| Installed copy | `~/.config/prwarden` | `~/.local/state/prwarden` |
| `PRWARDEN_HOME` set | `$PRWARDEN_HOME` | `$PRWARDEN_HOME/.state` |

`PRWARDEN_HOME` must be an absolute path set in the environment, not in `.env`,
because it decides which `.env` is read. An installed copy honours
`XDG_CONFIG_HOME` and `XDG_STATE_HOME`. Relative paths in `.env`, such as
`REPOSITORIES_FILE`, resolve from the configuration directory. Each layout keeps its
own state, so schedule only one of them on a machine.

## Usage

The examples run from a clone. With an installed copy, type `prwarden` in place
of `python3 -m prwarden`.

Display the installed project version:

```sh
python3 -m prwarden --version
```

Run a dry review of every eligible open pull request:

```sh
python3 -m prwarden
```

Target one pull request or temporarily select another engine:

```sh
python3 -m prwarden --repo your-org/example-app --pr 123
python3 -m prwarden --engine codex --repo your-org/example-app --pr 123
```

Every engine in the pipeline can be named for one run, so a combination can be tried
without editing configuration:

```sh
python3 -m prwarden \
  --engine kiro --model claude-opus-5.5 \
  --verify-engine claude --verify-model opus \
  --no-fallback \
  --repo your-org/example-app --pr 123
```

A model ID belongs to the provider that publishes it, so a model flag applies to the
engine beside it: `--engine kiro` alone drops the configured `REVIEW_MODEL` rather than
sending one provider's model to another, and `--model` alone applies to the engine
already configured. The same holds for `--verify-model` and `--fallback-model`, and
for the configured effort, which a replaced engine does not keep. Effort has no flag;
set `REVIEW_EFFORT` and its siblings in the environment for one run.
`--no-fallback` reviews with the primary alone, which is how a primary failure gets
reported instead of covered by a second engine, and it refuses to be combined with
`--fallback-engine` rather than guessing which was meant.

When the fallback and the verification engine are the same provider, startup says so:
one account's limit then stops both, which looks like two independent safety nets and
is one. It says so too when the reviewer and the verifier share a provider and the
fallback does not: while that provider is down the fallback can still review and
approve a clean change, but any finding it raises has no verifier to confirm it.

Dry runs invoke the selected model and may consume quota, but they do not post
or record terminal delivery state.

For live delivery, set `AUTO_REVIEW_DRY_RUN=0` in `.env` and pass `--live`:

```sh
python3 -m prwarden --live
```

Use `--force` only after inspecting GitHub, Slack, and local state when a
targeted head must be reconsidered:

```sh
python3 -m prwarden \
  --repo your-org/example-app \
  --pr 123 \
  --live \
  --force
```

## Reviewing several pull requests at once

`REVIEW_CONCURRENCY` sets how many pull requests are reviewed at the same time,
defaulting to `2`, and `--concurrency N` overrides it for one run:

```sh
python3 -m prwarden --concurrency 3
```

Reviews are limited by provider capacity rather than local CPU. Each slot is
another CLI invocation against one authenticated account, so a high value mostly
produces capacity rejections and wasted retry budget; 2 or 3 is the useful range.
The supported maximum is 8.

Parallel runs report in three phases. Every selected pull request is listed
first, so the plan is visible before any quota is spent. While reviews run, a
board shows one row per pull request with its latest step and elapsed time, plus
a row counting the reviews in progress. Each review's detailed report is kept
aside and printed in full once every review has finished, in the planned order
rather than the order they happened to complete.

With `REVIEW_CONCURRENCY=1`, or a single selected pull request, the run reviews
one at a time and reports each step as it happens, without a board.

`Ctrl+C` stops a run at any point. The review CLIs run in the same process
group, so they receive the interrupt too and stop with it. An interrupted
parallel run still prints what its reviews had reported before printing the
interrupt notice, so nothing captured for the log is lost. Interrupting during
delivery is the one case to avoid, because a GitHub or Slack write that was
already in flight may still land while local state records it as uncertain.

Concurrency changes only scheduling. Each review still fetches its own immutable
head into its own detached worktree, git operations against a shared local
checkout are serialized, delivery state stays atomic, and the process lock still
rejects a second live run.

## Review levels

A review runs in three levels, each reviewed by `REVIEW_ENGINE` and judged by its own
adversarial verification pass on `REVIEW_VERIFY_ENGINE`.

The **solution-design level** goes first and judges the approach rather than the
lines: where the change puts responsibility, what contracts it establishes, how
state and data flow, and whether this workspace already provides what the change
re-implements. Its findings carry no anchor line, so they are never inline
comments. They post as one advisory pull-request comment, go to Slack for your own
pull requests unless the repository is kept out of Slack, and **never block a merge,
withhold an approval, or change the run's exit code**. A design-level failure is
reported and the code review continues unaffected.

The design level runs on a pull request too large to review completely, because the
shape of a change is exactly what nobody can read all of. It does **not** run on a
release promotion: `DESIGN_EXCLUDED_HEAD_BRANCHES` lists head branches, `develop` and
`release/*` by default, whose changes were already designed and reviewed on the
feature pull requests that reached them.

The design level runs **at most once per pull request**, not once per push. It
judges the shape of the whole change, so a later head rarely changes its answer
enough to be worth another full review, and re-reviewing every push earned a second
advisory comment saying much the same thing. Once it has settled, later runs report
that and review only the code level; a design level that *failed* has settled
nothing and is attempted again. Use `--force` on a named pull request to review its
design again, for instance after a rewrite. Set `REVIEW_DESIGN_LEVEL=0` to switch
the level off, which removes its cost entirely.

The **documentation level** then asks whether the change leaves this repository's own
documentation saying something untrue, for the people reading the README and `docs`
and for the coding agents reading `AGENTS.md`, `CLAUDE.md`, `.cursor/rules`, or
`.kiro/steering`. It finds those documents in the reviewed head rather than being
told where they are, so a repository with no documentation costs nothing, and it
lists them for the reviewer to open rather than pasting them into the prompt.

Each proposal names the documentation line that is now wrong, the changed lines that
made it wrong, and the replacement text to paste, written in that document's voice.
It never proposes a code change: where the document is right and the code is wrong,
that belongs to the code level. Documentation that is merely silent about an internal
detail, or that is still true but could read better, is not reported. The exception
is a list or table of scripts, commands, or guides that now lacks an entry the change
added: its proposal is just the new rows or items, in the list's own format. Like the
design level it is advisory, skips release promotions, delivers a separate
pull-request comment, and set `REVIEW_DOCS_LEVEL=0` to switch it off.

It runs once per pull request, plus once more after any push that changes what the
pull request does to its documentation, so a repository that is mostly documentation
is checked as it is written. Only the documentation lines the pull request adds or
removes count: a push that changes only code, or a rebase that moves the same edit,
does not recheck. A push that changes only code can still make a document wrong,
which this does not catch; `--force` checks again on demand. A recheck reads the
notes this level already posted, so it does not propose the same edit twice.

The **code level** then reports line-level correctness, reliability, data-loss,
concurrency, and security defects on added lines, delivered as inline review
comments or an approval exactly as before. Approval authority belongs to this level
alone.

Both levels hold citations to the same standard: a cited path must resolve in the
reviewed worktree and the excerpt must be that line's exact text. Unverified design
findings are never delivered, so a failed design verification delivers nothing
rather than advisory notes nobody checked.

## Review outcomes

- Draft PRs and PRs with excluded labels are skipped.
- Confirmed findings on team PRs become inline GitHub review comments.
- Documentation the change outdated is reported as one advisory pull-request comment
  proposing the replacement text, never as an inline comment and never as a reason to
  withhold an approval.
- A clean, mergeable team PR receives a bodyless GitHub approval, unless one of its
  checks failed at that head. The checks are read again just before approving,
  since CI often finishes during a review; the approval, or the `LGTM` for your own
  PR, waits until a re-run passes or a new head arrives, and findings are still
  delivered. A cancelled, skipped, or unfinished check does not hold it back.
- The authenticated reviewer's own PR is never self-approved; findings or a
  clean mergeable `LGTM` result go to Slack. An `LGTM` reached by rejecting
  candidates also reports how many verification threw out, which distinguishes
  it from a review that raised nothing. In a repository kept out of Slack, the
  same results go to the PR instead: findings as a comment review, and a clean
  mergeable result as an `LGTM` comment naming the head, the reviewer, and the
  verifier, since GitHub does not let an author approve their own PR. When the
  review raised no candidates, the comment says the verifier had nothing to check
  rather than crediting it with a verification. Once a later head's result is
  posted, the earlier `LGTM` comments are marked as outdated, which collapses them
  without deleting them, so only the current verdict shows.
- A PR that is merged or closed while being reviewed still receives its confirmed
  findings, on GitHub or in Slack, but never an approval or an `LGTM`. The defect is
  in the base branch by then, and the review already verified it against the exact
  head. A PR that became a draft or gained an excluded label delivers nothing,
  because that is the author asking for no review.
- Merge conflicts, indeterminate mergeability, incomplete patches, and validation
  errors stop delivery for that PR.
  A conflict only blocks an approval, so confirmed findings are still delivered on
  a conflicted PR. The conflict is recorded together with the base commit it was
  measured against, and while neither the head nor that base has moved the PR is
  skipped instead of reviewed again, because the result could only be discarded a
  second time. When the base branch moves, GitHub is asked whether the PR still
  conflicts before any review is spent, and it stays skipped while it does. A
  new head or a cleared conflict reopens it, and `--force` overrides the skip.
  GitHub omits the patch of a generated file whose diff is too large, most often
  a lockfile. That file is reviewed around rather than stopping the PR: the rest
  of the change is reviewed, the path is named wherever the result is reported,
  and the approval is withheld because nobody read it. A path matching
  `IGNORED_MISSING_PATCH_PATTERNS` is said to be generated and to need no review,
  which ignores it outright and leaves the PR approvable. Left unset, that setting
  is a built-in list of dependency lockfiles (npm, Yarn, pnpm, Bun, Deno, Poetry,
  uv, Pipenv, PDM, Cargo, Go, Bundler, Composer, Pub, CocoaPods, Swift Package
  Manager, NuGet, Mix, and Nix), `*.tsbuildinfo`, and Wrangler's
  `worker-configuration.d.ts`, each at any depth; images, minified bundles, source
  maps, and test snapshots are left out because each can hold a line worth reading.
  A list of your own replaces the built-in one, and `[]` ignores nothing. Changing
  the setting reopens the PRs already recorded as unapprovable over an unread file,
  each for one more review. A repository can name its own generated files too: a
  path its `.gitattributes` marks `linguist-generated`, the mark GitHub uses to
  collapse a generated file in a diff, counts as a match, and one it unmarks with
  `-linguist-generated` stays unread even when the list names it. The marks are
  read from the base branch, never from the pull request, so a pull request cannot
  mark its own new file as generated, and a mark added there later reopens a PR
  recorded as unapprovable over that file. When the base cannot be read, every
  omitted file stays unread, a listed one included, since any mark might have been
  an unmark. Reading the marks needs Git 2.40 or newer; with an older Git they are
  not read and the list alone decides, as before. Withholding an
  approval this way is the controller's own decision, so it is not reconsidered
  while the head and those settings are unchanged; without that, every run would
  spend a full review to reach the recorded answer again. A binary file needs no such pattern:
  GitHub counts no changed line in it, so it is reported as having nothing to
  review and the rest of the PR is reviewed normally.
- A diff too large for one review is read in several. The files that did not fit the
  first bundle are reviewed in further bundles of whole files, up to
  `MAX_REVIEW_BUNDLES`, and a change every bundle covered is a complete review that can
  be approved — where before it was a partial review that never could. Only an oversized
  change is bundled, so the ordinary path is untouched, and `MAX_REVIEW_BUNDLES=1` keeps
  the single review this tool has always done. A file is never split across bundles,
  because a reviewer that saw half a function would cite lines nobody can reproduce, so
  a file too large for the budget on its own is still named and left unread. Each bundle
  is another invocation, which is what the cap limits; whatever the cap leaves over stays
  deferred exactly as below. A bundle speaks for its own files: it may add findings and
  it may take an approval away, but no bundle can grant one, because an approval speaks
  for the whole pull request. This is rare — measured across 394 reviewed pull requests,
  1.8% were ever limited by size and 1.0% materially, which is why the change is
  confined to the path that fires.
- A diff too large to read completely is reviewed in part rather than skipped. The
  budget is filled largest changed file first, the deferred paths are named in the
  Slack notification and in the GitHub review body, no approval is given, and no
  finding can be raised against a file the reviewer never saw. That notification is
  sent once per PR, not once per push. A PR where not one file fits `MAX_DIFF_BYTES`
  is not reviewed at all, and either way the rest of the run continues.
- Review feedback that arrives while a review is running is measured rather than
  treated as a blanket failure. A comment that disappeared, or that only became
  resolved, takes an objection away, so a result reached without knowing that is
  still delivered. A new thread, a reopened one, or an edited one may say
  something the review never read: an approval speaks for the whole PR and is
  always withheld against it, while findings speak for their own lines and are
  still delivered unless the new feedback shares one of those lines. A shared line
  is not proof of a duplicate, but it is proof that nobody local can tell them
  apart, so nothing is posted there. Holding a result this way is a skip, not a
  run failure: it leaves the exit code alone, keeps the head retryable, and still
  sends a Slack notification naming where the feedback landed, because deciding
  whether to run the reviewer again is yours.
- A clean result never creates a GitHub comment.

A repository can tell the reviewer what it is for. Commit
`.prwarden/background.md` and every level reads it from the reviewed head:
what the product is, what its money or units mean, what a reader is assumed to know.
It is framed as untrusted context in the prompt, capped, and may add background
rather than authority, so it can never relax a review rule. A repository without the
file costs nothing.

Every level also reads why the changed code is as it is: the latest three commits on
the base branch that touched each file the change edits, for its eight most-changed
files and at most twelve commits in all. A commit message often says why a guard, a
limit, or a workaround exists, so a change that removes one can be checked against
the reason it was added. Only the messages are read, never their diffs, and merges
are left out. They are framed as untrusted context like the background note, and
history that cannot be read is reported in the run log and left out, never a reason
to stop the review.

### What the pull request was asked to do

A diff shows what changed, not why. Every level also reads the issues a pull
request names, as the intent behind it:

- the GitHub issues GitHub records it as closing, such as one its description
  closes with `Fixes #12`;
- the Jira work items its title, description, or branch name names, for a
  repository whose entry lists their projects in `"jira_projects"`. They are read
  with Atlassian's own CLI, `acli`, signed in with `prwarden setup --jira` or
  `acli jira auth login`. Setup's API-token sign-in reads the token hidden and hands
  it to `acli` on standard input; PR Warden never prints or stores it, and a review
  uses only `acli`'s own sign-in.

Up to three of each are read, their text capped, and framed as untrusted content
that can never relax a review rule. A finding names an issue by its reference and
never quotes it, because a pull-request comment can reach readers the tracker does
not. An issue that cannot be read is reported in the run log and left out; the
review goes ahead as it would without it. Only the project keys a repository lists
are looked for, because a bare `KEY-123` pattern also matches `UTF-8` and
`ISO-8601`.

With that intent stated, by the description or a linked issue, the advisory design
level also checks scope: a changed file nothing stated explains, and that no
explained change needs, is reported as `change_outside_stated_intent`. A test, a
type, a migration, a lockfile, or a document that follows from an explained change
counts as explained. Like every design note it never blocks a merge.

A repository can also set the rules its reviews hold the code to. Write them under a
"Code review" heading in its agent instructions, such as `AGENTS.md`, `CLAUDE.md`,
`.kiro/steering/*.md`, `.cursor/rules`, or `.github/copilot-instructions.md`:

```markdown
## Code review

- Never log a customer's email address.
- Every new API endpoint checks the caller's permission.
```

The code level then reports an added line that breaks one of these rules as a defect,
of any kind, style included, and it withholds the approval like any other finding.
The rules are read from the base branch, never from the pull request, so a pull
request cannot write the rules it is judged by: to change a rule, merge that change
first, or change the code. A rule can only add requirements, never relax PR Warden's
own. Each such finding quotes the rule it breaks, which the controller checks against
the base branch's text and the comment shows with the file it came from, and the
verifier confirms it like any other finding. A repository without a "Code review"
section is reviewed exactly as before.

PR Warden's own code level reports correctness, reliability, data-loss, concurrency,
and security defects. It does not report performance or a pull request's scope,
because without a project's own standard either one is a matter of taste, and taste
is noise. A rule is how a project sets that standard. Each finding sits on one added
line, so write a rule that a single line can break:

```markdown
## Code review

- No database query or network call inside a loop over a request's items.
- Every list endpoint is paginated, with a maximum page size.
- No synchronous file or network I/O in a request handler.
- A pull request changes one feature or fixes one bug; refactors and dependency
  upgrades go in pull requests of their own.
```

A performance rule like the first three works well, because the line that breaks it
shows it. A scope rule like the last one works less well: the finding lands on a
line that plainly belongs to the unrelated part of the change, or nowhere. A rule
too vague to check on a line, such as "code must be fast" or "keep pull requests
small", is either thrown out by the verifier or becomes noise.

Database migrations suit rules well, because the migration file itself shows the
break:

```markdown
## Code review

- Never edit a migration that has been merged; add a new one.
- A migration that drops a column or a table, or rewrites existing rows, says in a
  comment how to undo it.
- A migration never renames or drops a column in the same pull request as the code
  that stops reading it; the code change merges and deploys first.
```

An edit to a merged migration lands on its changed lines, and a file the base branch
already has is a merged one. A missing undo comment lands on the line that drops or
rewrites. The last rule is a scope rule, so it is caught only when both changes are in
one pull request.

The controller validates cited evidence against the exact PR worktree and uses
a separate adversarial verification pass for candidate findings, optionally on a
second engine and model. That pass judges each candidate by reference and never
restates one, so a confirmed finding is delivered with the citation the controller
already validated; the only change it can make is the severity. When the primary engine itself fails, an optional
fallback engine reviews instead, at the design, documentation, and code levels alike, under identical validation, and the delivered
attribution names the engine that actually reviewed. A claim about a dependency's API must cite the installed
package at the resolved version, so the controller links the checkout's installed
dependencies into the worktree when the reviewed head's lockfiles match, and links
the individually unchanged packages when a lockfile differs, reporting why
whenever evidence is partial or absent. Findings claiming the head cannot compile
are rejected when an exact-head build or typecheck check succeeded. Detailed behavior and delivery safety rules are in
the [review specification](docs/auto_code_review_spec.md).

GitHub inline finding layout is configurable without changing Python code. See
the [GitHub comment template how-to](prwarden/templates/README.md) for the Markdown
template, complete tag reference, examples, local overrides, and validation
rules.

## Measuring a change to the reviewer

Every prompt, model, or setting change until now was justified by reasoning and
confirmed by watching production afterwards. `--metrics` writes what a run's reviews
decided, so two runs can be compared:

```sh
python3 -m prwarden --metrics /tmp/before.json
# change a prompt, a model, or a setting
python3 -m prwarden --metrics /tmp/after.json
python3 -m prwarden.compare_metrics /tmp/before.json /tmp/after.json
```

The comparison lists the numbers that moved first:

```
before: antigravity gemini-3.1-pro-high
after:  claude gemini-3.1-pro-high

7 numbers moved:
  + failed_invocations: 0 -> 2
  - seconds: 231.946 -> 3.646
  * verdicts.do_not_approve: 1 -> none
```

Each measurement holds one record per model invocation — level, wall-clock, verdict,
candidate count, what verification confirmed or rejected and under which class,
whether the controller had to ask for a correction, and the failure when one
happened — plus the run's aggregate. It works in dry runs, so a comparison costs
quota but posts nothing, and it works in live runs, so scheduled runs accumulate the
same numbers over time.

### Replaying the same work twice

A comparison over open pull requests decays: heads move and pull requests merge. Pin
them instead, and the input never changes again:

```sh
cp evals/cases.example.json evals/cases.json    # or capture the open ones:
python3 -m prwarden.evaluate capture
python3 -m prwarden.evaluate replay --metrics /tmp/baseline.json
python3 -m prwarden.evaluate replay --metrics /tmp/baseline-again.json   # the noise floor
python3 -m prwarden.evaluate replay --arm claude --metrics /tmp/claude.json
python3 -m prwarden.compare_metrics /tmp/baseline.json /tmp/claude.json
```

A case pins the head and the merge base it was reviewed against, so a merged pull
request stays replayable: the diff is rebuilt from the local checkout rather than from
GitHub, whose files API only reports the current head. `evals/cases.json` and
`evals/arms.json` name real repositories, so both are ignored by Git; commit nothing
but the examples.

An arm is a named set of settings in `evals/arms.json` — another engine, a level
switched off, a different budget — and the measurement records which arm ran, the
reviewer's own commit, whether that tree was dirty, and a fingerprint of the case
set. A prompt is code rather than a setting, so the commit is what identifies it.

### Where the labels come from

`python3 -m prwarden.evaluate label` builds the corpus out of delivered history rather
than asking anybody to read pull requests. For every inline finding this reviewer has
posted, it asks what the author did next: a finding whose lines were edited in a later
push is a finding that was worth posting.

```
Mined 47 cases and 66 labelled findings
   39  acted_on
   10  changed_elsewhere
    7  nothing_pushed_after
    5  not_acted_on
    5  acted_on_after_rebase
```

Each case is pinned at the commit its findings were posted against, because a label is
a line number and a line number only means something at the commit it was read from.
Only `acted_on` is scored: a comment nobody acted on is not evidence either way, since
the team may have disagreed with it, deferred it, or merged past it. A rebase is
reported apart because it rewrites everything and so proves nothing.

A replay then reports two numbers, and the gap between them is the diagnosis:

```
-> Raised 2 and confirmed 0 of 2 findings authors acted on
```

`raised` is what the review proposed and `confirmed` is what survived verification, so
a finding the review still raises but the verifier now throws out is a verification
change rather than a weaker review, and the two need different fixes.

Every harness command writes its own transcript beside the reviewer's, as
`.state/run-logs/eval-<command>-<date>-<time>-<pid>.log`, pruned by the same
`RUN_LOG_RETENTION_DAYS`. A replay's evidence is the verifier's reason for throwing out
each candidate, and reading that should not cost another hour of model time. Mining one
repository with `--repo` keeps the cases already pinned for the others, because scoping
the work is not scoping the corpus.

A label also records which reviewer and which verifier delivered it. Every acted-on
finding is scored whichever pipeline first posted it, because a defect an author fixed
is real whoever found it, and it is exactly what a new reviewer model has to find
again: scoring only the current pipeline's own findings scored nothing at all once the
reviewer changed. What the origin still decides is how a rejection reads:

```
-> Raised 2 and confirmed 1 of 3 findings authors acted on; missed app.ts:40, api.ts:12
-> First judged here: app.ts:40 reached the author with no verification pass, so throwing it out is not a stricter verifier
```

This is not pedantry. Sixteen of this corpus's first thirty-nine acted-on findings were
posted before any verification pass existed, and one of them looked exactly like a
verifier that had grown too strict: the replay raised it and the verifier threw it out,
while the author had acted on it in production. The comment said why — it was delivered
by an older model with no verifier configured at all, so today's verifier was not
rejecting what it once confirmed, it was reading that claim for the first time.

`--labelled-only` replays only the cases carrying a finding authors acted on, since
any other case spends invocations to score nothing. `--concurrency N` replays N cases
at a time, up to 8; each case's report is held until it finishes and printed whole,
and the measurement is the same as one at a time, because only when a case runs
changes, never how it is counted:

```sh
python3 -m prwarden.evaluate replay --labelled-only --concurrency 3 --metrics /tmp/baseline.json
```

The run ends with the verifier's own number beside the reviewer's, here from a one-case
code-only replay on Kiro:

```
Raised 1 and confirmed 1 of 1 findings authors acted on
The verifier kept 1 of the 1 labelled defects it was shown
```

Judge a verifier by the second line, its retention of real defects, not by the
confirmation rate in the summary. In the September verifier experiment the arm that
kept 4 of 5 labelled defects had the lower confirmation rate of the two, because it
also threw out more of everything else.

Recall alone cannot tell a better reviewer from a noisier one, so a replay also names
what it confirmed that matches nothing authors acted on:

```
-> 2 confirmed findings match nothing authors acted on: api.ts:12 (unacted), app.ts:7 (unlabelled)
```

`unacted` sits on a comment its author left alone, and `unlabelled` on a line nobody
commented on. Neither is called a false positive, because an unlabelled finding may be
a real defect nobody had found yet, so the run's `confirmed.unmatched_rate` is an
upper bound on noise. What it catches is a change that raises recall by confirming more
of everything.

That bound was loose in practice. Of six such findings one replay confirmed, four sat on
lines their author changed before merge. Mining therefore records where each pull request
ended, and a replay checks every unmatched finding against it with the same rule a
label uses, tagging it `fixed later` when its author changed those lines afterwards:

```
-> 2 confirmed findings match nothing authors acted on: app.ts:37 (unlabelled, fixed later), api.ts:118 (unlabelled)
```

The run reports `confirmed.fixed_later` and what remains as `confirmed.unexplained`,
the tighter bound. Like a label it is a proxy, since a line can change for another
reason, and a rebase proves nothing, so a pull request whose final head does not
descend from the reviewed one marks nothing as fixed later.

A replay also gives the review the pull request's title and description, which mining
and `capture` now record, because a live review reads them and they state intent: the
sixth finding above called an intended change of behaviour a regression, while the
description said that was the change. The description recorded is the pull request's
current one, which may have been edited since the pinned commit.

Mining also pins control cases: pull requests the reviewer approved on GitHub that then
merged with no further push, and that it never commented on. Each condition is checked
against GitHub, because a delivered record does not say whether it was an approval.
`--controls` replays them, alone or together with `--labelled-only`, and the run
reports how many it would approve again, in this form:

```
Approved 11 of 12 control cases, which needed nothing
  withheld on owner/app#12@1a2b3c4d5e6f: a defect remains
```

A control is judged on the decision a live run would deliver: a bundle that confirms a
finding takes the approval away, a partial review never approves, and a bundle that
fails leaves the decision undecided rather than approved. A code-level failure is a
failed case, never a clean one. Merged unchanged is not proof the code had no defect,
only that nobody found one before it shipped. `compare_metrics` reports the `labels`,
`confirmed`, and `controls` numbers beside the engine's own.

The honest limit: this measures whether the reviewer still finds what it already found
and authors acted on. It cannot measure what the reviewer never found, so it is a
regression set, not a ground truth, and a falling reproduction rate is a warning rather
than a verdict. The unmatched findings and the controls bound the other side, noise,
without measuring it exactly.

### Judging the verifier without paying for a review

A replay has two halves: the review proposes candidates, then the verification pass keeps
or throws out each one. The first half spends the provider budget — one pass of seventeen
cases is about a day of it — and the second runs on another provider entirely. So every
replay records what its review proposed, and `verify` re-judges those recordings without
reviewing anything:

```sh
python3 -m prwarden.evaluate replay --labelled-only --metrics /tmp/baseline.json
python3 -m prwarden.evaluate verify --metrics /tmp/strict-verifier.json
```

Measured on one real case: the replay took 8m40s across two invocations, the re-judge 58s
across one. That is what makes a verifier question askable at all — a different
verification model, a different evidence standard, a different effort level — for the price
of the verifying provider rather than a day of the reviewer's.

It also removes the reviewer from the comparison. Holding the candidates fixed means a
difference between two verifiers is the verifier, where a full replay would mix it with the
reviewer's own run-to-run variance, which is large enough to hide a finding or two.

A `verify` measurement names the verifying engine and model, because those are what it
called, and records no arm, because no arm may describe the verifier. Two verification
passes are therefore told apart by their `reviewer.commit` — which is the honest answer
when the thing under measurement is a prompt, since a prompt is code and no settings file
can name it.

Read the result for what it is: this measures whether a verifier judges *these* candidates
better, not whether the pipeline finds more defects. That is the right question for a
verification change and the wrong one for a review change. `evals/candidates.json` names
real repositories, so it is ignored by Git like the cases and labels.

### Measuring an advisory level alone

The design and documentation levels do not run at every head, so their measurement
names the pull request and the head to judge directly, and runs only the level asked
for:

```sh
python3 -m prwarden.evaluate advisory --level docs \
  --pr owner/app#12@1a2b3c4d --pr owner/app#15 \
  --baseline-verifier HEAD~1 --metrics /tmp/docs-verifier.json
```

`--pr` reads the pull request from GitHub and pins it at the named head, or at its
current one; a merged pull request is pinned against the branch it left, and nothing is
added to the case set. `--case` takes pinned cases instead. `--baseline-verifier REF`
has the same engine judge the same proposals under the verifier prompt committed at REF
before today's verifier judges them, so the two verdicts differ only by the prompt:

```
      -> [LOW] missing_list_entry: README commands table lacks the new export command
         now: confirmed
         at 318e53ef43f3: rejected because the table never claims to be complete
Proposed 7 documentation candidates across 4 cases; confirmed 4 now
Judged 7 at 318e53ef43f3 too: confirmed 1 there, 3 only now and 0 only there
```

That pairing is what the documentation category for an entry a list lacks was measured
with, by hand, before this command existed: three verifier wordings, each judged against
the one before on the same proposals, and the first two each let a neighbouring proposal
through that a single run could not have told from the reviewer's own variance.
`--metrics` records every proposal in full with both verdicts and reasons, the reviewer's
commit, the baseline's commit, and what the case spent. A baseline that cannot answer is
recorded as an error and judges nothing, and a level that fails makes the run exit 1.
Cases run one at a time, because the baseline prompt is swapped in for each call.

`--passes N` replays the same pins several times in one command, which is what a
nondeterministic reviewer needs to be measured at all:

```
Across 2 passes the reproduction rate was 0.381, 0.381, a spread of 0.0
  5 findings reproduced in every pass, 6 in some, 10 in none
  6 findings moved between passes, so a rate alone cannot tell a change from this:
```

Read the second line, not the first. Two passes can report an identical rate over a
different set of findings, which is how a measurement looks stable while measuring
nothing. The findings reproduced in every pass are the part of the corpus this reviewer
holds dependably, and they are the only part a single pass could have reported honestly.

**Run the baseline twice before believing any difference.** These models are
nondeterministic. Two baseline replays of one real pull request produced 2 candidates
then 1, moving the confirmation rate from 0.5 to 1.0 with no change to the reviewer at
all. A difference smaller than that spread is noise, not an improvement.

A replay bundles an oversized change exactly as a live review does, so the setting that
decides how much of a large pull request gets read is measurable rather than taken on
trust. Verified on one pull request [pr:pn2j] with the budget lowered to force it: 2
bundles covering all 11 files, one confirmed finding in the first and one candidate the
verifier threw out in the second, 7m09s across 5 invocations, and no partial review —
where a single review would have read 4 files and refused to approve.

A replay leaves out what belongs to a live pull request: eligibility and
mergeability, prior review comments, and delivery. So duplicate suppression is not
exercised, nothing is ever posted, and no state is recorded.

Two rules keep a comparison honest. Compare runs over the same pull requests at the
same heads, because a different set compares nothing; the report names each side's
engine and model so a mismatch is visible. And treat the numbers as evidence about
the reviewer's behaviour, not about its quality: a lower confirmation rate could mean
a stricter verifier or a weaker review, and only reading the findings tells you
which.

## State, errors, and scheduling

The default `.state/review-state.json`, or `review-state.json` in the state
directory of an [installed copy](#where-configuration-and-state-live), provides
atomic, single-host delivery idempotency. A process lock rejects overlapping live runs using that state file.
Dry runs do not take the lock.

A live run that meets the lock is a safe skip, not a failure: it reports the run
already holding it, exits `0`, and sends no notification, because the run it met
is reviewing the same pull requests against the same state. This is what a
scheduled run does when a manual one is still going, and a scheduled slot lost
that way is not retried before the next one. A state file that genuinely cannot
be used is still a failure.

Each reviewed pull request ends with what it spent, and the run ends with the total:

```
      -> Spent 9m12s of model time across 3 invocations (code, design, docs)

Spent 41m06s of model time across 11 invocations (code 4, code verification 2, design 3, docs 2)
```

A pull request that was skipped reports nothing, because it spent nothing. The
engines are coding-agent CLIs that do not report tokens, so this counts invocations
and the wall-clock they took, which is what a scheduled slot actually spends. A
review restarted by a moved head, or carried by the fallback engine, shows up here as
the extra invocations it was. An engine that reports what a call cost adds it, which Kiro does after every call, and
each stage then shows its own share:

```
      -> Spent 1m42s of model time and 8.38 credits across 2 invocations (code 4.12, code verification 4.26)
```

Once credits appear, a stage called more than once says so in calls, as in
`code 8.24 over 2 calls`, so no count can be read as a cost. The total covers only
calls whose engine reported a cost, so a run with an Antigravity fallback in it costs
more than the credits shown, and that stage reads `code fallback with no credits
reported`. A `--metrics` measurement records each call's credits and their total by
label.

Every run also copies what it reports to
`.state/run-logs/run-<date>-<time>-<pid>.log` and prints that path after the
banner, so a manual run's errors survive the terminal it scrolled past. The copy
holds reported text only, without the progress animation or board frames.

A parallel run also writes a timestamped line to that file as each step is
reported, before the grouped per-pull-request reports it prints at the end:

```
13:21:04  example-app #130  -> Running antigravity review (timeout: 10m)...
13:21:07  example-library #69  -> Linked 1249 installed node_modules packages at unchanged versions
```

The board draws on the terminal alone and each review's report is held until
every review finishes, so this timeline is the way to follow a run in progress,
including a scheduled one: `tail -f` the path printed after the banner. Every
line is flushed as it is written.

A rejected citation reports both texts whole. A mismatch is usually a re-quoted
template literal, a normalized quote character, or collapsed indentation, and the
part that differs is exactly the part a shortened excerpt used to hide. Where the
excerpt does resolve to lines the controller can find, the rejection names them:
an excerpt matching one line is silently re-anchored to it, and an excerpt matching
several says which, so a correction can cite the one it meant instead of guessing.
The Slack failure notification bounds itself the same way — it leaves whole errors
out and says how many, rather than cutting one off mid-sentence.

A review whose candidates verification rejected also reports each rejected
candidate with the reason the verifier gave for that specific candidate, so a
strict verifier can be told apart from a review producing weak candidates. Each
such line also carries one bracketed class — `not_a_defect`, `already_handled`,
`wrong_anchor`, `insufficient_evidence`, or `unclassified` when the verifier
offered none — so a period of transcripts tallies with one command:

```sh
grep -ho "Rejected \[[a-z_]*\]" .state/run-logs/*.log | sort | uniq -c | sort -rn
```

`wrong_anchor` is the one to watch: it counts real defects thrown out over a
citation, which is the only kind of finding a verifier judging by reference can no
longer rescue by re-citing it. The class is recorded and counted, never acted on. Rejected candidates are
unconfirmed by definition and appear only on the terminal and in the transcript,
never on GitHub or in Slack. Raise `RUN_LOG_RETENTION_DAYS` when they are worth
keeping longer than a week.

Each run then deletes transcripts older than `RUN_LOG_RETENTION_DAYS`, which
defaults to `7`, so a week of runs stays readable regardless of how often the
tool ran. Runs that overlap keep separate transcripts: only a live run takes the
process lock, so two runs can start in the same second, and a transcript is never
opened over an existing one.

Exit codes:

- `0`: every selected PR completed or was safely skipped
- `1`: at least one dependency, review, validation, or delivery operation failed,
  or the run hit a defect in this tool; either way a live run sends its Slack
  failure notification, and a defect's traceback is printed to the terminal
- `2`: startup configuration is invalid
- `130`: the run was interrupted with `Ctrl+C`

A provider that has run out of quota is told apart from one under momentary load, because
only one of the two can be waited out. A refusal naming a spent budget — an individual
quota, a session or usage limit, or a reset more than a minute away — is not retried, and
the first pull request to meet it settles the question for the whole run: later reviews go
straight to the fallback engine instead of spending another refusal to learn the same
thing, the advisory levels included, and with no fallback configured the advisory levels
report it instead of paying for it. Only the primary's refusals count: a fallback that
has run out says nothing about whether the primary can answer the next pull request.
That knowledge lasts for one run only, so the next scheduled run finds out for itself.
This is worth what it saves: on 2026-09-22 a spent daily quota cost 8m34s per pull request
in retries that could not have succeeded, which delayed each fallback long enough that the
fallback provider hit its own session limit and four pull requests went unreviewed.

Read-only GitHub operations retry recognized transient failures. GitHub writes
are not automatically retried because a lost response does not prove the write
failed. When a GitHub CLI command still fails after its retries, the run checks
GitHub's public status page once and reports any announced incident on the
terminal and in the Slack error notification, so a platform outage is not
mistaken for a local fault. If a PR receives a new commit during analysis, the
stale result is discarded and the complete review restarts against the new head,
up to five restarts per PR. Live runs can send one aggregated Slack error
notification after processing, while expected eligibility changes are reported
only as safe skips.

After targeted dry and live tests succeed, `prwarden schedule install` runs it on
a timer; see [scheduling](docs/scheduling.md), and
[Scheduling on macOS](docs/macos-scheduling.md) for a hand-written LaunchAgent or
cron entry.

## Development and releases

Project instructions for coding agents are in [AGENTS.md](AGENTS.md). Human
contributors can also use the linked architecture, code conventions, quality,
workflow, and release guides, and [CONTRIBUTING.md](CONTRIBUTING.md) says which
issues and pull requests are welcome. [Threat model](docs/threat-model.md) records what this
tool trusts, what stops each threat, and what risk remains in running an agent CLI
over untrusted content.

The project follows [Semantic Versioning](https://semver.org/) and
[Conventional Commits](https://www.conventionalcommits.org/). See
[CHANGELOG.md](CHANGELOG.md) for release history and
[Releases](docs/releases.md) for the release command and checklist.

## Security

To report a vulnerability, follow [SECURITY.md](SECURITY.md) rather than opening a
public issue. [The threat model](docs/threat-model.md) describes what the reviewer
defends against and the risk that remains.

## License

Copyright 2026 Pylyp Borysov. Licensed under the
[Apache License, Version 2.0](LICENSE); see [NOTICE](NOTICE).
