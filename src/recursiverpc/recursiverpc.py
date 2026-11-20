from __future__ import annotations

from typing import Tuple
from typing import Generator, TypeVar, Callable, Any, Dict, Generic
import os
import random
import sys
import tempfile
import time
import subprocess
import re
from dataclasses import dataclass

from pathlib import Path
from uv import find_uv_bin

from concurrent.futures import Future
from threading import Thread, Event, RLock

import rpyc
from rpyc import BgServingThread
from rpyc.utils.zerodeploy import DeployedServer

from plumbum import SshMachine
from plumbum.commands import CommandNotFound, ProcessExecutionError
from plumbum.commands.base import BoundCommand
from plumbum.machines.paramiko_machine import ParamikoMachine
from .ssh import SSHConnection, RemoteShell, shell_aware
from .dependencies import DependencyInput, DependencySpec, LocalProject, parse_dependency

old_print = print

##################################################################

SERVER_SCRIPT = r"""\
import logging

# logging.basicConfig(
#     level=logging.INFO,  
#     filename="app.log",
#     filemode="a"
# )
# logger = logging.getLogger(__name__)
    
if __name__ == "__main__":
    import sys
    import os
    import atexit
    import shutil
    import time
    
    here = os.path.dirname(__file__)
    os.chdir(here)

    def rmdir():
        shutil.rmtree(here, ignore_errors = True)
    atexit.register(rmdir)

    try:
        for dirpath, _, filenames in os.walk(here):
            for fn in filenames:
                if fn == "__pycache__" or (fn.endswith(".pyc") and os.path.exists(fn[:-1])):
                    os.remove(os.path.join(dirpath, fn))
    except Exception:
        pass

    sys.path.insert(0, here)
    from $SERVER_MODULE$ import $SERVER_CLASS$ as ServerCls
    from $SERVICE_MODULE$ import $SERVICE_CLASS$ as ServiceCls

    logger = None

    $EXTRA_SETUP$
    
    t = ServerCls(ServiceCls, hostname = "localhost", port = 0, reuse_addr = True, logger = logger)
    # ServiceCls.server = t
    thd = t._start_in_thread()

    sys.stdout.write(f"{t.port}\n")
    sys.stdout.flush()

    def watch_shutdown():
        # Some SSH/uv launchers expose EOF immediately. EOF is not evidence
        # that the RPC connection has ended, so only an explicit token stops us.
        # Read the fd directly: a daemon must not hold stdin's buffered IO lock
        # when OneShotServer exits after its client disconnects.
        pending = b""
        token = b"__recursiverpc_shutdown__\n"
        try:
            while True:
                chunk = os.read(sys.stdin.fileno(), 1024)
                if not chunk:
                    return
                pending += chunk
                if token in pending:
                    t.close()
                    return
                pending = pending[-len(token):]
        except (OSError, ValueError):
            return

    import threading
    threading.Thread(target=watch_shutdown, daemon=True).start()
    try:
        # Returning from main would begin Python's threading/futures shutdown
        # even if worker or SSH threads keep the process alive.
        thd.join()
    finally:
        t.close()
        thd.join(2)
"""


@dataclass
class tupleprocess:
    number: int


@dataclass
class localprocess(tupleprocess):
    number: int


@dataclass
class networkprocess(tupleprocess):
    number: int
    connection: SshMachine | ParamikoMachine | SSHConnection | RemoteShell | tuple[str, int]


@dataclass
class proxyprocess(tupleprocess):
    number: int
    host: str | None
    port: int | None
    client: list[localprocess | networkprocess | "proxyprocess"]
    ssh_login: Tuple
    ssh_remote: Dict[str, Tuple]


################################################################

T = TypeVar("T")
U = TypeVar("U")


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


class Recursive_RPC:
    """
    This class is a scheduler and load balancer
    support local process and ssh
    """

    def __init__(
        self,
        client: list[localprocess | networkprocess | proxyprocess],
        conn: Dict[str, Tuple] | None = None,
        dependency: DependencyInput | DependencySpec | None = None,
    ):
        self.dependency = parse_dependency(dependency).prepared() if dependency is not None else None
        self.client: list[localprocess | networkprocess | proxyprocess] = client
        self.connection: list[None | Runner] = [None for i in range(len(client))]
        self.weight: list[int] = [0 for i in range(len(self.connection))]
        try:
            for index, val in enumerate(client):
                runner = _make_runner(val, self.dependency)
                self.weight[index] = runner.process_num
                self.connection[index] = runner
        except BaseException:
            try:
                self.__exit__(None, None, None)
            except Exception:
                pass
            raise
        if not any(self.connection):
            raise RuntimeError("No runner")
        # print(len(self.connection))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        errors = []
        for connection in self.connection:
            if connection is not None:
                try:
                    connection.close()
                except Exception as error:
                    errors.append(error)
        self.connection.clear()
        if errors:
            raise errors[0]

    def schedule(self) -> "Runner":
        # start_time = time.time()
        # memo = {}
        # def get_status(i):
        #     if memo.get(i):
        #         return memo.get(i)
        #     is_active, p_num, n_done_count, latency = i.status()
        #     memo[i] = p_num - n_done_count, latency
        #     return p_num - n_done_count, latency

        # print(f"->: {time.time() - start_time:f} seconds")

        # return CustomList(self.connection).filter(
        #             lambda x: x is not None
        #         ).sort(
        #             key=lambda x: get_status(x)[0], reverse=True
        #         ).take(1)[0]
        # rand = random.randint(0, len(self.connection) - 1)
        # return self.connection[rand]
        rand = random.choices(self.connection, weights=self.weight, k=1)[0]
        if not rand:
            raise RuntimeError("No runner")
        return rand
        # return self.connection[0]
        # return self.connection[1]

    def apply(self, func: Callable[[Any], T], /, *args, **kwargs) -> T:
        # Apply then wait
        runner: Runner = self.schedule()
        if not runner:
            raise RuntimeError("No runner")
        result: RPC_Future = runner.run(func, *args, **kwargs)
        return result.get()

    def apply_async(self, func, /, *args, **kwargs) -> "RPC_Future":
        # Apply then give future object
        runner: Runner = self.schedule()
        if not runner:
            raise RuntimeError("No runner")
        return runner.run(func, *args, **kwargs)

    def map_ordered(self, iters, func, /, *args, **kwargs):
        future_list: list["RPC_Future"] = []
        for i in iters:
            runner: Runner = self.schedule()
            if not runner:
                raise RuntimeError("No runner")
            r = runner.run(func, *[i, *args], **kwargs)
            future_list.append(r)
        return [f.get() for f in future_list]

    def map_ordered_async(self, iters, func, /, *args, **kwargs) -> list["RPC_Future"]:
        return self.map_async(iters, func, *args, **kwargs)

    def map(self, iters, func, /, *args, **kwargs):
        future_list: list["RPC_Future"] = []
        for i in iters:
            runner: Runner = self.schedule()
            if not runner:
                raise RuntimeError("No runner")
            r = runner.run(func, *[i, *args], **kwargs)
            future_list.append(r)
        return [f for f in RPC_Future.as_completed(future_list)]

    def map_async(self, iters, func, /, *args, **kwargs) -> list["RPC_Future"]:
        future_list: list["RPC_Future"] = []
        for i in iters:
            runner: Runner = self.schedule()
            if not runner:
                raise RuntimeError("No runner")
            r = runner.run(func, *[i, *args], **kwargs)
            future_list.append(r)
        return future_list

    @staticmethod
    def as_completed(cls_act: list["RPC_Future"]) -> Generator[Any, None, None]:
        cls = cls_act.copy()
        while any(cls):
            for i, v in enumerate(cls):
                if v and v.status():
                    yield v.get()
                    cls.remove(v)


class RPC_Future(Generic[T]):
    """
    This class is a placeholder of future value
    """

    def __init__(self, result: Future, scheduler: "Runner"):
        self.result = result
        self.scheduler = scheduler

    def check_scheduler(self):
        return self.scheduler.status()

    def get(self):
        while not self.result.done():
            time.sleep(0.1)
        return self.result.result()

    def status(self):
        # self.scheduler.conn.ping()
        return self.result.done()

    @staticmethod
    def as_completed(cls_act: list["RPC_Future[T]"]) -> Generator[T, None, None]:
        cls = cls_act.copy()
        while any(cls):
            for i, v in enumerate(cls):
                if v and v.status():
                    yield v.get()
                    cls.remove(v)


################################################################


class Runner:
    """
    This class is concrete runner
    """

    def __init__(self):
        pass

    def run(self, func, /, *args, **kwargs): 
        raise NotImplementedError

    def close(self): ...

    def status(self):
        raise NotImplementedError


class ProcessRunner(Runner):
    def __init__(self, num: int, printer=print, dependency: DependencySpec | None = None):
        print("ProcessRunner", num)
        printer("ProcessRunner", num)

        self.pool: "Pool" = Pool(num, dependency=dependency)
        self.process_num = self.pool.max_workers
        self.process_handle: list[RPC_Future] = []

    def run(self, func, /, *args, **kwargs) -> RPC_Future:
        result = self.pool.apply_async(func, args, kwargs)
        self.process_handle.append(RPC_Future(result, self))
        return self.process_handle[-1]

    def close(self):
        self.pool.close()

    def status(self) -> tuple[bool, int, int, int]:
        for i, v in enumerate(self.process_handle):
            if self.process_handle[i].status():
                self.process_handle.remove(v)

        is_pool_active: bool = not not self.pool
        process_num: int = self.process_num
        not_done_count: int = len(self.process_handle)

        # Connnection, max capacity, used capacity, latency
        return (is_pool_active, process_num, not_done_count, 0)


class NetworkRunner(Runner):
    def __init__(self, num: int, con: SshMachine | ParamikoMachine | SSHConnection | RemoteShell | tuple[str, int], dependency: DependencySpec | None = None):
        self.process_num = num

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
                        self.machine, server_script=SERVER_SCRIPT[1:],
                        server_class="rpyc.utils.server.OneShotServer", dependency=dependency,
                    )
                else:
                    # Get uv binary path
                    self.uv_bin = os.fsdecode(find_uv_bin())
                    print(self.uv_bin)
                    if not self._check_uv_available():
                        raise RuntimeError("uv not installed")

                    # Generate requirements and script header
                    header = "#!/usr/bin/env uv run --script\n"
                    curdir = os.getcwd()

                    print(curdir)
                    with tempfile.TemporaryDirectory() as temp_dir:
                        temp_dir = Path(temp_dir)
                        # Generate requirements.txt using uv
                        os.chdir(temp_dir)
                        self._generate_requirements()

                        # Create util.py and add dependencies
                        Path(temp_dir / "util.py").touch()
                        self._add_script_dependencies()
                        os.chdir(curdir)

                        # Read and process the generated script
                        with open(temp_dir / "util.py", "r") as f:
                            lines = f.read()
                            # Fix recursiverpc path to use current environment
                            # TODO: this should be replaced
                            # use dist or just copy the project
                            newlines = self._fix_recursiverpc_path(lines)
                            header += newlines
                            # header += lines

                    server_script_user = header + "\n" + SERVER_SCRIPT[1:]
                    extra_setup = ""
                    print(server_script_user)

                    major = sys.version_info[0]
                    minor = sys.version_info[1]

                    # Use uv run with script mode
                    self.uv_bin = os.path.relpath(os.fsdecode(find_uv_bin()), os.getcwd())
                    # TODO: fix this, shell name is leaking
                    executable = [
                        "uv",
                        "run",
                        "-q",
                        "--python",
                        f"{major}.{minor}",
                        "--script",
                    ]
                    # executable = ["uv.exe", "run", "-q", "--python", f"{major}.{minor}", "--script"]
                    self.server = DeployedCrossPlatformServer(
                        self.machine,
                        server_script=server_script_user,
                        extra_setup=extra_setup,
                        server_class="rpyc.utils.server.OneShotServer",
                        python_executable=executable,
                    )

                self.conn = self.server.classic_connect()
            case (hostname, port):
                if dependency is not None:
                    raise ValueError("dependency requires a managed worker, not an existing RPC server")
                self.conn = rpyc.classic.connect(hostname, port=port)
            case _:
                raise RuntimeError("Invalid connection")

        self.bg_event_loop = BgServingThread(self.conn)
        Pool = self.conn.modules.recursiverpc.Pool
        self.conn.modules.sys.stdout = sys.stdout
        self.conn.modules.sys.stderr = sys.stderr
        self.conn.namespace["pool"] = Pool(self.process_num)
        self.pool = self.conn.namespace["pool"]
        # self.conn.root.close()
        # self.conn.root.close()

        self.process_handle: list[RPC_Future] = []

    def _check_uv_available(self) -> bool:
        result = subprocess.run(
            [self.uv_bin, "--version"], capture_output=True, text=True
        )
        return result.returncode == 0

    def _generate_requirements(self):
        result = subprocess.run(
            [self.uv_bin, "pip", "freeze"], capture_output=True, text=True, check=True
        )
        with open("requirements.txt", "w") as f:
            f.write(result.stdout)

    def _add_script_dependencies(self):
        """Add dependencies to script using uv"""
        print(
            subprocess.run(
                [
                    self.uv_bin,
                    "add",
                    "-q",
                    "-r",
                    "requirements.txt",
                    "--script",
                    "util.py",
                ],
                # check=True, capture_output=True
                capture_output=True,
            )
        )

    def _fix_recursiverpc_path(self, content: str) -> str:
        """Fix recursiverpc path to use current environment's installation"""
        recursiverpc_path = self._get_recursiverpc_path()
        if not recursiverpc_path:
            return content

        pattern = r"recursiverpc = \{[^}]*\}"
        escaped_path = recursiverpc_path.replace(
            "\\", "/"
        )  # TODO: this is windows specific line
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
            print("Warning: Could not import recursiverpc to determine path")
            return ""

    def _get_uv_executable(self, major: int, minor: int):
        """Get uv executable command for remote execution"""
        return [self.uv_bin, "run", "-q", "--python", f"{major}.{minor}", "--script"]

    def run(self, func, /, *args, **kwargs) -> RPC_Future:
        if self.conn is None:
            raise RuntimeError("No connection")

        # self.conn.ping()
        self.conn.teleport(func)
        self.conn.namespace["args"] = args
        self.conn.namespace["kwargs"] = kwargs
        self.conn.namespace["result"] = self.pool.apply_async(
            func, self.conn.namespace["args"], self.conn.namespace["kwargs"]
        )
        result = self.conn.namespace["result"]
        self.process_handle.append(RPC_Future(result, self))
        # print(result.done())
        return self.process_handle[-1]

    def close(self):
        print("cleaning")
        try:
            if self.conn is not None:
                self.pool.close()
        except Exception as e:
            print("abc", e)
        try:
            if self.conn is not None:
                self.bg_event_loop.stop()
                # print("-1 Cleaned up")
                # self.conn.root.close()
                # print(self.conn.root.server)
                # self.conn.root.server.close()
                # print("0 Cleaned up")
                # self.bg_event_loop.stop()
                self.conn.close()
                print("1 Cleaned up")
            self.conn = None
        except Exception as e:
            print("asdf", e)
        try:
            # time.sleep(10)
            if self.server is not None:
                self.server.close()
            self.server = None
        except Exception as e:
            print("b", e)
        try:
            if self.machine is not None:
                self.machine.close()
            self.machine = None
        except Exception as e:
            print("c", e)
        print("allesclar")

    def __del__(self):
        self.close()

    def status(self) -> tuple[bool, int, int, int]:
        for i, v in enumerate(self.process_handle):
            if self.process_handle[i].status():
                self.process_handle.remove(v)

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
        self.host = host
        self.port = port
        self.num = num
        self.ssh_login = ssh_login
        self.process_num = 0
        self.weight: list[int] = []
        self.connection: list[Runner] = []
        self.process_handle: list[RPC_Future] = []
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
                # Reuse the existing deployment, dependency setup, forwarding,
                # and connection cleanup instead of the removed activate_ssh API.
                self._gateway = NetworkRunner(1, SSHConnection(
                    host, user=user, port=port if port is not None else 22,
                    password=password,
                ), **({} if dependency is None else {"dependency": dependency}))
                self.conn = self._gateway.conn
                if self.conn is None:
                    raise RuntimeError("Gateway did not establish an RPC connection")
                remote = self.conn.modules.recursiverpc
                specs = [self._copy_spec(remote, spec) for spec in clientlist]
                options = {}
                if dependency is not None:
                    remote_dependency = self.conn.modules["recursiverpc.dependencies"].DependencySpec(*dependency.payload())
                    options["dependency"] = remote_dependency
                self._remote_proxy = remote.ProxyRunner(num, None, None, specs, **options)
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
                        connection.host, user=connection.user, port=connection.port,
                        password=connection.password, key_filename=connection.key_filename,
                        shell=connection.shell, timeout=connection.timeout,
                    )
                elif not (isinstance(connection, tuple) and len(connection) == 2):
                    raise TypeError(
                        "Gateway network workers require SSHConnection or an RPC (host, port) tuple"
                    )
                return remote.networkprocess(number, connection)
            case proxyprocess(number, host, port, children, login, remotes):
                return remote.proxyprocess(
                    number, host, port,
                    [cls._copy_spec(remote, child) for child in children], login, remotes,
                )
            case _:
                raise TypeError("Invalid runner descriptor")

    def schedule(self):
        if not self.connection:
            raise RuntimeError("No runner")
        return random.choices(self.connection, weights=self.weight, k=1)[0]

    def run(self, func, /, *args, **kwargs):
        if self._remote_proxy is not None:
            # Use the supplied function; the old path hard-coded worker_func.
            if self.conn is None:
                raise RuntimeError("Gateway RPC connection is closed")
            remote_func = self.conn.teleport(func)
            result = self._remote_proxy.run(remote_func, *args, **kwargs)
        else:
            result = self.schedule().run(func, *args, **kwargs)
        self.process_handle.append(result)
        return result

    def close(self):
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
                    resource.close()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]

    def status(self) -> tuple[bool, int, int, int]:
        self.process_handle = [f for f in self.process_handle if not f.status()]
        active = self._remote_proxy is not None or bool(self.connection)
        return active, self.process_num, len(self.process_handle), 0


#################################################################


class Pool:
    def __init__(self, max_workers: int, offset: int = 0, dependency: DependencySpec | None = None):
        import multiprocessing

        self.max_workers = (
            max_workers if max_workers > 0 else multiprocessing.cpu_count()
        )
        self.scheduler = []

        self._project = None
        try:
            if dependency is not None:
                self._project = LocalProject(dependency)
            for _ in range(self.max_workers):
                conn = (self._project.connect() if self._project is not None
                        else rpyc.classic.connect_multiprocess())
                self.scheduler.append(conn)
                conn.modules.sys.stdout = sys.stdout
                conn.modules.sys.stderr = sys.stderr
        except BaseException:
            self.close()
            raise

        self.scheduler_index = 0
        self.scheduler_status: list[bool] = [False] * self.max_workers
        self._scheduler_lock = RLock()
        self._pending_tasks = [0] * self.max_workers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _schedule(self):
        with self._scheduler_lock:
            start = self.scheduler_index
            count = len(self.scheduler_status)
            index = next(
                ((start + offset) % count for offset in range(count)
                 if not self.scheduler_status[(start + offset) % count]),
                (start + 1) % count,
            )
            self.scheduler_index = index
            self._pending_tasks[index] += 1
            self.scheduler_status[index] = True
            return self.scheduler[index]

    def _release_worker(self, index):
        with self._scheduler_lock:
            self._pending_tasks[index] -= 1
            self.scheduler_status[index] = self._pending_tasks[index] > 0

    def apply_async(self, func, args, kwargs):
        with self._scheduler_lock:
            runner = self._schedule()
            scheduler_index = self.scheduler_index
        result_future = Future()

        try:
            function = runner.teleport(func)
        except BaseException:
            self._release_worker(scheduler_index)
            raise

        def function_async(*args, **kwargs):
            try:
                if not result_future.set_running_or_notify_cancel():
                    return
                try:
                    result = function(*args, **kwargs)
                except BaseException as exc:
                    result_future.set_exception(exc)
                else:
                    result_future.set_result(result)
            finally:
                self._release_worker(scheduler_index)

        try:
            thread = Thread(target=function_async, args=args, kwargs=kwargs)
            thread.start()
        except BaseException:
            self._release_worker(scheduler_index)
            raise

        return result_future

    def close(self):
        errors = []
        for connection in self.scheduler:
            try:
                connection.close()
            except Exception as error:
                errors.append(error)
        self.scheduler.clear()
        if self._project is not None:
            try:
                self._project.close()
            except Exception as error:
                errors.append(error)
            finally:
                self._project = None
        if errors:
            raise errors[0]

    def __del__(self):
        self.close()


#################################################################


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
    ) -> None:
        self.proc = None
        self.tun = None
        self.remote_machine = remote_machine
        self._tmpdir_ctx = None
        remote_machine = shell_aware(remote_machine)
        self.remote_machine = remote_machine

        self._tmpdir_ctx = CrossPlatformTempDir(remote_machine)
        tmp = self._tmpdir_ctx.__enter__()
        try:
            if dependency is not None:
                dependency.install_remote(remote_machine, tmp)
                environment = tmp.rstrip('/\\') + '/.venv'
                cmd = remote_machine.command("uv").with_env(
                    UV_PROJECT_ENVIRONMENT=environment, VIRTUAL_ENV=None,
                )["run", "--project", tmp, "--no-sync", "--no-dev", "python", "-u"]
            else:
                # print(python_executable)
                if isinstance(python_executable, (list, tuple)):
                    cmd = remote_machine.command(python_executable[0])
                    for part in python_executable[1:]:
                        cmd = cmd[part]
                elif isinstance(python_executable, BoundCommand):
                    arguments = []
                    while isinstance(python_executable, BoundCommand):
                        arguments = list(python_executable.args) + arguments
                        python_executable = python_executable.cmd
                    cmd = remote_machine.command(str(python_executable.executable))[tuple(arguments)]
                elif python_executable:
                    cmd = remote_machine.command(python_executable)
                else:
                    cmd = remote_machine.command("uv")[
                        "run", "-q", "--", "python"
                    ]

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

            # Write server script
            script_path = self._write_server_script(remote_machine, tmp, server_script)

            self.proc = cmd.popen(script_path, new_session=True)
            self._handle_server_startup()

            # if hasattr(remote_machine, "connect_sock"):
            if remote_machine.client is not None:
                self.local_port = None
            else:
                import socket
                from contextlib import closing
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                with closing(s):
                    s.bind(("localhost", 0))
                    self.local_port = s.getsockname()[1]
                self.tun = remote_machine.machine.tunnel(self.local_port, self.remote_port)
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
                self.proc.stdin.write(b"__recursiverpc_shutdown__\n")
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
        """Handle server startup and output monitoring"""
        line = ""
        save_forward = Event()

        try:
            if self.proc is None:
                raise Exception("stdout is None")
            line = self.proc.stdout.readline()
            self.remote_port = int(line.strip())
            print("remote port", self.remote_port)
            save_forward.set()

            def output_monitor():
                save_forward.wait()
                while True:
                    try:
                        if self.proc is None:
                            raise Exception("stdout is None")
                        line = self.proc.stdout.read(1)
                        if line in (b"", ""):
                            break
                        else:
                            print(">", line, "<", sep="", end="")
                    except Exception as e:
                        print(e)
                        break

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

            BYTES_LITERAL: Callable
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
