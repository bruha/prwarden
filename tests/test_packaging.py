import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
