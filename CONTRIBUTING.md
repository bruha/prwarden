# Contributing

PR Warden has one maintainer, so contributions are welcome but deliberately
narrow.

## Issues

Open an issue for a bug, a question, or an idea. A useful bug report names the
version (`python3 -m src.prwarden --version`), the review engine and its CLI
version, what you ran, and what happened. Report a vulnerability through
[SECURITY.md](SECURITY.md) instead, never in a public issue.

Leave out anything from the repositories you review: their names, people's logins,
pull-request titles or numbers, file paths, and review output. Describe the shape of
a problem instead, or reproduce it on a public example.

## Pull requests

- **For an accepted issue.** Open or comment on an issue first and wait until the
  maintainer agrees on the approach. A pull request for a change nobody discussed
  may be closed unread, however good it is.
- **Small fixes any time.** A typo, a broken link, or an unclear sentence in the
  documentation needs no issue.
- **You own what you submit.** If an agent wrote the change, review it yourself
  before opening the pull request and say so in its description.

A change follows [AGENTS.md](AGENTS.md) and the guides it links. In short:

- Python 3.9 compatible, standard library only, no new runtime dependencies;
- one focused change, with its tests and documentation updated together;
- a branch and commit messages in [Conventional Commits](https://www.conventionalcommits.org/)
  form, as in [the development workflow](docs/development-workflow.md);
- the quality gates pass:

```sh
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
git diff --check
```

The public-safety hooks described in the workflow guide check commits against a
word list built from the maintainer's own reviewed repositories. You do not need
them, and without that list they refuse every commit, so do not install them.

## License

Contributions are accepted under the [Apache License 2.0](LICENSE), as its
section 5 provides; no separate agreement is needed.
