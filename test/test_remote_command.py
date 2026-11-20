"""Regression tests for shell-independent remote executable lookup."""
import contextlib
import importlib
import io
import unittest
from unittest.mock import MagicMock, Mock, patch
from plumbum.commands import CommandNotFound
from recursiverpc.ssh import RemoteShell, ShellAwareMachine, shell_aware, shell_command
from plumbum.machines.paramiko_machine import ParamikoMachine, ParamikoPopen
from plumbum.machines.ssh_machine import SshMachine
from recursiverpc.ssh import SSHProcess
rpc = importlib.import_module("recursiverpc.runners")

class RemoteCommandTests(unittest.TestCase):
    def remote(self, shell, path):
        remote = RemoteShell.__new__(RemoteShell)
        remote.shell = shell
        import json
        remote.python_run = Mock(return_value=json.dumps(path))
        return remote

    def test_windows_bash_path_uses_forward_slashes(self):
        remote = self.remote("bash", r"C:\Users\User Name\uv.exe")
        self.assertEqual(str(remote.command("uv").executable), "C:/Users/User Name/uv.exe")

    def test_linux_bash_preserves_native_path(self):
        remote = self.remote("bash", "/usr/local/bin/uv")
        self.assertEqual(str(remote.command("uv").executable), "/usr/local/bin/uv")

    def test_missing_executable_raises(self):
        with self.assertRaises(CommandNotFound):
            self.remote("cmd", None).command("uv")

    def test_cmd_rejects_variable_expansion_in_arguments(self):
        with self.assertRaises(ValueError):
            shell_command(["python", "%SECRET%"], "cmd")

    def test_adapter_does_not_patch_other_connections_or_upstream_classes(self):
        first = MagicMock(spec=ParamikoMachine)
        second = MagicMock(spec=ParamikoMachine)
        first_popen, second_popen = first.popen, second.popen
        paramiko_popen, ssh_popen = ParamikoMachine.popen, SshMachine.popen
        with patch.object(ShellAwareMachine, "run", return_value=(0, "731029", "")), \
             patch.object(ShellAwareMachine, "python_run", return_value='["nt", "C:/Python/python.exe"]'):
            adapted = shell_aware(first, shell="cmd")
            other = shell_aware(second, shell="powershell")
        self.assertIs(adapted.machine, first)
        self.assertIs(other.machine, second)
        self.assertEqual(adapted.shell, "cmd")
        self.assertEqual(other.shell, "powershell")
        self.assertIs(shell_aware(adapted), adapted)
        self.assertIs(first.popen, first_popen)
        self.assertIs(second.popen, second_popen)
        self.assertIs(ParamikoMachine.popen, paramiko_popen)
        self.assertIs(SshMachine.popen, ssh_popen)
        self.assertTrue(issubclass(SSHProcess, ParamikoPopen))
        with self.assertRaises(ValueError):
            shell_aware(adapted, shell="bash")
        with adapted:
            pass
        first.close.assert_called_once()
        second.close.assert_not_called()

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
