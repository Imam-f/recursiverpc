"""Local source bundles and setup hooks in isolated worker environments."""

from dataclasses import replace
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from recursiverpc import Recursive_RPC, SetupScript, localprocess
from recursiverpc.dependencies import DependencySpec, parse_dependency
from recursiverpc.setup import parse_setup, setup_code
from recursiverpc.sources import extract_sources
from test_cross_shell import LocalShell


def local_probe():
    import importlib
    import os
    from pathlib import Path
    helper = importlib.import_module('rpc_local_helper')
    return helper.value(), Path('configured.txt').read_text(), os.getcwd()


def make_projects(root):
    helper = root / 'helper'
    helper.mkdir()
    (helper / 'pyproject.toml').write_text('''[project]
name = "rpc-local-helper"
version = "0.1.0"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
''')
    package = helper / 'src/rpc_local_helper'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('def value(): return 37\n')
    app = root / 'application'
    app.mkdir()
    (app / 'pyproject.toml').write_text('''[project]
name = "rpc-local-app"
version = "0.1.0"
dependencies = ["rpc-local-helper"]
[tool.uv]
package = false
[tool.uv.sources]
rpc-local-helper = { path = "../helper", editable = true }
''')
    (app / 'data.txt').write_text('from app source')
    (app / '.env').write_text('excluded')
    return app, helper


class SourceTests(unittest.TestCase):
    def test_setup_accepts_python_code_and_native_files(self):
        self.assertEqual(parse_setup('print(1)'), ('python', 'print(1)'))
        self.assertEqual(parse_setup(SetupScript('echo ready', language='bash')), ('bash', 'echo ready'))
        with tempfile.TemporaryDirectory() as directory:
            for suffix, language in (('.py', 'python'), ('.sh', 'bash'), ('.ps1', 'powershell'), ('.cmd', 'cmd')):
                path = Path(directory) / ('setup' + suffix)
                path.write_text('test')
                self.assertEqual(parse_setup(path), (language, 'test'))

    def test_bundles_paths_without_original_absolute_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            app, helper = make_projects(Path(directory))
            spec = parse_dependency(app / 'pyproject.toml').configured(copy_local_dependencies=True, copy_project=True)
            self.assertNotIn(str(helper), spec.project)
            self.assertIsNotNone(spec.workdir)
            with zipfile.ZipFile(io.BytesIO(spec.files)) as archive:
                self.assertTrue(any(name.endswith('rpc_local_helper/__init__.py') for name in archive.namelist()))
                self.assertFalse(any(name.endswith('.env') for name in archive.namelist()))

    def test_bundle_traversal_is_rejected(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, 'w') as archive:
            archive.writestr('../outside', 'bad')
        with tempfile.TemporaryDirectory(prefix='recursiverpc-sources-') as directory:
            with self.assertRaises(ValueError):
                extract_sources(directory, data.getvalue())


class LocalSourceIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.framework = parse_dependency([]).prepared()

    def prepare(self, spec):
        return replace(spec, wheel_name=self.framework.wheel_name, wheel=self.framework.wheel)

    def test_local_worker_imports_bundled_helper_and_runs_setup_in_copied_app(self):
        with tempfile.TemporaryDirectory() as directory:
            app, _ = make_projects(Path(directory))
            setup = 'from pathlib import Path; import rpc_local_helper; Path("configured.txt").write_text(Path("data.txt").read_text())'
            with patch.object(DependencySpec, 'prepared', self.prepare):
                with Recursive_RPC([localprocess(1)], dependency=app / 'pyproject.toml',
                                   copy_local_dependencies=True, copy_project=True, setup_script=setup) as pool:
                    value, marker, cwd = pool.apply(local_probe)
                    self.assertEqual((value, marker), (37, 'from app source'))
                    self.assertNotEqual(cwd, str(app))
                self.assertFalse(Path(cwd).exists())
            self.assertFalse((app / 'configured.txt').exists())

    def test_remote_copied_venv_uses_bundled_editable_source_and_runs_setup_once(self):
        with tempfile.TemporaryDirectory() as directory:
            app, helper = make_projects(Path(directory))
            setup = 'from pathlib import Path; p=Path("configured.txt"); p.write_text(p.read_text()+"!" if p.exists() else "setup")'
            spec = self.prepare(parse_dependency(app / 'pyproject.toml').configured(
                copy_local_dependencies=True, copy_project=True, setup_script=setup))
            profile, _ = spec.environment_bundle()
            self.assertFalse(Path(json.loads(profile)['prefix']).parent.exists())
            remote = LocalShell('cmd' if os.name == 'nt' else 'linux')
            destination = remote.make_temp_dir()
            try:
                executable = spec.install_remote(remote, destination)
                self.assertIsNotNone(executable, 'Bundled editable source should be relocatable')
                result = remote.command(executable)('-I', '-c',
                    'import rpc_local_helper,pathlib,json; print(json.dumps([rpc_local_helper.value(), pathlib.Path('
                    + repr(destination + '/' + str(spec.workdir) + '/configured.txt') + ').read_text()]))')
                self.assertEqual(json.loads(result), [37, 'setup'])
                self.assertFalse((app / 'configured.txt').exists())
            finally:
                remote.remove_temp_dir(destination)

    def test_failed_setup_removes_the_project(self):
        spec = self.prepare(parse_dependency([]).configured(setup_script='raise RuntimeError("hook failed")'))
        created = []
        from recursiverpc.dependencies import LocalProject
        original = LocalProject.__init__
        def track(project, *args, **kwargs):
            try:
                original(project, *args, **kwargs)
            finally:
                created.append(project.path)
        with patch.object(LocalProject, '__init__', track):
            with self.assertRaisesRegex(RuntimeError, 'hook failed'):
                Recursive_RPC([localprocess(1)], dependency=spec)
        self.assertTrue(created)
        self.assertFalse(created[0].exists())


if __name__ == '__main__':
    unittest.main()
