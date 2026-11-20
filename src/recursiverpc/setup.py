"""User setup hooks run after dependencies, before RPC workers start."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import os
from pathlib import Path

SetupPayload = tuple[str, str]


@dataclass(frozen=True)
class SetupScript:
    content: str
    language: str = 'python'


def parse_setup(value: SetupScript | str | os.PathLike[str]) -> SetupPayload:
    if isinstance(value, SetupScript):
        content, language = value.content, value.language
    else:
        suffixes = {'.py': 'python', '.sh': 'bash', '.ps1': 'powershell', '.cmd': 'cmd', '.bat': 'cmd'}
        if isinstance(value, os.PathLike) or ('\n' not in value and Path(value).suffix.lower() in suffixes):
            path = Path(value)
            if path.suffix.lower() not in suffixes:
                raise ValueError('Setup script must be .py, .sh, .ps1, .cmd, or .bat')
            language = suffixes[path.suffix.lower()]
            content = path.read_text(encoding='utf-8-sig')
        else:
            content, language = str(value), 'python'
    if language not in ('python', 'bash', 'powershell', 'cmd'):
        raise ValueError('Setup language must be python, bash, powershell, or cmd')
    return language, content


def setup_driver(language, content, directory, workdir):
    import os
    from pathlib import Path
    import runpy
    import shutil
    import subprocess
    import sys
    root = Path(directory).resolve()
    assert root.name.startswith('recursiverpc-')
    cwd = (root / workdir).resolve() if workdir else root
    assert cwd.is_relative_to(root)
    os.chdir(cwd)
    environment = Path(sys.executable).parent.parent
    os.environ['VIRTUAL_ENV'] = str(environment)
    os.environ['PATH'] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', '')
    os.environ['RECURSIVERPC_PROJECT_DIR'] = str(root)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if callable(reconfigure):
            reconfigure(encoding='utf-8')
    suffix = {'python': '.py', 'bash': '.sh', 'powershell': '.ps1', 'cmd': '.cmd'}[language]
    path = root / ('worker_setup' + suffix)
    encoding = 'utf-8-sig' if language == 'powershell' else 'utf-8'
    path.write_bytes(content.replace('\r\n', '\n').encode(encoding))
    if language == 'python':
        sys.argv = [str(path)]
        runpy.run_path(str(path), run_name='__main__')
    else:
        if language == 'bash':
            executable = shutil.which('bash')
            git = Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Git/bin/bash.exe'
            if os.name == 'nt' and git.is_file():
                executable = str(git)
            if executable is None:
                raise RuntimeError('Bash is not installed for the setup script')
            command = [executable, str(path)]
        elif language == 'powershell':
            executable = shutil.which('powershell') or shutil.which('pwsh')
            if executable is None:
                raise RuntimeError('PowerShell is not installed for the setup script')
            command = [executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(path)]
        else:
            if os.name != 'nt':
                raise RuntimeError('cmd setup scripts require Windows')
            command = 'cmd.exe /d /s /c ""' + str(path) + '""'
        subprocess.run(command, check=True)


def setup_code(payload: SetupPayload, directory: str, workdir: str | None) -> str:
    return inspect.getsource(setup_driver) + '\nsetup_driver(' + ','.join(
        repr(value) for value in (*payload, directory, workdir)
    ) + ')\n'
