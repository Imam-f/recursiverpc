"""Public scheduler for mixed local, SSH, and recursive proxy workers."""

from __future__ import annotations

import os
import random
from pathlib import Path
from threading import Condition
from collections import deque
from concurrent.futures import wait, FIRST_COMPLETED
from typing import Any, Callable, Dict, Generator, Tuple, TypeVar

from .config import localprocess, networkprocess, proxyprocess
from .dependencies import DependencyInput, DependencySpec, parse_dependency
from .futures import RPC_Future
from .runners import Runner, _make_runner
from .setup import SetupScript

T = TypeVar("T")


class Recursive_RPC:
    """Schedule Python calls across local, SSH, and recursive proxy workers."""

    def __init__(
        self,
        client: list[localprocess | networkprocess | proxyprocess],
        conn: Dict[str, Tuple] | None = None,
        dependency: DependencyInput | DependencySpec | None = None,
        *,
        max_pending: int | None = None,
        copy_local_dependencies: bool = False,
        copy_project: bool | str | os.PathLike[str] = False,
        setup_script: SetupScript | str | os.PathLike[str] | None = None,
    ):
        if max_pending is not None and max_pending < 0:
            raise ValueError("max_pending must be nonnegative")
        self._condition = Condition()
        self._closed = False
        self._outstanding = 0
        if dependency is None and (
            copy_local_dependencies or copy_project or setup_script is not None
        ):
            source = (
                Path(copy_project) if not isinstance(copy_project, bool) else Path.cwd()
            )
            project = source / "pyproject.toml"
            dependency = project if project.is_file() else []
        parsed = parse_dependency(dependency) if dependency is not None else None
        if parsed is not None and (
            copy_local_dependencies or copy_project or setup_script is not None
        ):
            parsed = parsed.configured(
                copy_local_dependencies=copy_local_dependencies,
                copy_project=copy_project,
                setup_script=setup_script,
            )
        self.dependency = parsed.prepared() if parsed is not None else None
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
        self.max_pending = sum(self.weight) * 2 if max_pending is None else max_pending
        if not any(self.connection):
            raise RuntimeError("No runner")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self, wait=True, cancel_pending=False):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        errors = []
        for connection in self.connection:
            if connection is not None:
                try:
                    connection.close(wait=wait, cancel_pending=cancel_pending)
                except Exception as error:
                    errors.append(error)
        if errors:
            raise errors[0]

    def schedule(self) -> "Runner":

        if self._closed:
            raise RuntimeError("Scheduler is closed")
        rand = random.choices(self.connection, weights=self.weight, k=1)[0]
        if not rand:
            raise RuntimeError("No runner")
        return rand

    def apply(self, func: Callable[[Any], T], /, *args, **kwargs) -> T:
        return self.apply_async(func, *args, **kwargs).get()

    def apply_async(self, func, /, *args, **kwargs) -> "RPC_Future":
        with self._condition:
            while not self._closed and self._outstanding >= sum(self.weight) + self.max_pending:
                self._condition.wait()
            if self._closed:
                raise RuntimeError("Scheduler is closed")
            self._outstanding += 1
        def released(_):
            with self._condition:
                self._outstanding -= 1
                self._condition.notify_all()
        try:
            handle = self.schedule().run(func, *args, **kwargs)
        except BaseException:
            released(None)
            raise
        handle.result.add_done_callback(released)
        return handle

    def imap(self, iters, func, /, *args, **kwargs):
        """Yield completion-order results with a bounded input window."""
        yield from self._imap(iters, func, args, kwargs, ordered=False)

    def imap_ordered(self, iters, func, /, *args, **kwargs):
        """Yield input-order results with a bounded input window."""
        yield from self._imap(iters, func, args, kwargs, ordered=True)

    def _imap(self, iters, func, args, kwargs, ordered):
        iterator = iter(iters)
        pending = deque()
        exhausted = False
        window = sum(self.weight) + self.max_pending
        while pending or not exhausted:
            while not exhausted and len(pending) < window:
                try:
                    item = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                pending.append(self.apply_async(func, item, *args, **kwargs))
            if not pending:
                break
            if ordered:
                handle = pending.popleft()
            else:
                ready, _ = wait([h.result for h in pending], return_when=FIRST_COMPLETED)
                handle = next(h for h in pending if h.result in ready)
                pending.remove(handle)
            yield handle.get()

    def map_ordered(self, iters, func, /, *args, **kwargs):
        return list(self.imap_ordered(iters, func, *args, **kwargs))

    def map_ordered_async(self, iters, func, /, *args, **kwargs) -> list["RPC_Future"]:
        return self.map_async(iters, func, *args, **kwargs)

    def map(self, iters, func, /, *args, **kwargs):
        return list(self.imap(iters, func, *args, **kwargs))

    def map_async(self, iters, func, /, *args, **kwargs) -> list["RPC_Future"]:
        # Compatibility API retains handles; imap variants also bound result storage.
        return [self.apply_async(func, item, *args, **kwargs) for item in iters]

    @staticmethod
    def as_completed(cls_act: list["RPC_Future"], timeout=None) -> Generator[Any, None, None]:
        yield from RPC_Future.as_completed(cls_act, timeout=timeout)
