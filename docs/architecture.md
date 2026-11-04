# Architecture recommendations

## Boundaries

Keep the controller provider-neutral. `ReviewEngine` adapters translate a
shared prompt and structured-output contract to authenticated provider CLIs.
They must not own GitHub discovery, eligibility, finding validation, state,
routing, or delivery policy.

The shared prompt states only what holds for every engine and every pass. A
limit, sandbox rule, or failure consequence that is true of one CLI belongs in
that adapter's own prompt section, because the same shared prompt is what the
candidate pass, the verification pass, and the fallback engine all read: text
that describes one provider's behavior misinforms the others, and a warning
about losing work biases a verifier away from the judgment it was asked to make.

Use these module responsibilities:

- `config`: parse and validate untrusted local configuration.
- `commands`: execute subprocesses and preserve failure output.
- `github`: perform GitHub reads and writes through explicit operations.
- `workspace`: create and clean exact detached PR-head worktrees.
- `engine`: define the common AI contract and adapter selection.
- `review`: construct bounded material and validate model responses.
- `delivery`: format and send destination-specific messages.
- `comment_template`: load and render bounded, declarative GitHub comment
  templates without executing template code.
- `state`: coordinate one host and record confirmed delivery outcomes.
- `cli`: orchestrate the use cases and isolate failures per pull request.

Dependencies should point toward stable domain contracts. Provider and delivery
details stay at the edges; models and validation rules remain independent of a
specific CLI or destination.

## Data flow

The expected review flow is:

1. Load private configuration and resolve the authenticated GitHub identity.
2. Discover eligible PRs and immutable head SHAs.
3. Fetch complete GitHub change metadata, feedback, and exact-head checks.
4. Create a detached worktree for the selected head.
5. Run the advisory solution-design level, when enabled and not already settled
   for this pull request: generate design candidates, validate their citations,
   have the verifier judge each candidate by reference, re-fetch mutable PR state,
   and deliver confirmed notes as one pull-request comment. This level is keyed by
   pull request rather than by head, so a later push does not review the design
   again. Every fault here is contained to this level.
6. Generate and validate code-level candidate findings against repository
   evidence.
7. Adversarially verify candidates in a fresh model invocation.
8. Re-fetch mutable PR state and feedback.
9. Restart against a newer head, up to the configured controller limit, or
   deliver only if the reviewed snapshot is still valid.
10. Record state only after a confirmed outcome.
11. Clean the temporary worktree on success and failure.

Each level is one reviewer invocation and one verifier invocation, sequentially:
the same engine and model review every level and the same second engine and model
verify every level, so a level is defined by its prompt, schema, validation, and
delivery route rather than by its own provider configuration. The one exception is
reasoning effort: the code level may take its own (`REVIEW_CODE_EFFORT`), because it
alone can approve, and every other level runs at the role's effort. The levels share
no data, which is what keeps the design level advisory and makes running them
concurrently a scheduling decision rather than a redesign.

## Design rules

- Prefer explicit domain types over loosely structured dictionaries at module
  boundaries.
- Keep pure parsing, formatting, validation, and decision logic separate from
  subprocess, filesystem, network, and delivery side effects.
- Inject or patch boundary operations in tests; do not hide external calls in
  constructors or global initialization.
- Make retry policy operation-specific. Read retries and model-output
  correction retries do not imply delivery-write retries.
- Bound prompts, diffs, findings, diagnostics, subprocess output, and Slack
  alerts. Refuse partial coverage when it could be mistaken for a complete
  review.
- Prefer atomic replacement and explicit state transitions for durable files.
- Design new state values so interrupted and uncertain outcomes remain
  distinguishable from confirmed success.

## Changing the architecture

An architectural change should document its trust boundary, failure modes,
idempotency behavior, compatibility impact, migration path, and tests. Update
the review specification when externally observable behavior changes.
