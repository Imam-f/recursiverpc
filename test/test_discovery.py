"""Keep ordinary test discovery independent of external SSH/RPC services."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from test import test_cross_shell


class DiscoveryTests(unittest.TestCase):
    def test_fresh_discovery_does_not_open_connections(self):
        # A fresh interpreter catches import side effects hidden by sys.modules.
        code = """
from unittest.mock import patch
import unittest
import paramiko
import rpyc
import runpy

with patch("socket.socket.connect", side_effect=AssertionError("network during discovery")) as network, \\
     patch.object(paramiko.SSHClient, "connect", side_effect=AssertionError("SSH during discovery")) as ssh, \\
     patch.object(rpyc.classic, "connect", side_effect=AssertionError("RPC during discovery")) as rpc:
    loader = unittest.TestLoader()
    suite = loader.discover(".")
    assert not loader.errors, loader.errors
    assert suite.countTestCases() > 0, "Discovery found no tests"
    runpy.run_path("experiments/rpyc_teleport.py", run_name="import_check")
    network.assert_not_called()
    ssh.assert_not_called()
    rpc.assert_not_called()
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_required_ci_shell_fails_instead_of_skipping(self):
        with (
            patch(
                "test.test_cross_shell.available_shells",
                return_value={"gitbash": False},
            ),
            patch.dict(os.environ, {"RRPC_TEST_REQUIRED_SHELLS": "gitbash"}),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Required test shells unavailable: gitbash"
            ):
                test_cross_shell.CrossShellTests.setUpClass()


if __name__ == "__main__":
    unittest.main()
