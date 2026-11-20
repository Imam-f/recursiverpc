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


class SourceBundle:
    def __init__(self):
        self.destinations: dict[Path, str] = {}
        self.files: dict[str, tuple[bytes, int]] = {}
        self.sources: dict[str, dict] = {}

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
            if (any(part in ('.git', '.venv', 'venv', '__pycache__') for part in relative.parts)
                    or relative.parts[0] in ('dist', 'build')
                    or path.name.startswith('.env') or path.suffix in ('.pyc', '.pyo')):
                continue
            if path.is_symlink() and not path.resolve().is_relative_to(source):
                raise ValueError(f'External source symlink cannot be bundled: {path}')
            if path.is_file():
                self.files[target + '/' + relative.as_posix()] = path.read_bytes(), path.stat().st_mode
        project = source / 'pyproject.toml'
        if project.is_file():
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
                if not isinstance(entry, dict) or 'path' not in entry:
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
    bundle = SourceBundle()
    if copy_dependencies:
        bundle.rewrite(data, root)
    workdir = None
    if copy_project:
        path = root if copy_project is True else Path(copy_project).resolve()
        original = tomlkit.parse((path / 'pyproject.toml').read_text(encoding='utf-8')).unwrap() if (path / 'pyproject.toml').exists() else {}
        name = original.get('project', {}).get('name', 'project')
        workdir = bundle.add(path, name)
        if name != 'recursiverpc' and (path / 'pyproject.toml').is_file() and original.get('tool', {}).get('uv', {}).get('package') is not False:
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
    import site
    root = Path(directory).resolve()
    project = (root / workdir).resolve()
    assert root.name.startswith('recursiverpc-') and project.is_relative_to(root / 'local')
    import_path = project / 'src' if (project / 'src').is_dir() else project
    target = Path(site.getsitepackages()[0]) / 'recursiverpc-local-project.pth'
    target.write_text(str(import_path) + '\n', encoding='utf-8')
