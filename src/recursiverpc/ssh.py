"""SSH execution without assuming the server's login shell is POSIX."""

from dataclasses import dataclass
import json
import re
import shlex
import subprocess
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import paramiko
from plumbum.commands import CommandNotFound, ProcessExecutionError
from plumbum.commands.base import BaseCommand, BoundCommand
from plumbum.machines.remote import RemoteCommand
from plumbum.machines.paramiko_machine import ParamikoPopen


def shell_command(argv, shell):
    """Quote individual native-process arguments for the selected login shell."""
    argv = [str(arg) for arg in argv]
    if shell == "bash":
        return shlex.join(argv)
    if shell == "powershell":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv) + "; exit $LASTEXITCODE"
    if shell == "cmd":
        # Percent expansion occurs even inside double quotes. Our Python
        # operations use hex payloads; reject unsafe arbitrary command arguments.
        if any(any(c in arg for c in '%!\r\n') for arg in argv):
            raise ValueError("cmd arguments cannot contain %, !, or newlines")
        parts = [subprocess.list2cmdline([arg]) for arg in argv]
        return " ".join(part if part.startswith('"') else '"' + part + '"' for part in parts)
    raise ValueError(f"Unsupported SSH shell: {shell}")


@dataclass
class SSHConnection:
    """SSH configuration for any supported login shell (including native Windows).

    Host keys must be in known_hosts, unless a different policy is supplied.
    """

    host: str
    user: str | None = None
    port: int = 22
    password: str | None = None
    key_filename: str | None = None
    shell: str = "auto"
    timeout: float = 15
    missing_host_policy: object = None

    def connect(self):
        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if self.missing_host_policy is not None:
            client.set_missing_host_key_policy(self.missing_host_policy)
        try:
            client.connect(self.host, port=self.port, username=self.user,
                           password=self.password, key_filename=self.key_filename,
                           timeout=self.timeout, auth_timeout=self.timeout,
                           banner_timeout=self.timeout)
            return shell_aware(client, shell=self.shell, timeout=self.timeout)
        except BaseException:
            client.close()
            raise


class SSHProcess(ParamikoPopen):
    """Plumbum's Paramiko process with binary input and graceful server shutdown.

    The upstream communicate() has no input/timeout arguments and reads streams
    serially, so retain the small override needed for script uploads.
    """

    def __init__(self, client, command):
        stdin, stdout, stderr = client.exec_command(command)
        super().__init__([command], stdin, stdout, stderr, "utf-8")

    def terminate(self):
        # The bootstrap server waits on stdin. EOF requests graceful shutdown.
        if self.channel.closed or self.channel.exit_status_ready():
            return
        self.stdin.flush()
        self.channel.shutdown_write()

    def kill(self):
        self.channel.close()

    def communicate(self, input: bytes | None = None, timeout: float | None = None):
        deadline = None if timeout is None else time.monotonic() + timeout
        def remaining():
            return None if deadline is None else max(0, deadline - time.monotonic())
        def send():
            if input is not None:
                if hasattr(input, "read"):
                    shutil.copyfileobj(input, self.stdin, length=256 * 1024)
                else:
                    self.stdin.write(input)
            self.terminate()
        executor = ThreadPoolExecutor(max_workers=4)
        try:
            stdout = executor.submit(self.stdout.read)
            stderr = executor.submit(self.stderr.read)
            writer = executor.submit(send)
            writer.result(remaining())
            result = stdout.result(remaining()), stderr.result(remaining())
            executor.submit(self.wait).result(remaining())
            return result
        except FutureTimeout:
            self.kill()
            raise subprocess.TimeoutExpired(self.argv, timeout) from None
        except BaseException:
            self.kill()
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


class ShellAwareMachine:
    """Connection-scoped adapter returning ordinary Plumbum remote commands.

    Raw SSH transport bypasses Plumbum's POSIX initialization and cd/env prefix;
    command binding, execution, return-code checking, and process helpers remain
    Plumbum's. No upstream classes or other connections are monkey patched.
    """

    custom_encoding = "utf-8"

    def __init__(self, machine, shell="auto", timeout=15):
        self.machine = machine
        self.timeout = timeout
        self.client = machine if isinstance(machine, paramiko.SSHClient) else getattr(machine, "_client", None)
        self.shell = shell
        if shell == "auto":
            probes = [
                ("powershell", "[Console]::Write('__rrpc_powershell__')", "__rrpc_powershell__"),
                ("bash", 'if [ -n "$BASH_VERSION" ]; then printf __rrpc_bash__; fi', "__rrpc_bash__"),
                ("cmd", "echo %COMSPEC%", None),
            ]
            for candidate, command, marker in probes:
                code, output, _ = self.run(command)
                if code == 0 and ((marker and output.strip() == marker) or
                                  (candidate == "cmd" and output.strip().lower().endswith("cmd.exe"))):
                    self.shell = candidate
                    break
            else:
                raise RuntimeError("Could not detect SSH shell; specify bash, powershell, or cmd")
        if self.shell not in ("bash", "powershell", "cmd"):
            raise ValueError(f"Unsupported SSH shell: {self.shell}")
        self.python: list[str] = []
        for argv in (["python3"], ["python"], ["py", "-3"],
                     ["uv", "run", "--no-project", "python"]):
            probe = "import sys;print(731029*(sys.version_info>=(3,10)))"
            code, output, _ = self.run(shell_command(argv + ["-c", probe], self.shell))
            if code == 0 and output.strip() == "731029":
                self.python = argv
                break
        if not self.python:
            raise RuntimeError("Remote Python 3.10+ is required (python3, python, py -3, or uv)")
        self.platform, executable = json.loads(self.python_run(
            "import os,sys,json; print(json.dumps([os.name,sys.executable]))"
        ))
        if self.shell == "bash" and re.match(r"^[A-Za-z]:", executable):
            executable = executable.replace("\\", "/")
        self.python = [executable]

    def _popen_shell(self, command):
        if self.client is not None:
            return SSHProcess(self.client, command)
        # Bypass Plumbum's POSIX cd/env prefix and argument quoting.
        return self.machine._ssh_command[self.machine._fqhost, command].popen()

    def popen(self, command, *, new_session=False, cwd=None, env=None):
        if isinstance(command, BaseCommand):
            # Unwrap binding before formulation so Plumbum does not apply its
            # POSIX quoting. The selected shell quotes the resulting argv once.
            arguments = []
            while isinstance(command, BoundCommand):
                arguments = command.args + arguments
                command = command.cmd
            argv = command.formulate(args=arguments)
            if self.platform == "nt" or cwd is not None or env:
                # Windows PowerShell 5 and cmd cannot faithfully forward every
                # quoted native argument. Relay argv through Python's subprocess
                # API without another shell parse. The child inherits stdio.
                script = "import os,subprocess,sys; "
                if cwd is not None:
                    script += "os.chdir(" + repr(str(cwd)) + "); "
                for name, value in (env or {}).items():
                    if value is None:
                        script += "os.environ.pop(" + repr(name) + ", None); "
                    else:
                        script += "os.environ[" + repr(name) + "]=" + repr(str(value)) + "; "
                script += "sys.exit(subprocess.call(" + repr(argv) + "))"
                command = self._python_command(script)
            else:
                command = shell_command(argv, self.shell)
        elif cwd is not None or env:
            raise TypeError("cwd/env require a Plumbum command, not raw shell text")
        proc = self._popen_shell(command)
        proc.custom_encoding = self.custom_encoding
        proc.argv = [command]
        return proc

    def run(self, command, input=None):
        proc = self.popen(command)
        stdout, stderr = proc.communicate(input=input, timeout=self.timeout)
        def text(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        return proc.returncode, text(stdout), text(stderr)

    def python_run(self, script, input=None):
        command = self._python_command(script)
        code, stdout, stderr = self.run(command, input=input)
        if code:
            raise ProcessExecutionError(self.python, code, stdout, stderr)
        return stdout.strip()

    def _python_command(self, script):
        # No user text, paths, or script contents are interpolated into shell syntax.
        encoded = script.encode("utf-8").hex()
        expression = "exec(bytes.fromhex('" + encoded + "').decode('utf-8'))"
        return shell_command(self.python + ["-c", expression], self.shell)

    def which(self, executable):
        path = json.loads(self.python_run(
            "import json,shutil; print(json.dumps(shutil.which(" + repr(executable) + ")))"
        ))
        if path is None:
            raise CommandNotFound(executable, [])
        if self.shell == "bash" and re.match(r"^[A-Za-z]:", path):
            path = path.replace("\\", "/")
        return path

    def command(self, executable):
        return RemoteCommand(self, self.which(str(executable)))

    __getitem__ = command

    def make_temp_dir(self):
        return json.loads(self.python_run(
            "import json,tempfile; print(json.dumps(tempfile.mkdtemp(prefix='recursiverpc-')))"
        ))

    def write_script(self, directory, content):
        return self.write_file(directory, 'server.py', content.encode('utf-8'))

    def write_file(self, directory, name, content: bytes):
        if not name or '/' in name or '\\' in name or name in ('.', '..'):
            raise ValueError('Expected a file name within the temporary project')
        return json.loads(self.python_run(
            "import json,pathlib,sys; p=pathlib.Path(" + repr(directory) + ")/" + repr(name) + "; "
            "p.write_bytes(sys.stdin.buffer.read()); print(json.dumps(str(p)))",
            input=content,
        ))

    def write_artifact(self, directory, name, artifact):
        """Reuse a verified content-addressed archive, uploading bounded chunks."""
        import uuid
        if not name or '/' in name or '\\' in name or name in ('.', '..'):
            raise ValueError('Expected an artifact file name')
        digest = artifact.digest()
        if not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid artifact digest')
        prelude = (
            "import pathlib,tempfile,hashlib,shutil,os\n"
            "cache=pathlib.Path(tempfile.gettempdir())/'recursiverpc-artifacts'\n"
            "cache.mkdir(exist_ok=True)\n"
            "cached=cache/" + repr(digest) + "\n"
            "target=pathlib.Path(" + repr(directory) + ")/" + repr(name) + "\n"
            "def checksum(path):\n"
            "    value=hashlib.sha256()\n"
            "    with path.open('rb') as stream:\n"
            "        for block in iter(lambda:stream.read(1048576),b''): value.update(block)\n"
            "    return value.hexdigest()\n"
        )
        hit = self.python_run(prelude +
            "try:\n"
            "    if cached.is_file() and checksum(cached)==" + repr(digest) + ":\n"
            "        shutil.copyfile(cached,target)\n        os.utime(cached,None)\n        print('HIT')\n"
            "except OSError: pass")
        if hit == 'HIT':
            return str(directory).rstrip('/\\') + '/' + name
        temporary = digest + '-' + uuid.uuid4().hex + '.part'
        temporary_code = "partial=cache/" + repr(temporary) + "\n"
        try:
            self.python_run(prelude + temporary_code + "partial.touch(exist_ok=False)")
            offset = 0
            while True:
                block = artifact.read(offset, 1024 * 1024)
                if not block:
                    break
                self.python_run(prelude + temporary_code +
                    "import sys\nwith partial.open('ab') as output: shutil.copyfileobj(sys.stdin.buffer,output,1048576)", input=block)
                offset += len(block)
            self.python_run(prelude + temporary_code +
                "assert checksum(partial)==" + repr(digest) + ", 'Artifact checksum mismatch'\n"
                "shutil.copyfile(partial,target)\nos.replace(partial,cached)\n"
                "entries=[]\n"
                "for p in cache.iterdir():\n"
                "    try:\n        if len(p.name)==64: entries.append((p.stat().st_mtime,p))\n"
                "    except OSError: pass\n"
                "for _,old in sorted(entries,reverse=True)[4:]:\n"
                "    try: old.unlink()\n    except OSError: pass")
        finally:
            self.python_run(prelude + temporary_code + "partial.unlink(missing_ok=True)")
        return str(directory).rstrip('/\\') + '/' + name

    def remove_temp_dir(self, directory):
        self.python_run(
            "import pathlib,shutil; p=pathlib.Path(" + repr(directory) + ").resolve(); "
            "assert p.name.startswith('recursiverpc-'), 'Unexpected temporary path'; "
            "shutil.rmtree(p) if p.exists() else None"
        )

    def connect_sock(self, port):
        if self.client is None:
            raise RuntimeError("Direct socket forwarding requires a Paramiko connection")
        transport = self.client.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("SSH transport is not connected")
        return transport.open_channel(
            "direct-tcpip", ("localhost", port), ("localhost", 0)
        )

    def close(self):
        self.machine.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def shell_aware(machine, shell="auto", timeout=15):
    """Adapt one existing SSH machine/client; leave other connections unchanged.

    Use SSHConnection for native Windows shells, before Plumbum's POSIX probes
    could run. Reapplying this function to an adapter returns that adapter.
    """
    if isinstance(machine, ShellAwareMachine):
        if shell not in ("auto", machine.shell):
            raise ValueError("Connection is already adapted for a different shell")
        return machine
    return ShellAwareMachine(machine, shell=shell, timeout=timeout)


# Preserve the name used by the initial implementation and downstream callers.
RemoteShell = ShellAwareMachine
