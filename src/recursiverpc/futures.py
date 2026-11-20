"""Result handles shared by local and remote runners."""

from __future__ import annotations

import time
from concurrent.futures import Future
from typing import TYPE_CHECKING, Generator, Generic, TypeVar

if TYPE_CHECKING:
    from .runners import Runner

T = TypeVar("T")


class RPC_Future(Generic[T]):
    """Result handle with a reference to the runner that submitted the task."""

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
        return self.result.done()

    @staticmethod
    def as_completed(cls_act: list["RPC_Future[T]"]) -> Generator[T, None, None]:
        cls = cls_act.copy()
        while any(cls):
            for i, v in enumerate(cls):
                if v and v.status():
                    yield v.get()
                    cls.remove(v)
