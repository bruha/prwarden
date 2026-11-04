# Releases

## Version policy

PR Warden uses [Semantic Versioning](https://semver.org/):

- `MAJOR`: a breaking public, configuration, state, CLI, or delivery-contract
  change;
- `MINOR`: a backward-compatible feature (`feat`);
- `PATCH`: a backward-compatible fix or any other release-worthy maintenance
  change when no feature or breaking change is present.

`VERSION` is the canonical version and must equal `src.__version__`. Release
tags use `vMAJOR.MINOR.PATCH`. `CHANGELOG.md` follows Keep a Changelog structure
and is generated from Conventional Commits since the latest reachable SemVer
tag. Until the first tag exists, the checked-in release baseline is used.

## Release command

Run the command only from a clean feature-complete `main` after the user has
approved and the feature branch has been merged:

```sh
python3 -m src.release --dry-run
python3 -m src.release
```

The command validates Conventional Commit subjects, calculates the highest
required bump, updates `VERSION` and `src.__version__`, and moves generated
entries into a dated changelog section. It does not commit, tag, merge, or push.

Override the calculated bump only after explaining and approving the reason:

```sh
python3 -m src.release --bump patch
python3 -m src.release --bump minor
python3 -m src.release --bump major
```

An override cannot be lower than the bump required by the commits. The command
stops on a dirty worktree, no releasable commits, an invalid version, a missing
baseline, a non-Conventional Commit, or disagreement between `VERSION` and
`src.__version__`.

## Release checklist

1. Confirm the approved feature branch is merged and local `main` matches the
   intended remote head.
2. Confirm `git status --short` is empty.
3. Run the complete test and compile checks.
4. Run `python3 -m src.release --dry-run` and review the proposed version and
   changelog.
5. Run `python3 -m src.release`.
6. Inspect `git diff -- VERSION src/__init__.py CHANGELOG.md`.
7. Re-run the complete quality gates.
8. Commit only release metadata:

   ```sh
   git add VERSION src/__init__.py CHANGELOG.md
   git commit -m "chore(release): vX.Y.Z"
   ```

9. Create an annotated tag at that release commit:

   ```sh
   git tag -a vX.Y.Z -m "vX.Y.Z"
   ```

10. Verify the commit and tag, then push in order:

    ```sh
    git push origin main
    git push origin vX.Y.Z
    ```

11. Verify the remote branch and tag. Do not describe the release as published
    until both pushes succeed.

If the branch push succeeds but the tag push fails, retain the local tag,
report the partial publication, and retry only the tag push after resolving the
cause. Never move or replace an already published version tag; create a new
patch release for corrections.
