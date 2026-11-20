"""Bundle local project dependencies without controller-only absolute paths."""

from __future__ import annotations

from importlib import metadata
import io
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse
from urllib.request import url2pathname
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import tomlkit


def _file_url(value: str) -> Path | None:
    url = urlparse(value)
    if url.scheme != 'file':
        return None
    return Path(url2pathname(('//' + url.netloc if url.netloc not in ('', 'localhost') else '') + url.path))


def _workspace_source(origin: Path, name: str) -> Path:
    for root in (origin, *origin.parents):
        project = root / 'pyproject.toml'
        if not project.is_file():
            continue
        data = tomlkit.parse(project.read_text(encoding='utf-8')).unwrap()
        workspace = data.get('tool', {}).get('uv', {}).get('workspace')
        if not workspace:
            continue
        matches = []
        for pattern in workspace.get('members', []):
            for directory in root.glob(pattern):
                if any(directory.relative_to(root).match(excluded) for excluded in workspace.get('exclude', [])):
                    continue
                config = directory / 'pyproject.toml'
                if config.is_file():
                    child = tomlkit.parse(config.read_text(encoding='utf-8')).unwrap()
                    if canonicalize_name(child.get('project', {}).get('name', '')) == canonicalize_name(name):
                        matches.append(directory)
        if len(matches) == 1:
            return matches[0]
    raise ValueError(f'Cannot resolve uv workspace dependency {name!r}')


class SourceBundle:
    def __init__(self, follow_dependencies: bool = True):
        self.destinations: dict[Path, str] = {}
        self.files: dict[str, tuple[bytes, int]] = {}
        self.sources: dict[str, dict] = {}
        self.follow_dependencies = follow_dependencies

    def add(self, source: Path, name: str) -> str:
        source = source.resolve()
        if source in self.destinations:
            return self.destinations[source]
        if not source.exists():
            raise FileNotFoundError(f'Local dependency source does not exist: {source}')
        target = f'local/{len(self.destinations):03d}-{canonicalize_name(name)}'
        if source.is_file():
            target += '/' + source.name
        self.destinations[source] = target
        if source.is_file():
            self.files[target] = source.read_bytes(), source.stat().st_mode
            return target
        try:
            tracked = subprocess.run(['git', '-C', str(source), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                                     capture_output=True, check=True)
            candidates = [source / os.fsdecode(name) for name in tracked.stdout.split(b'\0') if name]
        except (OSError, subprocess.CalledProcessError):
            candidates = list(source.rglob('*'))
        for path in candidates:
            relative = path.relative_to(source)
            if (any(part in ('.git', '.venv', 'venv', '__pycache__') or part.endswith('.egg-info') for part in relative.parts)
                    or relative.parts[0] in ('dist', 'build')
                    or path.name.startswith('.env') or path.suffix in ('.pyc', '.pyo')):
                continue
            if path.is_symlink() and not path.resolve().is_relative_to(source):
                raise ValueError(f'External source symlink cannot be bundled: {path}')
            if path.is_file():
                self.files[target + '/' + relative.as_posix()] = path.read_bytes(), path.stat().st_mode
        project = source / 'pyproject.toml'
        if project.is_file() and self.follow_dependencies:
            data = tomlkit.parse(project.read_text(encoding='utf-8')).unwrap()
            self.rewrite(data, source, target)
            self.files[target + '/pyproject.toml'] = tomlkit.dumps(data).encode('utf-8'), 0o644
        return target

    def rewrite(self, data: dict, origin: Path, target: str = ''):
        uv = data.setdefault('tool', {}).setdefault('uv', {})
        sources = uv.setdefault('sources', {})
        project = data.get('project', {})
        groups = [project.get('dependencies', []), *project.get('optional-dependencies', {}).values()]
        for dependencies in groups:
            for index, text in enumerate(dependencies):
                requirement = Requirement(text)
                local = _file_url(requirement.url) if requirement.url else None
                if local is None and requirement.name not in sources:
                    try:
                        direct = metadata.distribution(requirement.name).read_text('direct_url.json')
                        if direct:
                            local = _file_url(json.loads(direct)['url'])
                    except metadata.PackageNotFoundError:
                        pass
                if local is not None:
                    sources[requirement.name] = {'path': str(local)}
                    extras = '[' + ','.join(sorted(requirement.extras)) + ']' if requirement.extras else ''
                    marker = '; ' + str(requirement.marker) if requirement.marker else ''
                    dependencies[index] = requirement.name + extras + str(requirement.specifier) + marker
        for name, configuration in list(sources.items()):
            entries = configuration if isinstance(configuration, list) else [configuration]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get('workspace'):
                    entry['path'] = str(_workspace_source(origin, name))
                    entry.pop('workspace')
                if 'path' not in entry:
                    continue
                path = Path(entry['path'])
                if not path.is_absolute():
                    path = origin / path
                destination = self.add(path, name)
                self.sources.setdefault(name, dict(entry, path=destination))
                entry['path'] = os.path.relpath(destination, target or '.').replace('\\', '/')
        uv.pop('workspace', None)

    def archive(self) -> bytes:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, (content, mode) in self.files.items():
                entry = zipfile.ZipInfo(name)
                entry.external_attr = (mode & 0o777) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, content)
        return stream.getvalue()


def bundle_sources(project: str, origin: str | None, copy_dependencies: bool,
                   copy_project: bool | str | os.PathLike[str]) -> tuple[str, bytes, str | None]:
    data = tomlkit.parse(project).unwrap()
    root = Path(origin) if origin else Path.cwd()
    bundle = SourceBundle(follow_dependencies=copy_dependencies)
    if copy_dependencies:
        bundle.rewrite(data, root)
    workdir = None
    if copy_project:
        path = root if copy_project is True else Path(copy_project).resolve()
        if not path.is_dir():
            raise NotADirectoryError(f'copy_project requires a project directory: {path}')
        original = tomlkit.parse((path / 'pyproject.toml').read_text(encoding='utf-8')).unwrap() if (path / 'pyproject.toml').exists() else {}
        name = original.get('project', {}).get('name', 'project')
        workdir = bundle.add(path, name)
        package = original.get('tool', {}).get('uv', {}).get('package')
        buildable = package is True or (package is not False and 'build-system' in original)
        if name != 'recursiverpc' and buildable:
            dependencies = data['project'].setdefault('dependencies', [])
            if not any(canonicalize_name(Requirement(item).name) == canonicalize_name(name) for item in dependencies):
                dependencies.append(name)
            data.setdefault('tool', {}).setdefault('uv', {}).setdefault('sources', {})[name] = {'path': workdir, 'editable': True}
    sources = data.setdefault('tool', {}).setdefault('uv', {}).setdefault('sources', {})
    for name, entry in bundle.sources.items():
        sources.setdefault(name, entry)
    return tomlkit.dumps(data), bundle.archive() if bundle.files else b'', workdir


def extract_sources(directory: str, archive: bytes):
    root = Path(directory).resolve()
    assert root.name.startswith('recursiverpc-')
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        for entry in source.infolist():
            path = (root / entry.filename).resolve()
            if not path.is_relative_to(root / 'local') or entry.is_dir():
                raise ValueError('Invalid local source archive path')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(source.read(entry))
            mode = (entry.external_attr >> 16) & 0o777
            if mode:
                path.chmod(mode)


def expose_project(directory: str, workdir: str):
    import sysconfig
    root = Path(directory).resolve()
    project = (root / workdir).resolve()
    assert root.name.startswith('recursiverpc-') and project.is_relative_to(root / 'local')
    import_path = project / 'src' if (project / 'src').is_dir() else project
    target = Path(sysconfig.get_path('purelib')) / 'recursiverpc-local-project.pth'
    target.write_text(str(import_path) + '\n', encoding='utf-8')
