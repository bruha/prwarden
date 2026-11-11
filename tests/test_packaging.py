import importlib
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import prwarden
from prwarden import MINIMUM_PYTHON
from prwarden.paths import PACKAGE_ROOT


PACKAGE = PACKAGE_ROOT / "prwarden"
PYPROJECT = PACKAGE_ROOT / "pyproject.toml"
# Documentation for people editing the checkout; an installed copy never reads it.
UNSHIPPED = {Path("templates") / "README.md"}


class PackageDataTests(unittest.TestCase):
    def test_every_file_the_package_reads_is_shipped(self) -> None:
        """A checkout reads files beside its modules that a wheel only carries when
        `pyproject.toml` names them; one left out works in every test and fails
        in every install."""

        text = PYPROJECT.read_text(encoding="utf-8")
        data_files = sorted(
            path.relative_to(PACKAGE)
            for path in PACKAGE.rglob("*")
            if path.is_file()
            and path.suffix not in {".py", ".pyc"}
            and "__pycache__" not in path.parts
            # Hidden files such as macOS's .DS_Store are the desktop's, not ours.
            and not path.name.startswith(".")
        )

        self.assertTrue(data_files)
        for relative in data_files:
            if relative in UNSHIPPED:
                continue
            with self.subTest(file=relative.as_posix()):
                self.assertIn(f'"{relative.as_posix()}"', text)

    def test_the_version_is_read_from_the_canonical_file(self) -> None:
        text = PYPROJECT.read_text(encoding="utf-8")

        self.assertIn('version = { file = "VERSION" }', text)
        self.assertIn('dynamic = ["version"]', text)


class PythonFloorTests(unittest.TestCase):
    """The documented floor, the enforced one, and the tested one never disagree."""

    def test_requires_python_the_entry_check_and_ci_name_one_floor(self) -> None:
        floor = "{0}.{1}".format(*MINIMUM_PYTHON)
        workflow = (PACKAGE_ROOT / ".github" / "workflows" / "test.yml").read_text(
            encoding="utf-8"
        )
        tested = re.findall(r'python-version: \[([^\]]*)\]', workflow)

        self.assertIn(
            f'requires-python = ">={floor}"', PYPROJECT.read_text(encoding="utf-8")
        )
        self.assertTrue(tested)
        for versions in tested:
            with self.subTest(versions=versions):
                oldest = min(
                    tuple(int(part) for part in version.strip(' "').split("."))
                    for version in versions.split(",")
                )
                self.assertEqual(oldest, MINIMUM_PYTHON)

    def test_an_older_python_is_told_what_it_needs_before_any_import(self) -> None:
        """Checked when the package is imported, so every entry point is covered:
        `python3 -m prwarden`, the public-safety hooks, release, and evaluate."""

        self.addCleanup(importlib.reload, prwarden)
        with patch.object(sys, "version_info", (3, 9, 6, "final", 0)), patch.object(
            sys, "version", "3.9.6 (default, May 22 2026)"
        ):
            with self.assertRaises(SystemExit) as raised:
                importlib.reload(prwarden)

        message = str(raised.exception.code)
        self.assertIn("needs Python 3.11 or newer", message)
        self.assertIn("this is Python 3.9.6", message)
        self.assertIn("brew install bruha/tap/prwarden", message)

    def test_a_supported_python_imports_the_package(self) -> None:
        version = prwarden.__version__

        importlib.reload(prwarden)

        self.assertEqual(prwarden.__version__, version)

if __name__ == "__main__":
    unittest.main()
