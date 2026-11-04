# Threat model

What this reviewer assumes about the things it reads, runs, and writes to, and what
stops each of them from doing harm. Written after reading the code, not from the
design intent: every mitigation below names where it is enforced.

The comparable document in Alibaba's reviewer is `ASSURANCE_CASE.md`, and this one
follows its shape deliberately so the two can be read side by side.

## What the system does

1. Asks GitHub, through the authenticated `gh` CLI, which pull requests are open.
2. Fetches each reviewed head into a detached worktree from a local checkout.
3. Builds a prompt from the diff, prior review comments, GitHub checks, and the
   repository's own `.prwarden/background.md`.
4. Runs an authenticated coding-agent CLI, in that worktree, with the prompt.
5. Validates every citation in the response against the worktree.
6. Posts inline comments, a review, an approval, or advisory pull-request comments
   through `gh`, or sends a Slack message through an incoming webhook.
7. Records what it delivered in a local JSON state file.

## Actors and trust

| Actor | Trust | Why |
| --- | --- | --- |
| Operator | Trusted | Owns `.env`, `repositories.json`, and the schedule |
| Local checkout | Trusted as a Git object source | Its working tree is never used; only objects are |
| Reviewed head | **Untrusted** | Any contributor can put anything in a diff |
| Coding-agent CLI | Semi-trusted | Authenticated by the operator, but it is a general tool with its own capabilities and its own sandbox |
| Model response | **Untrusted** | Validated in full before anything is delivered |
| GitHub API via `gh` | Semi-trusted | Data shapes are validated; the binary is the operator's |
| Slack webhook | Write-only sink | Never read from; the URL is a secret |
| State file | Trusted | Local, atomic, lock-guarded |

The boundary that matters most is the third one. Everything in the reviewed head —
code, file names, commit messages, the pull-request body, prior comments, and the
background note — is data written by whoever opened the pull request.

## Threats and what stops them

| # | Threat | Mitigation, and where |
| --- | --- | --- |
| T1 | Command injection through diff, path, branch, or model-supplied text | No shell anywhere: `prwarden/commands.py:112` is the single `subprocess.run`, always with an argument list, `check=False`, an explicit timeout, and a controlled environment. `shell=True`, `os.system`, and `os.popen` appear zero times in `src/`. The prompt, which carries untrusted diff text, is passed as one argv element, never interpolated into a command string |
| T2 | Prompt injection from the reviewed head, making the agent act rather than review | Every prompt names the untrusted inputs and forbids following instructions in them (`prwarden/engine.py`), forbids edits, commands, network, browsing, and host skills or subagents, and the engine's own sandbox enforces it independently — observed in production [pr:7nzi], where Antigravity refused a `ListDir` call the review had asked for. Data cannot end the block it was placed in: `contain_untrusted` neutralises the prompt's own tag names inside every untrusted value, so a pull-request body carrying `</pr-body>` cannot continue as the prompt's own voice. Residual risk is real and documented below |
| T3 | Secret leakage to the agent CLI | The engine environment is built from an allowlist, not inherited: `build_engine_environment` (`prwarden/engine.py:1691`) passes `HOME`, `PATH`, `TMPDIR`, `TEMP`, `TMP`, `LANG`, `LC_ALL`, `USER`, `LOGNAME`, `SHELL` and nothing else, and the call site sets `inherit_environment=False`. `SLACK_WEBHOOK_URL` and the rest of `.env` are therefore not visible to the model's process. The reviewer's pinned GitHub token, and the token of any account a repository entry names with `github_account`, is held in `prwarden/github.py` and added only to that module's `gh` calls, never to `os.environ`, so no engine allowlist can forward it. `refresh-terms` passes such a token in its `gh` child's environment, never in its arguments, which every process on the machine can read |
| T4 | Secret or local-path leakage into Slack or GitHub | The webhook is read from the environment and never printed; Slack message bodies pass through `redact_local_paths` (`prwarden/delivery.py`) so a machine path in a finding cannot reach a channel. A webhook belongs to one workspace, so a repository configured with `"slack": false` is never named in it: its owner's pull requests are reviewed on GitHub, and its run errors and notices stay in the run log. `.env` and `repositories.json` are untracked, and the setup instructions create `.env` mode 600 |
| T5 | Path traversal through a model-supplied citation path | `_required_evidence` (`prwarden/review.py:430`) rejects absolute paths, resolves the candidate, and requires it to sit inside the worktree or an allowed dependency root; resolution happens before the comparison, so a symlink cannot escape. A path that does not exist in the reviewed head is rejected outright |
| T6 | A fabricated or drifted finding reaching a pull request | The anchor must be an added line of the diff; every citation's excerpt must be that line's exact text; the whole response is rejected and retried once with the reason otherwise; a separate adversarial pass judges each candidate by reference, so it can neither restate nor re-anchor what it confirms. A finding whose position cannot be established is dropped, never posted with a guess |
| T7 | Duplicate or contradictory delivery after a retry | GitHub writes are never blindly retried, delivery revalidates mutable pull-request state first, and every terminal outcome is recorded per (repository, pull request, head, destination) in an atomically written state file guarded by a process lock (`prwarden/state.py`) |
| T8 | Two runs colliding on state | A live run takes the process lock and a second one exits 0 as a safe skip; dry runs take no lock and record nothing |
| T9 | A malformed or hostile GitHub response | Every field read from `gh` is type-checked before use; a record missing what a decision needs is treated as unknown and the safe branch is taken, for example a file with no provable changed-line count is deferred rather than assumed textless |
| T10 | Writing outside the intended places | A review run writes only under its state directory (`.state/` in a checkout, `~/.local/state/prwarden` when installed: state, run logs, worktrees) and into the clones it manages itself, and never to the reviewed repository or beside an installed package. A managed clone is fetched with GitHub CLI's token for its account through a credential helper set on the command line, with git's other helpers off, so the token is never stored. `prwarden setup` writes only the configuration directory and `prwarden schedule` only its own LaunchAgent or systemd units. Installing a missing tool runs a Homebrew formula or cask, or the vendor's own installer fetched from the vendor's domain, only after the operator has seen the command and said yes, and never with `sudo`; that installer is trusted exactly as far as it would be if the operator ran it by hand. Inside a review worktree it adds only Kiro's controller-owned settings file (T11), which is never committed or pushed. Applying fixes is deliberately not a feature |
| T11 | Agent host configuration shipped by the reviewed head | An agent CLI reads workspace configuration from the directory it starts in, which is the reviewed head, and some of it names commands the CLI runs itself, outside the tool permissions the review grants the model. Each case was confirmed with a head whose server, hook, or helper created a file. Kiro starts MCP servers from `.kiro/settings/mcp.json` and `.amazonq/mcp.json`; Antigravity starts MCP servers and hooks from `.agents/` and `.agent/`, plugins included, with its sandbox on. Neither CLI can be told to skip them, so `refuse_host_configuration` (`prwarden/engine.py`) refuses, before the CLI starts, a head whose configuration directories hold anything but documents (`.kiro/steering`, `.kiro/specs`, `.amazonq/rules`, `.agents/rules`, `.agents/workflows`) or a symlink, and Kiro gets the only configuration file it finds there. That file also turns off Kiro's code intelligence, so no language server starts and loads the project's configuration. Kiro's `code` tool still writes `.kiro/settings/lsp.json` during a call, and the next call there refused the head, so the adapter removes that one file after every call; a head that ships it is refused before any call, and a refusal names the file it found. Claude Code's safe mode still loaded a head's `.claude/settings.json`, whose `apiKeyHelper` ran and whose `env` redirected the API requests, so the adapter reads only user settings and no MCP configuration. Codex loads a head's `.codex/config.toml` only for a project the user configuration trusts, and the adapter never reads the user configuration |

## Residual risk, stated plainly

**The agent CLI is the weak point, and it cannot be fully mitigated from here.** It
runs with the operator's credentials in a directory full of untrusted content, and
what stops it acting on an instruction planted in that content is the prompt, its own
sandbox, and nothing else this project controls. Three things reduce the damage: it
is given no secrets (T3), the controller believes nothing it returns without checking
it against the worktree (T5, T6), and it can only ever cause a comment, an approval,
or a Slack message — never a code change or a merge.

The honest limit of that argument is that an approval is itself an action. A
successful injection that produced a clean verdict would spend the reviewer's
approval on a change nobody read. The rules that forbid approving drafts, conflicts,
partial reviews, and self-authored pull requests narrow this, and the second engine
verifying candidates narrows it further, but a clean review is the one outcome that
requires no evidence to be delivered.

## One hardening item this audit found

`pull_request.head_sha` reaches `git fetch origin <sha>`, `git cat-file`, and
`git worktree add` as an argv element, and is now validated as a Git object name at
the GitHub boundary instead: `validate_object_name` accepts 40 or 64 lowercase hex
characters and refuses everything else, applied where a pull request is discovered
and where its snapshot is read (`prwarden/github.py`). Git reads an object name as a
revision expression, so an unvalidated value would not merely fail to resolve —
`HEAD`, `main@{1}` and a path each mean something, and a leading `-` means an option.
Nothing has ever sent such a value; the guarantee was inherited from GitHub's API,
and this makes it local so the boundary holds on its own.

A second, smaller assumption: the prompt is delivered as a command-line argument, so
its size is bounded by the platform's argument limit. `MAX_DIFF_BYTES` defaults to
200 KB against roughly 1 MB available on macOS, so there is headroom, but raising
that setting far enough would fail as an exec error rather than as a review error.

## Automated verification

| Check | Command | When |
| --- | --- | --- |
| Unit suite, 461 tests | `python3 -m unittest discover -s tests` | Every change, on 3.9 through 3.15 in CI |
| Stock-interpreter compatibility | the same suite under `/usr/bin/python3` | Every change locally; CI covers 3.9 |
| Syntax | `python3 -m compileall -q prwarden tests` | Every change |
| Whitespace | `git diff --check` | Every change |
| Dependencies | none to scan: no third-party runtime dependencies | By construction |

The last row is a genuine advantage over a tool with a dependency tree: there is no
supply chain to monitor, because there is nothing installed to review with.
