import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prwarden import doctor, setup_wizard, tools
from prwarden.doctor import NOTE, OK, PROBLEM
from prwarden.models import Repository
from prwarden.setup_wizard import SetupError
from prwarden.tools import TOOLS_BY_KEY, ToolState
from tests.test_setup_wizard import ScriptedPrompter
from tests.test_slack_scope import settings_for


def state(key, installed=True, signed_in=True, detail=""):
    tool = TOOLS_BY_KEY[key]
    if not installed:
        return ToolState(tool, None, None, None)
    return ToolState(tool, f"/opt/bin/{tool.command}", "1.2.3", signed_in, detail)


class SignInReadingTests(unittest.TestCase):
    def test_github_cli_lists_only_logins_that_work(self) -> None:
        output = json.dumps(
            {
                "hosts": {
                    "github.com": [
                        {"login": "reviewer", "state": "success", "active": True},
                        {"login": "expired", "state": "error"},
                    ]
                }
            }
        )

        self.assertEqual(
            tools.read_sign_in(TOOLS_BY_KEY["gh"], 1, output), (True, "reviewer")
        )

    def test_claude_code_says_so_in_json_whatever_it_exits(self) -> None:
        claude = TOOLS_BY_KEY["claude"]

        self.assertEqual(
            tools.read_sign_in(
                claude, 0, '{"loggedIn": true, "authMethod": "claude.ai"}'
            ),
            (True, "claude.ai"),
        )
        self.assertEqual(
            tools.read_sign_in(claude, 1, '{"loggedIn": false}'), (False, "")
        )

    def test_the_rest_answer_with_their_exit_code(self) -> None:
        for key in ("kiro", "codex", "antigravity"):
            with self.subTest(tool=key):
                tool = TOOLS_BY_KEY[key]
                self.assertEqual(tools.read_sign_in(tool, 0, "ok"), (True, ""))
                self.assertEqual(tools.read_sign_in(tool, 1, "no"), (False, ""))
                self.assertEqual(
                    tools.read_sign_in(tool, -1, ""), (None, "did not answer")
                )


class CheckTests(unittest.TestCase):
    """A real executable on PATH, answering the way the tool does."""

    def test_a_check_sees_the_login_a_schedule_would_see(self) -> None:
        """Claude Code finds its login only with USER set, which launchd omits."""

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "claude"
            script.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "--version" ]; then echo "2.1.311 (Claude Code)"; '
                "exit 0; fi\n"
                'if [ -n "$USER" ] && [ -z "$SECRET" ]; then '
                'echo \'{"loggedIn": true, "authMethod": "claude.ai"}\'; exit 0; fi\n'
                "echo '{\"loggedIn\": false}'; exit 1\n",
                encoding="utf-8",
            )
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
            environment = {"PATH": directory, "HOME": directory, "SECRET": "x"}
            with patch.dict(os.environ, environment, clear=True):
                result = tools.check(TOOLS_BY_KEY["claude"])

        self.assertEqual(result.version, "2.1.311")
        self.assertTrue(result.signed_in)
        self.assertEqual(result.detail, "claude.ai")

    def test_a_missing_tool_is_reported_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ, {"PATH": directory, "HOME": directory}, clear=True
            ), patch("prwarden.tools.search_path", return_value=directory):
                result = tools.check(TOOLS_BY_KEY["codex"])

        self.assertFalse(result.installed)
        self.assertIsNone(result.signed_in)

    def test_the_table_says_what_is_missing_and_who_is_signed_in(self) -> None:
        lines = tools.render_table(
            [
                state("gh", detail="reviewer"),
                state("claude", signed_in=False),
                state("codex", installed=False),
                ToolState(
                    TOOLS_BY_KEY["antigravity"], "/opt/bin/agy", None, None,
                    "did not answer",
                ),
            ]
        )

        self.assertEqual(
            lines,
            [
                "Tool         Installed  Signed in",
                "GitHub CLI   1.2.3      yes (reviewer)",
                "Claude Code  1.2.3      no",
                "Codex        no         -",
                "Antigravity  yes        did not answer",
            ],
        )


class InstallPlanTests(unittest.TestCase):
    def plan(self, key, platform, brew):
        found = "/opt/homebrew/bin/brew" if brew else None
        with patch("prwarden.tools.shutil.which", return_value=found):
            return tools.install_plan(TOOLS_BY_KEY[key], platform)

    def test_homebrew_is_preferred_on_macos(self) -> None:
        self.assertEqual(
            self.plan("claude", "darwin", brew=True).command,
            ("/opt/homebrew/bin/brew", "install", "--cask", "claude-code"),
        )
        self.assertEqual(
            self.plan("gh", "darwin", brew=True).command,
            ("/opt/homebrew/bin/brew", "install", "gh"),
        )

    def test_without_casks_the_vendor_installer_runs(self) -> None:
        for platform, brew in (("linux", True), ("darwin", False), ("linux", False)):
            with self.subTest(platform=platform, brew=brew):
                command = self.plan("codex", platform, brew).command
                self.assertEqual(command[:2], ("/bin/bash", "-c"))
                self.assertIn("https://chatgpt.com/codex/install.sh", command[2])
                self.assertIn("pipefail", command[2])

    def test_github_cli_without_homebrew_is_left_to_its_instructions(self) -> None:
        """Its Linux packages need sudo, which setup never runs."""

        plan = self.plan("gh", "linux", brew=False)

        self.assertIsNone(plan.command)
        self.assertIn("https://github.com/cli/cli", plan.description)

    def test_no_plan_ever_runs_sudo_or_an_unofficial_domain(self) -> None:
        official = (
            "https://cli.kiro.dev/",
            "https://claude.ai/",
            "https://chatgpt.com/",
            "https://antigravity.google/",
        )
        for tool in tools.TOOLS:
            self.assertNotIn("sudo", " ".join(tool.homebrew))
            if tool.installer:
                self.assertNotIn("sudo", tool.installer)
                self.assertTrue(any(domain in tool.installer for domain in official))


class EnsureToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ran = []
        self.after = {}

    def run_tool(self, command):
        self.ran.append(tuple(command))
        return 0

    def ensure(self, states, answers, run=None):
        prompter = ScriptedPrompter(answers)
        def rechecked(tool):
            return self.after.get(tool.key, state(tool.key, False))

        def plan(tool):
            return tools.InstallPlan(("install", tool.key), f"install {tool.key}")

        with patch("prwarden.setup_wizard.tools.check", side_effect=rechecked), patch(
            "prwarden.setup_wizard.tools.install_plan", side_effect=plan
        ):
            result = setup_wizard.ensure_tools(
                prompter, run or self.run_tool, lambda: list(states)
            )
        self.assertEqual(prompter.answers, [])
        return {item.tool.key: item for item in result}, prompter

    def test_a_chosen_cli_is_installed_then_signed_in(self) -> None:
        self.after["codex"] = state("codex", signed_in=False)
        states = [
            state("gh"),
            state("kiro"),
            state("claude", installed=False),
            state("codex", installed=False),
            state("antigravity", installed=False),
        ]

        def run(command):
            self.ran.append(tuple(command))
            if command[0].endswith("codex"):
                self.after["codex"] = state("codex")
            return 0

        result, prompter = self.ensure(states, ["2", "y", "y"], run)

        self.assertEqual(
            self.ran, [("install", "codex"), ("/opt/bin/codex", "login")]
        )
        self.assertTrue(result["codex"].signed_in)
        self.assertFalse(result["claude"].installed)
        self.assertTrue(any("Two AI CLIs" in line for line in prompter.transcript))

    def test_nothing_is_run_unless_the_operator_agrees(self) -> None:
        states = [state("gh"), state("kiro"), state("codex", installed=False)]

        self.ensure(states, ["1", "n"])

        self.assertEqual(self.ran, [])

    def test_a_signed_out_cli_is_offered_its_own_sign_in(self) -> None:
        self.after["claude"] = state("claude")
        states = [state("gh"), state("claude", signed_in=False)]

        result, _ = self.ensure(states, [""])

        self.assertEqual(self.ran, [("/opt/bin/claude", "auth", "login")])
        self.assertTrue(result["claude"].signed_in)

    def test_github_cli_is_required(self) -> None:
        with self.assertRaisesRegex(SetupError, "GitHub CLI is required"):
            self.ensure([state("gh", installed=False), state("kiro")], ["n"])


class DoctorTests(unittest.TestCase):
    def test_a_configured_engine_that_is_signed_out_is_a_problem(self) -> None:
        settings = settings_for("/tmp", Repository("app", "owner/app"))
        with patch("prwarden.doctor.shutil.which", return_value="/opt/bin/agy"):
            signed_out = doctor.check_engines(
                settings, [state("antigravity", signed_in=False)]
            )
            unknown = doctor.check_engines(
                settings, [state("antigravity", signed_in=None)]
            )
            ready = doctor.check_engines(settings, [state("antigravity")])

        self.assertEqual([check.status for check in signed_out], [PROBLEM])
        self.assertIn("sign in with: agy", signed_out[0].message)
        self.assertEqual([check.status for check in unknown], [NOTE])
        self.assertEqual([check.status for check in ready], [OK])


if __name__ == "__main__":
    unittest.main()
