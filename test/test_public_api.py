"""Compatibility and logging regressions for the module split."""

import contextlib
import importlib
import io
import pickle
import unittest
from unittest.mock import MagicMock, patch

import recursiverpc
from recursiverpc import (
    ProcessRunner,
    localprocess,
    networkprocess,
    proxyprocess,
    runners,
)


class PublicAPITests(unittest.TestCase):
    def test_legacy_imports_resolve_to_the_same_classes(self):
        legacy = importlib.import_module("recursiverpc.recursiverpc")
        for name in (
            "Recursive_RPC",
            "RPC_Future",
            "Pool",
            "ProcessRunner",
            "NetworkRunner",
            "ProxyRunner",
            "tupleprocess",
            "localprocess",
            "networkprocess",
            "proxyprocess",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(recursiverpc, name), getattr(legacy, name))
        self.assertIs(legacy.Runner, runners.Runner)
        deployment = importlib.import_module("recursiverpc.deployment")
        self.assertIs(
            legacy.DeployedCrossPlatformServer, deployment.DeployedCrossPlatformServer
        )

    def test_old_descriptor_pickles_still_load(self):
        descriptor = proxyprocess(
            1,
            None,
            None,
            [localprocess(2), networkprocess(1, ("localhost", 18812))],
            (),
            {},
        )
        serialized = pickle.dumps(descriptor, protocol=2)
        old_serialized = serialized.replace(
            b"crecursiverpc.config\n", b"crecursiverpc.recursiverpc\n"
        )
        self.assertNotEqual(old_serialized, serialized)
        self.assertEqual(pickle.loads(old_serialized), descriptor)
        self.assertEqual(pickle.loads(serialized), descriptor)

    def test_local_startup_uses_logging_and_keeps_printer_callback(self):
        output = io.StringIO()
        with patch.object(runners, "Pool") as pool, contextlib.redirect_stdout(output):
            pool.return_value.max_workers = 2
            with self.assertLogs("recursiverpc.runners", level="DEBUG") as logs:
                runner = ProcessRunner(2)
            runner.close()
            printer = MagicMock()
            runner = ProcessRunner(2, printer=printer)
            runner.close()
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Starting local runner with 2 processes", logs.output[0])
        printer.assert_called_once_with("ProcessRunner", 2)

    def test_network_cleanup_logs_failures_and_continues(self):
        runner = runners.NetworkRunner.__new__(runners.NetworkRunner)
        runner.conn = MagicMock()
        runner.pool = MagicMock()
        runner.pool.close.side_effect = OSError("pool failed")
        runner.bg_event_loop = MagicMock()
        server, machine = MagicMock(), MagicMock()
        runner.server, runner.machine = server, machine
        output = io.StringIO()
        with (
            self.assertLogs("recursiverpc.runners", level="DEBUG") as logs,
            contextlib.redirect_stdout(output),
        ):
            runner.close()
        self.assertTrue(
            any("Failed to close remote worker pool" in entry for entry in logs.output)
        )
        self.assertEqual(output.getvalue(), "")
        server.close.assert_called_once()
        machine.close.assert_called_once()
        self.assertIsNone(runner.conn)


if __name__ == "__main__":
    unittest.main()
