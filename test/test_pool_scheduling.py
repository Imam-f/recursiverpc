"""Pool regression tests using real threads and simulated RPC connections."""

import contextlib
import importlib
import io
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

rpc = importlib.import_module("recursiverpc.recursiverpc")


class PoolSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.threads = []
        self.gates = []
        self.pools = []

        def make_thread(*args, **kwargs):
            thread = threading.Thread(*args, **kwargs)
            self.threads.append(thread)
            return thread

        def connection():
            return SimpleNamespace(
                modules=SimpleNamespace(sys=SimpleNamespace()),
                teleport=lambda func: func,
                close=lambda: None,
            )

        self.thread_patch = patch.object(rpc, "Thread", make_thread)
        self.connect_patch = patch.object(
            rpc.rpyc.classic, "connect_multiprocess", side_effect=connection
        )
        self.thread_patch.start()
        self.connect_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.addCleanup(self.connect_patch.stop)

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for thread in self.threads:
            thread.join(timeout=2)
        for pool in self.pools:
            pool.close()
        self.assertFalse(any(thread.is_alive() for thread in self.threads))

    def make_pool(self, workers):
        pool = rpc.Pool(workers)
        self.pools.append(pool)
        return pool

    def submit_blocked(self, pool):
        gate = threading.Event()
        started = threading.Event()
        self.gates.append(gate)

        def task():
            started.set()
            if not gate.wait(timeout=5):
                raise TimeoutError("test did not release task")
            return "done"

        future = pool.apply_async(task, (), {})
        self.assertTrue(started.wait(timeout=1), "worker did not start")
        return future, gate, self.threads[-1]

    def test_result_preserves_args_and_kwargs(self):
        pool = self.make_pool(1)
        future = pool.apply_async(lambda x, scale: x * scale, (3,), {"scale": 4})
        self.assertEqual(future.result(timeout=1), 12)
        self.threads[-1].join(timeout=1)
        self.assertEqual(pool.scheduler_status, [False])

    def test_task_exception_completes_future_and_releases_worker(self):
        pool = self.make_pool(1)

        def task():
            raise ValueError("worker failed")

        future = pool.apply_async(task, (), {})
        with self.assertRaisesRegex(ValueError, "worker failed"):
            future.result(timeout=1)
        self.threads[-1].join(timeout=1)
        self.assertEqual(pool.scheduler_status, [False])

    def test_completion_releases_assigned_worker(self):
        pool = self.make_pool(2)
        first, release_first, first_thread = self.submit_blocked(pool)
        second, release_second, second_thread = self.submit_blocked(pool)
        self.assertEqual(pool.scheduler_status, [True, True])
        release_first.set()
        first_thread.join(timeout=1)
        self.assertEqual(first.result(timeout=1), "done")
        self.assertFalse(second.done())
        self.assertEqual(pool.scheduler_status, [False, True])
        release_second.set()
        second_thread.join(timeout=1)
        self.assertEqual(second.result(timeout=1), "done")
        self.assertEqual(pool.scheduler_status, [False, False])

    def test_teleport_failure_releases_worker(self):
        pool = self.make_pool(1)

        def teleport(func):
            raise ValueError("cannot teleport")

        pool.scheduler[0].teleport = teleport
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "cannot teleport"):
                pool.apply_async(lambda: None, (), {})
        self.assertEqual(pool.scheduler_status, [False])

    def test_scheduler_selects_the_only_idle_worker(self):
        pool = self.make_pool(3)
        pool.scheduler_index = 0
        pool.scheduler_status = [True, True, False]
        with contextlib.redirect_stdout(io.StringIO()):
            selected = pool._schedule()
        self.assertIs(selected, pool.scheduler[2], "scheduler skipped the idle worker")
        self.assertEqual(pool.scheduler_status, [True, True, True])

    def test_future_reports_running_while_task_executes(self):
        pool = self.make_pool(1)
        future, _, _ = self.submit_blocked(pool)
        self.assertTrue(future.running(), "executing task still has a pending Future")
        self.assertFalse(future.cancel(), "a running task cannot be cancelled")

    def test_worker_stays_busy_until_all_assigned_tasks_finish(self):
        pool = self.make_pool(1)
        first, release_first, first_thread = self.submit_blocked(pool)
        second, release_second, second_thread = self.submit_blocked(pool)
        release_first.set()
        first_thread.join(timeout=1)
        self.assertEqual(first.result(timeout=1), "done")
        self.assertFalse(second.done())
        self.assertEqual(pool.scheduler_status, [True])
        release_second.set()
        second_thread.join(timeout=1)
        self.assertEqual(second.result(timeout=1), "done")
        self.assertEqual(pool.scheduler_status, [False])

    def test_cancelled_pending_task_does_not_execute(self):
        pool = self.make_pool(1)
        executed = []
        with patch.object(rpc, "Thread") as thread:
            future = pool.apply_async(lambda: executed.append(True), (), {})
            target = thread.call_args.kwargs["target"]
        self.assertTrue(future.cancel())
        target()
        self.assertTrue(future.cancelled())
        self.assertEqual(executed, [])
        self.assertEqual(pool.scheduler_status, [False])

    def test_thread_start_failure_releases_worker(self):
        pool = self.make_pool(1)
        with patch.object(rpc, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("cannot start thread")
            with self.assertRaisesRegex(RuntimeError, "cannot start thread"):
                pool.apply_async(lambda: None, (), {})
        self.assertEqual(pool.scheduler_status, [False])

    def test_base_exception_completes_future_and_releases_worker(self):
        pool = self.make_pool(1)

        def task():
            raise SystemExit("worker exited")

        future = pool.apply_async(task, (), {})
        self.assertIsInstance(future.exception(timeout=1), SystemExit)
        self.threads[-1].join(timeout=1)
        self.assertEqual(pool.scheduler_status, [False])


if __name__ == "__main__":
    unittest.main()
