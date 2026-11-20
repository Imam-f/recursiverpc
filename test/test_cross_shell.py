"""Exercise the actual local shells; Linux Bash runs in WSL on Windows."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from recursiverpc.ssh import RemoteShell, shell_command
from recursiverpc.recursiverpc import DeployedCrossPlatformServer


class LocalShell(RemoteShell):
    def __init__(self, kind):
        self.kind = kind
        self.env = os.environ.copy()
        self.env['PATH'] = str(Path(sys.executable).parent) + os.pathsep + self.env['PATH']
        super().__init__(object())
        # Use the direct socket path; these tests do not open RPC sockets.
        self.client = object()

    def popen(self, command):
        if self.kind == 'gitbash':
            args = ['C:/Program Files/Git/bin/bash.exe', '-c', command]
        elif self.kind == 'linux':
            args = (['wsl', '-d', 'Ubuntu-22.04', '--exec', '/bin/bash', '-c', command]
                    if os.name == 'nt' else ['bash', '-c', command])
        elif self.kind == 'powershell':
            args = ['powershell', '-NoProfile', '-NonInteractive', '-Command', command]
        else:
            # /s strips this outer pair, leaving the quoted executable intact.
            args = 'cmd.exe /d /s /c "' + command + '"'
        return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=self.env)


class CrossShellTests(unittest.TestCase):
    def check_shell(self, kind, expected):
        remote = LocalShell(kind)
        self.assertEqual(remote.shell, expected)
        directory = remote.make_temp_dir()
        try:
            # This exceeds cmd's command-line limit if embedded in the command.
            content = "# Unicode: café 雪\nprint(\"quotes: ' \\\" & | % ! $ `\")\n" + "# padding\n" * 2000
            path = remote.write_script(directory, content)
            readback = remote.python_run(
                'import pathlib,json; print(json.dumps(pathlib.Path(' + repr(path) + ').read_bytes().hex()))'
            )
            self.assertEqual(bytes.fromhex(json.loads(readback)), content.encode('utf-8'))
            executable = json.loads(remote.python_run('import sys,json; print(json.dumps(sys.executable))'))
            command = remote.command(executable)
            self.assertEqual(command('-c', 'print(42)').strip(), '42')
        finally:
            remote.remove_temp_dir(directory)
        self.assertEqual(remote.python_run('import os; print(os.path.exists(' + repr(directory) + '))'), 'False')

        # Full deployment lifecycle through actual shell launch, with a tiny
        # bootstrap so the test needs no remote RPyC install or package download.
        server = DeployedCrossPlatformServer(
            remote, python_executable=[executable, '-u'],
            server_script='import sys\nprint(12345, flush=True)\nsys.stdin.read()\n',
        )
        created = server._tmpdir_ctx.path
        try:
            self.assertEqual(server.remote_port, 12345)
        finally:
            server.close()
        self.assertEqual(remote.python_run('import os; print(os.path.exists(' + repr(created) + '))'), 'False')

    @unittest.skipUnless(os.name == 'nt' and Path('C:/Program Files/Git/bin/bash.exe').exists(), 'Git Bash unavailable')
    def test_windows_git_bash(self):
        self.check_shell('gitbash', 'bash')

    @unittest.skipUnless(os.name == 'nt' and shutil.which('powershell'), 'PowerShell unavailable')
    def test_windows_powershell(self):
        self.check_shell('powershell', 'powershell')

    @unittest.skipUnless(os.name == 'nt', 'Windows cmd unavailable')
    def test_windows_cmd(self):
        self.check_shell('cmd', 'cmd')

    @unittest.skipUnless(os.name != 'nt' or shutil.which('wsl'), 'Linux Bash unavailable')
    def test_linux_bash(self):
        self.check_shell('linux', 'bash')


if __name__ == '__main__':
    unittest.main()
