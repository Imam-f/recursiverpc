"""SSH deadlines, full-duplex uploads, and content-addressed artifacts."""
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from recursiverpc.ssh import SSHProcess, ShellAwareMachine
from recursiverpc.artifacts import Artifact, CHUNK_SIZE

class SSHCommunicationTests(unittest.TestCase):
    def process(self, stdin, stdout, stderr):
        process = SSHProcess.__new__(SSHProcess)
        process.stdin, process.stdout, process.stderr = stdin, stdout, stderr
        process.argv = ['fake-ssh']
        process.wait = lambda: 0
        process.terminate = lambda: None
        return process

    def test_output_is_drained_before_blocking_input_write(self):
        drained = threading.Event()
        class Input(io.BytesIO):
            def write(self, block):
                if not drained.wait(1): raise RuntimeError('output was not drained')
                return super().write(block)
        class Output(io.BytesIO):
            def read(self):
                drained.set()
                return super().read()
        process = self.process(Input(), Output(b'out'), io.BytesIO(b'err'))
        self.assertEqual(process.communicate(input=b'payload', timeout=1), (b'out', b'err'))

    def test_deadline_includes_blocked_input_and_closes_channel(self):
        killed = threading.Event()
        class Input(io.BytesIO):
            def write(self, value):
                killed.wait(2)
                return len(value)
        process = self.process(Input(), io.BytesIO(), io.BytesIO())
        process.kill = killed.set
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            process.communicate(input=b'payload', timeout=.05)
        self.assertTrue(killed.is_set())
        self.assertLess(time.monotonic() - start, .5)

    def test_one_deadline_covers_both_output_streams(self):
        class Delayed(io.BytesIO):
            def __init__(self, delay): super().__init__(); self.delay=delay
            def read(self): time.sleep(self.delay); return b''
        process = self.process(io.BytesIO(), Delayed(.06), Delayed(.14))
        process.kill = lambda: None
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired): process.communicate(timeout=.1)
        self.assertLess(time.monotonic()-start, .5)

class ArtifactTests(unittest.TestCase):
    def test_chunk_upload_cache_hit_and_corruption_recovery(self):
        artifact = Artifact()
        # Vary identity between runs, avoiding interference with other tests.
        Path(artifact.path).write_bytes((artifact.path.encode()*100000)[:CHUNK_SIZE+42])
        calls = []
        def python_run(script, input=None):
            calls.append(input)
            result = subprocess.run([sys.executable, '-c', script], input=input,
                                    capture_output=True, check=True)
            return result.stdout.decode().strip()
        remote = SimpleNamespace(python_run=python_run)
        try:
            with tempfile.TemporaryDirectory(prefix='recursiverpc-test-') as directory:
                upload = lambda: ShellAwareMachine.write_artifact(remote,directory,'source.zip',artifact)
                upload()
                self.assertEqual(Path(directory,'source.zip').read_bytes(),Path(artifact.path).read_bytes())
                self.assertEqual([len(block) for block in calls if block], [CHUNK_SIZE,42])
                calls.clear()
                upload()
                self.assertEqual(len(calls), 1)
                cache = Path(tempfile.gettempdir())/'recursiverpc-artifacts'/artifact.digest()
                cache.write_bytes(b'corrupt')
                calls.clear()
                upload()
                self.assertEqual([len(block) for block in calls if block], [CHUNK_SIZE,42])
        finally:
            artifact.close()

class LegacySetupTests(unittest.TestCase):
    def test_setup_failure_keeps_callers_working_directory(self):
        import os
        from unittest.mock import patch, MagicMock
        from recursiverpc.runners import NetworkRunner
        from plumbum import SshMachine
        original = os.getcwd()
        runner = NetworkRunner.__new__(NetworkRunner)
        with patch.object(NetworkRunner, '_check_uv_available', return_value=True), \
             patch.object(NetworkRunner, '_generate_requirements'), \
             patch.object(NetworkRunner, '_add_script_dependencies', side_effect=RuntimeError('setup failed')):
            with self.assertRaisesRegex(RuntimeError, 'setup failed'):
                runner.__init__(1, MagicMock(spec=SshMachine))
        self.assertEqual(os.getcwd(), original)
        runner.close()

class EnvironmentCacheTests(unittest.TestCase):
    def test_local_dependency_contents_invalidate_shared_cache(self):
        from dataclasses import replace
        from unittest.mock import patch
        from recursiverpc.dependencies import parse_dependency
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source/'module.py').write_text('value=1')
            project = '[project]\nname="x"\nversion="0"\ndependencies=[]\n[tool.uv.sources]\nexample={path='+json.dumps(str(source))+'}\n'
            spec = replace(parse_dependency(project),wheel_name='x.whl',wheel=b'unique-cache-test')
            with patch('recursiverpc.dependencies.LocalProject') as local, \
                 patch('recursiverpc.dependencies.archive_environment', return_value=('profile',b'zip')) as archive:
                spec.environment_bundle()
                replace(spec,venv=None).environment_bundle()
                self.assertEqual(archive.call_count, 1)
                (source/'module.py').write_text('value=2')
                replace(spec,venv=None).environment_bundle()
                self.assertEqual(archive.call_count, 2)
