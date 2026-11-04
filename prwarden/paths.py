"""Where PR Warden keeps its configuration, its state, and the clones it manages.

A source checkout keeps everything beside the code, as it always has: `.env` and
`repositories.json` in the checkout root and state under `.state/`. An installed copy
lives in a shared directory, such as Homebrew's, that must never hold private
settings or grow with review state, so it follows the XDG base directories, which
command-line tools use on macOS as well as Linux:

- configuration in `~/.config/prwarden`;
- state, run logs, and review worktrees in `~/.local/state/prwarden`;
- the clones PR Warden manages itself in `~/.local/share/prwarden/checkouts`.

`PRWARDEN_HOME` names one directory laid out like a checkout and overrides both.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
HOME_VARIABLE = "PRWARDEN_HOME"
APPLICATION_DIRECTORY_NAME = "prwarden"
STATE_DIRECTORY_NAME = ".state"
CHECKOUTS_DIRECTORY_NAME = "checkouts"
# Only a source checkout carries this file beside the package. An installed copy's
# parent directory is shared with every other installed package.
CHECKOUT_MARKER = "repositories.example.json"

LAYOUT_HOME = "home"
LAYOUT_CHECKOUT = "checkout"
LAYOUT_USER = "user"


class LocationError(ValueError):
    """Raised when the configured PR Warden home cannot be used."""


@dataclass(frozen=True)
class Locations:
    """The three places one PR Warden installation reads and writes."""

    layout: str
    config_directory: Path
    state_directory: Path
    checkouts_directory: Path

    @property
    def env_file(self) -> Path:
        return self.config_directory / ".env"

    @property
    def repositories_file(self) -> Path:
        return self.config_directory / "repositories.json"

    @property
    def state_file(self) -> Path:
        return self.state_directory / "review-state.json"


def is_source_checkout(root: Path) -> bool:
    """Return whether ``root`` is a PR Warden source checkout, not an install."""

    return (root / CHECKOUT_MARKER).is_file() and (
        root / APPLICATION_DIRECTORY_NAME / "__init__.py"
    ).is_file()


def _single_directory(layout: str, root: Path) -> Locations:
    state = root / STATE_DIRECTORY_NAME
    return Locations(layout, root, state, state / CHECKOUTS_DIRECTORY_NAME)


def _base_directory(
    environ: Mapping[str, str], variable: str, fallback: str, home: Path
) -> Path:
    value = environ.get(variable, "").strip()
    # The XDG specification ignores a relative value, which also keeps a stray one
    # from moving private configuration into whatever directory a run starts in.
    if value and Path(value).is_absolute():
        return Path(value)
    return home / fallback


def resolve_locations(
    environ: Optional[Mapping[str, str]] = None,
    package_root: Path = PACKAGE_ROOT,
    home: Optional[Path] = None,
) -> Locations:
    """Return where this run keeps its configuration, state, and managed clones.

    ``PRWARDEN_HOME`` wins, then a source checkout's own root, then the XDG base
    directories. The home must be absolute: a scheduled run starts in a directory
    nobody chose, and a relative home would quietly read another configuration.
    """

    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    configured = environ.get(HOME_VARIABLE, "").strip()
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            raise LocationError(
                f"{HOME_VARIABLE} must be an absolute path, not {configured!r}"
            )
        return _single_directory(LAYOUT_HOME, root.resolve())
    if is_source_checkout(package_root):
        return _single_directory(LAYOUT_CHECKOUT, package_root)
    return Locations(
        LAYOUT_USER,
        _base_directory(environ, "XDG_CONFIG_HOME", ".config", home)
        / APPLICATION_DIRECTORY_NAME,
        _base_directory(environ, "XDG_STATE_HOME", ".local/state", home)
        / APPLICATION_DIRECTORY_NAME,
        _base_directory(environ, "XDG_DATA_HOME", ".local/share", home)
        / APPLICATION_DIRECTORY_NAME
        / CHECKOUTS_DIRECTORY_NAME,
    )
