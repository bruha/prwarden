#!/usr/bin/env python3
"""Command-line entry point for PR Warden."""

import sys

# Checked before anything else is imported, so `python3 -m prwarden` from a clone on
# an older interpreter, such as macOS's /usr/bin/python3, explains what it needs
# instead of failing on syntax that interpreter cannot read. An installed copy is
# already held to this by requires-python.
MINIMUM_PYTHON = (3, 11)

if sys.version_info < MINIMUM_PYTHON:
    sys.exit(
        "PR Warden needs Python {0}.{1} or newer, and this is Python {2}. Install "
        "it with Homebrew (brew install bruha/tap/prwarden) or pipx (pipx install "
        "prwarden), or run it with a newer python3.".format(
            MINIMUM_PYTHON[0], MINIMUM_PYTHON[1], sys.version.split()[0]
        )
    )

from .cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
