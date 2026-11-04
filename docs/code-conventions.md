# Code conventions

## Python

- Support Python 3.9 and newer. See
  [Why Python 3.9](#why-python-39) for the reason and the cost.
- Use four spaces, descriptive names, type hints on public and cross-module
  interfaces, and focused functions.
- Prefer standard-library facilities and immutable values where practical.
- Keep imports explicit and module-level. Avoid wildcard imports and hidden
  import-time side effects.
- Use `pathlib.Path` for filesystem paths and explicit UTF-8 for tracked text.
- Catch the narrowest useful exception. Preserve command, exit status, and
  bounded stderr/stdout details when wrapping failures.
- Do not use mutable default arguments or broad `except Exception` unless the
  boundary must isolate one PR and the error is immediately reported or
  re-raised.

### Why Python 3.9

`/usr/bin/python3` on current macOS is Python 3.9.6. Apple ships that
interpreter as part of the Xcode Command Line Tools because Xcode's own tooling
depends on it, pins the version, and rebuilds it against new compilers instead
of upgrading it. Verified on macOS 26.5.2 with Command Line Tools 26.6.0, where
`/usr/bin/python3` reports `3.9.6 (default, May 22 2026)` from
`Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/Versions/3.9`.
The version cannot be upgraded in place; installing a newer Python adds a
second, independent interpreter elsewhere, such as `/opt/homebrew/bin/python3`.

Combined with the no-third-party-runtime-dependency policy, a 3.9 floor lets
anyone clone this repository onto a stock Mac and run it with no interpreter
install, virtual environment, or package installation. That is the entire
benefit, and it is the reason the floor is not treated as legacy debt.

The cost is accepted deliberately:

- Python 3.9 reached upstream end of life in October 2025, so the stock
  interpreter receives only Apple's own patches. Operators who care about that
  should point their schedule at a maintained interpreter; see
  [Scheduling on macOS](macos-scheduling.md).
- Language features newer than 3.9 are unavailable, most often PEP 604 `X | Y`
  unions in annotations, `match` statements, and PEP 701 multi-line expressions
  inside f-strings. Use `Optional[...]` and `Union[...]`, and bind a local
  before an f-string instead of breaking an expression across lines.
- PEP 585 builtin generics such as `list[str]` and `tuple[int, ...]` are
  available in 3.9 and are used directly.

The floor is a verified contract, not an aspiration. `.github/workflows/test.yml`
runs `compileall` and the unit suite on 3.9 through 3.15 for every push and pull
request, because the compatibility claim was documented before anything checked
it and silently became false until a commit restored it and added the matrix
(`771ce83`, in the history before 1.0.0, which was not published).
Local checks on a newer interpreter cannot detect a 3.9 syntax regression, so
run the suite under `/usr/bin/python3` when changing syntax-sensitive code:

```sh
/usr/bin/python3 -m compileall -q prwarden tests
/usr/bin/python3 -m unittest discover -s tests
```

Raising the floor is a legitimate decision, not a forbidden one. It requires
updating this section, [AGENTS.md](../AGENTS.md), the
[README](../README.md) requirements, and the CI matrix in the same change, so
the documented floor and the enforced floor never disagree again.

## DRY

Remove duplicated knowledge, not merely similar-looking lines. Extract a shared
helper when the same rule or transformation appears in multiple places and is
expected to change together. Keep small local duplication when abstraction
would couple unrelated behavior or obscure provider-specific differences.

Tests may repeat setup when the repetition makes the scenario easier to read.
Prefer small factories over deeply configurable fixtures.

## SOLID

- **Single responsibility:** modules and functions should have one clear reason
  to change. Keep orchestration separate from parsing, validation, and I/O.
- **Open/closed:** add engines through the adapter contract rather than adding
  provider conditionals throughout the controller.
- **Liskov substitution:** every adapter must honor the same structured result,
  failure semantics, timeout behavior, and read-only review contract.
- **Interface segregation:** pass the smallest useful data and capability set;
  do not give model subprocesses delivery secrets or unnecessary tools.
- **Dependency inversion:** domain decisions depend on interfaces and validated
  models, while GitHub, Slack, subprocesses, and filesystems remain boundaries.

Apply these as design checks, not as reasons to add layers without a concrete
need.

## Clean Code

- Choose names that express intent and use the same term for the same concept.
- Keep happy paths visible; use early validation and guard clauses for invalid
  state.
- Explain *why* a safety constraint exists. Do not add comments that restate the
  code.
- Avoid boolean parameters when named operations or enums better express
  meaning, but keep command-line flags idiomatic.
- Keep functions small enough to understand and test, while avoiding mechanical
  fragmentation into one-line wrappers.
- Delete dead code instead of commenting it out. Git retains history.
- Make logs actionable: identify the PR and phase, retain the actual diagnostic,
  and never print credentials or complete untrusted payloads unnecessarily.

## Test-driven development

Prefer test-driven development for behavior changes and bug fixes: describe the
observable behavior with a failing test, implement the smallest change that
makes it pass, and refactor while the suite remains green. Do not write tests
against private implementation details merely to follow the sequence.

TDD is not required for exploratory work, documentation-only changes,
configuration-only changes, or external integrations that cannot be exercised
usefully before the boundary is available. Convert confirmed exploratory
behavior into tests before treating the work as complete. Every bug fix still
requires a regression test.

The complete workflow and test-design guidance are in
[Testing and quality recommendations](testing-and-quality.md).

## Security and untrusted input

Treat PR text, diffs, repository files, review comments, model output,
configuration, and subprocess output as untrusted. Never interpolate untrusted
text into a shell command. Execute argument arrays without a shell, validate
paths before reads, constrain model capabilities, escape destination markup,
and bound retained diagnostics.

Secrets belong only in ignored private configuration. New environment
variables passed to an engine must be explicitly allowlisted and documented.
