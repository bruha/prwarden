import subprocess
import unittest
from unittest.mock import Mock, patch

from prwarden.commands import CommandError, run_cmd


class CommandRetryTests(unittest.TestCase):
    def test_retries_transient_failure_with_exponential_delay(self):
        failure = Mock(
            returncode=1,
            stderr="error connecting to api.github.com",
            stdout="",
        )
        success = Mock(returncode=0, stderr="", stdout="reviewer\n")

        with patch(
            "prwarden.commands.subprocess.run",
            side_effect=[failure, failure, success],
        ) as subprocess_run, patch("prwarden.commands.time.sleep") as sleep:
            output = run_cmd(
                ["gh", "api", "user"],
                retry_transient=True,
                retry_attempts=3,
                retry_delay_seconds=2,
            )

        self.assertEqual(output, "reviewer")
        self.assertEqual(subprocess_run.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_does_not_retry_permanent_failure(self):
        failure = Mock(returncode=1, stderr="HTTP 401: Bad credentials", stdout="")

        with patch(
            "prwarden.commands.subprocess.run",
            return_value=failure,
        ) as subprocess_run, patch("prwarden.commands.time.sleep") as sleep:
            with self.assertRaisesRegex(CommandError, "Bad credentials"):
                run_cmd(["gh", "api", "user"], retry_transient=True)

        subprocess_run.assert_called_once()
        sleep.assert_not_called()

    def test_command_errors_carry_the_failing_command_name(self):
        cases = (
            (
                "exit status",
                Mock(returncode=1, stderr="HTTP 503: unavailable", stdout=""),
                None,
                "gh exited with 1",
            ),
            (
                "timeout",
                None,
                subprocess.TimeoutExpired(cmd="gh", timeout=60),
                "Command timed out",
            ),
            (
                "missing binary",
                None,
                FileNotFoundError("gh"),
                "Required command not found",
            ),
        )
        for name, result, failure, expected in cases:
            with self.subTest(case=name):
                with patch(
                    "prwarden.commands.subprocess.run",
                    return_value=result,
                    side_effect=failure,
                ):
                    with self.assertRaises(CommandError) as caught:
                        run_cmd(["gh", "api", "user"])

                self.assertIn(expected, str(caught.exception))
                self.assertEqual(caught.exception.command, "gh")

    def test_command_name_is_absent_for_directly_raised_errors(self):
        self.assertIsNone(CommandError("Invalid GitHub repository slug: bad").command)

    def test_retries_command_timeout(self):
        timeout = subprocess.TimeoutExpired(["gh", "api", "user"], 60)
        success = Mock(returncode=0, stderr="", stdout="reviewer")

        with patch(
            "prwarden.commands.subprocess.run",
            side_effect=[timeout, success],
        ), patch("prwarden.commands.time.sleep") as sleep:
            output = run_cmd(
                ["gh", "api", "user"],
                retry_transient=True,
                retry_attempts=1,
                retry_delay_seconds=1,
            )

        self.assertEqual(output, "reviewer")
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
