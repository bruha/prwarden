import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from src import public_safety
from src.public_safety import (
    RepositorySource,
    SafetyError,
    Terms,
    build_terms,
    install_hooks,
    main,
    new_ref,
    refresh_terms,
    scan,
    scan_line,
    scan_refs,
    staged_lines,
)


def terms() -> Terms:
    return Terms(
        words=["acme-corp", "acme-shop", "jdoe-dev"],
        titles=[
            "feat: add rocket launcher",
            "add rocket launcher",
            "Develop into Main",
        ],
        branches=["feat/rocket-launcher"],
        paths={"src/rockets/launch/Pad.tsx"},
        allow=["develop into main"],
    )


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=str(root),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def write_terms(root: Path) -> None:
    private = root / ".private"
    private.mkdir(exist_ok=True)
    (private / "terms.json").write_text(
        json.dumps({"words": ["acme-corp"], "titles": [], "branches": [], "paths": []})
    )


class ScanTests(unittest.TestCase):
    def test_names_match_whole_names_in_any_case(self) -> None:
        found = scan_line("seen on ACME-CORP#12", terms())
        self.assertEqual(found, [("name", "ACME-CORP")])
        found = scan_line("see org/acme-shop/pull/3", terms())
        self.assertEqual(found[0][1], "acme-shop")
        for unrelated in ("acme-corporate", "notacme-corp", "acme corp"):
            with self.subTest(unrelated):
                self.assertEqual(scan_line(unrelated, terms()), [])

    def test_pull_request_titles_match_anywhere_in_a_line(self) -> None:
        found = scan_line('"title": "Feat: Add Rocket Launcher",', terms())
        self.assertIn(("pull-request title", "Feat: Add Rocket Launcher"), found)

    def test_an_allowed_phrase_hides_only_what_it_covers(self) -> None:
        """A generic title such as a release merge is ordinary prose elsewhere."""

        self.assertEqual(scan_line("a promotion of develop into main", terms()), [])
        found = scan_line("develop into main, then add rocket launcher", terms())
        self.assertEqual(found, [("pull-request title", "add rocket launcher")])

    def test_branches_match_exactly(self) -> None:
        self.assertEqual(
            scan_line("head_ref: feat/rocket-launcher", terms()),
            [("branch", "feat/rocket-launcher")],
        )
        self.assertEqual(scan_line("feat/rocket-launcher-v2", terms()), [])

    def test_a_reviewed_repository_path_matches_inside_a_longer_path(self) -> None:
        found = scan_line("cited app/src/rockets/launch/Pad.tsx:12", terms())
        self.assertEqual(
            found,
            [
                (
                    "file path from a reviewed repository",
                    "app/src/rockets/launch/Pad.tsx",
                )
            ],
        )
        self.assertEqual(scan_line("src/rockets/launch/PadX.tsx", terms()), [])

    def test_references_must_be_well_formed_and_known(self) -> None:
        refs = {"a2b3": {"url": "https://example.invalid/pull/1"}}
        self.assertEqual(scan_refs("lost a level [pr:a2b3]", refs), [])
        self.assertEqual(scan_refs("cite it as [pr:xxxx]", refs), [])
        self.assertEqual(
            scan_refs("[pr:c4d5] and [pr:A2B3] and [pr:z9z9]", refs),
            [
                ("reference missing from the private map", "[pr:c4d5]"),
                ("malformed reference", "[pr:A2B3]"),
                ("malformed reference", "[pr:z9z9]"),
            ],
        )

    def test_references_are_only_checked_for_form_without_the_map(self) -> None:
        """A clone without the private map can still catch a mistyped reference."""

        self.assertEqual(scan_refs("[pr:c4d5]", None), [])
        self.assertEqual(
            scan_refs("[pr:c4]", None), [("malformed reference", "[pr:c4]")]
        )

    def test_findings_name_where_they_are(self) -> None:
        findings = scan([("README.md:7", "on acme-corp")], terms(), None)
        self.assertEqual(
            [f.render() for f in findings], ["README.md:7: name: acme-corp"]
        )

    def test_only_this_module_may_spell_out_malformed_references(self) -> None:
        line = "acme-corp and [pr:c4]"
        for location, expected in (
            ("src/public_safety.py:9", ["name"]),
            ("tests/test_public_safety.py:9", ["name"]),
            ("src/cli.py:9", ["name", "malformed reference"]),
        ):
            with self.subTest(location):
                kinds = [f.kind for f in scan([(location, line)], terms(), {})]
                self.assertEqual(kinds, expected)


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        git(self.root, "init", "-q")
        (self.root / "notes.txt").write_text("one\n")
        git(self.root, "add", "notes.txt")
        git(self.root, "commit", "-q", "-m", "chore: start")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_main(self, *argv: str) -> "tuple[int, str]":
        error = io.StringIO()
        with redirect_stderr(error), redirect_stdout(io.StringIO()):
            code = main(list(argv), root=self.root)
        return code, error.getvalue()

    def test_only_staged_additions_and_file_names_are_checked(self) -> None:
        (self.root / "notes.txt").write_text("one\nacme-corp here\n")
        (self.root / "acme-corp.md").write_text("clean\n")
        git(self.root, "add", "notes.txt", "acme-corp.md")
        (self.root / "unstaged.txt").write_text("acme-corp\n")

        lines = staged_lines(self.root)

        self.assertIn(("notes.txt:2", "acme-corp here"), lines)
        self.assertIn(("acme-corp.md (file name)", "acme-corp.md"), lines)
        self.assertNotIn("one", [text for _, text in lines])
        self.assertFalse(any("unstaged" in location for location, _ in lines))
        write_terms(self.root)
        code, error = self.run_main("check")
        self.assertEqual(code, 1)
        self.assertIn("notes.txt:2: name: acme-corp", error)
        self.assertIn("acme-corp.md (file name): name: acme-corp", error)

    def test_the_whole_tree_can_be_checked(self) -> None:
        write_terms(self.root)
        self.assertEqual(self.run_main("check", "--all")[0], 0)
        (self.root / "docs.md").write_text("ok\nacme-corp\n")
        git(self.root, "add", "docs.md")
        code, error = self.run_main("check", "--all")
        self.assertEqual(code, 1)
        self.assertIn("docs.md:2: name: acme-corp", error)

    def test_a_commit_message_is_checked_without_git_comments(self) -> None:
        write_terms(self.root)
        message = self.root / "MESSAGE"
        message.write_text("fix: a thing\n\n# acme-corp appears in the status\n")
        self.assertEqual(self.run_main("check-message", str(message))[0], 0)
        message.write_text("fix: a thing\n\nSeen on acme-corp.\n")
        code, error = self.run_main("check-message", str(message))
        self.assertEqual(code, 1)
        self.assertIn("commit message:3: name: acme-corp", error)

    def test_a_missing_word_list_fails_rather_than_passing(self) -> None:
        code, error = self.run_main("check")
        self.assertEqual(code, 2)
        self.assertIn("refresh-terms", error)

    def test_new_references_are_private_unique_and_stable(self) -> None:
        first = new_ref(self.root, "https://example.invalid/pull/1", "a note")
        again = new_ref(self.root, "https://example.invalid/pull/1")
        second = new_ref(self.root, "https://example.invalid/pull/2")

        self.assertEqual(first, again)
        self.assertNotEqual(first, second)
        for ref in (first, second):
            self.assertRegex(ref, r"\A[a-z2-7]{4}\Z")
            self.assertTrue(any(character.isdigit() for character in ref))
        path = self.root / ".private" / "pr-refs.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        stored = json.loads(path.read_text())["refs"]
        self.assertEqual(
            stored[first], {"url": "https://example.invalid/pull/1", "note": "a note"}
        )

    def test_a_new_reference_skips_word_like_and_taken_ids(self) -> None:
        taken = new_ref(self.root, "https://example.invalid/pull/1")
        draws = iter("abcd" + taken + "k4m5")
        with patch("src.public_safety.secrets.choice", lambda _: next(draws)):
            ref = new_ref(self.root, "https://example.invalid/pull/2")
        self.assertEqual(ref, "k4m5")

    def test_hooks_are_installed_and_never_replace_a_foreign_hook(self) -> None:
        written = install_hooks(self.root)

        names = sorted(path.name for path in written)
        self.assertEqual(names, ["commit-msg", "pre-commit"])
        for path in written:
            self.assertTrue(os.access(path, os.X_OK))
            self.assertIn("src.public_safety", path.read_text())
        install_hooks(self.root)
        foreign = self.root / ".git" / "hooks" / "pre-commit"
        foreign.write_text("#!/bin/sh\nexit 0\n")
        with self.assertRaisesRegex(SafetyError, "--force"):
            install_hooks(self.root)
        install_hooks(self.root, force=True)
        self.assertIn(public_safety.HOOK_MARKER, foreign.read_text())

    def test_the_word_list_is_built_from_the_reviewed_repositories(self) -> None:
        checkout = self.root / "checkout"
        checkout.mkdir()
        (self.root / "repositories.json").write_text(
            json.dumps([{"slug": "acme-corp/acme-shop", "path": str(checkout)}])
        )
        pulls = [
            {"title": "feat: add rocket launcher", "author": {"login": "jdoe-dev"},
             "headRefName": "feat/rocket-launcher"},
            {
                "title": "fix: typo",
                "author": {"login": "operator"},
                "headRefName": "dev",
            },
        ]
        def run(args):
            if args[:3] == ["gh", "api", "user"]:
                return "operator\n"
            if args[:2] == ["gh", "pr"]:
                return json.dumps(pulls)
            if args[:3] == ["gh", "api", "--paginate"]:
                return "renovate[bot]\nreviewer-two\n" if "pulls/" in args[3] else ""
            if args[-1] == "ls-files":
                return "src/rockets/launch/Pad.tsx\nsrc/a/README.md\nnotes.txt\n"
            raise AssertionError(args)

        private = self.root / ".private"
        private.mkdir()
        previous = {"extra": ["hand-added"], "allow": ["ok"]}
        (private / "terms.json").write_text(json.dumps(previous))
        result = refresh_terms(self.root, self.root / "repositories.json", run)

        self.assertEqual(
            result["words"],
            [
                "acme-corp",
                "acme-corp/acme-shop",
                "acme-shop",
                "jdoe-dev",
                "reviewer-two",
            ],
        )
        self.assertEqual(
            result["titles"], ["add rocket launcher", "feat: add rocket launcher"]
        )
        self.assertEqual(result["branches"], ["feat/rocket-launcher"])
        self.assertEqual(result["paths"], ["src/rockets/launch/Pad.tsx"])
        self.assertEqual((result["extra"], result["allow"]), (["hand-added"], ["ok"]))
        written = private / "terms.json"
        self.assertEqual(json.loads(written.read_text())["words"], result["words"])
        self.assertEqual(stat.S_IMODE(written.stat().st_mode), 0o600)


class BuildTermsTests(unittest.TestCase):
    def test_paths_this_repository_also_has_are_not_private(self) -> None:
        source = RepositorySource(
            slug="acme-corp/acme-shop",
            logins=frozenset(),
            titles=(),
            branches=frozenset(),
            paths=frozenset(
                {".github/workflows/test.yml", "src/rockets/launch/Pad.tsx"}
            ),
        )
        result = build_terms([source], "operator", [".github/workflows/test.yml"])
        self.assertEqual(result["paths"], ["src/rockets/launch/Pad.tsx"])


if __name__ == "__main__":
    unittest.main()
