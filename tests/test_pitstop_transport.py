import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from services.pitstop import PitStopTransportError, SSHSettings, SSHTransport


class PitStopSSHTransportTests(unittest.TestCase):
    @patch("services.pitstop.transport.subprocess.run")
    def test_enforces_strict_noninteractive_ssh_and_timeout(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
        transport = SSHTransport(
            SSHSettings(
                host="pitstop.internal",
                username="operator",
                port=2222,
                known_hosts_file=Path("/etc/pitstop_known_hosts"),
                identity_file=Path("/run/secrets/pitstop_key"),
            )
        )

        result = transport.execute("whoami", timeout_seconds=15)

        self.assertEqual(result.stdout, "ok")
        command = run.call_args.args[0]
        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("UserKnownHostsFile=/etc/pitstop_known_hosts", command)
        self.assertNotIn("password", " ".join(command).lower())
        self.assertEqual(run.call_args.kwargs["timeout"], 15)

    @patch("services.pitstop.transport.subprocess.run")
    def test_converts_timeout_to_safe_domain_error(self, run):
        run.side_effect = subprocess.TimeoutExpired(["ssh"], 2)
        transport = SSHTransport(
            SSHSettings("host", "user", Path("/known_hosts"))
        )

        with self.assertRaisesRegex(PitStopTransportError, "не ответил"):
            transport.execute("command", timeout_seconds=2)


if __name__ == "__main__":
    unittest.main()
