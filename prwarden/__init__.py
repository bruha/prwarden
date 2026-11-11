"""Automated pull-request reviewer application modules."""

import sys

__version__ = "3.0.1"

# The oldest Python PR Warden runs on. Checked here, when the package is first
# imported, so every way in is covered: `python3 -m prwarden` from a clone, the
# public-safety Git hooks, the release tool, and the evaluation harness. An older
# interpreter, such as macOS's /usr/bin/python3, then says what it needs instead of
# failing on syntax it cannot read. An installed copy is already held to it by
# requires-python.
MINIMUM_PYTHON = (3, 11)


def python_floor_message(version: str) -> str:
    """Return what to tell someone running PR Warden on Python ``version``."""

    return (
        "PR Warden needs Python {0}.{1} or newer, and this is Python {2}. Install "
        "it with Homebrew (brew install bruha/tap/prwarden) or pipx (pipx install "
        "prwarden), or run it with a newer python3."
    ).format(MINIMUM_PYTHON[0], MINIMUM_PYTHON[1], version)


if sys.version_info < MINIMUM_PYTHON:
    sys.exit(python_floor_message(sys.version.split()[0]))
