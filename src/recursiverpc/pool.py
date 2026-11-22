"""RPyC process connections and asynchronous task dispatch."""

from __future__ import annotations

import sys
from concurrent.futures import Future
from threading import RLock, Thread, Condition, current_thread
from collections import deque

import rpyc

from .dependencies import DependencySpec, LocalProject


class Pool:
    def __init__(
        self,
        max_workers: int,
        offset: int = 0,
        dependency: DependencySpec | None = None,
        max_pending: int | None = None,
    ):
        import multiprocessing

        self.max_workers = (
            max_workers if max_workers > 0 else multiprocessing.cpu_count()
        )
        self._project = None
        self.scheduler = []
        self._cleanup_lock = RLock()
        self._scheduler_lock = RLock()
        self._condition = Condition(self._scheduler_lock)
        self._closed = False
        self._threads = []
        self._tasks = deque()
        self._outstanding = 0
        self.max_pending = self.max_workers * 2 if max_pending is None else max_pending
        if self.max_pending < 0:
            raise ValueError("max_pending must be nonnegative")

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
            try:
                self._close_resources()
            except Exception:
                pass
            raise

        self.scheduler_status: list[bool] = [False] * self.max_workers
        try:
            for index in range(self.max_workers):
                thread = Thread(target=self._worker, args=(index,), daemon=True)
                self._threads.append(thread)
                thread.start()
        except BaseException:
            self._threads = [thread for thread in self._threads if thread.ident is not None]
            self.close(cancel_pending=True)
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def apply_async(self, func, args, kwargs):
        with self._condition:
            while not self._closed and self._outstanding >= self.max_workers + self.max_pending:
                self._condition.wait()
            if self._closed:
                raise RuntimeError("Pool is closed")
            future = Future()
            self._tasks.append((future, func, args, kwargs))
            self._outstanding += 1
            self._condition.notify_all()
            return future

    def _worker(self, index):
        while True:
            with self._condition:
                while not self._tasks and not self._closed:
                    self._condition.wait()
                if not self._tasks:
                    return
                future, func, args, kwargs = self._tasks.popleft()
                self.scheduler_status[index] = True
            value = None
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        value = self.scheduler[index].teleport(func)(*args, **kwargs)
                    except BaseException as error:
                        future.set_exception(error)
                    else:
                        future.set_result(value)
            finally:
                with self._condition:
                    self.scheduler_status[index] = False
                    self._outstanding -= 1
                    self._condition.notify_all()
                del future, func, args, kwargs, value

    def close(self, wait=True, cancel_pending=False):
        with self._condition:
            first_close = not self._closed
            self._closed = True
            if cancel_pending:
                while self._tasks:
                    future, *_ = self._tasks.popleft()
                    future.cancel()
                    self._outstanding -= 1
            self._condition.notify_all()
        def finish():
            for thread in self._threads:
                if thread is not current_thread():
                    thread.join()
            self._close_resources()
        if wait:
            finish()
        elif first_close:
            Thread(target=finish, daemon=True).start()

    def _close_resources(self):
        with self._cleanup_lock:
            self._cleanup_resources()

    def _cleanup_resources(self):
        errors = []
        with self._scheduler_lock:
            connections, self.scheduler = self.scheduler, []
        for connection in connections:
            try:
                connection.close()
            except Exception as error:
                errors.append(error)
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
        # Explicit close/context management is required for active pools.
        if not getattr(self, "_threads", []):
            self._close_resources()
