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

## Every change goes through a pull request

Nothing reaches `main` without a pull request that PR Warden has reviewed, this
repository's own changes and the Homebrew tap's formula bumps included. A release
commit is the one exception: it holds only the version and changelog the release
command generated from commits that were reviewed already.

1. Push the branch and open a pull request against `main`:

   ```sh
   git push -u origin docs/example-change
   gh pr create --base main --title "docs: example change" --body-file pr.md
   ```

   Open it as the maintainer account, not as the account PR Warden reviews as. PR
   Warden never approves its reviewer's own pull requests, so one opened by the
   reviewer gets comments and an `LGTM` at best, never an approval.

2. Wait for CI and for PR Warden's review. The next scheduled run picks the pull
   request up; to review it now, which spends credits, run
   `prwarden --repo bruha/prwarden --pr <number> --live`.
3. Address each finding with a new commit, or answer it on its thread when it is
   wrong. Every push is a new head, so it is reviewed again; push fixes together
   rather than one at a time.
4. Merge only after the maintainer approves. When the branch is still on top of
   `main`, fast-forward from the command line, so `main` receives exactly the
   commits that were reviewed and GitHub marks the pull request merged:

   ```sh
   git checkout main && git pull --ff-only
   git merge --ff-only docs/example-change
   git push origin main
   ```

   When `main` has moved on, merge through GitHub with a merge commit instead, which
   keeps the reviewed commits as they were; the release command skips merge commits,
   so their subjects never reach the changelog:

   ```sh
   gh pr merge <number> --merge --delete-branch
   ```

   Rebasing would make a new head, and with it another full review. Rebase only when
   the branch conflicts with `main`, push it with `git push --force-with-lease`,
   which is allowed on a feature branch and never on `main`, and let the new head be
   reviewed before merging.

5. Delete the branch, as in [branch cleanup](#branch-cleanup).

The tap's pull requests merge through GitHub instead, with
`gh pr merge --rebase --delete-branch`, once its macOS and Linux checks pass and PR
Warden has reviewed them.

## Public safety

This tool is developed against private repositories, and nothing learned there may
reach this repository: not their organisation or repository names, people's logins,
pull-request numbers, titles, or branch names, file paths from their code, nor
details of their product. That covers commit messages too, because the changelog is
built from them.

A real pull request worth citing, as the origin of a rule or a regression test, is
cited by a reference instead of a link:

```sh
python3 -m prwarden.public_safety new-ref https://github.com/<org>/<repo>/pull/<n> \
  --note "what happened there"
# prints [pr:xxxx]; write that in the comment, docstring, or document
```

The reference is random, so it reveals nothing, and the map back to the URL lives in
`.private/pr-refs.json`, which is ignored. Back that file up: unlike the word list,
it cannot be rebuilt. Measurements are reported as counts, never as named cases.

A maintainer with access to the reviewed repositories sets up the check once per
clone:

```sh
python3 -m prwarden.public_safety refresh-terms   # builds .private/terms.json from GitHub
python3 -m prwarden.public_safety install-hooks   # pre-commit and commit-msg hooks
```

`refresh-terms` reads `repositories.json` and collects each repository's owner and
name, the logins of everyone who opened or commented on a pull request, every
pull-request title and branch name, and the repository's file paths. Rerun it when
a repository is added. Two hand-kept lists in `.private/terms.json` survive a
refresh: `extra`, for names the reviewed repositories do not spell out, and
`allow`, for generic phrases that happen to match, such as a release title. A
reviewed repository whose slug is in `allow` contributes nothing, which is how this
project's own public repositories are reviewed without their names refusing every
commit that mentions the project.

The hooks then refuse a commit whose added lines, file names, or message contain
any of those terms, a reference missing from the map, or a malformed one, and
print where each one is. Check the whole tree with
`python3 -m prwarden.public_safety check --all`. Without the word list the check fails
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
7. Commit with Conventional Commits, push the feature branch, and open a pull
   request, as in [every change goes through a pull
   request](#every-change-goes-through-a-pull-request). Never merge a change
   without one.
8. Wait for CI and PR Warden's review, and address its findings. Then present the
   outcome, the review, and the verification. Obtain explicit user approval before
   merging to `main`; silence or an earlier general instruction is not approval
   for a materially changed result.
9. Merge the approved pull request to `main`, by fast-forward or a merge commit,
   only after required checks pass.
10. Delete the merged branch locally, and on the remote when it was published.
11. Prepare the SemVer release on the updated `main`, review the generated
    version and changelog, commit and tag it, then push `main` and the tag.

Do not merge, tag, publish, or deliver externally merely because local tests
pass. If a requested task is diagnosis or review only, do not implement or
publish a change without authorization.
