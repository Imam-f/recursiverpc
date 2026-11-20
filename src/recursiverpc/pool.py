"""RPyC process connections and asynchronous task dispatch."""

from __future__ import annotations

import sys
from concurrent.futures import Future
from threading import RLock, Thread

import rpyc

from .dependencies import DependencySpec, LocalProject


class Pool:
    def __init__(
        self,
        max_workers: int,
        offset: int = 0,
        dependency: DependencySpec | None = None,
    ):
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
                conn = (
                    self._project.connect()
                    if self._project is not None
                    else rpyc.classic.connect_multiprocess()
                )
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
                (
                    (start + offset) % count
                    for offset in range(count)
                    if not self.scheduler_status[(start + offset) % count]
                ),
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
