"""SSH setup regression tests. Run: python -m unittest discover -s test -p test_ssh_setup.py -v

No SSH credentials or live servers are used. Shell tests run in temporary folders.
"""

import contextlib
import importlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from plumbum.commands import ProcessExecutionError

rpc = importlib.import_module("recursiverpc.recursiverpc")


class ShellSession:
    """Execute actual shell commands inside one isolated test directory."""

    def __init__(self, executable, directory, powershell=False):
        self.executable = executable
        self.directory = directory
        self.powershell = powershell

    def run(self, command, retcode=0):
        if self.powershell:
            args = [self.executable, "-NoProfile", "-NonInteractive", "-Command",
                    "$ErrorActionPreference = 'Stop'; " + command]
        else:
            args = [self.executable, "-c", command]
        result = subprocess.run(args, cwd=self.directory, capture_output=True,
                                text=True, timeout=10)
        if retcode is not None and result.returncode != retcode:
            raise ProcessExecutionError(command, result.returncode,
                                        result.stdout, result.stderr)
        return result.returncode, result.stdout, result.stderr


class SSHSetupTests(unittest.TestCase):
    def test_missing_mktemp_uses_python_fallback(self):
        calls = []

        def run(command, retcode=0):
            calls.append(command)
            if command == "mktemp -d":
                if retcode is not None:
                    raise ProcessExecutionError(command, 127, "", "mktemp not found")
                return 127, "", "mktemp not found"
            return 0, "C:/isolated-temp\n", ""

        remote = SimpleNamespace(session=lambda: SimpleNamespace(run=run))
        directory = rpc.CrossPlatformTempDir(remote)
        self.assertEqual(directory.__enter__(), "C:/isolated-temp")
        self.assertTrue(any("tempfile.mkdtemp" in cmd for cmd in calls))

    def test_cleanup_retries_after_command_failure(self):
        attempts = []

        def run(command, retcode=0):
            attempts.append(command)
            if len(attempts) == 1:
                if retcode is not None:
                    raise ProcessExecutionError(command, 1, "", "temporarily busy")
                return 1, "", "temporarily busy"
            return 0, "", ""

        directory = rpc.CrossPlatformTempDir(None)
        directory.path = "/isolated-test-directory"
        directory.session = SimpleNamespace(run=run)
        with patch.object(rpc.time, "sleep"):
            directory.__exit__(None, None, None)
        self.assertEqual(len(attempts), 2)

    def test_output_monitor_stops_at_eof(self):
        class Stream:
            reads = 0

            def readline(self):
                return b"12345\n"

            def read(self, count):
                self.reads += 1
                if self.reads > 1:
                    raise RuntimeError("test guard: monitor read again after EOF")
                return b""

        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        stream = Stream()
        # Avoid constructing/deleting a real deployed server and its resources.
        server = SimpleNamespace(proc=SimpleNamespace(stdout=stream))
        with patch.object(rpc, "Thread", InlineThread), contextlib.redirect_stdout(io.StringIO()):
            rpc.DeployedCrossPlatformServer._handle_server_startup(server)
        self.assertEqual(server.remote_port, 12345)
        self.assertEqual(stream.reads, 1, "monitor must stop at the first EOF")

    def check_script_upload(self, shell, powershell=False):
        script = "print(\"single ' quote\")\nprint(r'C:\\temp\\file')"
        with tempfile.TemporaryDirectory(prefix="recursiverpc-shell-test-") as directory:
            session = ShellSession(shell, directory, powershell)
            remote = SimpleNamespace(session=lambda: session)
            with contextlib.redirect_stdout(io.StringIO()):
                rpc.DeployedCrossPlatformServer._write_server_script(
                    None, remote, Path(directory).as_posix(), script
                )
            self.assertEqual((Path(directory) / "server.py").read_text(), script + "\n")

    def test_script_upload_with_bash(self):
        shell = shutil.which("bash")
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        if os.name == "nt" and git_bash.exists():
            shell = str(git_bash)
        if not shell:
            self.skipTest("Bash unavailable")
        self.check_script_upload(shell)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell test")
    def test_script_upload_with_powershell(self):
        shell = shutil.which("powershell")
        if not shell:
            self.skipTest("Windows PowerShell unavailable")
        self.check_script_upload(shell, powershell=True)


if __name__ == "__main__":
    unittest.main()
