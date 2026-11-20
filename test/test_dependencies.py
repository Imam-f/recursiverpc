"""Dependency normalization, propagation, isolation, and cleanup regressions."""

import contextlib
from dataclasses import replace
from importlib import metadata
import importlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import tomlkit
from recursiverpc import Recursive_RPC, localprocess
from recursiverpc.dependencies import DependencySpec, LocalProject, parse_dependency

rpc = importlib.import_module('recursiverpc.recursiverpc')


def worker_probe():
    import importlib
    humanize = importlib.import_module('humanize')
    import sys
    from importlib.metadata import version
    return humanize.intcomma(1234567), version('humanize'), sys.prefix


class DependencyTests(unittest.TestCase):
    def dependencies(self, value):
        return tomlkit.parse(parse_dependency(value).project)['project']['dependencies']

    def test_requirement_list(self):
        self.assertEqual(self.dependencies(['humanize==4.11.0', 'humanize==4.11.0']), ['humanize==4.11.0'])

    def test_comma_string_keeps_version_ranges_and_extras(self):
        values = self.dependencies('requests[socks,security]>=2,<3, humanize==4.11.0')
        self.assertEqual(len(values), 2)
        self.assertIn('<3', values[0])
        self.assertIn('>=2', values[0])
        self.assertEqual(values[1], 'humanize==4.11.0')

    def test_imported_modules_resolve_distributions_and_skip_stdlib(self):
        self.assertEqual(self.dependencies([tomlkit, sys]), ['tomlkit'])
        self.assertEqual(self.dependencies('os, json, pathlib, humanize'), ['humanize'])

    def test_uv_header_preserves_python_and_sources(self):
        spec = parse_dependency('''# /// script
# requires-python = ">=3.10"
# dependencies = ["humanize==4.11.0"]
# [tool.uv.sources]
# helper = { git = "https://example.com/helper.git" }
# ///
''')
        data = tomlkit.parse(spec.project)
        self.assertEqual(data['project']['dependencies'], ['humanize==4.11.0'])
        self.assertEqual(data['tool']['uv']['sources']['helper']['git'], 'https://example.com/helper.git')

    def test_pyproject_path_rebases_relative_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pyproject.toml'
            path.write_text('''[project]
name = "original"
version = "1.0"
dependencies = ["helper"]
[tool.uv.sources]
helper = { path = "helper", editable = true }
''')
            data = tomlkit.parse(parse_dependency(path).project)
            self.assertEqual(data['project']['name'], 'recursiverpc-worker')
            self.assertEqual(data['tool']['uv']['sources']['helper']['path'], str(Path(directory) / 'helper'))

    def test_invalid_inputs_fail_before_installing(self):
        for value in ('numpy,,requests', '# /// script\n# dependencies = ["numpy"]',
                      '[project]\ndynamic=["dependencies"]', '[project]\ndependencies="numpy"'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_dependency(value)

    def test_project_includes_framework_and_compatible_python(self):
        spec = replace(parse_dependency([]), wheel_name='framework.whl', wheel=b'placeholder')
        data = tomlkit.parse(spec.project_text())
        self.assertEqual(data['project']['dependencies'], ['recursiverpc'])
        self.assertIn('~=3.10.16', data['project']['requires-python'])
        self.assertEqual(data['tool']['uv']['sources']['recursiverpc']['path'], 'framework.whl')

    def test_recursive_rpc_forwards_dependency_to_local_runner(self):
        spec = replace(parse_dependency([]), wheel_name='framework.whl', wheel=b'placeholder')
        worker = MagicMock(process_num=1)
        with patch.object(rpc, 'ProcessRunner', return_value=worker) as constructor:
            with Recursive_RPC([localprocess(1)], dependency=spec):
                pass
        constructor.assert_called_once_with(1, dependency=spec)
        worker.close.assert_called_once()

    def test_install_failure_removes_temp_project_without_changing_cwd(self):
        spec = replace(parse_dependency([]), wheel_name='framework.whl', wheel=b'placeholder')
        original = os.getcwd()
        with patch('recursiverpc.dependencies._run_uv', side_effect=RuntimeError('install failed')) as uv:
            with self.assertRaisesRegex(RuntimeError, 'install failed'):
                LocalProject(spec)
        arguments = uv.call_args.args[0]
        directory = Path(arguments[arguments.index('--project') + 1])
        self.assertFalse(directory.exists())
        self.assertEqual(os.getcwd(), original)

    def test_existing_rpc_server_is_not_mutated(self):
        spec = replace(parse_dependency([]), wheel_name='framework.whl', wheel=b'placeholder')
        with patch.object(rpc.rpyc.classic, 'connect') as connect, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'managed worker'):
                rpc.NetworkRunner(1, ('localhost', 18812), dependency=spec)
        connect.assert_not_called()

    def test_proxy_forwards_prepared_dependency_to_gateway_and_children(self):
        spec = replace(parse_dependency([]), wheel_name='framework.whl', wheel=b'placeholder')
        remote = MagicMock()
        connection = MagicMock()
        connection.modules.recursiverpc = remote
        gateway = MagicMock(conn=connection)
        with patch.object(rpc, 'NetworkRunner', return_value=gateway) as constructor:
            proxy = rpc.ProxyRunner(1, 'gateway', 22, [localprocess(1)], dependency=spec)
        self.assertIs(constructor.call_args.kwargs['dependency'], spec)
        module = connection.modules.__getitem__.return_value
        module.DependencySpec.assert_called_once_with(*spec.payload())
        self.assertIs(remote.ProxyRunner.call_args.kwargs['dependency'], module.DependencySpec.return_value)
        proxy.close()

    def test_partial_pool_creation_keeps_original_failure_and_closes_workers(self):
        spec = replace(parse_dependency([]), wheel_name='framework.whl', wheel=b'placeholder')
        worker = MagicMock(process_num=1)
        worker.close.side_effect = RuntimeError('cleanup failed')
        with patch.object(rpc, 'ProcessRunner', side_effect=[worker, ValueError('install failed')]):
            with self.assertRaisesRegex(ValueError, 'install failed'):
                Recursive_RPC([localprocess(1), localprocess(1)], dependency=spec)
        worker.close.assert_called_once()


class IsolatedDependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = parse_dependency(['humanize==4.11.0']).prepared()

    def test_all_input_forms_install_in_worker_and_clean_up(self):
        # Reuse the real built wheel so the test measures input behavior rather
        # than rebuilding identical framework code four times.
        bundle = self.bundle
        def prepared(spec):
            return replace(spec, wheel_name=bundle.wheel_name, wheel=bundle.wheel)

        with self.assertRaises(metadata.PackageNotFoundError):
            metadata.version('humanize')
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'pyproject.toml'
            project.write_text('[project]\nname="demo"\nversion="1.0"\ndependencies=["humanize==4.11.0"]\n')
            inputs = [
                ['humanize==4.11.0'], 'humanize==4.11.0', project,
                '# /// script\n# dependencies = ["humanize==4.11.0"]\n# ///\n',
            ]
            for dependency in inputs:
                with self.subTest(dependency=dependency), patch.object(DependencySpec, 'prepared', prepared):
                    with Recursive_RPC([localprocess(1)], dependency=dependency) as pool:
                        result, version, prefix = pool.apply(worker_probe)
                        self.assertEqual((result, version), ('1,234,567', '4.11.0'))
                        self.assertNotEqual(prefix, sys.prefix)
                    self.assertFalse(Path(prefix).parent.exists())
        with self.assertRaises(metadata.PackageNotFoundError):
            metadata.version('humanize')


if __name__ == '__main__':
    unittest.main()
