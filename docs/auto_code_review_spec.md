# Specification: Automated GitHub and Slack Code Review Workflow

## Objective

Periodically inspect ready-for-review open pull requests in user-configured
GitHub repositories, review each eligible head commit through a configured
review-engine adapter, and route confirmed correctness or security findings:

- Team pull requests with findings: one GitHub review containing inline comments.
- Team pull requests without findings: one `APPROVE` review without a body,
  or, for a review-policy change a person signed off, one naming them;
  `LGTM` is terminal output only.
- Configured reviewer's pull requests: one private Slack message.
- Configured reviewer's pull requests without findings: after final head and
  mergeability checks, send an `LGTM` Slack message stating that the PR can be
  merged once required checks and branch protections pass. Never approve it on
  GitHub because authors cannot approve their own pull requests.
- No clean result, approval or `LGTM`, is delivered while a check on the exact
  head that holds it has failed. Just before delivering one, the controller reads
  the head's check runs and commit statuses again, because CI often finishes while
  a review runs, and a conclusion of `failure`, `timed_out`, `startup_failure`, or
  `action_required`, or a status of `failure` or `error`, withholds it: the head
  failed a test the team chose, and no review verdict outweighs that. A cancelled,
  skipped, neutral, or still-running check does not withhold, since it says nothing
  about the code. Which failed checks hold it is GitHub's answer, read after the
  checks so a check that reports in between is never missed: each check's
  `isRequired` for this pull request, from the base branch's protection and
  rulesets (`github.check_requirements`). When the branch requires some check, a
  failed check GitHub says it does not require does not withhold; the clean result
  is delivered and then followed, once per head, by a `## Checks to fix` reminder
  naming those checks, kept under its own `github:reminder`-style destination and
  carrying the waiting-note marker, so a later result minimizes it like a waiting
  note. A reminder that definitely was not posted is tried again by the run that
  skips the approved head, while those checks still fail, without a reading. When
  the branch requires no check, when GitHub's answer cannot be read
  whole, or when a failed check is missing from it, every failed check withholds,
  as before GitHub was asked. Findings are delivered as usual. The record names the
  failed checks that withheld it, and while any of them still fails a later run
  skips it without reviewing; a re-run that passes, a later head, `--force`, or
  GitHub no longer requiring any of them reopens it. Checks that cannot be read
  keep it skipped.
- No clean result is delivered for a pull request that changes review policy until
  a person signs it off, because a reviewer that approved it alone would be signing
  off its own reconfiguration.
  Policy is any file under `.prwarden/` or `.github/workflows/`, any
  `.gitattributes`, a `CODEOWNERS` at the root, in `.github/`, or in `docs/`, and
  the "Code review" sections of the instruction files the rules are read from
  (`review.review_policy_changes`). An instruction file counts only when those
  sections differ between the reviewed head and the commit its diff is taken from,
  where the head left the base branch's tip, which is what GitHub's list of changed
  files is measured against; comparing with the tip itself would blame a rule change
  merged since on every open pull request that edits the same file. Editing the rest
  of an `AGENTS.md` does not count. Each side holds the rules a review would read
  there, so a rename off the instruction-file paths removes the rules and a rename
  onto them adds some, and a link, or a file over 200 KB, holds none. A head file
  that cannot be read counts as changed, and so does every changed instruction file
  when the base side cannot be read. The review still runs and its findings are
  delivered; only the clean result is withheld, recorded with the policy it changed,
  and a later run of the same head skips it without reviewing until a person signs
  the change off, a later head, or `--force`. A sign-off is an approval of that exact
  head, or a comment beginning `LGTM`, "looks good", "ship it", "approved", or a
  thumbs-up, on the pull request, in a review, or in a review thread, written after
  the head's review was withheld by the controller's own clock, never by a date the
  change carries (`github.policy_signoff`). It counts only from a person with write
  access, read from the repository's collaborator permission, who is not the pull
  request's author and not a bot, and never from a comment carrying this tool's
  marker; a permission or a list that cannot be read is no sign-off. Once one exists
  the head is reviewed again, and a review that would approve now approves; findings
  are delivered as always. A sign-off written from the reviewer's own account, as a
  reply in a thread, is not that account's review of the head, though GitHub files
  the reply as an empty "commented" review (`github.has_current_review`); a head an
  earlier run recorded as reviewed on such a reply alone is reviewed again. A replay
  applies the same gate against its pinned base, so a control case reports what a
  live run would deliver.
- A review that found nothing and was withheld only over a failed check, or only
  because the change is to review policy, tells its author what it waits for, once
  per head: one pull-request conversation comment, `## Waiting for checks` naming
  the failed checks that hold it, and apart from them any GitHub does not require,
  or `## Waiting for a person` naming the policy changes, with the
  head it speaks for and a `<!-- prwarden:waiting -->` marker. A note waiting for a
  person sits on the first line the first changed policy file adds, as one `COMMENT`
  review with that inline comment, lists the other policy changes, and says how to
  sign the change off; the review's body carries the marker, so it never counts as
  the head's review. A change that adds no line, or a head whose record names no
  line, gets the conversation comment instead. The reviewer's own
  pull request gets the same as a Slack message, or as a comment in a repository
  with `"slack": false`. Without it the author could not tell a clean review held
  back from PR Warden never having looked. A head withheld before this existed gets
  it from the run that skips it. The pull request's mutable state is read again
  before the note is posted, and a note is kept under its own
  `github:waiting`-style destination beside the head's record, which it never
  changes. A note that may have been delivered is never posted again; only one that
  definitely failed is tried on a later run. It is informational, so a failure to
  post it is reported and contained. Once a later result or a later head's note is
  delivered, earlier waiting notes are minimized as `OUTDATED`, as LGTM comments
  are, except a note on a line, which stays beside the change a person was asked to
  sign off. Another installation's waiting notes, on the pull request or on a line,
  never reach the code level, like its advisory notes. A review withheld for any other reason, such as an unresolved
  comment, already has that feedback on the pull request and gets no note.
- The **checks level** adds to a note waiting for checks, or to the reminder beside
  an approval, why each one failed and a possible fix, when the reviews file
  configures `types.checks`. It runs only for a review that
  found nothing and would approve, but for the checks that had failed by the time
  it finished or beside them, read again then when any was still running or none
  had started, with no committed credential, no policy change a person has not
  signed off, and nothing left unread, so it never spends a call on a pull request
  that has a defect to fix first, nor on a head whose note is already posted. It
  reads at most five failed checks: a GitHub Actions job from its own log, the
  failing step up to its last error and at most 120 lines, and any other check from
  its reported summary. The text has credential-shaped values replaced and reaches
  the prompt only as contained data, since the pull request's own code printed it.
  It runs on the one engine, model, and effort the reviews file's
  `types.checks.review` names, with no verifier and no fallback: each diagnosis
  must name a supplied check once, quote a line found in that check's text, and cite
  only workspace lines found in the head, and a response that fails that is retried
  once with the controller's reason. A reading that still fails, or a provider out of
  quota, costs only the reading: the note is posted without it. The note names the
  model that read it, quotes each log line as code, and breaks up any mention in the
  model's text, so a log cannot make the comment notify anybody.
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

- `.env`: delivery gates, Slack secret, GitHub identity, and file locations.
- `reviews.json`: which types of review run, in what order, and on which engines,
  and the limits every review shares (see [The reviews file](#the-reviews-file)). A
  review run needs it; the engine selection and limits older releases read from
  `.env` are moved into it once, and with it are not read.
- `repositories.json`: GitHub slugs and, optionally, matching local Git checkout
  paths used to fetch immutable PR head commits. An entry with no path is cloned
  by PR Warden into the managed checkouts directory on first use, objects only
  and without a working tree, and every fetch into it goes through GitHub CLI's
  credential helper with the token of the account the repository is read as.
  Git's other credential helpers are disabled for those commands so the token is
  never stored, and a slug that is not a valid GitHub owner and name is refused
  before it can name a directory outside the checkouts directory.
- `.state/review-state.json`: delivery idempotency records.
- `.state/history/`: the review history, one line per pull request per run, which
  only `prwarden history` reads (see [Review history](#review-history)).

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

Safe templates are committed as `.env.example`, `reviews.example.json`, and
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

Reviewing needs only to read code, pull requests, checks, and closing issues and to
post reviews and comments, so `prwarden doctor` reports, as notes that never fail
it, what each account's token can do beyond that: the OAuth scopes a classic or
GitHub CLI token lists in its `X-OAuth-Scopes` header, naming `workflow`,
`delete_repo`, package writes, and any `admin:` scope as more than reviewing needs,
and the repositories where the account is an administrator, since an approval
counts towards required reviews with write access. A token that sends no such
header is fine-grained. Each credential is reported by the route that carries it:
the reviewer's, which a `GH_TOKEN` in the environment replaces, and the GitHub CLI
login of each account a repository is read as, which no `GH_TOKEN` reaches, even
when it is the reviewer's own account; each is told the remedy that reaches it. A
fine-grained `GH_TOKEN` in `.env` with Contents read, Pull requests read and write,
Issues read, Checks read, Commit statuses read, and Actions read covers every call a
review makes as the reviewer, and the same token logged in for a `github_account`
covers its repositories. Actions read is only for the checks level's job logs: a
token without it costs the level the log, and it reads the check's summary instead.

### The reviews file

`reviews.json` sits in the configuration directory, or where `REVIEWS_FILE` in
`.env` points (`prwarden/reviews_file.py`); a `REVIEWS_FILE` that names no file
stops the run. It is private configuration like `repositories.json` and is never
committed; `reviews.example.json` is, and a test loads it with the real loader.
`prwarden setup` writes it, mode `0600`, from the engines the operator chose, and
`setup --import` copies a source checkout's.

Releases before 4.0.0 read these settings from `.env`, and `reviews_payload` still
writes what `.env` declares out as the file that plans the same reviews. A review
run moves them, once: `cli.run` alone calls `config.load_settings(move_env=True)`,
and every other caller, `prwarden doctor`, `prwarden schedule`, and
`prwarden.evaluate` among them, reads `.env` as it is. With `move_env` and no reviews
file, `load_settings`, through `_move_env_settings` and
`reviews_file.move_env_into_file`:

1. stops with an error pointing to `prwarden setup`, or to starting from
   `reviews.example.json`, when `.env` declares none of the keys the file replaces
   (`reviews_file.FILE_KEYS`);
2. refuses when one of those keys is set in the process environment to a value
   `.env` does not have, naming the key, so the file keeps only what `.env` says;
   unsetting it for that run lets the move go ahead;
3. writes nothing unless `plan_summary` finds the moved plan equal to `.env`'s: the
   same types in the same order, on the same engines, models, efforts and
   allowlists, within the same timeouts and limits, from the same commands. `.env`'s
   one command, `REVIEW_ENGINE_COMMAND`, moves to `engines.<engine>.command` for the
   reviewer's engine; where `.env` ran that engine from its default executable in
   another role, the plans differ and nothing is moved;
4. copies `.env` aside as `.env.before-4.0.0`, or `.env.before-4.0.0-2`, `-3`, and
   so on when that name is taken, mode `0600`;
5. writes `reviews.json`, mode `0600`, atomically, before `.env` loses a line, so no
   step leaves the settings in neither place;
6. removes those keys' assignments from `.env`, keeping its comments and every other
   setting, and prints `Moved N review settings from <.env> into <reviews.json>`
   with the backup's name.

The run then loads the file it wrote. `prwarden setup --reviews` makes the same
move at once, with the same plan check and backup, and refuses when a reviews file
already exists or PR Warden already reads one. With a reviews file, the `.env` keys
it replaces are not read at all, as since 3.8.0, and setting one in the environment
changes nothing; the command-line options below, or an edit to the file, change a
run. `prwarden doctor` lists any left in `.env` as a note. Without a file it notes
that the next review run moves the ones `.env` declares, or, with none to move,
reports a problem pointing to `prwarden setup`.

- `types` holds `design`, `docs`, `code`, and `checks`. `code` is required and
  cannot be disabled, since it alone approves; another type left out or set
  `"enabled": false` does not run.
- Each type has a `review` stage and, except `checks`, an optional `verify` stage.
  A stage lists one to four engines, each a built-in engine with an optional model
  and effort, and no engine and model twice. The custom `command` engine may be a
  review stage's first engine only, takes no effort, and needs
  `engines.command.command`. The first reviews or verifies, and each
  after it takes over when the one before fails at the engine level, as the single
  fallback always did; the error that ends the chain names every failure. A type
  with no `verify` stage verifies with its reviewer in a separate pass, within the
  default verification budget. The checks reading lists exactly one engine.
- A stage's `timeout_seconds` is the budget of every engine in it, 30 to 1800 for a
  review and 30 to 3600 for a verification, and a verification adds
  `timeout_per_candidate_seconds`, 0 to 600, for each candidate. `max_findings` is
  1 to 10 per type.
- `order` lists the enabled ordered types, `design`, `docs`, and `code`, once each,
  and must end with `code`.
- `concurrency`, `diff.max_bytes`, `diff.max_bundles`, `diff.ignored_missing_patch`,
  and `engines.<engine>` apply to the whole run; each engine's CLI is given the
  variables its own `env` names, beyond the few every engine gets, and runs from
  its `command`, an argument list, when one is named, in every role.
- `rules` lists, in order, changes for the pull requests each one matches
  (`ReviewRule`). A rule's `match` names one or more of `repositories` (slug or
  name), `authors`, and `labels`, each matched without regard to case by any one
  of its entries, and `files`, matched when every file the pull request changes,
  under its old name and its new, matches one of its patterns
  (`review.path_matches_pattern`, read as `.gitignore` reads one); every condition
  named must hold. Its `order` replaces the order, among the types the file or the
  rule configures, ending with `code`, and never listing `checks`. Its `types`
  change a type's `review` or `verify` stage, each part given replacing the same
  part, or its `max_findings`; a type or stage the file does not configure must be
  given its `engines` whole. Matching rules apply first to last, so a later rule's
  change stands. Each rule is checked alone against the file's own types when the
  file is read, which keeps every combination of matching rules one the file can
  review with.

An unknown key, a value out of range, or a misplaced `command` engine is refused with
the setting's path in the file. The run's own settings, which name its reviewer in
the run's reports, are the code review's. The command-line engine and model options
change the file's first engine of every type's matching stage for one run, a
different engine taking none of the model or effort written for the one it
replaces; `--engine` moves the checks reading too, and `--model` alone does not.
They are made again after a pull request's rules, so they have the last word, and
each rule is tried with them before any review, so a flag no rule's types can take
stops the run. `--types` keeps one run to the types it lists among `design`, `docs`,
and `code`, which it must include; the checks reading still runs when the code
review needs it, and a pull request's rules cannot add a type back.

A run prints the file's rules after its plan. A pull request's rules are matched in
`_process_pull_request` once its labels and changed files are read, before anything
reads which engine reviews it, and again at each new head (`cli._ReviewRouter`).
A pull request a rule matches is reviewed with that plan's stages, made the first
time a pull request needs them and shared by every later one, and the review
prints which rules it follows and the plan they give. State is unchanged: the code
level's record is still keyed by head and the advisory levels' by pull request, and
an earlier head's carried-over files are read again when a rule changes the code
review's engine, model, or effort, as `_review_basis` already compares.
`--fallback-engine` replaces each review's second engine, `--no-fallback` leaves
each review only its first, and a `--verify-engine` gives a type without a verify
stage one. `prwarden doctor` checks every engine the file names and reports any
`.env` key it replaces as not read. An evaluation arm that names no reviews file
still overrides the run's own settings, so its replay reviews every type on the code
review's engines; an arm that names one replays each case with that file's types,
engines, limits, and rules, as described under evaluation.

#### Moving from `.env`

| `.env` key | Moved to |
| --- | --- |
| `REVIEW_ENGINE`, `REVIEW_MODEL`, `REVIEW_EFFORT` | Each type's `review.engines[0]`: `engine`, `model`, `effort` |
| `REVIEW_CODE_EFFORT` | `types.code.review.engines[0].effort` |
| `REVIEW_FALLBACK_ENGINE`, `_MODEL`, `_EFFORT` | Each type's `review.engines[1]`; up to four engines in all |
| `REVIEW_VERIFY_ENGINE`, `_MODEL`, `_EFFORT` | Each type's `verify.engines[0]` |
| `REVIEW_VERIFY_FALLBACK_ENGINE`, `_MODEL`, `_EFFORT` | Each type's `verify.engines[1]` |
| `REVIEW_TIMEOUT_SECONDS` | `types.<type>.review.timeout_seconds` |
| `REVIEW_VERIFY_TIMEOUT_SECONDS` | `types.<type>.verify.timeout_seconds` |
| `REVIEW_VERIFY_TIMEOUT_PER_CANDIDATE_SECONDS` | `types.<type>.verify.timeout_per_candidate_seconds` |
| `REVIEW_DESIGN_LEVEL`, `REVIEW_DOCS_LEVEL`, `REVIEW_CHECKS_LEVEL` | Whether `types.design`, `types.docs`, `types.checks` is present and enabled |
| `REVIEW_CHECKS_MODEL`, `REVIEW_CHECKS_EFFORT` | `types.checks.review.engines[0]`, exactly one engine |
| `MAX_FINDINGS`, `MAX_DESIGN_FINDINGS`, `MAX_DOCS_FINDINGS` | `types.code`, `types.design`, `types.docs` `.max_findings` |
| `MAX_DIFF_BYTES`, `MAX_REVIEW_BUNDLES` | `diff.max_bytes`, `diff.max_bundles` |
| `IGNORED_MISSING_PATCH_PATTERNS` | `diff.ignored_missing_patch` |
| `REVIEW_CONCURRENCY` | `concurrency` |
| `REVIEW_ENGINE_ENV_ALLOWLIST` | `engines.<engine>.env` |
| `REVIEW_ENGINE_COMMAND` | `engines.<engine>.command`; `REVIEW_ENGINE=command` becomes the engine `command` with `engines.command.command` |

Every other `.env` key stays in `.env`.

### Installation commands

`prwarden setup`, `prwarden doctor`, `prwarden schedule`, and `prwarden history`
look after an installation and never review. A first word naming one of them is
dispatched before the review parser, which takes no positional argument, so no
review invocation can be mistaken for one.

- `setup` and `doctor` first check every tool with its own status command, which
  reads a stored login and calls no model, in the environment a scheduled run
  gets. `setup` then offers to install a missing one with one vetted command, a
  Homebrew formula or cask where Homebrew can install it and otherwise the vendor's
  own installer from the vendor's domain, shown in full and run only on a yes, and
  to run the tool's own sign-in. It never runs `sudo`; GitHub CLI on Linux without
  Homebrew is left to its instructions. GitHub CLI and one signed-in AI CLI are
  required to continue.
- `setup` writes `.env`, `repositories.json`, and `reviews.json`, each mode `0600`,
  keeping any files it replaces beside them; the engines chosen go into
  `reviews.json`, written as a move from `.env` writes it, and not into `.env`.
  Unless declined, it adds `reviews_file.recommended_rules`: files `*.md` and
  `docs/` run docs then code, the code review on the file's first code engine at no
  more than medium effort with no fallback, and the label `dependencies` runs code
  alone; neither names a model, which belongs to one provider. `setup --reviews`
  adds none, since it only moves settings. It
  pins `GITHUB_REVIEWER_USERNAME` to the chosen account, tries each signed-in
  account in turn for a repository the reviewer cannot read and records the first
  that can as that entry's `github_account`, and leaves Slack off for every entry
  unless a webhook is connected. It writes `AUTO_REVIEW_DRY_RUN=1`; a preview, going
  live, and a schedule are each asked for. `--import` copies a source checkout's
  configuration with its relative paths made absolute, its `reviews.json` once the
  loader accepts it, and its delivery records, and refuses to merge into an existing
  installation.
- Connecting Slack, in `setup` or alone with `setup --slack`, is asked for. Without
  a webhook it can open Slack's new-app page with a manifest filled in for an app
  whose one scope is `incoming-webhook`, and only on a desktop session: never over
  SSH, and on Linux only with a display. The pasted URL is read hidden and must be
  an app's incoming webhook, `https://hooks.slack.com/services/...`. It is kept
  only after Slack accepts one test message through it, which names no repository;
  a refusal asks again, and an unreachable Slack keeps it only when told to. The
  operator then picks the repositories that report there, and every other entry is
  written `"slack": false`. `setup --slack` changes nothing when declined, and
  otherwise keeps the `.env` and repository list it rewrites beside them.
- Connecting Jira, in `setup` or alone with `setup --jira`, is asked for too. A yes
  installs `acli` when it is missing, after the operator has seen the command, and
  signs it in when it is not: in the browser, or with an API token that is read
  hidden and handed to `acli` on standard input, never printed or stored. The
  operator then names the project keys, each checked against Jira's key shape, and
  the repositories whose pull requests name them, which get `"jira_projects"`.
  Declining, or an `acli` that stays missing or signed out, leaves Jira off;
  `setup --jira` then changes nothing, and otherwise keeps the repository list it
  rewrites beside it.
- `doctor` only reads: no post, clone, or model call. It shows which tools are
  installed and signed in, and exits 1 for anything that would stop reviews or
  delivery, a configured engine that is signed out, two schedules running at
  once, and a schedule whose Python is older than 3.11 included. That last one is
  found by asking the scheduled interpreter, or the one an installed `prwarden`
  script names on its first line, for its version. It also notes, without failing,
  a GitHub token or role that can do more than reviewing needs (see above).
- `schedule install` writes a per-user LaunchAgent on macOS or a systemd user
  timer on Linux that runs `prwarden --live`, and refuses while the configuration
  keeps `AUTO_REVIEW_DRY_RUN` on: a dry run records no review state, only its
  review history, so a scheduled one would review the same heads at every slot. On macOS it also refuses while
  another LaunchAgent runs PR Warden.
- `history` only reads the review history on this host, beside the state file a run
  would use, and asks neither GitHub nor a model anything; it does not resolve the
  GitHub identity a run needs. It exits 1 when part of the history cannot be read.

## Review Scheduling

Discovery completes for every configured repository before the first review
starts, so the run reports the whole plan up front and draws work from all
repositories rather than emptying one at a time.

The reviews file's `concurrency`, overridable with `--concurrency`, bounds how many
pull requests are reviewed at the same time between 1 and 8, defaulting to 2. The
bound exists because capacity, not local CPU, limits a run.

Concurrency changes scheduling only. Every guarantee elsewhere in this
specification continues to hold per pull request:

- Each review fetches its own immutable head into its own detached worktree.
  A fetch only reads, so one that times out or loses its connection to GitHub is
  tried once more after 15 seconds, and the run log names the retry; a commit
  GitHub does not have, or a refused login, fails at once. Each attempt has 180
  seconds. The same holds for the base-branch fetches behind the review rules,
  the generated-file marks, and the file history.
- Git operations against one shared local checkout are serialized, because Git
  refuses concurrent index, ref, and worktree updates in one repository.
- Delivery state remains atomic: the in-memory records are serialized in the
  process and the file lock still excludes other processes.
- Each engine invocation that needs per-review configuration receives its own,
  so one review's permissions cannot narrow another's.

Reviews running together record their own output instead of writing to the
terminal, and the recordings print in the planned order after the last review
finishes. A run with a single review, or a `concurrency` of 1, reports each
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
   have all been resolved. A reply in an existing thread is not such a review,
   though GitHub files it as a "commented" review with no body: one whose inline
   comments are all replies does not count.
2. JSON state contains a terminal record for the code level's tuple
   `(repository, PR number, head SHA, destination)`. The advisory design level is
   keyed by pull request instead, as described under its own heading below. An
   `approval_withheld` record is skipped while at least one controller-validated
   blocking comment remains unresolved and its feedback fingerprint remains
   unchanged, and, whatever the feedback, while it names review policy the head
   changes; that one reopens when a person signs the head off, for a later head, or
   for `--force`. A `delivered`
   comment review is skipped only while at least one exact inline finding from its
   review receipt remains unresolved.

Terminal JSON states are `delivered`, `no_findings`, `approval_withheld`,
`delivery_uncertain`, `delivery_blocked`, and `skipped_oversized`.
`delivery_blocked` means nothing was posted because the text held a secret.
`approval_withheld` means there
was no new finding to post but the approval is still blocked: by an existing
qualifying issue, or by the controller itself, as for a head that changes review
policy, whose record keeps what changed under `policy_changes`. It stores the
reason. One blocked only by an existing issue is reconsidered when review feedback
changes without a new commit, when every recorded blocker is resolved, or when a
legacy record has no validated blocker IDs. Resolved and unresolved inline
review threads are included in engine context, and each thread's resolution
state participates in the feedback fingerprint. Resolved feedback provides
duplicate, intent, and counterevidence context but does not itself block
approval. A delivered GitHub approval remains terminal, while a delivered
comment review is reconsidered after all of its inline findings are resolved.
`skipped_oversized` means not one changed file fitted
`diff.max_bytes`, so nothing could be reviewed, and a manual-review Slack
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
remains an error. One that cannot be written at all, as from a sandbox that lets a
run read and reach the network but not write the state directory, says so and that
the run belongs outside the sandbox, before anything is reviewed.

A run asked for one pull request by `--repo` and `--pr` that GitHub reports merged,
closed, a draft, or carrying an excluded label says which and reviews nothing, with
no error and no failure notification, since nothing was asked of it that it did not
do (`github.why_not_listed`). A number GitHub cannot describe, or an open pull
request missing from the list, remains an error.

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

A change over the budget is first read in bundles, up to `diff.max_bundles`. Each
bundle is a complete review of whole files that fit the budget, and the files no bundle
had room for stay deferred exactly as a single review would have left them. Bundling is
confined to the oversized path: a change within the budget produces one bundle, which is
byte for byte the material a single review received, and a `max_bundles` of 1 disables
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

One limit holds across the whole review. Each bundle's response is capped at the
code type's `max_findings` on its own, and committed credentials are found apart from
any model, so together they could post several times that. Once every bundle is
verified and the credentials are added, `review.select_findings` posts at most
`max_findings` of them, the most severe first, with findings of one severity in the
order they were confirmed.
Every finding that tells the author about a committed credential is posted, even past
the limit, and counts towards it: a credential's own finding, ahead of a model finding
of its severity, and a model finding that kept the only line a credential is on. A key
left unsaid stays live in the history, and there are at most ten. Only model findings
are left over. The confirmed findings
the limit leaves over are not posted, and the review body says how many there were;
their files are left out of `reviewed_patches`, so the next head reads them again
rather than carrying them over as clean. They never let the review approve. A replay
applies the same limit, so it scores what a live run would post, and names on its
transcript each finding it held back. The prompt
asks for the same from each response: the most severe first when more qualify, and each
root cause once, on the line where it is, with the other lines it affects as evidence.

A later head reads only what moved. Every `delivered` and `approval_withheld` record
of the code level keeps `reviewed_patches`, a SHA-256 of each file's change that this
review read in full or carried over and raised no finding on, leaving out any file
under a finding or a blocking prior comment, and `review_basis`, a fingerprint of the
code review rules it judged by, ignoring the base commit they were read at, and of
the engine, model, and effort that reviewed. The map is written even when it is
empty, because the latest such record is the one the next head is compared with: an
older head's record could list a file the newer review has just found a defect in.
A file's change is its path, status, earlier path, and patch with its hunk headers, so
the same edit against a base that has since moved counts as a new change. At the
next review of the pull request, for the same destination, each changed file whose
change matches the latest such record is carried over: left out of the diff and
the added-line index, so no finding can point at it, and named to the reviewer as a
file it may read in the workspace and that a changed line can still break. The code
level then reads, and bundles, only the rest. This applies to the code level alone,
since the advisory levels read the whole change. Nothing is carried over under
`--force`, when the base branch's rules or the reviewer changed, when the rules
cannot be read for the comparison, or when nothing would be left to read, because a
review of no file cannot speak for any. A review whose every file was read at one
head or another, with no finding left on any of them, is a complete review and may
be approved, as one covered by bundles is. A record keeps at most
`MAX_RECORDED_PATCHES` files; past that it keeps none and the next head is reviewed
whole. A replay reviews each pinned case whole, so it never exercises this.

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
adversarial verifier invocation, and the levels run sequentially. The code level
always runs last: its approval is what a person or an agent merges on, so the
advisory notes are on the pull request before it. The checks level, described with
the waiting note under Objective, is the exception: one invocation with no verifier,
made only after a code review that found nothing but for a failed check. Each type
in the reviews file names its own review and verify engines, in order, each with its
own model and effort (see [The reviews file](#the-reviews-file)), so a level is
defined by its prompt, response schema, validation, and delivery route, while which
provider reads it is configuration. The checks level, below, is the exception: it
reads failed checks rather than the change, on the one engine `types.checks.review`
names, with no verifier and no fallback.
The adapters are unaware of levels: the level travels on the review
material they are handed, and prompt, schema, and effort selection stays in the
provider-neutral core.

The **solution-design level** runs first when the reviews file configures
`types.design`. It judges the approach: where the change puts responsibility, what
contracts it establishes, how state and data flow, which failure modes the structure
gives no place to handle, and whether this workspace already provides what the change
re-implements, or still lacks what it was asked to deliver. A design finding must
name a consequence that is reachable in this workspace and a cost that does not
depend on taste, and it must cite the line that shows the structure. It carries no
anchor line, which is what lets it describe a whole change and also why it can never
become an inline comment. At most `types.design.max_findings` are reported,
deliberately fewer than the code level allows.

The level runs on a pull request whose diff was too large to read completely, since
the shape of a change is what nobody can read all of, and it does not run at all on a
release promotion. Both advisory levels read the diff without the sections of
dependency lockfiles and the other files `GENERATED_FILE_NAMES` lists
(`prwarden/config.py`), which they name instead and may still read in the workspace:
such a file holds no design and no documentation, and its diff can outweigh the rest
of the change on every turn of the call. The code level reads those sections.
`DESIGN_EXCLUDED_HEAD_BRANCHES` holds case-insensitive globs
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

Another developer may run PR Warden too, on their own machine and under their own
GitHub account, and nothing is shared between the two: each keeps its own state,
history, and schedule, and touches only what its own account posted. Its comments
reach this installation as that developer's feedback, and three rules keep them from
colliding with this review:

- Its design and documentation notes, its `LGTM` comments, and its waiting notes
  never reach the code level. `github.code_level_feedback` leaves every conversation
  comment that carries a design, documentation, `LGTM`, or waiting marker, and every
  inline comment that carries a waiting marker, under either name of this tool, out
  of
  what the code level weighs and may cite as a blocker, out of the feedback
  fingerprint an `approval_withheld` record is reconsidered by, and out of the
  feedback re-read before delivery, so a note posted during a review is not an
  objection to it. Advisory notes never withhold an approval, whoever runs PR
  Warden. Only a comment's author, or a maintainer who could delete it anyway, can
  put a marker in it, so a marker never makes another person's objection advisory.
  The design and documentation levels still read these notes, and are told not to
  repeat them.
- Its inline findings are review comments like a colleague's: the code level is told
  not to repeat them, and one left unresolved stands in the way of an approval until
  someone resolves it, as any reviewer's would.
- Selecting by marker alone would collect its findings too, so the evaluation corpus
  takes inline findings by marker and by this reviewer's account together.

Each installation approves as its own account, so two developers who both run it can
each approve a pull request; whether two approvals from tools satisfy a review rule
is the repository's branch protection to decide.

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

The **documentation level** runs next when the reviews file configures `types.docs`.
It asks one question: does a document in this repository now say something the
change made untrue, and what should it say instead. Both audiences count, the README
and the docs directory that people read, and the agent instruction files that coding
agents read, which are the ones nobody notices going stale.

The documents are discovered in the reviewed head rather than configured, so a
repository needs no setting to be checked and a repository with no documentation
costs no invocation at all. Human documentation is `README.md`, `CONTRIBUTING.md`,
`ARCHITECTURE.md`, `docs/**`, and a `README.md` up to two directories down, which
covers a monorepo package without collecting a dependency's own README. Agent
documentation is `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.cursorrules`,
`.cursor/rules/**`, `.kiro/steering/**`, the rules and workflows under `.agents/` or
its legacy name `.agent/`, and the Copilot instruction files. A
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
heading, new table, or sentence around them. A line of a fenced code block that lists
commands or paths one per line is an entry too, and a comment-only line is not: new
lines for it share its indent, carry a trailing comment when it does, and include no
fence. For that category alone the verifier
treats the missing entry as incomplete rather than silent, and decides it by whether
the list already holds the new entry's siblings, not by whether the list claims to be
complete; every other candidate is judged as before. Its delivered note names the
cited entry as the place the new entries go, since the proposal holds only them. A
prompt rule that loosened
silence for every candidate was tried twice and each time passed a neighbouring
proposal, a clarification of a sentence that was still true and then a whole new
section, which is why the exception is held to a shape the controller can check. At
most `types.docs.max_findings` are reported.

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
are present for an engine that reports them: Kiro reports every headless call's cost
in its event stream, the adapter reads it, and the spend line adds the total of the
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

### Review history

Every run appends one line per pull request it looked at to its own file under
`history/` beside the state file, `run-<date>-<time>-<pid>.jsonl`, named like its
transcript and created mode `0600`. A line records the run, live or dry, the
repository, number, title, author, and URL, the head read and any head it replaced
mid-review, and for each of the code, design, and documentation levels the outcome,
its reason or error, the delivery receipt, and the findings it would post or posted,
after credential redaction, each with the severity the reviewer gave when the
verifier changed it and the verifier's reason. Confirmed findings over the one limit
a review posts are counted, under `withheld`, rather than listed, as the pull request
only hears their count. A manual-review request and the model time spent are recorded
when they happened.

Outcomes come from two places. What a live review settles is written to the state
file, so the history takes each state record this run wrote for the pull request,
comparing the records before and after the review, and cannot miss an outcome added
later. The state file holds no finding text, and a skip, a dry run, or a failed
advisory level writes nothing there, so the controller notes those as they happen. A
result the controller noted and the state file never confirmed delivering is
recorded as not delivered, and a review that raised is recorded as failed with its
error, never as the verdict it had reached: an error is not a result. A skip of a
head an earlier run settled carries that run's status, time, and receipt.

The history is a report, never an input: no decision reads it, deleting it changes
nothing, and a line that cannot be written is reported once and costs no review.
Each run appends to its own file only, so overlapping runs need no lock. Each run
deletes history files last written more than `REVIEW_HISTORY_RETENTION_DAYS` ago,
14 by default. `prwarden history` reads it: every pull request with what each level
last decided, or with `--repo` and `--pr` every review of one, and `--json` for
another program. It prints titles and findings with control characters replaced, as
they are text the reviewed project and a model wrote, and counts a line it cannot
read rather than dropping it.

### Replaying pinned work

`python3 -m prwarden.evaluate` pins pull requests and reviews them again, so a change to
the reviewer can be attributed to the reviewer. A case records the slug, the number,
the head, and the merge base that head was reviewed against; pinning the merge base
rather than the base branch is what keeps a case the same case after the base moves.
The diff is rebuilt with `git diff <base> <head>`, per file, each patch trimmed to
its first hunk header, because a `+++ b/path` line reaching the parser would be read
as an added line and become a legal finding anchor. Line counts come from `--numstat`
in its NUL-separated form, so a path containing a space or a quote survives, and a
rename reports where it came from. Each file's status, added, modified, removed, or
renamed, comes from `--name-status` in the same form, named as GitHub names it,
because the controller decides with it: a removed file is not reviewed line by line,
and only a file that existed before has a history to read.

A merged pull request stays replayable and needs no special handling: GitHub keeps the
commit reachable through its pull-request ref, so fetching the pinned head by object
name still succeeds after the branch is deleted, which was confirmed against a merged
pull request whose branch no longer exists.

A replay omits what belongs to a live pull request, deliberately: eligibility and
mergeability, prior review comments, and delivery, Slack, and state. Duplicate
suppression is therefore not exercised by a replay, and a replay can post nothing.
It reads the issues the pull request names as a live review does, as they stand when
the replay runs, because a case pins code and not the tracker; an advisory replay's
record lists the issues it read, so two measurements whose inputs differed show it.

An arm is a named set of overridable scalar settings plus an optional engine. An arm
may not set anything that needs a matching command, such as the verification engine,
because an arm must not be able to describe a configuration the loader would refuse.
Such an arm replays every type on the code review's engines, as every measurement did
before an arm could name a reviews file, so the two stay comparable.

An arm may instead name a reviews file, `"reviews": "path"`, read from the arm set's
folder when relative, and then sets nothing else, since the file says every setting an
arm could. The file is checked as the loader checks a live one, before any review, and
each case is replayed with the types, order, engines, and limits the file gives it,
its rules matched on the case's repository, changed files under their old and new
names, author, and labels. A case records its author and labels when `capture` pins
it, or when it is mined as a control or named with `--pr`; a case pinned without them
is matched as if it had none, and the replay says so for each rule that matches on
them. The case set's fingerprint covers a case's author and labels once it records
them, since they choose its rules. An advisory replay of a level the file gives a case no review of skips that
case. The checks reading is not replayed. The arm's description records the file's
path and a fingerprint of its content, which `compare_metrics` names on each side and
counts as a thing that changed.

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
worth posting. Findings are selected by the comment template's own marker and by the
reviewer's account together. Never by author alone, because the reviewer posts under a
human account and that person comments too: selecting by author collected human review
remarks as though the reviewer had written them. Never by marker alone either, because
another developer's PR Warden marks its findings the same way under their own name. A
rebase is reported as its own outcome, since it rewrites everything and proves
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

### Linked issues

Every level is given the issues the pull request names, read once per pull request
before any level runs and shared by every bundle: up to three issues GitHub records
it as closing (`list_closing_issues`, GraphQL `closingIssuesReferences`, as the
repository's account), and up to three Jira work items of the projects the
repository's entry lists in `jira_projects` that its title, description, or head
branch names (`linked_issues.find_jira_keys`). A work item is read with
`acli jira workitem view KEY --json` in the scrubbed environment every agent CLI
gets, after its key is checked against Jira's own shape so nothing a pull request
says reaches `acli` as an option. A description in Atlassian Document Format is
reduced to its text, and every body is capped at 3,000 characters.

Issues are context, never a gate. An issue that cannot be read, because GitHub
fails, `acli` is missing or signed out, or the answer is not the work item asked
for, is reported in the run log and left out, and the review proceeds as it would
without it. The prompts frame them inside a contained `linked-issues` block as
untrusted content that never relaxes a rule and is never quoted in a finding, since
a finding can reach readers the tracker does not. Without any, no prompt carries a
`linked-issues` block, and the code and documentation prompts are the ones they were
before issues were read; the design prompt still offers the scope check below when
the description alone states the intent.

When the description or a linked issue states the intent, the design level may
report `change_outside_stated_intent`: a changed file nothing stated explains and no
explained change requires. Its verifier rejects one whose change is explained, its
own test, type, migration, lockfile, configuration entry, or document included.
Without a stated intent the category is not offered.

With a stated intent the design reviewer also lists each behaviour the description
and the issues ask the change to deliver, and may report
`requested_behavior_missing`: one that no code in the reviewed head delivers, such
as a route nothing calls, a client call to an endpoint nothing serves, a setting
nothing reads, or a test an issue explicitly asks for that no assertion makes. It is
reported only when the missing part belongs in this repository and in this change,
and code the base branch already had counts as delivering it, because the head is
searched whole rather than the diff. An absence has no line to cite, so the finding
also carries `searches`, at most four: a plain-text pattern matched without regard to
case, the directory or file searched, what it looked for, and every file it matched.
Every design finding answers `searches`, empty unless it claims something is missing.
The controller runs each search again over the head's own files, hidden ones
included and `.git`, binary files, files over 1 MB, and anything reached through a
symbolic link excluded, so installed dependencies are never searched; the prompt
states the exclusions, and a listed file the controller could not search is believed
rather than refused, since an extra match only weakens a claim of absence. It rejects
the whole response when a search matches a file the finding left out or does not
match one it listed, when a search matches more than five files and so shows nothing
missing, or when the category arrives without a search; the correction retry names
the files. The verifier then searches for the behaviour under the names the reviewer
did not try, and rejects the candidate when that finds it, when nothing stated asks
for it in this change, or when this repository is not where it would live. The
absence is the claim, so nothing reaching the cited line is never a reason to reject
it. A Jira work item is read for its description, so acceptance criteria written
there are read; a custom acceptance-criteria field is not.

### Repository background

Every level is given the base branch's `.prwarden/background.md` when it exists:
what the product is, what its units mean, what everybody on the team already knows
and no diff states. Alibaba's reviewer asks the calling agent to summarise business
context and pass it in on every invocation; the same context committed to the
repository stays true as the product moves and needs no operator to remember it.

It is read from the base branch's tip, or a replay's pinned base, never from the
reviewed head, for the reason the code review rules are: a pull request must not
write the context its own review is given. One that edits the note is changing
review policy, so it is never approved until a person signs it off. The note is still text whoever merged it
wrote, so it is untrusted like the diff: capped at 4000 characters with the
remainder marked truncated, delimited in the prompt, and named in the sentence that
forbids following instructions found in reviewed content. It may add context, never
authority, and never permission to relax a rule the prompt states. A repository
without the file costs nothing and the prompts say nothing about one; a note that
cannot be read is reported and left out.

### File history

Every level and its verification pass are given the base branch's recent commits on
the files the change edits, read once per pull request, because a commit message
often records why a guard, a limit, an ordering, or a workaround exists, and no diff
shows that. The files are those that
existed before the change, under their earlier name when renamed, the eight with the
most changed lines first; an added file has no history. For each, `git log
--no-merges` reads the latest three commits reachable from the base branch's tip, or
from the case's pinned base in a replay, never from the reviewed head, so the pull
request's own commits are the change and not its history. Merges are left out because
their message says which branch arrived, not why the code changed. A commit touching
several of the files appears once and names them all, newest first, at most twelve in
all. Only the hash, the commit date, the subject, capped at 200 characters, and the
body, capped at 500, are read; never a diff.

The messages were merged but written by whoever committed, so they are untrusted like
the background note: contained in a delimited block, stated to be messages and not
code, and never permission to relax a rule. Every claim about the code is still cited
from the workspace. History is context, never a gate: a base tip that is not a Git
object name, a failed fetch or `git log`, or output that does not parse is printed in
the run log and the review goes ahead without it. A change of added files only reads
nothing.

### Repository code review rules

A repository states what its reviews must hold the code to in a section headed
"Code review" in its agent instruction files: the root `AGENTS.md`, `CLAUDE.md`, or
`GEMINI.md`, `.cursorrules`, `.cursor/rules`, `.kiro/steering`, `.agents/rules`,
`.agents/workflows` and their legacy `.agent/` twins,
`.github/copilot-instructions.md`, or `.github/instructions`, the same files the
documentation level reads. The heading may be any level and may say "Code reviews"
or add "guidelines", "rules", "instructions", "checklist", or "standards"; the
section runs to the next heading of the same or a higher level, so its subsections
belong to it, and a heading inside a fenced code block is not one.

The rules are read from the base branch, never from the reviewed head, so a pull
request cannot write the rules it is judged by: a change to a rule is a change of its
own, merged first. The controller asks GitHub for the base branch's current tip,
refuses anything that is not a Git object name, fetches that commit into the local
checkout, and reads the matching files from its objects without checking anything
out. A replay reads them at the case's pinned base. Identical sections, as `CLAUDE.md`
often repeats `AGENTS.md`, are kept once, root-level files first, and the whole is
capped at 6000 characters with the rest named as left out. They are read after the
advisory levels, which never receive them, and a failure to read them fails that pull
request's code review, which is retried on the next run, rather than letting it pass
unchecked against them; the advisory levels have already run.

Only the code level and its verification pass receive them, delimited in their own
block, as requirements: an added line that breaks one is a defect, whatever its kind,
and withholds the approval like any other confirmed finding. A rule may only add
requirements; the prompt says that none relaxes a rule it states, grants an approval,
or changes the response format. A finding of a breach carries `rule`, a fragment of
one line of the rule copied exactly, and the controller rejects a response whose
quote is not in the base branch's rules, so a preference cannot be dressed up as a
rule. The verifier confirms a breach only when the cited added line breaks the rule
as written. The rule and the file it is in are shown with the finding on GitHub, in
Slack, and in the terminal. A base branch without rules leaves the prompt and schema
byte for byte as they were.

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
A corrected severity is never silent: the finding keeps the severity the reviewer
gave and the verifier's reason, collapsed to one line, and the GitHub comment, the
Slack message, and the terminal say "the verifier raised this from LOW to HIGH" or
lowered it, with that reason. The review history keeps both, and `prwarden history`
marks the finding "high (was low)". A judgment that restates the
reviewer's severity is no correction. The verifier is told that the author reads the
reason of a corrected judgment, so it says what about the impact decided the change.

Every posted finding also says what was checked, from facts the controller holds
rather than a confidence any model states: how many of its citations matched the
reviewed commit, and which verifier confirmed it. The verifier is recorded on each
finding as it is confirmed, because a review read in bundles can hand its
verification to a fallback part way through. A different model is claimed only when
the two model IDs name different models, compared by the model they display as
without the effort tier a host appends, so one model on two hosts is not called two;
a verifier with the same model on another engine, or with a provider default,
confirmed it "in a separate pass", and a reviewer that verified its own candidates
"re-checked" it. A committed credential says that PR Warden's own pattern check found
it, not a model.

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
- `codex`: uses non-interactive, ephemeral Codex execution with a required
  output schema, under a permission profile given on the command line that
  reads only the workspace, its linked dependency directories, and the minimal
  paths tools need, writes nothing, and has no network; `--sandbox` is not
  passed, since its read-only mode reads the whole disk, and a Codex older than
  0.138.0, which would ignore the profile, is refused. It never reads the user configuration, where trusted
  projects are recorded, so a head's `.codex/config.toml` is never loaded. Codex
  reads files only through shell commands, so its prompt ends with a note that a
  command that only reads, inside the workspace, is the reading the prompt allows,
  and that nothing else may run. An answer that raises no finding and confirms no
  candidate, whatever its verdict, is refused as an engine failure when `--json`
  events show no command that succeeded.
- `claude`: uses Claude Code print and safe modes with read-only tools and a
  required output schema, in `dontAsk` permission mode, which keeps reads in the
  workspace and the linked dependency directories given by `--add-dir`. It reads only the operator's own settings and no MCP
  configuration, because safe mode still loads a head's project settings.
- `kiro`: uses Kiro CLI headless mode with read/search tools and strict JSON
  parsing before common finding validation. No tool is trusted, and its agent
  allows reading only under `./**`, so a read outside the workspace is asked
  about, which a headless call refuses. It refuses a head that ships Kiro
  configuration other than documents, because Kiro would load and could execute
  it, and gives the worktree its own setting that keeps the answer unrendered and
  its own agent, which offers only those tools, denies skills so the list of every
  installed skill is not resent with each turn, and replaces the default agent's
  system prompt, resent with every turn, with none.
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
matching the primary engine and model is omitted rather than repeated. With a reviews
file, startup instead names the file and gives one line per enabled type, with that
type's review engines in the order they are tried and its verifying engines, or the
reviewer's own separate pass. The warnings for one provider carrying both the
fallback and verification, or both review and verification, are then said of the
code review's chains.

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
bodyless, except the approval of a review-policy change a person signed off,
whose text names that person and the head, carries the
`<!-- prwarden:approval -->` marker, and never reaches another installation's
code level. The `LGTM` comment and Slack message for such a head name them too.

The common prompt treats repository and pull-request content as untrusted data.
The code-level prompt places each block of that data inside one of its own tags, and
every prompt neutralises any text in untrusted data that would open or close one of
those tags, writing `&lt;` for its `<`, and says so once. Only the registered tag
names are touched, so markup in a diff reads as written, and a test fails if a prompt
gains a tag that is not registered. A correction retry quotes the controller's
rejection, which can quote the rejected response and the head, such as a search
pattern or the files it matched: the code level encodes it as JSON, and the advisory
levels place it inside a contained `controller-rejection` block. Every prompt also
says that text in the reviewed content addressed to the reviewer or vouching for the
change, such as "ignore this" or "this key is only for tests", is a claim to check
and never a reason to omit a finding or approve, and that no credential is ever
copied into a returned field.

Independently of any model, `secret_scan.find_credentials` reads every added
line of every changed file GitHub sends a patch for, in every bundle, for a
credential with a distinctive shape: AWS access key IDs, GitHub classic and
fine-grained tokens, Slack tokens and incoming webhook URLs, Stripe live keys,
Google, OpenAI, and Anthropic API keys, npm tokens, and private-key block headers.
A value whose random part contains a placeholder word such as `EXAMPLE`, or fewer
than eight distinct characters, is not one. Each committed value becomes one `high`
finding, added to the confirmed findings without verification, for at most ten values
per review, so the review delivers findings and cannot approve. A value on several
added lines, in one file or several, is one finding on the first of them that no
confirmed finding already holds, listing the next five by place and the rest by count;
a private key is told apart only by its line, because every one opens with the same
header. When a confirmed finding holds every line a value is on, one comment per line
still holds: the more severe of the two stays, and a tie keeps the model's. The
finding names the kind of credential and
shows only the value's fixed prefix. The credentials are found once, before any level
runs, and every credential on every matched line is replaced by its prefix wherever
it appears in the code level's findings and in the design and documentation notes,
excerpts and citations included, in comments, in Slack, and on the terminal, so a
comment kept after a force-push never preserves a value the push removed. Nothing
written beside the value is read. A replay applies the same check, so a control
case reports what a live run would deliver.

Everything PR Warden posts is checked once more just before it is sent, whatever
wrote it: every inline comment and review body, approval text, conversation comment,
and Slack message, error notifications included. A model that a pull request
talked into reading a key from the reviewing host could otherwise write it into a
finding or a note, where the pull request's author reads it. The check refuses text
holding a value of any shape above, private key material (the base64 body after a
private-key header, or an OpenSSH key's body anywhere; a header alone, as a finding
about a committed key quotes it, passes), an Anthropic sign-in token, or any value
this host holds: its GitHub tokens, its Slack webhook, and the value of every
variable an engine is allowed to see. A refused delivery sends nothing and is a
`DeliveryError`, never an uncertain one, and its error names only the kind of secret.
It leaves the head `delivery_blocked`, a terminal status, because the same review
would write the same text: the head is not reviewed again until a new head or
`--force`, and the run's error reaches the operator once. A level whose notes were
refused is settled for its pull request the same way, and a waiting note so refused
is not attempted again for its head.

The common prompt permits only read-only local file search and reading for semantic
verification, and prohibits edits, command execution, networking, browsing, and
external actions. It requests only correctness, reliability, data-loss,
concurrency, and security defects introduced by added lines, and breaches of the
base branch's code review rules when it has any.
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
as noise; inline bot findings remain available. Conversation comments carrying a
PR Warden design, documentation, `LGTM`, or waiting marker, and inline comments
carrying a waiting marker, such as another developer's installation posts, are left
out of the code level's feedback and kept for the advisory levels, as described under Review Levels. The common prompt requires every
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
The first engine of a type's `verify.engines` runs that pass, optionally on a
different authenticated adapter or model from the reviewer, so a candidate produced
from one model's priors is judged by another. A verification engine that matches the
reviewer's engine and model judges in a separate pass with the reviewer's priors, as
a type with no `verify` stage does. All controller validation,
routing, state, and delivery rules apply identically regardless of which engine
verified.

Each engine entry's optional `effort` sets a reasoning effort of `low`, `medium`,
`high`, `xhigh`, or `max` for that engine in that stage of that type: the reviewer,
each fallback, the verifier, the verifier's fallbacks, and the checks reading alike.
Each type's engines carry their own, so the code level needs no override of its own.
The reviews file refuses an unknown level and an effort for the `command` engine,
which cannot pass one, because each would otherwise run at a level nobody chose
without saying so. An engine replaced on the command line does not keep the
configured effort.

The engines after the first in a type's `review.engines`, up to three of them,
review a pull request, in order, when the one before fails at the engine level, which
covers an unavailable, unauthenticated, or refusing provider, an exhausted adapter
retry budget, and a timeout. Every level hands over the same way: the design and
documentation levels as well as the code level, each at most once per pull request.
The advisory levels stay advisory when a fallback carries them, and a failure of
every engine at an advisory level remains contained to that level. A stage that lists
one engine and model twice is refused, because repeating the same provider call
cannot recover from that provider being unavailable. A controller validation failure
never hands over: the review phase already retries it with the exact diagnostic, and
trying a second engine for a compliant answer would weaken validation rather than
recover from an outage. Only the primary engine's refusals are recorded as the
primary's spent quota.

The fallback reviewer is a reviewer, not an exception to any rule. Its response
passes through identical schema, evidence, added-line, dependency, and build
validation, its candidates go through the same adversarial verification, and
delivered feedback names the engine that actually reviewed instead of the
configured primary. When a type has no `verify` stage, the verification pass runs on
the engine that produced the candidates, because the primary engine is the one that
just failed. When every engine fails, the reported error names each failure so the
outage is not mistaken for a single provider's fault.

The engines after the first in a type's `verify.engines` judge, in order, when the
one before fails at the engine level, under the same rules: an engine failure hands
over and a validation failure does not, no engine and model may be listed twice, and
a failure of all of them is reported naming each. They need a `verify` stage, since
without one the reviewer verifies. The hand-over is made per
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
and passes that path to the engine. The base branch's tip is fetched the same way,
and its code review rules are read from Git's objects, never checked out. Cleanup runs after successful and failed
engine calls. The configured checkout's branch, index, and working files are
not changed.

Each worktree has a lock file beside it, which its review holds for as long as
the worktree exists; the operating system releases it when the process ends,
however it ends. A run that was killed, or a `git worktree add` stopped by its
timeout, can still leave a worktree behind, so every run, dry runs included,
starts by removing each worktree whose lock no process holds, and each worktree
without a lock file, which an earlier version made, once it is a day old. It also
drops a configured checkout's record of any such worktree, since Git keeps a
half-made worktree locked and never prunes it on its own. Removal uses a double
`--force` for the same reason. A problem in this cleanup is printed as a warning
and never fails the run.

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
the repository code review rule the finding breaks, when it breaks one, trigger,
impact, evidence, counterevidence, expected behavior and fix, reviewer attribution,
and a hidden reviewer marker. The code prompt asks for the root cause in `problem`,
what the code does wrong and why rather than only the symptom, and for the expected
behavior before the change in `suggestion`; that wording shapes how a finding is
written and never decides whether one is reported or confirmed. Template tags map only to supported
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
terminal. For a team pull request, submit an `APPROVE` review for the exact head
SHA, without a body, or, when a person's sign-off of its review-policy change is
what let it through, with a body that names that person and the head. For a self-authored pull request, send the clean-review Slack
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
  team pull request may receive a bodyless approval, or one naming the person
  who signed off its review-policy change, while a clean self-authored
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
  fingerprints `diff.max_bytes` and `diff.ignored_missing_patch`, the only
  settings that decide what goes unread for an immutable head, and a later run
  skips while both still match. A new head, either setting changing, or `--force`
  reopens it. Reconsideration stays for a withholding the engine chose without
  validated blocking comment IDs, where another attempt can genuinely answer
  differently; a rule the controller applied itself cannot, so re-reviewing only
  spends a model invocation to record the same result.
- The reviews file's `diff.ignored_missing_patch` therefore no longer decides whether
  a pull request is reviewed at all. It states that a matching path is generated and
  needs no review, which ignores the file outright and leaves the change
  approvable. Left out, it is `DEFAULT_IGNORED_MISSING_PATCH_PATTERNS`:
  dependency lockfiles, `*.tsbuildinfo`, and Wrangler's
  `worker-configuration.d.ts`, each by name at the root and in its `*/` form. An
  image, a minified bundle, a source map, a test snapshot, and other generators'
  output are not on it, because each can hold a line worth reading. A configured
  list replaces the built-in one rather than adding to it, so a release never
  widens what an operator chose, and `[]` ignores nothing. A release that changes
  the built-in list changes the fingerprint above, which reopens every pull request
  recorded as unapprovable over an unread file for one more review, whether or not
  the new list covers that file.
- A repository's own `linguist-generated` marks count beside the list. For each
  omitted patch, the controller asks `git check-attr --source=<base tip>` how the
  base branch's `.gitattributes` mark the path: set or `true` makes it a generated
  file that needs no review, and unset or `false` keeps it unread even when a
  pattern names it, so a repository can widen review by unmarking a file but never
  narrow it beyond what it marked. The base branch's current tip is read, never the
  reviewed head, for the reason the code review rules are: a pull request must not
  mark its own new file generated. Git matches the marks itself, nested
  `.gitattributes` and later lines included, and reads them from the commit rather
  than from the local checkout's working tree. Marks that cannot be read, because
  the base cannot be fetched or its tip is not an object name, keep every omitted
  file unread, one a pattern names included, because any of them might have been
  an unmark: failing to read the marks can only withhold an approval. Git older
  than 2.40 has no `check-attr --source` and would read the checkout's working
  tree instead, so it is never asked; that lasting fact of the installation is
  reported and the patterns alone decide, exactly as before the marks were read. The withheld record keeps
  what the base said about each omitted unread file as `unread_generated_marks`, and
  a later run reviews again only when one of them would now need no review under
  the same rule, `ignores_omitted_patch`: the base now marks it generated, or no
  longer unmarks it while a pattern names it. Any other answer, and a lookup that
  fails, would leave the file unread and spend a review to record the same result. A record written before the marks were read names no omitted files, so
  its unread paths are asked about, and a mark added on the base reopens it too.
  Widening it is a decision to allow approval over an unread file,
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
  context; bot summaries and bot conversation comments are filtered as noise,
  and PR Warden's own marked advisory and `LGTM` comments, from any installation,
  stay out of the code level's context and its pre-delivery re-read.
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
