"""Notification-driven local and remote result handles."""
from __future__ import annotations
from concurrent.futures import Future, as_completed
from rpyc.core.netref import BaseNetref
from typing import Generic, TypeVar
T = TypeVar("T")

class RPC_Future(Generic[T]):
    def __init__(self, result, scheduler):
        self.scheduler = scheduler
        if isinstance(result, Future) and not isinstance(result, BaseNetref):
            self.result = result
        else:
            # Remote Future callbacks are delivered through RPyC's serving loop.
            self.result = Future()
            def completed(remote):
                try:
                    if remote.cancelled():
                        self.result.cancel()
                        return
                    value = remote.result()
                except BaseException as error:
                    self.result.set_exception(error)
                else:
                    self.result.set_result(value)
            result.add_done_callback(completed)

    def check_scheduler(self):
        return self.scheduler.status()

    def get(self, timeout=None):
        return self.result.result(timeout=timeout)

    def status(self):
        return self.result.done()

    @staticmethod
    def as_completed(handles, timeout=None):
        for result in as_completed([handle.result for handle in handles], timeout=timeout):
            yield result.result()
