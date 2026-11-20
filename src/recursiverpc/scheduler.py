"""Public scheduler for mixed local, SSH, and recursive proxy workers."""

from __future__ import annotations

import os
import random
from pathlib import Path
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
        copy_local_dependencies: bool = False,
        copy_project: bool | str | os.PathLike[str] = False,
        setup_script: SetupScript | str | os.PathLike[str] | None = None,
    ):
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
        if not any(self.connection):
            raise RuntimeError("No runner")

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

        rand = random.choices(self.connection, weights=self.weight, k=1)[0]
        if not rand:
            raise RuntimeError("No runner")
        return rand

    def apply(self, func: Callable[[Any], T], /, *args, **kwargs) -> T:
        runner: Runner = self.schedule()
        if not runner:
            raise RuntimeError("No runner")
        result: RPC_Future = runner.run(func, *args, **kwargs)
        return result.get()

    def apply_async(self, func, /, *args, **kwargs) -> "RPC_Future":
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
        yield from RPC_Future.as_completed(cls_act)
