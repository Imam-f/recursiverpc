"""Regression tests for shell-independent SSH setup and cleanup."""
import contextlib
import importlib
import io
import unittest
from typing import cast
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from plumbum.commands import ProcessExecutionError
from recursiverpc.ssh import RemoteShell
rpc = importlib.import_module("recursiverpc.recursiverpc")

class SSHSetupTests(unittest.TestCase):
    def test_temp_directory_uses_remote_python(self):
        remote = MagicMock(spec=RemoteShell)
        remote.make_temp_dir.return_value = "C:/isolated-temp"
        directory = rpc.CrossPlatformTempDir(remote)
        self.assertEqual(directory.__enter__(), "C:/isolated-temp")
        remote.make_temp_dir.assert_called_once_with()

    def test_cleanup_retries_after_command_failure(self):
        remote = MagicMock(spec=RemoteShell)
        remote.remove_temp_dir.side_effect = [
            ProcessExecutionError(["python"], 1, "", "temporarily busy"), None
        ]
        directory = rpc.CrossPlatformTempDir(remote)
        directory.path = "/recursiverpc-test"
        with patch.object(rpc.time, "sleep"):
            directory.__exit__(None, None, None)
        self.assertEqual(remote.remove_temp_dir.call_count, 2)
        self.assertIsNone(directory.path)

    def test_output_monitor_stops_at_eof(self):
        class Stream:
            reads = 0

            def readline(self):
                return b"12345\n"

            def read(self, count):
                self.reads += 1
                if self.reads > 1:
                    raise RuntimeError("test guard: monitor read again after EOF")
                return b""

        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        stream = Stream()
        # Avoid constructing/deleting a real deployed server and its resources.
        server = cast(rpc.DeployedCrossPlatformServer,
                      SimpleNamespace(proc=SimpleNamespace(stdout=stream)))
        with patch.object(rpc, "Thread", InlineThread), contextlib.redirect_stdout(io.StringIO()):
            rpc.DeployedCrossPlatformServer._handle_server_startup(server)
        self.assertEqual(server.remote_port, 12345)
        self.assertEqual(stream.reads, 1, "monitor must stop at the first EOF")


if __name__ == "__main__":
    unittest.main()
