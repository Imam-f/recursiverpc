"""Compatibility checks and real offline relocation into an owned directory."""

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from recursiverpc.dependencies import DependencySpec, parse_dependency
from recursiverpc.venv import compatible, runtime_info, try_copy_environment
from test_cross_shell import LocalShell


class CompatibilityTests(unittest.TestCase):
    def profile(self):
        return {'os': 'linux', 'arch': 'x86_64', 'bits': 64, 'implementation': 'CPython',
                'version': '3.10.18', 'abi': 'cpython-310-x86_64-linux-gnu', 'libc': ['glibc', '2.35'],
                'executable': '/usr/bin/python3.10'}

    def test_compatible_runtime(self):
        source = self.profile()
        target = dict(source, version='3.10.19', libc=['glibc', '2.39'])
        self.assertTrue(compatible(source, target, '~=3.10.16'))

    def test_os_arch_python_abi_and_libc_mismatches(self):
        source = self.profile()
        for change in ({'os': 'windows'}, {'arch': 'arm64'}, {'bits': 32},
                       {'implementation': 'PyPy'}, {'version': '3.11.0'}, {'abi': 'other'},
                       {'libc': ['musl', '1.2']}, {'libc': ['glibc', '2.31']}):
            with self.subTest(change=change):
                self.assertFalse(compatible(source, dict(source, **change)))
        self.assertFalse(compatible(source, source, '>=3.11'))

    def test_os_mismatch_does_not_prepare_or_transfer_local_env(self):
        source = self.profile()
        remote = MagicMock()
        remote.python_run.return_value = json.dumps(dict(source, os='windows'))
        factory = MagicMock()
        with patch('recursiverpc.venv.runtime_info', return_value=source):
            self.assertIsNone(try_copy_environment(remote, '/recursiverpc-test', factory))
        factory.assert_not_called()
        remote.write_file.assert_not_called()
        remote.command.assert_not_called()

    def test_dependency_copy_skips_remote_sync(self):
        spec = replace(parse_dependency([]), wheel_name='framework.whl', wheel=b'placeholder')
        remote = MagicMock()
        with patch('recursiverpc.dependencies.try_copy_environment', return_value='/worker/.venv/bin/python'):
            result = spec.install_remote(remote, '/worker')
        self.assertEqual(result, '/worker/.venv/bin/python')
        remote.command.assert_not_called()

    def test_dependency_mismatch_uses_normal_remote_installation(self):
        spec = replace(parse_dependency([]), wheel_name='framework.whl', wheel=b'placeholder')
        remote = MagicMock()
        with patch('recursiverpc.dependencies.try_copy_environment', return_value=None):
            self.assertIsNone(spec.install_remote(remote, '/worker'))
        command = remote.command.return_value.with_env.return_value
        self.assertEqual(command.call_args.args[0], 'sync')

    def test_bad_copy_is_removed_before_fallback(self):
        source = self.profile()
        remote = MagicMock()
        remote.python_run.side_effect = [json.dumps(source), RuntimeError('bad archive'), '']
        factory = lambda: (json.dumps(source), b'broken archive')
        with patch('recursiverpc.venv.runtime_info', return_value=source):
            self.assertIsNone(try_copy_environment(remote, '/recursiverpc-test', factory))
        self.assertIn('shutil.rmtree', remote.python_run.call_args.args[0])


class RelocationTests(unittest.TestCase):
    def test_copy_runs_after_source_is_deleted_without_remote_package_install(self):
        spec = parse_dependency(['humanize==4.11.0']).prepared()
        profile, archive = spec.environment_bundle()
        info = json.loads(profile)
        self.assertFalse(Path(info['prefix']).exists())
        with zipfile.ZipFile(io.BytesIO(archive)) as contents:
            self.assertFalse(any('__pycache__' in name for name in contents.namelist()))
            self.assertNotIn('.venv/pyvenv.cfg', contents.namelist())
        remote = LocalShell('cmd' if os.name == 'nt' else 'linux')
        directory = remote.make_temp_dir()
        original = remote.command
        calls = []

        class OfflineUV:
            def __call__(self, *args):
                calls.append(args)
                if args[0] not in ('venv', 'python'):
                    raise AssertionError('Remote package installation was attempted')
                return original('uv')(*args)

            def with_env(self, **kwargs):
                raise AssertionError('Remote package installation was attempted')

        def command(name):
            return OfflineUV() if name == 'uv' else original(name)

        try:
            with patch.object(remote, 'command', side_effect=command):
                executable = spec.install_remote(remote, directory)
            self.assertIsNotNone(executable, 'The copy unexpectedly fell back to uv sync')
            result = original(executable)('-I', '-c',
                'import humanize,sys,json;print(json.dumps([humanize.intcomma(1234567),sys.prefix]))')
            value, prefix = json.loads(result)
            self.assertEqual(value, '1,234,567')
            self.assertIn(directory, prefix)
            self.assertEqual(calls[0][0], 'venv')
            self.assertIn('--no-python-downloads', calls[0])
            # RPyC's console launcher contains an absolute Python shebang.
            launcher = prefix + ('/Scripts/rpyc_classic.exe' if os.name == 'nt' else '/bin/rpyc_classic')
            self.assertIn('Usage', original(launcher)('--help'))
        finally:
            remote.remove_temp_dir(directory)


if __name__ == '__main__':
    unittest.main()
