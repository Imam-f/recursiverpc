"""Proxy regression tests without SSH credentials or remote services."""

import importlib
from concurrent.futures import Future
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from recursiverpc import SSHConnection, localprocess, networkprocess, proxyprocess

rpc = importlib.import_module('recursiverpc.recursiverpc')


def complete(value):
    future = Future()
    future.set_result(value)
    return rpc.RPC_Future(future, MagicMock(spec=rpc.Runner))


def square_with_offset(value, offset=0):
    return value * value + offset


class ProxyRunnerTests(unittest.TestCase):
    def test_nested_proxy_executes_on_a_real_local_worker(self):
        proxy = rpc.ProxyRunner(1, None, None, [
            proxyprocess(1, None, None, [localprocess(1)], (), {}),
        ])
        try:
            result = proxy.run(square_with_offset, 5, offset=2)
            self.assertEqual(result.result.result(timeout=10), 27)
        finally:
            proxy.close()

    def test_local_group_uses_current_network_worker_signature(self):
        local = MagicMock(process_num=2)
        network = MagicMock(process_num=3)
        network.run.return_value = complete(15)
        connection = SSHConnection('worker')
        with patch.object(rpc, 'ProcessRunner', return_value=local), \
             patch.object(rpc, 'NetworkRunner', return_value=network) as constructor:
            proxy = rpc.ProxyRunner(1, None, None, [
                localprocess(2), networkprocess(3, connection),
            ])
        self.assertEqual(proxy.process_num, 5)
        constructor.assert_called_once_with(3, connection)
        function = lambda x: x * 3
        with patch.object(proxy, 'schedule', return_value=network):
            self.assertEqual(proxy.run(function, 5).get(), 15)
        network.run.assert_called_once_with(function, 5)
        self.assertEqual(proxy.status(), (True, 5, 0, 0))
        proxy.close()
        proxy.close()
        local.close.assert_called_once()
        network.close.assert_called_once()

    def test_nested_local_proxy_no_longer_raises_infinite_loop_error(self):
        worker = MagicMock(process_num=2)
        with patch.object(rpc, 'ProcessRunner', return_value=worker):
            proxy = rpc.ProxyRunner(1, None, None, [
                proxyprocess(1, None, None, [localprocess(2)], (), {}),
            ])
        self.assertEqual(proxy.process_num, 2)
        proxy.close()
        worker.close.assert_called_once()

    def test_remote_proxy_reuses_gateway_and_supplied_function(self):
        remote = MagicMock()
        remote.ProxyRunner.return_value.process_num = 2
        conn = MagicMock()
        conn.modules.recursiverpc = remote
        gateway = MagicMock(conn=conn)
        remote.ProxyRunner.return_value.run.return_value = complete(9)
        with patch.object(rpc, 'NetworkRunner', return_value=gateway) as constructor:
            proxy = rpc.ProxyRunner(1, 'gateway', 2222, [localprocess(2)], ('user', 'password'))
        ssh = constructor.call_args.args[1]
        self.assertIsInstance(ssh, SSHConnection)
        self.assertEqual((ssh.host, ssh.user, ssh.port), ('gateway', 'user', 2222))
        remote.localprocess.assert_called_once_with(2)
        function = lambda x: x * x
        self.assertEqual(proxy.run(function, 3).get(), 9)
        conn.teleport.assert_called_once_with(function)
        remote.ProxyRunner.return_value.run.assert_called_once_with(conn.teleport.return_value, 3)
        proxy.close()
        remote.ProxyRunner.return_value.close.assert_called_once()
        gateway.close.assert_called_once()

    def test_gateway_descriptors_are_native_and_recursive(self):
        remote = MagicMock()
        config = SSHConnection('child', user='user', port=2222)
        spec = proxyprocess(1, None, None, [networkprocess(2, config)], (), {})
        rpc.ProxyRunner._copy_spec(remote, spec)
        remote.SSHConnection.assert_called_once_with(
            'child', user='user', port=2222, password=None, key_filename=None,
            shell='auto', timeout=15,
        )
        remote.networkprocess.assert_called_once_with(2, remote.SSHConnection.return_value)
        remote.proxyprocess.assert_called_once()

    def test_partial_initialization_closes_started_workers(self):
        worker = MagicMock(process_num=1)
        with patch.object(rpc, 'ProcessRunner', return_value=worker), \
             patch.object(rpc, 'NetworkRunner', side_effect=ValueError('connect failed')):
            with self.assertRaisesRegex(ValueError, 'connect failed'):
                rpc.ProxyRunner(1, None, None, [localprocess(1), networkprocess(1, ('host', 18812))])
        worker.close.assert_called_once()

    def test_gateway_closes_even_if_remote_proxy_close_fails(self):
        remote = MagicMock()
        conn = SimpleNamespace(modules=SimpleNamespace(recursiverpc=remote))
        gateway = MagicMock(conn=conn)
        with patch.object(rpc, 'NetworkRunner', return_value=gateway):
            proxy = rpc.ProxyRunner(1, 'gateway', 22, [localprocess(1)])
        remote.ProxyRunner.return_value.close.side_effect = RuntimeError('close failed')
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            proxy.close()
        gateway.close.assert_called_once()
        proxy.close()


if __name__ == '__main__':
    unittest.main()
