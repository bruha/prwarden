# Testing and quality recommendations

## Test strategy

Use standard-library `unittest`. Mirror application modules with focused test
modules under `tests/` and name tests after observable behavior.

## TDD workflow

Prefer a red-green-refactor loop for behavior changes and bug fixes:

1. **Red:** add the smallest test that describes the intended observable
   behavior and confirm that it fails for the expected reason.
2. **Green:** implement the smallest production change that makes the new test
   pass without weakening existing assertions.
3. **Refactor:** improve the implementation and test structure while keeping
   the full relevant suite green.

Test public behavior and boundary contracts rather than private implementation
details. A test that already passes does not demonstrate the missing behavior;
confirm the red step before changing production code when practical.

TDD is not required for exploratory work, documentation-only changes,
configuration-only changes, or external integrations that cannot yet be
exercised meaningfully. After exploration establishes the intended behavior,
add tests before considering the implementation complete. Every confirmed bug
fix requires a regression test.

Prioritize tests for:

- safety and delivery gates;
- eligibility, state transitions, and idempotency;
- exact-head and mutable-snapshot validation;
- malformed, adversarial, and semantically invalid model output;
- transient reads versus uncertain writes;
- timeout, cleanup, and per-PR failure isolation;
- public-safe configuration and secret filtering;
- SemVer and changelog behavior in temporary Git repositories.

Prefer real temporary files and Git repositories for filesystem/release
behavior. Mock network services, provider CLIs, clocks, and external delivery at
the narrow boundary. Assert both the desired action and forbidden side effects.

Every bug fix should add a regression test that fails for the reported behavior
before the implementation changes. Do not weaken an assertion just to make a
new implementation pass.

## Required checks

Run from the repository root:

```sh
python3 -m unittest discover -s tests
python3 -m compileall -q prwarden tests
git diff --check
```

Also run the smallest relevant targeted test while iterating, then run the full
suite before requesting approval. Inspect `git status --short` and `git diff`
so generated files, secrets, and unrelated edits do not enter the change.

A local `python3` is usually newer than the supported floor, and it accepts
syntax that fails on Python 3.9 without any warning. When a change touches
syntax rather than only logic, also run the suite on the stock macOS
interpreter, which is the oldest supported version
([why](code-conventions.md#why-python-39)):

```sh
/usr/bin/python3 -m compileall -q prwarden tests
/usr/bin/python3 -m unittest discover -s tests
```

CI runs the same checks on 3.9 through 3.15, so a missed incompatibility fails
the pull request rather than reaching a release.

A second CI job builds the wheel on Linux and macOS under 3.9 and 3.15, installs
it into a clean virtual environment, and runs `prwarden` from outside the checkout
with an empty home directory. A file the package needs but does not ship, or a
lookup that still assumes a checkout, fails there instead of in a user's install.
The same check by hand:

```sh
python3 -m pip wheel --no-deps --wheel-dir /tmp/prwarden-dist .
python3 -m venv /tmp/prwarden-venv
/tmp/prwarden-venv/bin/python -m pip install --no-deps /tmp/prwarden-dist/*.whl
(cd /tmp && env -i HOME=/tmp/prwarden-home PATH=/tmp/prwarden-venv/bin:/usr/bin:/bin \
  GITHUB_REVIEWER_USERNAME=example prwarden)
```

It should report the missing `~/.config/prwarden/repositories.json` under that
home and create nothing.

For release automation changes, verify at least:

```sh
python3 -m unittest tests.test_release
python3 -m prwarden.release --dry-run
```

The dry run may intentionally report that there are no commits to release on a
fresh release boundary; that diagnostic is not a successful release preview.

## Review standard

Review for correctness before style. Check error paths, interruption behavior,
external side effects, compatibility, trust boundaries, and whether docs still
describe the implementation. A passing test suite is necessary but does not by
itself establish that live GitHub or Slack delivery is safe.
