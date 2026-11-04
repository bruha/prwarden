# Development workflow

## Branches

Start work from an up-to-date `main` and create one focused branch. Branch names
use a Conventional Commits type followed by a slash and a short kebab-case
description:

```text
feat/chunked-diff-review
fix/uncertain-slack-delivery
docs/release-process
refactor/github-client
```

Allowed types are `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `build`,
`ci`, `chore`, `style`, and `revert`. Do not commit feature work directly to
`main`.

## Branch cleanup

Delete a branch as soon as its work is merged, so only active work remains
listed. Confirm the merge first, then remove the local branch and any published
copy:

```sh
git branch --merged main
git branch -d fix/example-change
git push origin --delete fix/example-change
```

`git branch -d` refuses to delete unmerged work, which is why it is the required
form. Never reach for `git branch -D`, and never delete a branch that is still
awaiting user approval. Deleting a merged branch loses nothing: its commits are
already reachable from `main`.

## Commits and pull requests

Every commit message and PR title must follow Conventional Commits:

```text
<type>[optional scope][!]: <imperative description>
```

Examples:

```text
feat(review): support chunked diff verification
fix(github): preserve uncertain delivery state
docs: document LaunchAgent removal
feat(state)!: replace the local state schema
```

Use a `BREAKING CHANGE: ...` footer when a commit changes a public contract,
configuration schema, state compatibility, supported command, or documented
delivery behavior incompatibly. Keep commits cohesive and independently
reviewable. Do not mix formatting or unrelated cleanup with behavior changes.

A PR should explain the problem, behavior change, safety implications, tests,
documentation updates, and any manual verification still required. Its title
must be suitable as a squash commit without editing.

## Public safety

This tool is developed against private repositories, and nothing learned there may
reach this repository: not their organisation or repository names, people's logins,
pull-request numbers, titles, or branch names, file paths from their code, nor
details of their product. That covers commit messages too, because the changelog is
built from them.

A real pull request worth citing, as the origin of a rule or a regression test, is
cited by a reference instead of a link:

```sh
python3 -m src.public_safety new-ref https://github.com/<org>/<repo>/pull/<n> \
  --note "what happened there"
# prints [pr:xxxx]; write that in the comment, docstring, or document
```

The reference is random, so it reveals nothing, and the map back to the URL lives in
`.private/pr-refs.json`, which is ignored. Back that file up: unlike the word list,
it cannot be rebuilt. Measurements are reported as counts, never as named cases.

A maintainer with access to the reviewed repositories sets up the check once per
clone:

```sh
python3 -m src.public_safety refresh-terms   # builds .private/terms.json from GitHub
python3 -m src.public_safety install-hooks   # pre-commit and commit-msg hooks
```

`refresh-terms` reads `repositories.json` and collects each repository's owner and
name, the logins of everyone who opened or commented on a pull request, every
pull-request title and branch name, and the repository's file paths. Rerun it when
a repository is added. Two hand-kept lists in `.private/terms.json` survive a
refresh: `extra`, for names the reviewed repositories do not spell out, and
`allow`, for generic phrases that happen to match, such as a release title.

The hooks then refuse a commit whose added lines, file names, or message contain
any of those terms, a reference missing from the map, or a malformed one, and
print where each one is. Check the whole tree with
`python3 -m src.public_safety check --all`. Without the word list the check fails
rather than passing, because it has nothing to compare against.

## AI-assisted development process

1. Confirm the requested scope and inspect the current branch, status, relevant
   implementation, tests, and documentation.
2. Update local `main` without discarding user changes.
3. Create a focused branch using the required naming scheme.
4. Implement the smallest cohesive solution. Keep code, tests, configuration
   examples, and docs synchronized.
5. Run targeted tests followed by all required quality gates.
6. Review the full diff for secrets, unrelated changes, compatibility, failure
   handling, and public safety.
7. Commit with Conventional Commits and push the feature branch when the user
   has authorized publication.
8. Present the outcome and verification. Obtain explicit user approval before
   merging to `main`; silence or an earlier general instruction is not approval
   for a materially changed result.
9. Merge the approved branch to `main` only after required checks pass.
10. Delete the merged branch locally, and on the remote when it was published.
11. Prepare the SemVer release on the updated `main`, review the generated
    version and changelog, commit and tag it, then push `main` and the tag.

Do not merge, tag, publish, or deliver externally merely because local tests
pass. If a requested task is diagnosis or review only, do not implement or
publish a change without authorization.
