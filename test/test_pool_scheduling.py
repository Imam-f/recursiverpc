"""Bounded scheduling, cancellation and notification regressions."""
import threading
import time
import unittest
from concurrent.futures import Future, CancelledError, TimeoutError
from types import SimpleNamespace
from unittest.mock import patch
from recursiverpc.pool import Pool
from recursiverpc.futures import RPC_Future
from recursiverpc.runners import ProcessRunner
from recursiverpc import Recursive_RPC, localprocess, NetworkRunner

class PoolSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.gate = threading.Event()
        self.pools = []
        connection = lambda: SimpleNamespace(modules=SimpleNamespace(sys=SimpleNamespace()),
                                            teleport=lambda f:f, close=lambda:None)
        self.patch = patch('recursiverpc.pool.rpyc.classic.connect_multiprocess', side_effect=connection)
        self.patch.start()

    def tearDown(self):
        self.gate.set()
        for pool in self.pools:
            pool.close()
        self.patch.stop()

    def pool(self, workers=1, pending=1):
        pool = Pool(workers, max_pending=pending)
        self.pools.append(pool)
        return pool

    def blocked(self, pool):
        started = threading.Event()
        def task():
            started.set()
            self.gate.wait(5)
            return 7
        result = pool.apply_async(task, (), {})
        self.assertTrue(started.wait(1))
        return result

    def test_bounded_queue_blocks_producer_and_keeps_threads_fixed(self):
        pool = self.pool()
        first = self.blocked(pool)
        second = pool.apply_async(lambda: 8, (), {})
        submitted = threading.Event()
        def submit():
            pool.apply_async(lambda: 9, (), {})
            submitted.set()
        producer = threading.Thread(target=submit)
        producer.start()
        self.assertFalse(submitted.wait(.05))
        self.assertEqual(len(pool._threads), 1)
        self.gate.set()
        producer.join(2)
        self.assertTrue(submitted.is_set())
        self.assertEqual((first.result(1), second.result(1)), (7, 8))

    def test_cancel_pending_and_reject_after_close(self):
        pool = self.pool()
        first = self.blocked(pool)
        second = pool.apply_async(lambda: self.fail('cancelled task executed'), (), {})
        pool.close(wait=False, cancel_pending=True)
        self.assertTrue(second.cancelled())
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            pool.apply_async(lambda: None, (), {})
        self.gate.set()
        self.assertEqual(first.result(1), 7)
        pool.close()
        self.assertFalse(any(t.is_alive() for t in pool._threads))

    def test_teleport_and_task_errors_reach_future_and_worker_recovers(self):
        pool = self.pool()
        def fail(_): raise ValueError('teleport failed')
        pool.scheduler[0].teleport = fail
        with self.assertRaisesRegex(ValueError, 'teleport failed'):
            pool.apply_async(lambda: None, (), {}).result(1)
        pool.scheduler[0].teleport = lambda f:f
        def task(): raise SystemExit('task failed')
        self.assertIsInstance(pool.apply_async(task, (), {}).exception(1), SystemExit)
        self.assertEqual(pool.apply_async(lambda x, scale: x*scale, (3,), {'scale':4}).result(1), 12)

    def test_runner_releases_completed_handles_without_status_call(self):
        runner = ProcessRunner(1)
        self.pools.append(runner.pool)
        handles = [runner.run(lambda: 1) for _ in range(10)]
        self.assertEqual(list(RPC_Future.as_completed(handles)), [1]*10)
        runner.pool.close()
        self.assertEqual(runner.process_handle, [])
        self.assertEqual(runner.status(), (False, 1, 0, 0))

    def test_future_timeout_completion_order_and_exception(self):
        first, second = Future(), Future()
        handles = [RPC_Future(first, None), RPC_Future(second, None)]
        with self.assertRaises(TimeoutError): handles[0].get(.01)
        second.set_result(2)
        results = RPC_Future.as_completed(handles)
        self.assertEqual(next(results), 2)
        first.set_exception(ValueError('failed'))
        with self.assertRaisesRegex(ValueError, 'failed'): next(results)

    def test_streaming_map_consumes_only_window_and_close_rejects_work(self):
        seen = []
        def items():
            for i in range(100):
                seen.append(i)
                yield i
        with Recursive_RPC([localprocess(1)], max_pending=1) as rpc:
            result = rpc.imap_ordered(items(), lambda x:x*2)
            self.assertEqual(next(result), 0)
            self.assertEqual(len(seen), 2)
            self.assertEqual(list(result), list(range(2,200,2)))
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            rpc.apply_async(lambda: None)

class ProxyShutdownTests(unittest.TestCase):
    def test_background_close_waits_for_children_before_gateway(self):
        from recursiverpc.runners import ProxyRunner
        from unittest.mock import MagicMock
        proxy = ProxyRunner.__new__(ProxyRunner)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        child = MagicMock()
        def close(**kwargs):
            entered.set()
            release.wait(2)
        child.close.side_effect = close
        gateway = MagicMock()
        proxy.connection, proxy.weight, proxy.process_handle = [child], [1], []
        proxy._remote_proxy, proxy._gateway, proxy.conn = None, gateway, None
        proxy.close(wait=False)
        self.assertTrue(entered.wait(1))
        gateway.close.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'closed'): proxy.run(lambda:None)
        def wait_close():
            proxy.close()
            finished.set()
        waiter = threading.Thread(target=wait_close)
        waiter.start()
        self.assertFalse(finished.wait(.05))
        release.set()
        waiter.join(2)
        self.assertTrue(finished.is_set())
        gateway.close.assert_called_once_with(wait=True,cancel_pending=False)

class RemoteNotificationTests(unittest.TestCase):
    def test_real_rpyc_results_are_bridged_and_released(self):
        import subprocess
        import sys
        server = subprocess.Popen([sys.executable, '-u', '-c',
            "from rpyc.utils.server import ThreadedServer; "
            "from rpyc.core.service import ClassicService; "
            "s=ThreadedServer(ClassicService,hostname='localhost',port=0); "
            "print(s.port,flush=True); s.start()"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            port = int(server.stdout.readline())
            runner = NetworkRunner(1, ('localhost', port))
            try:
                handle = runner.run(lambda x:x+1, 4)
                self.assertEqual(handle.get(timeout=10), 5)
                self.assertIs(type(handle.result), Future)
                self.assertEqual(list(RPC_Future.as_completed([runner.run(lambda: 6)])), [6])
                from recursiverpc.runners import ProxyRunner
                proxy = ProxyRunner.__new__(ProxyRunner)
                proxy.conn = runner.conn
                proxy._remote_proxy = runner.conn.modules.recursiverpc.ProxyRunner(
                    1, None, None, [runner.conn.modules.recursiverpc.localprocess(1)])
                proxy._gateway = None
                proxy.connection, proxy.weight, proxy.process_handle = [], [], []
                proxy._handles_lock = threading.RLock()
                proxy.process_num = 1
                try:
                    remote_handle = proxy.run(lambda: 11)
                    self.assertIs(type(remote_handle.result), Future)
                    self.assertEqual(list(RPC_Future.as_completed([remote_handle])), [11])
                finally:
                    proxy.close()
                runner.pool.close()
                self.assertEqual(runner.status()[2], 0)
            finally:
                runner.close()
        finally:
            server.terminate()
            server.communicate(timeout=5)
