"""Copy compatible virtual environments without resolving packages remotely."""

from __future__ import annotations

from .artifacts import Artifact, upload_artifact
import inspect
import hashlib
from functools import lru_cache
import io
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable
import zipfile

from packaging.specifiers import SpecifierSet

log = logging.getLogger(__name__)
Bundle = tuple[str, Artifact | bytes]


def runtime_info():
    import platform
    import struct
    import sys
    import sysconfig
    arch = platform.machine().lower()
    arch = {'amd64': 'x86_64', 'x64': 'x86_64', 'aarch64': 'arm64'}.get(arch, arch)
    return {
        'os': platform.system().lower(), 'arch': arch,
        'bits': struct.calcsize('P') * 8, 'implementation': platform.python_implementation(),
        'version': platform.python_version(), 'abi': sysconfig.get_config_var('SOABI'),
        'libc': list(platform.libc_ver()), 'prefix': sys.prefix,
        'executable': sys.executable, 'site': sysconfig.get_path('purelib'),
    }


RUNTIME_SCRIPT = 'import json\n' + inspect.getsource(runtime_info) + '\nprint(json.dumps(runtime_info()))'


def compatible(source: dict, target: dict, python_constraint: str = '') -> bool:
    for field in ('os', 'arch', 'bits', 'implementation', 'abi'):
        if source[field] != target[field]:
            return False
    if source['version'].split('.')[:2] != target['version'].split('.')[:2]:
        return False
    if python_constraint and not SpecifierSet(python_constraint).contains(target['version']):
        return False
    if source['os'] == 'linux':
        source_libc, source_version = source['libc']
        target_libc, target_version = target['libc']
        if not source_libc or source_libc != target_libc:
            return False
        def version(value):
            return tuple(int(part) for part in value.split('.') if part.isdigit())
        if version(target_version) < version(source_version):
            return False
    return True


def archive_environment(prefix: Path, framework: tuple[str, bytes] | None = None,
                        project_root: Path | None = None) -> Bundle:
    executable = prefix / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    result = subprocess.run([str(executable), '-I', '-c', RUNTIME_SCRIPT],
                            check=True, capture_output=True, text=True)
    info = json.loads(result.stdout)
    site = Path(info['site'])
    info['site_relative'] = site.relative_to(prefix).as_posix()
    if project_root is not None:
        info['project_prefix'] = str(project_root)
    versions = subprocess.run([
        str(executable), '-I', '-c',
        'import importlib.metadata as m,json; print(json.dumps({d.metadata["Name"]:d.version for d in m.distributions()}))',
    ], check=True, capture_output=True, text=True)
    info['packages'] = json.loads(versions.stdout)
    stream = Artifact()
    with zipfile.ZipFile(stream.path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        for directory in (site, prefix / ('Scripts' if os.name == 'nt' else 'bin')):
            for path in directory.rglob('*'):
                if path.is_symlink() and path.is_dir():
                    raise ValueError('Cannot copy an environment with external directory links')
                if not path.is_file() or '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
                    continue
                relative_site = path.relative_to(site) if path.is_relative_to(site) else None
                if framework and relative_site is not None:
                    first = relative_site.parts[0]
                    if first == 'recursiverpc' or (first.startswith('recursiverpc-') and first.endswith('.dist-info')):
                        continue
                    if path.suffix == '.pth' and 'recursiverpc' in path.name:
                        continue
                if path.parent == directory and directory != site:
                    name = path.name.lower()
                    if name.startswith(('activate', 'deactivate')) or re.fullmatch(r'(?:python|pypy)(?:\d(?:\.\d+)?)?w?(?:\.exe)?', name):
                        continue
                if path.suffix in ('.pth', '.egg-link'):
                    content = path.read_text(encoding='utf-8')
                    if (path.name.startswith('__editable__') or path.suffix == '.egg-link') and project_root is None:
                        raise ValueError('Editable dependencies require normal installation')
                    for line in content.splitlines():
                        if os.path.isabs(line) and not Path(line).is_relative_to(project_root or prefix):
                            raise ValueError('External .pth paths cannot be relocated')
                archive.write(path, '.venv/' + path.relative_to(prefix).as_posix())
        if framework:
            with zipfile.ZipFile(io.BytesIO(framework[1])) as wheel:
                for entry in wheel.infolist():
                    if not entry.is_dir():
                        archive.writestr('.venv/' + info['site_relative'] + '/' + entry.filename, wheel.read(entry))
    return json.dumps(info), stream


def current_environment_bundle() -> Bundle:
    # Refresh when installed files or editable framework sources change.
    digest = hashlib.sha256(json.dumps(runtime_info(), sort_keys=True).encode())
    site = Path(runtime_info()['site'])
    for path in sorted(site.rglob('*')):
        if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc', '.pyo'):
            stat = path.stat()
            digest.update(str((str(path), stat.st_size, stat.st_mtime_ns)).encode())
    for path in sorted(Path(__file__).parent.rglob('*.py')):
        digest.update(path.read_bytes())
    return _current_environment_bundle(digest.hexdigest())


@lru_cache(maxsize=2)
def _current_environment_bundle(identity: str) -> Bundle:
    if sys.prefix == sys.base_prefix:
        raise ValueError('The controller is not running in a virtual environment')
    # Editable recursiverpc code lives outside the venv; include a real wheel.
    framework = None
    if (Path(__file__).resolve().parents[2] / 'pyproject.toml').is_file():
        from .dependencies import parse_dependency
        dependency = parse_dependency([]).prepared()
        framework = dependency.wheel_name, dependency.wheel
    return archive_environment(Path(sys.prefix), framework)


def relocate_script(content: bytes, before: str, after: str) -> bytes:
    import io
    import zipfile
    pairs = ((before, after), (before.replace('\\', '/'), after.replace('\\', '/')))
    def rewrite(value):
        for source, target in pairs:
            value = value.replace(source.encode('utf-8'), target.encode('utf-8'))
        return value
    if content.endswith(b'UVSC'):
        size = int.from_bytes(content[-8:-4], 'little')
        offset = len(content) - 8 - size
        if offset < 0 or size == 0:
            raise ValueError('Invalid uv console launcher')
        executable = rewrite(content[offset:-8])
        with zipfile.ZipFile(io.BytesIO(content[:offset])) as original:
            stub = content[:min(entry.header_offset for entry in original.infolist())]
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w') as archive:
                for entry in original.infolist():
                    data = original.read(entry)
                    if entry.filename == '__main__.py':
                        data = rewrite(data)
                    archive.writestr(entry, data)
        return stub + stream.getvalue() + executable + len(executable).to_bytes(4, 'little') + b'UVSC'
    # distlib launchers have a shebang before the appended zip, while POSIX
    # console scripts begin with the shebang directly.
    marker, archive = content.find(b'#!'), content.find(b'PK\x03\x04')
    if marker >= 0 and (archive < 0 or marker < archive):
        end = content.find(b'\n', marker)
        if end >= 0:
            return content[:marker] + rewrite(content[marker:end]) + content[end:]
    return content


RESTORE_SCRIPT = inspect.getsource(relocate_script) + '\n' + '''
import json,pathlib,zipfile,os
root=pathlib.Path(DIRECTORY).resolve()
assert root.name.startswith('recursiverpc-')
venv=root/'.venv'
data=json.loads((root/'.venv-copy.json').read_text(encoding='utf-8'))
with zipfile.ZipFile(root/'.venv-copy.zip') as archive:
    for entry in archive.infolist():
        path=(root/entry.filename).resolve()
        assert path.is_relative_to(venv.resolve()), 'Invalid environment archive path'
        assert not entry.is_dir(), 'Unexpected directory entry'
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.parent.name not in ('Scripts','bin') and path.suffix not in ('.pth','.egg-link') and not path.name.startswith('__editable__'):
            import shutil
            with archive.open(entry) as incoming, path.open('wb') as outgoing:
                shutil.copyfileobj(incoming,outgoing,1024*1024)
            mode=(entry.external_attr>>16)&0o777
            if mode: path.chmod(mode)
            continue
        content=archive.read(entry)
        if data.get('project_prefix') and (path.suffix in ('.pth','.egg-link') or path.name.startswith('__editable__')):
            before=data['project_prefix']
            after=str(root)
            for old,new in ((before,after),(before.replace('\\\\','/'),after.replace('\\\\','/')),
                            (before.replace('\\\\','\\\\\\\\'),after.replace('\\\\','\\\\\\\\'))):
                content=content.replace(old.encode('utf-8'),new.encode('utf-8'))
        if path.parent.name in ('Scripts','bin'):
            content=relocate_script(content,data['prefix'],str(venv))
        path.write_bytes(content)
        mode=(entry.external_attr>>16)&0o777
        if mode: path.chmod(mode)
(root/'.venv-copy.zip').unlink()
print(json.dumps(str(venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python'))))
'''


def _reset_remote_venv(remote, directory: str):
    remote.python_run(
        'import pathlib,shutil; root=pathlib.Path(' + repr(directory) + ').resolve(); '
        "assert root.name.startswith('recursiverpc-'); target=root/'.venv'; "
        'assert target.resolve().parent==root; '
        'shutil.rmtree(target) if target.exists() else None; '
        "[(root/name).unlink(missing_ok=True) for name in ('.venv-copy.zip','.venv-copy.json')]"
    )


def try_copy_environment(remote, directory: str, bundle_factory: Callable[[], Bundle],
                         python_constraint: str = '') -> str | None:
    """Return a relocated Python path, or None to use normal uv installation."""
    touched = False
    try:
        source = runtime_info()
        target = json.loads(remote.python_run(RUNTIME_SCRIPT))
        if any(source[key] != target[key] for key in ('os', 'arch', 'bits')):
            return None
        interpreter = target['executable']
        if not compatible(source, target, python_constraint):
            version = '.'.join(source['version'].split('.')[:2])
            interpreter = remote.command('uv')('python', 'find', '--no-python-downloads', version).strip()
            target = json.loads(remote.command(interpreter)('-I', '-c', RUNTIME_SCRIPT))
            if not compatible(source, target, python_constraint):
                return None
        profile, archive = bundle_factory()
        data = json.loads(profile)
        if not compatible(data, target, python_constraint):
            return None
        environment = directory.rstrip('/\\') + '/.venv'
        touched = True
        remote.command('uv')('venv', environment, '--python', interpreter,
                             '--relocatable', '--no-python-downloads')
        remote.write_file(directory, '.venv-copy.json', profile.encode('utf-8'))
        upload_artifact(remote, directory, '.venv-copy.zip', archive)
        executable = json.loads(remote.python_run(RESTORE_SCRIPT.replace('DIRECTORY', repr(directory))))
        verification = (
            'import json,importlib.metadata as m,pathlib; '
            'import recursiverpc,rpyc,paramiko; '
            'from packaging.tags import parse_tag,sys_tags; supported=set(sys_tags()); '
            'data=json.loads(pathlib.Path(' + repr(directory) + ").joinpath('.venv-copy.json').read_text(encoding='utf-8')); "
            'assert all(m.version(name)==version for name,version in data["packages"].items()); '
            'tags=[[line[5:].strip() for line in (m.distribution(name).read_text("WHEEL") or "").splitlines() '
            'if line.startswith("Tag:")] for name in data["packages"]]; '
            'assert all(not group or any(supported.intersection(parse_tag(tag)) for tag in group) for group in tags); '
            'print("OK")'
        )
        if remote.command(executable)('-I', '-c', verification).strip() != 'OK':
            raise RuntimeError('Copied environment did not pass validation')
        log.info('Copied compatible venv to %s', directory)
        return executable
    except Exception as error:
        log.info('Venv copy unavailable; using uv installation: %s', error)
        if touched:
            _reset_remote_venv(remote, directory)
        return None
