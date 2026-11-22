"""Exercise the actual local shells; Linux Bash runs in WSL on Windows."""

import json
import os
import unittest

from plumbum.commands import ProcessExecutionError
from plumbum.machines.remote import RemoteCommand

from recursiverpc.recursiverpc import DeployedCrossPlatformServer
from recursiverpc.ssh import shell_aware
from test.shells import LocalShell, available_shells


class CrossShellTests(unittest.TestCase):
    def check_shell(self, kind, expected):
        remote = LocalShell(kind)
        self.assertEqual(remote.shell, expected)
        self.assertIs(shell_aware(remote), remote)
        directory = remote.make_temp_dir()
        try:
            # This exceeds cmd's command-line limit if embedded in the command.
            content = (
                '# Unicode: café 雪\nprint("quotes: \' \\" & | % ! $ `")\n'
                + "# padding\n" * 2000
            )
            path = remote.write_script(directory, content)
            readback = remote.python_run(
                "import pathlib,json; print(json.dumps(pathlib.Path("
                + repr(path)
                + ").read_bytes().hex()))"
            )
            self.assertEqual(
                bytes.fromhex(json.loads(readback)), content.encode("utf-8")
            )
            executable = json.loads(
                remote.python_run("import sys,json; print(json.dumps(sys.executable))")
            )
            command = remote.command(executable)
            self.assertIsInstance(command, RemoteCommand)
            self.assertEqual(command("-c", "print(42)").strip(), "42")
            # Exercise Plumbum's own binding/run/error interfaces, not a copy.
            argument = "quote ' \" & | $ % !\nsecond line"
            bound = command["-c"]["import sys;print(sys.argv[1])"][argument]
            self.assertEqual(bound().replace("\r\n", "\n").strip(), argument)
            code, output, error = command.run(["-c", "print(17)"])
            self.assertEqual((code, output.strip(), error), (0, "17", ""))
            with self.assertRaises(ProcessExecutionError):
                command("-c", "import sys;sys.exit(7)")
            self.assertEqual(
                command.run(["-c", "import sys;sys.exit(7)"], retcode=7)[0], 7
            )
            configured = command.with_env(RRPC_TEST_VALUE="unicode 雪 & quotes ' \"")
            output = configured(
                "-c", "import os,json;print(json.dumps(os.environ['RRPC_TEST_VALUE']))"
            )
            self.assertEqual(json.loads(output), "unicode 雪 & quotes ' \"")
            output = command.with_cwd(directory)(
                "-c", "import os,json;print(json.dumps(os.getcwd()))"
            )
            self.assertEqual(json.loads(output), directory)
        finally:
            remote.remove_temp_dir(directory)
        self.assertEqual(
            remote.python_run(
                "import os; print(os.path.exists(" + repr(directory) + "))"
            ),
            "False",
        )

        # Full deployment lifecycle through actual shell launch, with a tiny
        # bootstrap so the test needs no remote RPyC install or package download.
        server = DeployedCrossPlatformServer(
            remote,
            python_executable=[executable, "-u"],
            server_script="import sys\nprint(12345, flush=True)\nsys.stdin.read()\n",
        )
        assert server._tmpdir_ctx is not None
        created = server._tmpdir_ctx.path
        try:
            self.assertEqual(server.remote_port, 12345)
        finally:
            server.close()
        self.assertEqual(
            remote.python_run(
                "import os; print(os.path.exists(" + repr(created) + "))"
            ),
            "False",
        )

    @classmethod
    def setUpClass(cls):
        cls.available = available_shells()
        required = set(
            filter(None, os.environ.get("RRPC_TEST_REQUIRED_SHELLS", "").split(","))
        )
        missing = required - {
            name for name, present in cls.available.items() if present
        }
        if missing:
            raise RuntimeError(
                "Required test shells unavailable: " + ", ".join(sorted(missing))
            )

    def run_shell(self, kind, expected):
        if not self.available[kind]:
            self.skipTest(
                f"{kind} unavailable"
                + (" (requires WSL Ubuntu-22.04 on Windows)" if kind == "linux" else "")
            )
        self.check_shell(kind, expected)

    def test_windows_git_bash(self):
        self.run_shell("gitbash", "bash")

    def test_windows_powershell(self):
        self.run_shell("powershell", "powershell")

    def test_windows_cmd(self):
        self.run_shell("cmd", "cmd")

    def test_linux_bash(self):
        self.run_shell("linux", "bash")


if __name__ == "__main__":
    unittest.main()
