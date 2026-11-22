"""Local, SSH, and proxy runner implementations."""

from __future__ import annotations

import logging
import weakref
import os
import random
import re
import subprocess
import sys
import tempfile
from threading import RLock
from pathlib import Path
from typing import Dict, Tuple

import rpyc
from plumbum import SshMachine
from plumbum.machines.paramiko_machine import ParamikoMachine
from rpyc import BgServingThread
from uv import find_uv_bin

from .bootstrap import SERVER_SCRIPT
from .config import localprocess, networkprocess, proxyprocess
from .dependencies import DependencySpec
from .deployment import DeployedCrossPlatformServer
from .futures import RPC_Future
from .pool import Pool
from .ssh import RemoteShell, SSHConnection

log = logging.getLogger(__name__)


def _make_runner(spec, dependency: DependencySpec | None = None):
    options = {} if dependency is None else {"dependency": dependency}
    match spec:
        case localprocess(number):
            return ProcessRunner(number, **options)
        case networkprocess(number, connection):
            return NetworkRunner(number, connection, **options)
        case proxyprocess(number, host, port, clients, login, remotes):
            return ProxyRunner(number, host, port, clients, login, remotes, **options)
        case _:
            raise TypeError("Invalid runner descriptor")


class Runner:
    """Common interface for task dispatch, capacity reporting, and cleanup."""

    def run(self, func, /, *args, **kwargs):
        raise NotImplementedError

    def close(self, wait=True, cancel_pending=False): ...

    def _track(self, handle):
        with self._handles_lock:
            self.process_handle.append(handle)
        owner_ref, handle_ref = weakref.ref(self), weakref.ref(handle)
        def completed(_):
            owner, finished = owner_ref(), handle_ref()
            if owner is not None and finished is not None:
                with owner._handles_lock:
                    if finished in owner.process_handle:
                        owner.process_handle.remove(finished)
        handle.result.add_done_callback(completed)
        return handle

    def status(self):
        raise NotImplementedError


class ProcessRunner(Runner):
    def __init__(
        self, num: int, printer=None, dependency: DependencySpec | None = None
    ):
        log.debug("Starting local runner with %s processes", num)
        if printer is not None:
            printer("ProcessRunner", num)

        self.pool: "Pool" = Pool(num, dependency=dependency)
        self.process_num = self.pool.max_workers
        self.process_handle: list[RPC_Future] = []
        self._handles_lock = RLock()

    def run(self, func, /, *args, **kwargs) -> RPC_Future:
        result = self.pool.apply_async(func, args, kwargs)
        return self._track(RPC_Future(result, self))

    def close(self, wait=True, cancel_pending=False):
        self.pool.close(wait=wait, cancel_pending=cancel_pending)

    def status(self) -> tuple[bool, int, int, int]:
        with self._handles_lock:
            self.process_handle[:] = [f for f in self.process_handle if not f.status()]

        is_pool_active: bool = not getattr(self.pool, "_closed", False)
        process_num: int = self.process_num
        not_done_count: int = len(self.process_handle)

        # Connnection, max capacity, used capacity, latency
        return (is_pool_active, process_num, not_done_count, 0)


class NetworkRunner(Runner):
    def __init__(
        self,
        num: int,
        con: SshMachine
        | ParamikoMachine
        | SSHConnection
        | RemoteShell
        | tuple[str, int],
        dependency: DependencySpec | None = None,
    ):
        self.process_num = num

        self._closed = False
        self.server = None
        self.conn = None
        self.machine = None
        if isinstance(con, SSHConnection):
            con = con.connect()
        match con:
            case SshMachine() | ParamikoMachine() | RemoteShell():
                self.machine = con

                if dependency is not None:
                    self.server = DeployedCrossPlatformServer(
                        self.machine,
                        server_script=SERVER_SCRIPT[1:],
                        server_class="rpyc.utils.server.OneShotServer",
                        dependency=dependency,
                    )
                else:
                    # Get uv binary path
                    self.uv_bin = os.fsdecode(find_uv_bin())
                    log.debug("Preparing remote worker using %s", self.uv_bin)
                    if not self._check_uv_available():
                        raise RuntimeError("uv not installed")

                    # Generate requirements and script header
                    header = "#!/usr/bin/env uv run --script\n"

                    with tempfile.TemporaryDirectory() as temp_dir:
                        temp_dir = Path(temp_dir)
                        # Generate requirements.txt using uv
                        self._generate_requirements(temp_dir)

                        # Create util.py and add dependencies
                        Path(temp_dir / "util.py").touch()
                        self._add_script_dependencies(temp_dir)

                        # Read and process the generated script
                        with open(temp_dir / "util.py", "r") as f:
                            lines = f.read()
                            # Fix recursiverpc path to use current environment
                            newlines = self._fix_recursiverpc_path(lines)
                            header += newlines

                    server_script_user = header + "\n" + SERVER_SCRIPT[1:]
                    extra_setup = ""

                    major = sys.version_info[0]
                    minor = sys.version_info[1]

                    # Use uv run with script mode
                    executable = [
                        "uv",
                        "run",
                        "-q",
                        "--python",
                        f"{major}.{minor}",
                        "--script",
                    ]
                    self.server = DeployedCrossPlatformServer(
                        self.machine,
                        server_script=server_script_user,
                        extra_setup=extra_setup,
                        server_class="rpyc.utils.server.OneShotServer",
                        python_executable=executable,
                        copy_venv=True,
                    )

                self.conn = self.server.classic_connect()
            case (hostname, port):
                if dependency is not None:
                    raise ValueError(
                        "dependency requires a managed worker, not an existing RPC server"
                    )
                self.conn = rpyc.classic.connect(hostname, port=port)
            case _:
                raise RuntimeError("Invalid connection")

        self.bg_event_loop = BgServingThread(self.conn)
        Pool = self.conn.modules.recursiverpc.Pool
        self.conn.modules.sys.stdout = sys.stdout
        self.conn.modules.sys.stderr = sys.stderr
        self.conn.namespace["pool"] = Pool(self.process_num)
        self.pool = self.conn.namespace["pool"]

        self.process_handle: list[RPC_Future] = []
        self._handles_lock = RLock()

    def _check_uv_available(self) -> bool:
        result = subprocess.run(
            [self.uv_bin, "--version"], capture_output=True, text=True
        )
        return result.returncode == 0

    def _generate_requirements(self, directory):
        result = subprocess.run(
            [self.uv_bin, "pip", "freeze"], capture_output=True, text=True, check=True
        )
        with open(Path(directory) / "requirements.txt", "w") as f:
            f.write(result.stdout)

    def _add_script_dependencies(self, directory):
        """Add dependencies to script using uv"""
        result = subprocess.run(
            [
                self.uv_bin,
                "add",
                "-q",
                "-r",
                "requirements.txt",
                "--script",
                "util.py",
            ],
            capture_output=True, cwd=directory, check=True,
        )
        log.debug("uv script dependency setup returned %s", result.returncode)

    def _fix_recursiverpc_path(self, content: str) -> str:
        """Fix recursiverpc path to use current environment's installation"""
        recursiverpc_path = self._get_recursiverpc_path()
        if not recursiverpc_path:
            return content

        pattern = r"recursiverpc = \{[^}]*\}"
        escaped_path = recursiverpc_path.replace("\\", "/")
        replacement = f'recursiverpc = {{ path = "{escaped_path}", editable = true }}'

        # Replace the pattern
        return re.sub(pattern, replacement, content)

    def _get_recursiverpc_path(self) -> str:
        """Get the path to recursiverpc project root using __file__"""
        try:
            import recursiverpc

            # Get the module file path
            module_file = Path(recursiverpc.__file__)
            current = module_file.parent  # src/recursiverpc

            # Go up until we find pyproject.toml
            while current.parent != current:
                if (current / "pyproject.toml").exists():
                    return str(current)
                current = current.parent

            # If no pyproject.toml found, assume it's one level up from src
            if module_file.parent.parent.name == "src":
                project_root = module_file.parent.parent.parent
                if (project_root / "pyproject.toml").exists():
                    return str(project_root)

            # Fallback: return the parent of the module directory
            return str(module_file.parent.parent)
        except ImportError:
            log.warning("Could not import recursiverpc to determine its project path")
            return ""

    def run(self, func, /, *args, **kwargs) -> RPC_Future:
        if self._closed or self.conn is None:
            raise RuntimeError("Network runner is closed")

        function = self.conn.teleport(func)
        result = self.pool.apply_async(function, args, kwargs)
        return self._track(RPC_Future(result, self))

    def close(self, wait=True, cancel_pending=False):
        self._closed = True
        if not wait:
            from threading import Thread
            Thread(target=self.close, kwargs={"cancel_pending": cancel_pending}, daemon=True).start()
            return
        with self.__dict__.setdefault('_close_lock', RLock()):
            self._close_resources(wait, cancel_pending)

    def _close_resources(self, wait, cancel_pending):
        log.debug("Closing network runner")
        try:
            if self.conn is not None:
                self.pool.close(wait=wait, cancel_pending=cancel_pending)
        except Exception:
            log.debug("Failed to close remote worker pool", exc_info=True)
        try:
            if self.conn is not None:
                self.bg_event_loop.stop()
                self.conn.close()
            self.conn = None
        except Exception:
            log.debug("Failed to close RPC connection", exc_info=True)
        try:
            if self.server is not None:
                self.server.close()
            self.server = None
        except Exception:
            log.debug("Failed to close deployed server", exc_info=True)
        try:
            if self.machine is not None:
                self.machine.close()
            self.machine = None
        except Exception:
            log.debug("Failed to close SSH connection", exc_info=True)

    def __del__(self):
        self.close()

    def status(self) -> tuple[bool, int, int, int]:
        with self._handles_lock:
            self.process_handle[:] = [f for f in self.process_handle if not f.status()]

        is_pool_active: bool = not not self.conn
        process_num: int = self.process_num
        not_done_count: int = len(self.process_handle)

        # Connnection, max capacity, used capacity, latency
        return (is_pool_active, process_num, not_done_count, 0)


class ProxyRunner(Runner):
    """Group runners locally, or bootstrap the same group on an SSH gateway."""

    def __init__(
        self,
        num: int,
        host: str | None,
        port: int | None,
        clientlist: list[localprocess | networkprocess | proxyprocess],
        ssh_login: Tuple | None = None,
        ssh_remote: Dict[str, Tuple] | None = None,
        dependency: DependencySpec | None = None,
    ):
        self._closed = False
        self.host = host
        self.port = port
        self.num = num
        self.ssh_login = ssh_login
        self.process_num = 0
        self.weight: list[int] = []
        self.connection: list[Runner] = []
        self.process_handle: list[RPC_Future] = []
        self._handles_lock = RLock()
        self.conn = None
        self._gateway = None
        self._remote_proxy = None
        try:
            if host is None:
                for spec in clientlist:
                    runner = _make_runner(spec, dependency)
                    self.connection.append(runner)
                    self.weight.append(runner.process_num)
                    self.process_num += runner.process_num
                if not self.connection:
                    raise RuntimeError("No runner")
            else:
                user, password = ssh_login if ssh_login is not None else (None, None)
                # Gateway workers share ordinary SSH deployment and cleanup.
                self._gateway = NetworkRunner(
                    1,
                    SSHConnection(
                        host,
                        user=user,
                        port=port if port is not None else 22,
                        password=password,
                    ),
                    **({} if dependency is None else {"dependency": dependency}),
                )
                self.conn = self._gateway.conn
                if self.conn is None:
                    raise RuntimeError("Gateway did not establish an RPC connection")
                remote = self.conn.modules.recursiverpc
                specs = [self._copy_spec(remote, spec) for spec in clientlist]
                options = {}
                if dependency is not None:
                    remote_dependency = self.conn.modules[
                        "recursiverpc.dependencies"
                    ].DependencySpec(*dependency.payload())
                    options["dependency"] = remote_dependency
                self._remote_proxy = remote.ProxyRunner(
                    num, None, None, specs, **options
                )
                self.process_num = self._remote_proxy.process_num
        except BaseException:
            try:
                self.close()
            except Exception:
                pass
            raise

    @classmethod
    def _copy_spec(cls, remote, spec):
        """Build native descriptors on the gateway, rather than local netrefs."""
        match spec:
            case localprocess(number):
                return remote.localprocess(number)
            case networkprocess(number, connection):
                if isinstance(connection, SSHConnection):
                    policy = connection.missing_host_policy
                    if policy is not None:
                        raise TypeError(
                            "Gateway SSHConnection must use the gateway's known_hosts policy"
                        )
                    connection = remote.SSHConnection(
                        connection.host,
                        user=connection.user,
                        port=connection.port,
                        password=connection.password,
                        key_filename=connection.key_filename,
                        shell=connection.shell,
                        timeout=connection.timeout,
                    )
                elif not (isinstance(connection, tuple) and len(connection) == 2):
                    raise TypeError(
                        "Gateway network workers require SSHConnection or an RPC (host, port) tuple"
                    )
                return remote.networkprocess(number, connection)
            case proxyprocess(number, host, port, children, login, remotes):
                return remote.proxyprocess(
                    number,
                    host,
                    port,
                    [cls._copy_spec(remote, child) for child in children],
                    login,
                    remotes,
                )
            case _:
                raise TypeError("Invalid runner descriptor")

    def schedule(self):
        if getattr(self, "_closed", False):
            raise RuntimeError("Proxy runner is closed")
        if not self.connection:
            raise RuntimeError("No runner")
        return random.choices(self.connection, weights=self.weight, k=1)[0]

    def run(self, func, /, *args, **kwargs):
        if getattr(self, "_closed", False):
            raise RuntimeError("Proxy runner is closed")
        if self._remote_proxy is not None:
            if self.conn is None:
                raise RuntimeError("Gateway RPC connection is closed")
            remote_func = self.conn.teleport(func)
            result = self._remote_proxy.run(remote_func, *args, **kwargs)
        else:
            result = self.schedule().run(func, *args, **kwargs)
        if isinstance(result, RPC_Future) and not isinstance(result, rpyc.core.netref.BaseNetref):
            return self._track(result)
        return self._track(RPC_Future(result.result, self))

    def close(self, wait=True, cancel_pending=False):
        self._closed = True
        if not wait:
            from threading import Thread
            Thread(target=self.close, kwargs={"cancel_pending": cancel_pending}, daemon=True).start()
            return
        with self.__dict__.setdefault('_close_lock', RLock()):
            self._close_resources(wait, cancel_pending)

    def _close_resources(self, wait, cancel_pending):
        errors = []
        resources = [*self.connection, self._remote_proxy, self._gateway]
        self.connection.clear()
        self.weight.clear()
        self.process_handle.clear()
        self._remote_proxy = None
        self._gateway = None
        self.conn = None
        for resource in resources:
            if resource is not None:
                try:
                    resource.close(wait=wait, cancel_pending=cancel_pending)
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]

    def status(self) -> tuple[bool, int, int, int]:
        with self._handles_lock:
            self.process_handle[:] = [f for f in self.process_handle if not f.status()]
        active = self._remote_proxy is not None or bool(self.connection)
        return active, self.process_num, len(self.process_handle), 0
