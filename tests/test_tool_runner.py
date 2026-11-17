import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from core.tool_runner import ExternalToolError, run_command


class ToolRunnerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    @patch("core.tool_runner.subprocess.Popen")
    def test_applies_timeout_and_isolated_process_group(self, mocked_popen):
        process = mocked_popen.return_value.__enter__.return_value
        process.args = ["magick", "-version"]
        process.communicate.return_value = (None, None)
        process.poll.return_value = 0

        run_command(["magick", "-version"], timeout=12, check=True)

        mocked_popen.assert_called_once_with(["magick", "-version"], start_new_session=True)
        process.communicate.assert_called_once_with(None, timeout=12)

    @unittest.skipUnless(os.name == "posix", "POSIX subprocess wrapper")
    @patch("core.tool_runner.subprocess.Popen")
    def test_interrupted_communication_stops_tool_before_context_exit(self, mocked_popen):
        process = mocked_popen.return_value.__enter__.return_value
        process.communicate.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run_command(["magick", "-version"])
        process.kill.assert_called_once_with()

    def test_preserves_captured_output_on_nonzero_exit(self):
        command = [sys.executable, "-c", "import sys; sys.exit(sys.stdin.read())"]

        with self.assertRaises(subprocess.CalledProcessError) as raised:
            run_command(command, input="failure", capture_output=True, text=True, check=True)

        self.assertEqual(raised.exception.cmd, command)
        self.assertEqual(raised.exception.returncode, 1)
        self.assertEqual(raised.exception.stdout, "")
        self.assertEqual(raised.exception.stderr, "failure\n")

    @patch("core.tool_runner.os.name", "nt")
    @patch("core.tool_runner.subprocess.run")
    def test_non_posix_timeout_becomes_domain_error(self, mocked_run):
        mocked_run.side_effect = subprocess.TimeoutExpired(["gs"], 5)

        with self.assertRaisesRegex(ExternalToolError, "превысил лимит"):
            run_command(["gs", "-q"], timeout=5)

    @unittest.skipUnless(os.name == "posix", "POSIX process groups required")
    def test_timeout_terminates_descendant(self):
        import fcntl

        child = """
import fcntl
import os
import sys
import time
from pathlib import Path

with open(sys.argv[1], "w") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    Path(sys.argv[2]).write_text(str(os.getpid()))
    time.sleep(60)
"""
        parent = """
import subprocess
import sys
import time

subprocess.Popen([sys.executable, "-c", *sys.argv[1:]])
time.sleep(60)
"""
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "child.lock"
            pid_path = Path(directory) / "child.pid"
            try:
                with self.assertRaisesRegex(ExternalToolError, "превысил лимит"):
                    run_command(
                        [sys.executable, "-c", parent, child, str(lock_path), str(pid_path)],
                        timeout=1,
                        capture_output=True,
                    )

                self.assertTrue(pid_path.exists(), "descendant did not acquire the lock")
                deadline = time.monotonic() + 1
                with lock_path.open() as lock:
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                self.fail("descendant still holds the lock after timeout")
                            time.sleep(0.01)
                        else:
                            break
            finally:
                if pid_path.exists():
                    try:
                        os.kill(int(pid_path.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
