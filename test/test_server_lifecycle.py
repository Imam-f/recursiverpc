"""Run the real bootstrap with early EOF and an open SSH-style stdin pipe."""

from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import rpyc
from recursiverpc.recursiverpc import SERVER_SCRIPT


class ServerLifecycleTests(unittest.TestCase):
    def start_server(self, stdin):
        temporary = tempfile.TemporaryDirectory(prefix='recursiverpc-lifecycle-')
        self.addCleanup(temporary.cleanup)
        script = SERVER_SCRIPT[1:]
        for source, value in (
            ('$SERVER_MODULE$', 'rpyc.utils.server'),
            ('$SERVER_CLASS$', 'OneShotServer'),
            ('$SERVICE_MODULE$', 'rpyc.core.service'),
            ('$SERVICE_CLASS$', 'SlaveService'),
            ('$EXTRA_SETUP$', ''),
        ):
            script = script.replace(source, value)
        path = Path(temporary.name) / 'server.py'
        path.write_text(script)
        process = subprocess.Popen([sys.executable, '-B', '-u', str(path)],
                                   stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_server, process)
        assert process.stdout is not None
        port = int(process.stdout.readline().strip())
        return process, port

    @staticmethod
    def stop_server(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def test_early_stdin_eof_does_not_begin_interpreter_shutdown(self):
        process, port = self.start_server(subprocess.DEVNULL)
        connection = rpyc.classic.connect('localhost', port=port)
        try:
            # A non-daemon thread keeps the old bootstrap alive during Python's
            # shutdown, reproducing a gateway with active worker/SSH threads.
            connection.execute(
                'import threading,time\n'
                'from concurrent.futures import ThreadPoolExecutor\n'
                'threading.Thread(target=lambda: time.sleep(6), daemon=False).start()'
            )
            time.sleep(2.5)  # Beyond the old bootstrap's two-second join.
            connection.execute(
                'with ThreadPoolExecutor(1) as executor:\n'
                '    answer = executor.submit(lambda: 42).result()'
            )
            self.assertEqual(connection.eval('answer'), 42)
        finally:
            connection.close()
        self.assertEqual(process.wait(timeout=10), 0)

    def test_explicit_shutdown_token_stops_server_without_stdin_eof(self):
        process, _ = self.start_server(subprocess.PIPE)
        assert process.stdin is not None
        process.stdin.write(b'__recursiverpc_shutdown__\n')
        process.stdin.flush()
        self.assertEqual(process.wait(timeout=5), 0)

    def test_rpc_disconnect_stops_oneshot_server_with_stdin_open(self):
        process, port = self.start_server(subprocess.PIPE)
        connection = rpyc.classic.connect('localhost', port=port)
        connection.close()
        self.assertEqual(process.wait(timeout=5), 0)


if __name__ == '__main__':
    unittest.main()
