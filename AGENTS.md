# Project instructions for coding agents

These instructions apply to the entire repository. Read them before changing
code, tests, documentation, configuration examples, or release metadata.

## Project purpose

PR Warden is a dependency-free Python 3.9+ application that coordinates
authenticated AI CLIs, GitHub CLI, Slack incoming webhooks, exact detached Git
worktrees, and atomic local JSON state. The controller, not an AI provider,
owns eligibility, prompt construction, response validation, duplicate handling,
delivery, and idempotency.

The repository is public. Never commit `.env`, `repositories.json`, Slack
webhooks, tokens, company-only repository names or paths, generated state,
logs, review output, or temporary worktrees. Use public-safe placeholders in
tracked examples and documentation.

The same applies to everything learned from the repositories this tool reviews,
in tracked files and in commit messages alike: their organisation and repository
names, people's logins, pull-request numbers, titles, and branch names, file paths
from their code, and details of their product. Cite a real pull request as a
`[pr:xxxx]` reference created with `python3 -m prwarden.public_safety new-ref <url>`;
the map back to the URL lives in the ignored `.private/` directory. Report
measurements as counts, never as named cases. The public-safety hook enforces
this; see [development workflow](docs/development-workflow.md#public-safety).

## Repository map

- `prwarden/__main__.py`: application entry point.
- `prwarden/cli.py`: orchestration, retry boundaries, and terminal behavior.
- `prwarden/config.py`: dotenv and repository configuration validation.
- `prwarden/engine.py`: provider-neutral prompt, schemas, and adapter registry.
- `prwarden/engine_*.py`: authenticated review-engine adapters.
- `prwarden/github.py`: GitHub discovery, feedback, checks, and delivery.
- `prwarden/github_status.py`: read-only lookup of announced GitHub incidents.
- `prwarden/review.py`: diff construction and semantic response validation.
- `prwarden/delivery.py`: Slack message formatting and delivery helpers.
- `prwarden/comment_template.py`: validated GitHub comment templates and tag aliases.
- `prwarden/templates/`: public-safe default GitHub comment layout and tag registry,
  shipped inside the package.
- `prwarden/progress.py`: in-place progress line and per-thread output capture.
- `prwarden/metrics.py`: structured measurement of what a run's reviews decided.
- `prwarden/compare_metrics.py`: reports what moved between two measurements.
- `prwarden/evaluate.py`: pins pull requests and replays them to measure a change.
- `prwarden/public_safety.py`: keeps reviewed projects' names out of commits; the
  public-safety Git hooks and the private `[pr:xxxx]` reference map.
- `prwarden/state.py`: atomic JSON state and the single-host process lock.
- `prwarden/paths.py`: where configuration and state live, checkout or installed.
- `prwarden/schedule.py`: `prwarden schedule`, the LaunchAgent or systemd timer.
- `prwarden/doctor.py`: `prwarden doctor`, read-only checks of an installation.
- `prwarden/setup_wizard.py`: `prwarden setup`, interactive configuration and import.
- `prwarden/workspace.py`: immutable PR-head fetch, detached worktree lifecycle, and
  read-only installed-dependency links.
- `tests/`: standard-library `unittest` suite mirroring application modules.
- `evals/`: example replay cases and experiment arms; the real ones are ignored.
- `VERSION`: canonical SemVer version, mirrored by `prwarden.__version__`.
- `pyproject.toml`: packaging metadata and the `prwarden` command; build-time only.
- `CHANGELOG.md`: Keep a Changelog-style release history.
- `LICENSE`, `NOTICE`: Apache License 2.0 and the copyright notice it carries.
- `SECURITY.md`: how to report a vulnerability, and what is in scope.
- `CONTRIBUTING.md`: which issues and pull requests are welcome, and how.

## Non-negotiable behavior

- Dry run is the default; live delivery requires `AUTO_REVIEW_DRY_RUN=0` and
  `--live`.
- Errors must never become empty successful reviews.
- GitHub write operations are not blindly retried when delivery may have
  succeeded.
- Review the exact PR head and revalidate mutable PR state before delivery.
- Only controller-validated findings on added diff lines may be posted inline.
- Do not approve drafts, excluded labels, self-authored PRs, conflicts,
  indeterminate mergeability, partial reviews, or unverified clean results.
  "Self" is the repository's `github_account` when its entry names one, compared
  case-insensitively, and the configured reviewer otherwise.
- A repository configured with `"slack": false` is never named in a Slack message,
  error notifications included.
- A pull request that closed or merged during review still receives its confirmed
  findings; it never receives an approval or a clean-result `LGTM`.
- Approval authority belongs to the code level. The solution-design and
  documentation levels are advisory: neither may block a merge, withhold an
  approval, post a review comment, or change the run's exit code, and their
  failures stay contained to the level that had them. Their own earlier notes,
  read back so a rerun does not repeat them, never reach the code level.
- The documentation level proposes documentation edits only. A disagreement it finds
  is reported as text for the document, never as a code change and never as a defect.
- Preserve provider neutrality: adapters translate the common contract;
  discovery, validation, state, routing, and delivery stay in the controller.
- Preserve Python 3.9 compatibility and the no-third-party-runtime-dependency
  policy unless a separately approved architectural change says otherwise.
  Together they let a stock macOS `/usr/bin/python3`, which Apple pins to
  3.9.6, run this tool with no setup; CI enforces 3.9 through 3.15. Avoid
  `X | Y` annotations, `match`, and multi-line expressions inside f-strings,
  and see [why Python 3.9](docs/code-conventions.md#why-python-39).
- Keep state and delivery changes atomic, bounded, idempotent, and testable.

The complete operational contract is
[docs/auto_code_review_spec.md](docs/auto_code_review_spec.md).

## Required development process

1. Start from current `main` and create a focused feature branch.
2. Use a Conventional Commits type and kebab-case description for the branch.
3. Make the smallest cohesive change and update tests and docs with it.
4. Run all quality gates listed below.
5. Present the result, risks, and verification to the user and wait for explicit
   approval before merging.
6. Merge the approved branch to `main`, then delete the merged branch.
7. On `main`, prepare the SemVer release and changelog, review it, commit it,
   tag it, and push the branch and tag.

Branch names, commits, PR titles, review expectations, and the exact release
sequence are defined in
[docs/development-workflow.md](docs/development-workflow.md) and
[docs/releases.md](docs/releases.md).

## Quality gates

Run from the repository root:

```sh
python3 -m unittest discover -s tests
python3 -m compileall -q prwarden tests
git diff --check
```

Where the private word list exists, also run
`python3 -m prwarden.public_safety check --all`; the installed hooks check every commit
and its message on their own.

For documentation-only changes, still run the unit suite unless the user
explicitly narrows verification. For release-tool changes, also run its dry-run
and targeted tests. Never claim a command passed unless it was run successfully
in the current worktree.

## Guidance index

- [Architecture](docs/architecture.md): boundaries, data flow, and design rules.
- [Code conventions](docs/code-conventions.md): Python style, DRY, SOLID, Clean
  Code, TDD, error handling, and security.
- [Testing and quality](docs/testing-and-quality.md): TDD workflow, test
  strategy, and gates.
- [GitHub comment template how-to](prwarden/templates/README.md): layout, complete tag
  reference, aliases, examples, local overrides, and validation.
- [Development workflow](docs/development-workflow.md): branches, commits, PRs,
  user approval, and merge policy.
- [Releases](docs/releases.md): SemVer policy, release automation, changelog,
  tags, and push order.
- [Review engine setup](docs/review-engines.md): adapter contracts and provider
  verification.
- [Defensive prompt rules](docs/prompt-rules.md): where each omit, reject, or
  approve rule in the code prompt came from, and which to re-test on a new model.
- [Scheduling](docs/scheduling.md): `prwarden schedule` on macOS and Linux.
- [macOS scheduling](docs/macos-scheduling.md): LaunchAgent and cron operation.
- [Threat model](docs/threat-model.md): trust boundaries, what stops each threat,
  and the residual risk of running an agent CLI over untrusted content.

When documentation and implementation disagree, stop and determine which is
authoritative for the requested change. Do not silently weaken a safety rule to
make a test or delivery pass.
