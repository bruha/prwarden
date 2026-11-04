import tempfile
import unittest
from pathlib import Path

from prwarden import paths


def make_package(root: Path, checkout: bool) -> Path:
    """Lay out ``root`` as a source checkout or as an installed copy's parent."""

    (root / "prwarden").mkdir(parents=True)
    (root / "prwarden" / "__init__.py").write_text("", encoding="utf-8")
    if checkout:
        (root / "repositories.example.json").write_text("[]", encoding="utf-8")
    return root


class ResolveLocationsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()

    def test_a_source_checkout_keeps_everything_beside_the_code(self) -> None:
        """The layout every existing clone and schedule already depends on."""

        package_root = make_package(self.root / "clone", checkout=True)

        locations = paths.resolve_locations({}, package_root, self.home)

        self.assertEqual(locations.layout, paths.LAYOUT_CHECKOUT)
        self.assertEqual(locations.env_file, package_root / ".env")
        self.assertEqual(
            locations.repositories_file, package_root / "repositories.json"
        )
        self.assertEqual(
            locations.state_file, package_root / ".state" / "review-state.json"
        )
        self.assertEqual(
            locations.checkouts_directory, package_root / ".state" / "checkouts"
        )

    def test_an_installed_copy_never_writes_beside_the_package(self) -> None:
        """Its parent is a shared directory such as Homebrew's site-packages."""

        package_root = make_package(self.root / "site-packages", checkout=False)

        locations = paths.resolve_locations({}, package_root, self.home)

        self.assertEqual(locations.layout, paths.LAYOUT_USER)
        self.assertEqual(
            locations.env_file, self.home / ".config" / "prwarden" / ".env"
        )
        self.assertEqual(
            locations.state_file,
            self.home / ".local" / "state" / "prwarden" / "review-state.json",
        )
        self.assertEqual(
            locations.checkouts_directory,
            self.home / ".local" / "share" / "prwarden" / "checkouts",
        )
        for location in (
            locations.config_directory,
            locations.state_directory,
            locations.checkouts_directory,
        ):
            self.assertNotIn(package_root, location.parents)

    def test_absolute_xdg_directories_are_honoured_and_relative_ones_ignored(
        self,
    ) -> None:
        package_root = make_package(self.root / "site-packages", checkout=False)
        environ = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": "relative/state",
            "XDG_DATA_HOME": str(self.root / "data"),
        }

        locations = paths.resolve_locations(environ, package_root, self.home)

        self.assertEqual(locations.config_directory, self.root / "config" / "prwarden")
        self.assertEqual(
            locations.state_directory, self.home / ".local" / "state" / "prwarden"
        )
        self.assertEqual(
            locations.checkouts_directory,
            self.root / "data" / "prwarden" / "checkouts",
        )

    def test_prwarden_home_overrides_a_checkout_and_the_xdg_directories(
        self,
    ) -> None:
        package_root = make_package(self.root / "clone", checkout=True)
        chosen = self.root / "chosen"
        environ = {
            paths.HOME_VARIABLE: str(chosen),
            "XDG_CONFIG_HOME": str(self.root / "config"),
        }

        locations = paths.resolve_locations(environ, package_root, self.home)

        self.assertEqual(locations.layout, paths.LAYOUT_HOME)
        self.assertEqual(locations.env_file, chosen / ".env")
        self.assertEqual(locations.state_file, chosen / ".state" / "review-state.json")

    def test_a_relative_prwarden_home_is_refused(self) -> None:
        """A scheduled run starts in a directory nobody chose."""

        package_root = make_package(self.root / "clone", checkout=True)

        with self.assertRaisesRegex(paths.LocationError, "absolute"):
            paths.resolve_locations(
                {paths.HOME_VARIABLE: "prwarden-home"}, package_root, self.home
            )

    def test_this_repository_is_recognised_as_a_source_checkout(self) -> None:
        """Losing the marker would quietly move every clone's configuration."""

        self.assertTrue(paths.is_source_checkout(paths.PACKAGE_ROOT))


if __name__ == "__main__":
    unittest.main()
