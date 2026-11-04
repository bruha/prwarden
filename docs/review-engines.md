# Review engine setup

The controller has first-party adapters for Antigravity, Codex, Claude Code,
and Kiro CLI. Select a persistent default with `REVIEW_ENGINE` or override it
for one invocation with `--engine`.

```sh
python3 -m prwarden --engine codex
python3 -m prwarden --engine claude
python3 -m prwarden --engine kiro
python3 -m prwarden --engine antigravity
```

The override uses the built-in executable name. Do not set
`REVIEW_ENGINE_COMMAND` when switching this way unless every selected provider
is exposed through the same wrapper. To use a nonstandard executable path, set
the command and engine together for that run:

```sh
REVIEW_ENGINE=codex REVIEW_ENGINE_COMMAND='["/absolute/path/to/codex"]' \
  python3 -m prwarden
```

## Codex

Install Codex CLI and authenticate it, then verify both before reviewing:

```sh
codex --version
codex login status
python3 -m prwarden --engine codex --repo owner/repository --pr 123
```

The adapter uses non-interactive `codex exec`, an ephemeral session, a read-only
sandbox, disabled user configuration and project instruction loading, and a
JSON output schema. It uses the saved session reported by `codex login status`.

Skipping the user configuration also keeps a reviewed head's own
`.codex/config.toml` out. Codex loads that file only for a project the user
configuration marks as trusted, and a trusted parent directory, such as the home
directory, covers every review worktree beneath it. A head's file can name MCP
servers, hooks, and its own sandbox mode. With the user configuration skipped,
no project is trusted, which was confirmed with a head whose MCP server and hook
would each have created a file.

## A command and a model belong to one engine

`REVIEW_ENGINE_COMMAND` and `REVIEW_MODEL` are read as settings of the
`REVIEW_ENGINE` named in the same configuration file. Selecting a different engine,
either with `--engine` or by setting `REVIEW_ENGINE` in the environment, drops both:
a provider's model ID means nothing to another provider, and a path to one provider's
CLI is not a path to another's.

This is not pedantry about configuration. Handing one provider's arguments to another
provider's binary does not fail loudly — the binary prints its own help text and exits,
which the controller can only report as a failed review. A scheduled run would leave the
pull request unreviewed and say nothing about why.

To run another engine with its own command or model, set those in the environment
alongside the engine, and they are kept because they were chosen with that engine in
view:

```sh
REVIEW_ENGINE=claude REVIEW_MODEL=sonnet \
  python3 -m prwarden --repo your-org/example-app --pr 123
```

## Claude Code

Install Claude Code and authenticate it, then verify both before reviewing:

```sh
claude --version
claude auth status
python3 -m prwarden --engine claude --repo owner/repository --pr 123
```

The adapter uses print mode, safe mode, plan permissions, only the built-in
`Read`, `Grep`, and `Glob` tools, no MCP tools, no browser integration, no
session persistence, and JSON-schema output, and passes the role's effort as
`--effort`. It uses the saved session reported by `claude auth status`.

Safe mode turns off hooks, MCP servers, and `CLAUDE.md`, but it still loads a
reviewed head's `.claude/settings.json` and `.claude/settings.local.json`. A
head's `apiKeyHelper` there ran as a command before the first request, and its
`env` sent the API requests to a host the head chose. The adapter therefore
passes `--setting-sources user`, so only the operator's own settings load, and
`--strict-mcp-config`, so no `.mcp.json` is read. With both flags the same head
ran nothing and the request went to the normal API.

## Kiro CLI

Install and authenticate Kiro CLI, then verify the saved session and run a
targeted dry review:

```sh
kiro-cli --version
kiro-cli whoami
python3 -m prwarden --engine kiro --repo owner/repository --pr 123
```

The adapter uses headless mode and trusts only Kiro's read and grep tool
categories. Kiro CLI does not expose a general JSON-schema output flag in its
headless command, so the adapter requests one exact JSON object, accepts either
raw JSON or one JSON code fence, and passes the result through the controller's
strict finding validation. A malformed response fails the review and is not
treated as an empty result.

Kiro reads its workspace configuration from the directory it runs in, which is
the reviewed head, and starts any MCP server that configuration names. Before
every call the adapter therefore refuses a head whose `.kiro/` or `.amazonq/`
holds anything other than documents (`.kiro/steering`, `.kiro/specs`,
`.amazonq/rules`), and writes its own `.kiro/settings/cli.json` into the review
worktree. That file turns off Kiro's Markdown rendering: rendered, an answer
that is not inside a code fence loses its backticks, asterisks, and underscores
on the way to standard output, so an excerpt copied exactly from the file
arrives changed and the controller rejects it.

The same file turns off code intelligence, so Kiro starts no language server; a
language server loads the project's own configuration. Kiro's `code` tool still
writes `.kiro/settings/lsp.json` into the workspace when a review uses it, and starts
nothing from it. The next call in the same worktree then found configuration the
controller had not written and refused the head, which failed the review. A head that
ships that file is refused before any call, so the adapter removes it after every
call, whether the call succeeded or not; it unlinks a link rather than following it.
A refusal names the file it found, because the head is not the only possible author.

Kiro's `--output-format stream-json`, which would return the answer unrendered,
needs the v2 agent engine, and Kiro CLI 2.22.1 cannot select a model on that
engine. A resumed session (`--resume-id`) reloads every installed skill file into
context, which made a one-line follow-up cost more than a full review, so a
correction retry starts a new session rather than continuing the rejected one.

## Antigravity

Install and authenticate Antigravity CLI, then verify it:

```sh
agy --version
agy models
python3 -m prwarden --engine antigravity --repo owner/repository --pr 123
```

The adapter uses sandboxing, disabled slash-command expansion, and JSON-schema
output inside the controller's exact detached PR worktree. It does not pass
Antigravity's `--mode plan` flag because the CLI reports that the flag has no
effect while slash-command expansion is disabled. It gives Antigravity an
isolated app-data directory under `.state/antigravity-cli` with terminal
sandboxing enabled and non-workspace access disabled. Reviews can run in
parallel and each one grants reads only to its own pull request's installed
dependencies, so every review writes its settings into its own subdirectory,
`review-0`, `review-1`, and so on for as many reviews as run at once. Any
`settings.json` in `.state/antigravity-cli` itself is a read-only template whose
keys carry into each review's copy.

Those settings bound the model's tool calls, not what Antigravity runs on its
own. It loads a customization root from the directory it starts in, which is the
reviewed head, and starts the MCP servers in `.agents/mcp_config.json` and the
hooks in `.agents/hooks.json`, including a plugin's under `.agents/plugins/`.
That was confirmed with the terminal sandbox on and `command(*)` denied: each
server and hook created a file. `.agent/` is a legacy name for the same root.
Antigravity has no flag to skip workspace customizations, and its saved trusted
workspaces are not consulted in print mode, so the adapter refuses, before
Antigravity starts, a head whose `.agents/` or `.agent/` holds anything but
`rules/` and `workflows/` documents, which are instructions rather than code. A
refusal is an engine failure, so a code review the
primary engine refuses goes to the fallback engine when one is configured.

A tool call that cannot be completed ends an Antigravity review outright, taking
every finding it had already verified with it. That consequence belongs to this
adapter alone, so the adapter states it in its own prompt section rather than in
the shared prompt: the verification pass runs on its own engine, and telling a
verifier that a check can destroy finished work biases it into dropping the
candidates it exists to weigh. The adapter also treats such a fault as
recoverable, reviewing once more with feedback naming the exact path or action
that failed rather than spending a delayed retry on a request that would fail
again. A read of a path the model inferred rather than observed is the common
case.

Antigravity populates each of those directories with a complete profile of
binaries and caches, tens of megabytes apiece, so the directories are named by
slot rather than by process: repeated runs reuse the same warm profiles instead
of leaving a fresh copy behind every time. Removing them is safe; Antigravity
rebuilds what it needs on the next review. Its fine-grained policy
allows recursive reads only inside the controller's `.state/worktrees` directory
and explicitly denies file writes, commands, URL access, unsandboxed execution,
and MCP tools. The isolated profile keeps Antigravity's default `request-review`
preset; the explicit read grant lets print mode proceed without an interactive
prompt.

Because that policy denies commands, the adapter's prompt states the denial and
names the read-only commands a reviewer would otherwise reach for. Antigravity
turns a rejected command into a failed result envelope rather than steering the
model to another tool, so one attempted `grep` ends the whole review after the
invocation is already spent. The prompt therefore directs verification to the
file read, directory listing, glob, and search tools instead.

A standing instruction alone does not stop it, so a failed tool call also earns
one immediate re-review. Antigravity aborts a print-mode review whenever a tool
call cannot be completed, whether the sandbox denied a command or the model read
a path that does not exist, and either way an otherwise complete review is lost
after its invocation is spent. The adapter quotes the CLI's own reason back into
the prompt, states that the fault discarded the previous attempt, and invokes the
engine again without a delay, because a denied command or a guessed path is a
local fault rather than a transient provider one. Only one such re-review is
allowed per review, it consumes one of the three engine invocations, and a second
fault is reported with its diagnostic log so the fallback engine takes over
instead of the reviewer looping.

A refused tool call is the one such fault the CLI reports as a success. Print mode
cannot ask anybody to approve a call the policy does not already permit, so it
denies the call, exits zero, and returns an envelope whose `response` is empty,
writing the reason to standard error: `a tool required the "read_file" permission
that headless mode cannot prompt for, so it was auto-denied`. Read as a bare empty
response that looked unfixable and spent the fallback engine on a review the model
could have finished.

The envelope also lists what it refused, as `denied_actions: [{"action":
"read_file", "display_name": "ListDir"}]`, and that field is read first: it
survives a reworded diagnostic and a lost stream, and the CLI's prose is written
for an operator, telling it to add an allow-rule that these roots deliberately
withhold. So the re-review is told that a named tool call was refused for want of
a named permission, never to edit settings. Standard error and then the CLI log,
which names the tool and step but no path, are the fallbacks. No source names the
path that was refused.

Directory listing is governed by the same read permission as a file read, and each
granted root is readable at and below itself alone. A dependency root is the
installed package directory of a checkout that is otherwise unreadable, so
following the workspace symlink into a package is allowed while stepping up out of
it into that checkout, or across into a sibling, is refused. The prompt names every
readable root and states that boundary, because a reviewer looking for the real
repository around a linked package is how an otherwise complete review gets
discarded.

Only a fault the feedback can correct earns that re-review: a refused call, a
denied command, an invalid tool call, or a read of a missing path. An unsuccessful envelope that
merely reports a transient provider fault, such as an interrupted stream, takes
the ordinary delayed retry with the prompt unchanged, so it cannot spend the one
re-review that a later denied command needs. Any other envelope failure is
reported without a retry.

Each invocation receives a temporary diagnostic log under the ignored isolated
app-data directory. If the CLI exits unsuccessfully, the adapter extracts a
bounded provider status from that log so terminal and Slack errors do not contain
only an unrelated CLI warning. It recognizes both plain-text and JSON-formatted
backend statuses. When the log records that the CLI stopped on its own print
timeout, that outranks every earlier diagnostic, because the CLI logs and
recovers from failures mid-run; the reported error names the configured timeout
instead of an unexplained exit. When no provider status is present, a logged
network-transport failure is reported instead, so a route, DNS, or TLS fault
reads as one and stays retryable rather than spending the fallback engine.
A warning-only failed exit is treated as transient because the
CLI may have written the actual provider failure only to its internal state.
Recognized transient provider, routing, transport, timeout, and availability
failures retry twice with 15-second and 30-second delays, for at most three
engine invocations, and all of them share one `REVIEW_TIMEOUT_SECONDS` budget: a
retry runs only for what is left of it, so a provider that hangs costs one timeout
rather than three. Authentication, authorization, invalid-request, print-timeout
expiry, and other recognized permanent failures are not retried, and neither is
the CLI refusing the configured model or its effort, which is reported in the
CLI's own words with the models it offers instead. Diagnostic logs
are removed on a best-effort basis after successful, recognized, and retryable
attempts. If the final failed log is
non-empty but cannot be parsed, it is retained with owner-only permissions and
the reported error includes its path for local diagnosis. If no usable
diagnostic exists after retries, the adapter reports that condition instead of
presenting the unrelated mode warning as the cause. A result envelope that
reports an unsuccessful status is treated the same way: the adapter reports the
message the envelope carries, and retains the log with its path, because a
zero-exit failure would otherwise arrive with no explanation at all. The adapter does not change
the user's global `~/.gemini/antigravity-cli/settings.json`.

## Models, credentials, and custom commands

`REVIEW_MODEL` passes an explicit model ID through the selected built-in
adapter. Leave it empty to use that CLI's default.

## Verification engine

`REVIEW_VERIFY_ENGINE` runs the adversarial verification pass on a second
built-in adapter, and `REVIEW_VERIFY_MODEL` selects its model. Only the named
adapters are accepted; `command` is not, because the wrapper contract already
allows a custom primary engine. Setting the same engine and model as the primary
is treated as unconfigured, since a second pass with identical priors adds cost
without independent judgment.

The point is decorrelation: a candidate finding produced from one model's
training priors should be judged by a different model, ideally from a different
provider family. The verification pass runs only when candidates exist, so its
cost is proportional to findings rather than to pull requests.

Kiro CLI is a practical choice because one authenticated adapter exposes several
provider families. List what your account can use:

```sh
kiro-cli chat --list-models -f json
```

With a Claude model reviewing through Kiro, a GPT model on the same CLI
verifies, so one model family's priors are not the only judge of its output:

```dotenv
REVIEW_ENGINE=kiro
REVIEW_MODEL=claude-opus-5.5
REVIEW_VERIFY_ENGINE=kiro
REVIEW_VERIFY_MODEL=gpt-5.6-terra
```

Verify the pairing on one pull request before scheduling it:

```sh
python3 -m prwarden --repo owner/repository --pr 123
```

## Fallback engine

`REVIEW_FALLBACK_ENGINE` reviews a pull request when the primary engine fails at
the engine level, and `REVIEW_FALLBACK_MODEL` selects its model. It covers every
level the primary reviews, the advisory design and documentation levels as well as
the code level, and with no separate verifier it also judges what it found. The same
rules as the verification engine apply: built-in adapters only, and a fallback equal
to the primary engine and model counts as unconfigured.

The fallback exists for provider outages, not for disagreement. A provider that
is unavailable, unauthenticated, refusing requests, timing out, or exhausting its
adapter retry budget cannot review at all, and a second authenticated CLI is the
only way to finish the run. A controller validation failure does not hand over,
because the review phase already retries once with the exact diagnostic and
asking another engine for a compliant answer would weaken validation.

Choose a fallback from a different provider than the primary, so a single
provider incident cannot stop both:

```dotenv
REVIEW_ENGINE=kiro
REVIEW_MODEL=claude-opus-5.5
REVIEW_FALLBACK_ENGINE=antigravity
REVIEW_FALLBACK_MODEL=claude-opus-5-5-high
```

Antigravity updates itself, and its model list moves with it: agy 1.4.6 dropped
`claude-opus-4-6-thinking` mid-run and now names the effort tier in the ID, so a
fallback that worked yesterday can fail with `invalid model selection` today. A
failed fallback is reported beside the primary's failure, and `agy models` lists the
IDs it accepts now.

The fallback review is validated and verified exactly like a primary review, and
delivered feedback names the engine that actually reviewed, so a GitHub comment
or Slack message never credits an engine that did not produce it. A run that
loses both engines reports both failures for that pull request.

Confirm the fallback before relying on it. Point `REVIEW_ENGINE` at a deliberately
broken command so the primary fails immediately, and watch the fallback finish
the review:

```sh
REVIEW_ENGINE=command REVIEW_ENGINE_COMMAND='["false"]' \
  python3 -m prwarden --repo owner/repository --pr 123
```

## Verification fallback

`REVIEW_VERIFY_FALLBACK_ENGINE` verifies when the verification engine fails at the
engine level, and `REVIEW_VERIFY_FALLBACK_MODEL` and `_EFFORT` choose its model and
effort. It needs `REVIEW_VERIFY_ENGINE`, because without a separate verifier the
reviewer verifies its own findings and its fallback already covers that.

It exists because one provider served both review and verification. While that
provider was down the review fallback still reviewed, but every finding it raised
went to a verifier that was down too, so nothing was delivered; a workspace the
verifier's CLI refused cost the verification the same way. The rules match the
review fallback's: only an engine failure hands over, a malformed answer stays with
the engine the controller tells how to correct it, and a fallback equal to the
verifier's engine and model counts as unconfigured. Once a pull request's verifier
has handed over, the rest of that pull request is verified by the fallback, so a
correction retry reaches the engine whose answer it corrects; the next pull request
starts with the verifier again.

```dotenv
REVIEW_VERIFY_ENGINE=kiro
REVIEW_VERIFY_MODEL=gpt-5.6-terra
REVIEW_VERIFY_FALLBACK_ENGINE=claude
REVIEW_VERIFY_FALLBACK_MODEL=claude-sonnet-5-5
REVIEW_VERIFY_FALLBACK_EFFORT=xhigh
```

Pick a model other than the reviewer's. The controller compares engines and model
IDs, and the same model reached through another provider has another ID, so it
cannot tell that a fallback verifier would be judging its own findings. Delivered
feedback names the verifier that actually judged, startup states the fallback
beside the verifier, and the spend line counts its calls as `verification
fallback`. A replay never uses it: a measurement that quietly changed verifier
would compare nothing.

## Reasoning effort

Each role may set a reasoning effort, `low`, `medium`, `high`, `xhigh`, or `max`,
and leaving it empty runs the model at its own default:

```dotenv
REVIEW_EFFORT=xhigh
REVIEW_CODE_EFFORT=max
REVIEW_VERIFY_EFFORT=xhigh
REVIEW_FALLBACK_EFFORT=
```

`REVIEW_EFFORT` applies to every level the reviewer runs, and `REVIEW_CODE_EFFORT`
overrides it for the code level alone, the one level that can approve. The
verifier and the fallback each run at their own effort on every level; neither
inherits the code override, which was chosen for the reviewer's model.

Only the `kiro`, `antigravity`, and `claude` adapters pass an effort to their CLI,
as `--effort`, so an effort named for any other engine is refused at startup rather
than silently dropped. Claude Code took `--effort` from 2.1.304, the version this
was checked against. The level is checked here too, because Kiro accepts any
string and runs the model's default when it does not recognise one. Which levels a
model supports is up to its provider: Antigravity refuses an effort for a model
that takes none, as it did for `claude-opus-4-6-thinking`, and Kiro documents
that not every model supports every level. Switching the engine with `--engine`,
or naming a different secondary engine on the command line, drops the configured
effort along with the model, since both were chosen for the engine being replaced.
Startup states each role's effort beside its model.

Built-in adapters use each CLI's saved authenticated session through the
preserved home directory. Provider API keys are not configured or passed
automatically. `REVIEW_ENGINE_ENV_ALLOWLIST` remains available for a custom
wrapper that requires additional non-default variables. The Slack webhook is
never inherited unless it is explicitly allowlisted.

For an unsupported provider, select `command` and configure a wrapper:

```dotenv
REVIEW_ENGINE=command
REVIEW_ENGINE_COMMAND=["/absolute/path/to/review-wrapper","--structured"]
```

`REVIEW_ENGINE_COMMAND` is a JSON string array and is executed directly without
a shell. Engine subprocesses receive a minimal environment; list any additional
variable names explicitly with `REVIEW_ENGINE_ENV_ALLOWLIST`. Delivery secrets
are not inherited by default. The wrapper contract is:

- Working directory: the exact detached PR worktree.
- Standard input: the complete provider-neutral review prompt and unified diff.
- `AI_CODE_REVIEW_SCHEMA`: required output schema as compact JSON.
- `AI_CODE_REVIEW_MAX_FINDINGS`: configured finding limit.
- `AI_CODE_REVIEW_MODEL`: optional model ID when `REVIEW_MODEL` is set.
- Standard output: one JSON object conforming to the supplied schema.
- Nonzero exit: review failure; output is not treated as no findings.

Another CLI can be integrated through this wrapper contract or with a new
`prwarden/engine_<provider>.py` adapter registered by `create_review_engine`.
