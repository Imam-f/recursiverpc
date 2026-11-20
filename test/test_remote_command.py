"""Regression tests for shell-independent remote executable lookup."""
import contextlib
import importlib
import io
import unittest
from unittest.mock import MagicMock, Mock, patch
from plumbum.commands import CommandNotFound
from recursiverpc.ssh import RemoteShell, shell_command
rpc = importlib.import_module("recursiverpc.recursiverpc")

class RemoteCommandTests(unittest.TestCase):
    def remote(self, shell, path):
        remote = RemoteShell.__new__(RemoteShell)
        remote.shell = shell
        import json
        remote.python_run = Mock(return_value=json.dumps(path))
        return remote

    def test_windows_bash_path_uses_forward_slashes(self):
        remote = self.remote("bash", r"C:\Users\User Name\uv.exe")
        self.assertEqual(remote.command("uv").argv, ["C:/Users/User Name/uv.exe"])

    def test_linux_bash_preserves_native_path(self):
        remote = self.remote("bash", "/usr/local/bin/uv")
        self.assertEqual(remote.command("uv").argv, ["/usr/local/bin/uv"])

    def test_missing_executable_raises(self):
        with self.assertRaises(CommandNotFound):
            self.remote("cmd", None).command("uv")

    def test_cmd_rejects_variable_expansion_in_arguments(self):
        with self.assertRaises(ValueError):
            shell_command(["python", "%SECRET%"], "cmd")

    def test_failed_initialization_can_clean_up_without_missing_attributes(self):
        runner = rpc.NetworkRunner.__new__(rpc.NetworkRunner)
        with patch.object(rpc.NetworkRunner, "_check_uv_available", return_value=False):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "uv not installed"):
                    runner.__init__(1, MagicMock(spec=rpc.ParamikoMachine))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            runner.close()
        self.assertNotIn("no attribute", output.getvalue())
        # Keep the destructor's existing diagnostic output out of test results.
        with contextlib.redirect_stdout(io.StringIO()):
            del runner


if __name__ == "__main__":
    unittest.main()
