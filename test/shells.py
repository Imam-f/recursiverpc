"""Local subprocess adapters used to exercise SSH command generation without SSH."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from plumbum.machines.local import PlumbumLocalPopen

from recursiverpc.ssh import RemoteShell

WSL_DISTRO = "Ubuntu-22.04"
GIT_BASH = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/bin/bash.exe"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def available_shells():
    linux = bool(shutil.which("bash")) if os.name != "nt" else False
    if os.name == "nt" and shutil.which("wsl"):
        try:
            result = subprocess.run(
                ["wsl", "--list", "--quiet"], capture_output=True, timeout=10
            )
            linux = result.returncode == 0 and WSL_DISTRO in result.stdout.decode(
                "utf-16-le", errors="replace"
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "linux": linux,
        "gitbash": os.name == "nt" and GIT_BASH.is_file(),
        "powershell": os.name == "nt" and bool(POWERSHELL),
        "cmd": os.name == "nt",
    }


class LocalShell(RemoteShell):
    def __init__(self, kind):
        self.kind = kind
        self.env = os.environ.copy()
        self.env["PATH"] = (
            str(Path(sys.executable).parent) + os.pathsep + self.env["PATH"]
        )
        super().__init__(object())
        # Use the direct socket path; these tests do not open RPC sockets.
        self.client = object()

    def _popen_shell(self, command):
        if self.kind == "gitbash":
            args = [str(GIT_BASH), "-c", command]
        elif self.kind == "linux":
            args = (
                ["wsl", "-d", WSL_DISTRO, "--exec", "/bin/bash", "-c", command]
                if os.name == "nt"
                else ["bash", "-c", command]
            )
        elif self.kind == "powershell":
            args = [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            # /s strips this outer pair, leaving the quoted executable intact.
            args = 'cmd.exe /d /s /c "' + command + '"'
        return PlumbumLocalPopen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
