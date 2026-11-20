"""Remote bootstrap startup, output monitoring, and temporary resources."""

from __future__ import annotations

import logging
import socket
import time
from contextlib import closing
from threading import Thread

from plumbum import SshMachine
from plumbum.commands import ProcessExecutionError
from plumbum.commands.base import BoundCommand
from plumbum.machines.paramiko_machine import ParamikoMachine
from rpyc.utils.zerodeploy import DeployedServer

from .bootstrap import SERVER_SCRIPT, SHUTDOWN_TOKEN
from .dependencies import DependencySpec
from .ssh import RemoteShell, shell_aware
from .venv import current_environment_bundle, try_copy_environment

log = logging.getLogger(__name__)


class DeployedCrossPlatformServer(DeployedServer):
    def __init__(
        self,
        remote_machine: SshMachine | ParamikoMachine | RemoteShell,
        server_class="rpyc.utils.server.ThreadedServer",
        service_class="rpyc.core.service.SlaveService",
        server_script=SERVER_SCRIPT,
        extra_setup="",
        python_executable=None,
        dependency: DependencySpec | None = None,
        copy_venv: bool = False,
    ) -> None:
        self.proc = None
        self.tun = None
        self.remote_machine = remote_machine
        self._tmpdir_ctx = None
        self.environment_setup = None
        remote_machine = shell_aware(remote_machine)
        self.remote_machine = remote_machine

        self._tmpdir_ctx = CrossPlatformTempDir(remote_machine)
        tmp = self._tmpdir_ctx.__enter__()
        try:
            if dependency is not None:
                copied = dependency.install_remote(remote_machine, tmp)
                if copied is not None:
                    self.environment_setup = "copied"
                    environment = tmp.rstrip("/\\") + "/.venv"
                    cmd = remote_machine.command(copied).with_env(
                        VIRTUAL_ENV=environment
                    )["-u"]
                else:
                    self.environment_setup = "installed"
                    environment = tmp.rstrip("/\\") + "/.venv"
                    cmd = remote_machine.command("uv").with_env(
                        UV_PROJECT_ENVIRONMENT=environment,
                        VIRTUAL_ENV=None,
                    )["run", "--project", tmp, "--no-sync", "--no-dev", "python", "-u"]
                if dependency.workdir is not None:
                    workdir = tmp.rstrip("/\\") + "/" + dependency.workdir
                    cmd = cmd.with_env(RECURSIVERPC_WORKDIR=workdir).with_cwd(workdir)
            else:
                copied = (
                    try_copy_environment(
                        remote_machine, tmp, current_environment_bundle
                    )
                    if copy_venv
                    else None
                )
                self.environment_setup = "installed" if copy_venv else None
                if copied is not None:
                    self.environment_setup = "copied"
                    environment = tmp.rstrip("/\\") + "/.venv"
                    cmd = remote_machine.command(copied).with_env(
                        VIRTUAL_ENV=environment
                    )["-u"]
                elif isinstance(python_executable, (list, tuple)):
                    cmd = remote_machine.command(python_executable[0])
                    for part in python_executable[1:]:
                        cmd = cmd[part]
                elif isinstance(python_executable, BoundCommand):
                    arguments = []
                    while isinstance(python_executable, BoundCommand):
                        arguments = list(python_executable.args) + arguments
                        python_executable = python_executable.cmd
                    cmd = remote_machine.command(str(python_executable.executable))[
                        tuple(arguments)
                    ]
                elif python_executable:
                    cmd = remote_machine.command(python_executable)
                else:
                    cmd = remote_machine.command("uv")["run", "-q", "--", "python"]

            server_modname, server_clsname = server_class.rsplit(".", 1)
            service_modname, service_clsname = service_class.rsplit(".", 1)

            for source, target in (
                ("$SERVER_MODULE$", server_modname),
                ("$SERVER_CLASS$", server_clsname),
                ("$SERVICE_MODULE$", service_modname),
                ("$SERVICE_CLASS$", service_clsname),
                ("$EXTRA_SETUP$", extra_setup),
            ):
                server_script = server_script.replace(source, target)

            script_path = self._write_server_script(remote_machine, tmp, server_script)

            self.proc = cmd.popen(script_path, new_session=True)
            self._handle_server_startup()

            if remote_machine.client is not None:
                self.local_port = None
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                with closing(s):
                    s.bind(("localhost", 0))
                    self.local_port = s.getsockname()[1]
                self.tun = remote_machine.machine.tunnel(
                    self.local_port, self.remote_port
                )
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _write_server_script(self, remote_machine, tmp_dir, server_script):
        remote = shell_aware(remote_machine)
        return remote.write_script(tmp_dir, server_script)

    def close(self, timeout: int | None = 10):
        if self.proc is not None:
            try:
                self.proc.stdin.write(SHUTDOWN_TOKEN)
                self.proc.stdin.flush()
            except (OSError, ValueError):
                pass
            try:
                self.proc.terminate()
                self.proc.communicate(timeout=timeout)
            except Exception:
                self.proc.kill()
            finally:
                self.proc = None
        if self.tun is not None:
            self.tun.close()
            self.tun = None
        # Cleanup must happen while the SSH connection is still open.
        if self._tmpdir_ctx is not None:
            self._tmpdir_ctx.__exit__(None, None, None)
            self._tmpdir_ctx = None

    def _handle_server_startup(self):
        """Read the protocol port, then log complete bootstrap output lines."""
        line = ""

        try:
            if self.proc is None:
                raise Exception("stdout is None")
            line = self.proc.stdout.readline()
            self.remote_port = int(line.strip())
            log.debug("Remote RPC server listening on port %s", self.remote_port)
            stream = self.proc.stdout

            def output_monitor():
                pending = bytearray()
                try:
                    while True:
                        chunk = stream.read(1)
                        if chunk in (b"", ""):
                            break
                        pending.extend(
                            chunk.encode() if isinstance(chunk, str) else chunk
                        )
                        if pending.endswith(b"\n"):
                            log.debug(
                                "Remote bootstrap: %s",
                                pending.decode(errors="replace").rstrip(),
                            )
                            pending.clear()
                except Exception:
                    log.debug("Remote output monitor stopped", exc_info=True)
                finally:
                    if pending:
                        log.debug(
                            "Remote bootstrap: %s",
                            pending.decode(errors="replace").rstrip(),
                        )

            thread = Thread(target=output_monitor, daemon=True)
            thread.start()

        except Exception:
            if self.proc:
                try:
                    self.proc.terminate()
                except Exception:
                    pass

            stdout, stderr = self.proc.communicate() if self.proc else (b"", b"")
            from rpyc.lib.compat import BYTES_LITERAL

            raise ProcessExecutionError(
                self.proc.argv if self.proc else [],
                self.proc.returncode if self.proc else -1,
                BYTES_LITERAL(line) + stdout,
                stderr,
            )


class CrossPlatformTempDir:
    """Create and clean an owned directory using remote Python on any shell."""

    def __init__(self, remote_machine):
        self.remote_machine = remote_machine
        self.path = None

    def __enter__(self):
        if not isinstance(self.remote_machine, RemoteShell):
            self.remote_machine = shell_aware(self.remote_machine)
        self.path = self.remote_machine.make_temp_dir()
        return self.path

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.path is None:
            return
        for attempt in range(3):
            try:
                self.remote_machine.remove_temp_dir(self.path)
                self.path = None
                return
            except (ProcessExecutionError, OSError):
                if attempt == 2:
                    raise
                time.sleep(1)
