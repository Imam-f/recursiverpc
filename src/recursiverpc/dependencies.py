"""Normalize dependency inputs and create isolated uv worker projects."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from importlib import metadata
import os
import inspect
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import ModuleType
from typing import Any, Sequence, cast

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
import tomlkit
from uv import find_uv_bin
from .venv import Bundle, archive_environment, try_copy_environment
from .setup import SetupPayload, SetupScript, parse_setup, setup_code
from .sources import bundle_sources, extract_sources, expose_project

DependencyInput = str | os.PathLike[str] | Sequence[str | ModuleType]


def _requirements(items: Sequence[str | ModuleType]) -> list[str]:
    result = []
    for item in items:
        if isinstance(item, ModuleType):
            name = item.__name__.split('.')[0]
            if name in sys.stdlib_module_names:
                continue
            distributions = metadata.packages_distributions().get(name, [])
            if not distributions:
                # Python 3.10 only inspects top_level.txt, which modern wheels
                # often omit. Their RECORD files still identify package owners.
                distributions = [
                    distribution.metadata['Name']
                    for distribution in metadata.distributions()
                    if any(file.parts[0] == name or str(file) == name + '.py'
                           for file in distribution.files or [])
                ]
            distributions = list(dict.fromkeys(distributions))
            if len(distributions) != 1:
                raise ValueError(f"Cannot identify one distribution for module {name!r}; use a requirement string")
            item = distributions[0]
        if not isinstance(item, str) or not item.strip():
            raise ValueError('Dependencies must be nonempty requirement strings or imported modules')
        if re.fullmatch(r'[A-Za-z_]\w*(?:\.\w+)*', item.strip()) and item.strip().split('.')[0] in sys.stdlib_module_names:
            continue
        requirement = str(Requirement(item.strip()))
        if requirement not in result:
            result.append(requirement)
    return result


def _split_requirements(text: str) -> list[str]:
    # Extras and quoted markers may contain commas; version continuations such
    # as "numpy>=1.25,<3" belong to the preceding requirement.
    parts, start, depth, quote = [], 0, 0, ''
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ''
        elif char in "\"'":
            quote = char
        elif char == '[':
            depth += 1
        elif char == ']':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    merged: list[str] = []
    for part in parts:
        if not part:
            raise ValueError('Empty dependency in comma-separated string')
        if part[0] in '<>=!~' and merged:
            merged[-1] += ',' + part
        else:
            merged.append(part)
    return merged


def _uv_header(text: str) -> dict[str, Any]:
    blocks = re.findall(r'(?m)^# /// script\s*\r?\n(.*?)^# ///\s*$', text, re.DOTALL)
    if len(blocks) != 1:
        raise ValueError('Expected exactly one complete uv # /// script metadata block')
    lines = blocks[0].splitlines()
    if any(not line.startswith('#') for line in lines):
        raise ValueError('Every uv header metadata line must start with #')
    content = '\n'.join(line[2:] if line.startswith('# ') else line[1:] for line in lines)
    return tomlkit.parse(content).unwrap()


@dataclass(frozen=True)
class DependencySpec:
    project: str
    wheel_name: str = ''
    wheel: bytes = b''
    venv: Bundle | None = field(default=None, repr=False, compare=False)
    files: bytes = field(default=b'', repr=False)
    setup: SetupPayload | None = None
    workdir: str | None = None
    origin: str | None = field(default=None, repr=False, compare=False)

    def prepared(self) -> DependencySpec:
        if self.wheel:
            return self
        root = Path(__file__).resolve().parents[2]
        if not (root / 'pyproject.toml').is_file():
            raise RuntimeError('Building the worker framework requires the recursiverpc source checkout')
        with tempfile.TemporaryDirectory(prefix='recursiverpc-wheel-') as output:
            _run_uv(['build', str(root), '--wheel', '--out-dir', output])
            wheels = list(Path(output).glob('recursiverpc-*.whl'))
            if len(wheels) != 1:
                raise RuntimeError('Expected one recursiverpc framework wheel')
            return replace(self, wheel_name=wheels[0].name, wheel=wheels[0].read_bytes())

    def payload(self):
        return self.project, self.wheel_name, self.wheel, self.venv, self.files, self.setup, self.workdir

    def configured(self, *, copy_local_dependencies: bool = False,
                   copy_project: bool | str | os.PathLike[str] = False,
                   setup_script: SetupScript | str | os.PathLike[str] | None = None) -> DependencySpec:
        project, files, workdir = self.project, self.files, self.workdir
        if copy_local_dependencies or copy_project:
            project, files, workdir = bundle_sources(project, self.origin, copy_local_dependencies, copy_project)
        setup = parse_setup(setup_script) if setup_script is not None else self.setup
        return replace(self, project=project, files=files, workdir=workdir, setup=setup, venv=None)

    def environment_bundle(self) -> Bundle:
        if self.venv is None:
            project = LocalProject(self, run_setup=False)
            try:
                bundle = archive_environment(project.path / '.venv', project_root=project.path)
            finally:
                project.close()
            # This is a transport cache; the dependency specification is unchanged.
            object.__setattr__(self, 'venv', bundle)
        assert self.venv is not None
        return self.venv

    def project_text(self) -> str:
        if not self.wheel or Path(self.wheel_name).name != self.wheel_name:
            raise ValueError('A prepared framework wheel is required')
        document = tomlkit.parse(self.project)
        project = document['project']
        framework_python = metadata.metadata('recursiverpc')['Requires-Python'] or '>=3.10'
        project['requires-python'] = str(
            SpecifierSet(project.get('requires-python', '>=3.10')) & SpecifierSet(framework_python)
        )
        dependencies = list(project.get('dependencies', []))
        if not any(canonicalize_name(Requirement(item).name) == 'recursiverpc' for item in dependencies):
            dependencies.append('recursiverpc')
        project['dependencies'] = dependencies
        tool = document.setdefault('tool', {})
        uv = tool.setdefault('uv', {})
        uv['package'] = False
        uv.setdefault('sources', {})['recursiverpc'] = {'path': self.wheel_name}
        return tomlkit.dumps(document)

    def install_remote(self, remote, directory: str) -> str | None:
        if self.files:
            remote.write_file(directory, 'local-sources.zip', self.files)
            code = ('import io,zipfile\nfrom pathlib import Path\n' + inspect.getsource(extract_sources)
                    + '\nextract_sources(' + repr(directory) + ',Path(' + repr(directory) + ").joinpath('local-sources.zip').read_bytes())")
            remote.python_run(code)
        remote.write_file(directory, self.wheel_name, self.wheel)
        project = self.project_text()
        remote.write_file(directory, 'pyproject.toml', project.encode('utf-8'))
        constraint = tomlkit.parse(project)['project']['requires-python']
        copied = try_copy_environment(remote, directory, self.environment_bundle, constraint)
        if copied is not None:
            self.finish_remote(remote, directory, copied)
            return copied
        environment = directory.rstrip('/\\') + '/.venv'
        uv = remote.command('uv').with_env(UV_PROJECT_ENVIRONMENT=environment, VIRTUAL_ENV=None)
        uv('sync', '--project', directory, '--no-dev', '--no-install-project',
           '--python', f'{sys.version_info.major}.{sys.version_info.minor}')
        if self.setup is not None or self.workdir is not None:
            executable = json_executable(remote, directory)
            self.finish_remote(remote, directory, executable)
        return None

    def finish_remote(self, remote, directory: str, executable: str):
        if self.setup is None and self.workdir is None:
            return
        python = remote.command(executable)
        if self.workdir is not None:
            content = ('from pathlib import Path\n' + inspect.getsource(expose_project)
                       + '\nexpose_project(' + repr(directory) + ',' + repr(self.workdir) + ')')
            path = remote.write_file(directory, 'expose_project.py', content.encode('utf-8'))
            python('-I', path)
        if self.setup is not None:
            path = remote.write_file(directory, 'setup_driver.py', setup_code(self.setup, directory, self.workdir).encode('utf-8'))
            python('-I', path)


def parse_dependency(value: DependencyInput | DependencySpec) -> DependencySpec:
    if isinstance(value, DependencySpec):
        return value
    origin = None
    data: dict[str, Any]
    if isinstance(value, os.PathLike):
        origin = Path(cast(os.PathLike[str], value)).resolve()
        text = origin.read_text(encoding='utf-8')
    elif isinstance(value, str):
        text = value.strip()
        if '\n' not in text and len(text) < 1024 and text.lower().endswith(('.toml', '.py')):
            origin = Path(text).resolve()
            text = origin.read_text(encoding='utf-8')
    else:
        text = ''
    if isinstance(value, (str, os.PathLike)):
        if '# /// script' in text:
            header = _uv_header(text)
            data = {'project': {
                'dependencies': header.get('dependencies', []),
                'requires-python': header.get('requires-python', '>=3.10'),
            }, 'tool': header.get('tool', {})}
        elif origin is not None or re.search(r'(?m)^\s*\[project\]', text):
            data = tomlkit.parse(text).unwrap()
        else:
            data = {'project': {'dependencies': _split_requirements(text)}}
    else:
        data = {'project': {'dependencies': _requirements(value)}}
    project = data.get('project')
    if not isinstance(project, dict):
        raise ValueError('pyproject.toml must contain a [project] table')
    if 'dependencies' in project.get('dynamic', []):
        raise ValueError('Dynamic project dependencies cannot be used without building that project')
    requirements = project.get('dependencies', [])
    if not isinstance(requirements, list) or not all(isinstance(item, str) for item in requirements):
        raise ValueError('dependencies must be a list of requirement strings')
    project['dependencies'] = _requirements(requirements)
    project['name'] = 'recursiverpc-worker'
    project['version'] = '0.0.0'
    project.setdefault('requires-python', '>=3.10')
    project.pop('dynamic', None)
    # Install the declared runtime dependencies, not the caller's source tree.
    data.pop('build-system', None)
    data.pop('dependency-groups', None)
    uv = data.setdefault('tool', {}).setdefault('uv', {})
    uv.pop('workspace', None)
    if origin is not None:
        for source in uv.get('sources', {}).values():
            entries = source if isinstance(source, list) else [source]
            for entry in entries:
                if isinstance(entry, dict) and 'path' in entry:
                    entry['path'] = str((origin.parent / entry['path']).resolve())
    return DependencySpec(tomlkit.dumps(data), origin=str(origin.parent) if origin else None)


def json_executable(remote, directory: str) -> str:
    import json
    return json.loads(remote.python_run(
        'import pathlib,os,json; print(json.dumps(str(pathlib.Path(' + repr(directory)
        + ")/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python'))))"
    ))


def _run_uv(arguments: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run([os.fsdecode(find_uv_bin()), *arguments],
                              capture_output=True, text=True, check=True, **kwargs)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f'uv failed to prepare worker dependencies:\n{error.stderr}') from error


class LocalProject:
    def __init__(self, dependency: DependencySpec, run_setup: bool = True):
        self.temporary = tempfile.TemporaryDirectory(prefix='recursiverpc-project-')
        self.path = Path(self.temporary.name)
        self.processes = []
        self.workdir = dependency.workdir
        try:
            if dependency.files:
                extract_sources(str(self.path), dependency.files)
            (self.path / dependency.wheel_name).write_bytes(dependency.wheel)
            (self.path / 'pyproject.toml').write_text(dependency.project_text(), encoding='utf-8')
            environment = os.environ.copy()
            environment.pop('VIRTUAL_ENV', None)
            environment['UV_PROJECT_ENVIRONMENT'] = str(self.path / '.venv')
            _run_uv(['sync', '--project', str(self.path), '--no-dev', '--no-install-project',
                     '--python', sys.executable], env=environment)
            executable = self.path / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            if dependency.workdir is not None:
                code = ('from pathlib import Path\n' + inspect.getsource(expose_project)
                        + '\nexpose_project(' + repr(str(self.path)) + ',' + repr(dependency.workdir) + ')')
                (self.path / 'expose_project.py').write_text(code, encoding='utf-8')
                subprocess.run([str(executable), '-I', str(self.path / 'expose_project.py')], check=True)
            if dependency.setup is not None and run_setup:
                script = self.path / 'setup_driver.py'
                script.write_text(setup_code(dependency.setup, str(self.path), dependency.workdir), encoding='utf-8')
                result = subprocess.run([str(executable), '-I', str(script)], capture_output=True,
                                        text=True, encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
                if result.returncode:
                    raise RuntimeError(f'Worker setup script failed:\n{result.stdout}\n{result.stderr}')
        except BaseException:
            self.close()
            raise

    def connect(self):
        from rpyc.core.service import ClassicService
        from rpyc.utils.factory import connect_subproc
        executable = self.path / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        connection = connect_subproc([
            str(executable), '-I', '-u', '-c',
            'import os; os.chdir(' + repr(str(self.path / self.workdir if self.workdir else self.path))
            + '); from rpyc.cli.rpyc_classic import main; main()', '-q', '-m', 'stdio',
        ], service=ClassicService, config={'close_catchall': True})
        self.processes.append(connection.proc)
        return connection

    def close(self):
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
        self.processes.clear()
        self.temporary.cleanup()
