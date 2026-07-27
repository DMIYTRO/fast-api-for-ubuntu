import subprocess
import unittest
from unittest.mock import patch

from core.tool_runner import ExternalToolError, run_command


class ToolRunnerTests(unittest.TestCase):
    @patch("core.tool_runner.subprocess.run")
    def test_applies_timeout_and_isolated_process_group(self, mocked_run):
        mocked_run.return_value = subprocess.CompletedProcess(["magick"], 0)

        run_command(["magick", "-version"], timeout=12, check=True)

        mocked_run.assert_called_once_with(
            ["magick", "-version"],
            timeout=12,
            start_new_session=True,
            check=True,
        )

    @patch("core.tool_runner.subprocess.run")
    def test_timeout_becomes_domain_error(self, mocked_run):
        mocked_run.side_effect = subprocess.TimeoutExpired(["gs"], 5)

        with self.assertRaisesRegex(ExternalToolError, "превысил лимит"):
            run_command(["gs", "-q"], timeout=5)


if __name__ == "__main__":
    unittest.main()
