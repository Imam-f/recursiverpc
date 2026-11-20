$env:Path = "C:\Users\User\.local\bin;$env:Path

uv init recursiverpc --lib
uv add multiprocess dill rpyc
uv venv
uv sync
uv pip install -e .
.\.venv\Scripts\activate.ps1
uv run --script .\rpyc_classic.py
uv run .\test.py
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Program Files\Git\bin\bash.exe" -PropertyType String -Force

dependency
    uv
    rpyc
    paramiko
    plumbum
    ssh
    dill

## Worker dependencies

`Recursive_RPC(..., dependency=...)` creates isolated temporary uv projects for
local workers, SSH workers, and proxy gateways/children. Packages are installed
before work starts. The caller's environment is unchanged, and temporary projects
are removed when the pool closes. Keep the pool in a `with` block for cleanup.

```python
from pathlib import Path
from recursiverpc import Recursive_RPC, localprocess

def task(value):
    import humanize
    return humanize.intcomma(value)

if __name__ == "__main__":
    with Recursive_RPC(
        [localprocess(2)],
        dependency=["humanize==4.11.0"],
    ) as pool:
        print(pool.apply(task, 1234567))
```

The `dependency` argument accepts:

```python
dependency = ["numpy>=1.25,<3", "requests"]  # package requirement strings
dependency = "numpy>=1.25,<3, requests"      # comma-separated requirements
dependency = Path("pyproject.toml")         # also accepts a string file path
dependency = """# /// script
# requires-python = ">=3.10"
# dependencies = ["humanize==4.11.0"]
# ///
"""
```

Lists may also contain imported module objects; their installed distribution
names are resolved automatically, and standard-library module objects are skipped.
For imports whose package name differs from the import name, use the distribution
name (for example `Pillow`) or the imported module object (`PIL`). A `.py` file
containing a uv header is also accepted.

For a pyproject input, runtime `[project].dependencies`, Python constraints, and
uv sources/index configuration are used. The caller's project itself and dev
dependency groups are not installed. Relative source paths are resolved relative
to that pyproject; external local sources must also be accessible on remote hosts.
The worker's Python requirement must be compatible with this library's supported
Python version. Dependency-resolution or installation errors stop pool creation.

The recursiverpc framework is bundled into each explicit dependency project as a
wheel, so SSH workers do not need the framework checkout at the caller's path.
The initial caller currently needs the source checkout to build that wheel.
Omitting `dependency` retains the existing behavior; `dependency=[]` requests an
isolated project containing only recursiverpc and its runtime dependencies.
Existing RPC `(host, port)` connections cannot be reconfigured with dependencies;
use a managed `SSHConnection` worker to create a new environment.

## SSH login shells

Deployment supports Linux Bash, Windows Git Bash, Windows PowerShell, and Windows
cmd. It detects the login shell and uses remote Python for temporary directories,
UTF-8 script writes, executable lookup, and cleanup. Remote Python 3.10+ must be
available through `python3`, `python`, `py -3`, or `uv run --no-project python`.
`uv` must also be installed for `NetworkRunner`'s dependency setup.

Use `SSHConnection` for native Windows shells. Unlike Plumbum's SSH machine
initialization, it does not require a POSIX login shell:

```python
from recursiverpc import SSHConnection, Recursive_RPC, networkprocess

connection = SSHConnection(
    host="worker.example.com",
    user="worker",
    port=22,
    key_filename="C:/Users/User/.ssh/id_rsa",  # or a Linux key path
    shell="auto",  # optional override: "bash", "powershell", or "cmd"
)

def square(value):
    return value * value

if __name__ == "__main__":
    with Recursive_RPC([networkprocess(2, connection)], conn={}) as pool:
        result = pool.apply(square, 5)
```

The server's host key is checked against `known_hosts`. Existing `SshMachine` and
`ParamikoMachine` connections remain accepted; their own initialization may
require Bash. Git Bash and Linux Bash both use the `bash` override.

Shell support does not copy your project or its local dependencies to another
computer. The existing editable dependency paths must be accessible remotely.
Windows command arguments are forwarded through Python's subprocess API to
preserve quotes and shell-sensitive characters without another shell parse.

### Adapting an existing Plumbum connection

```python
from recursiverpc import shell_aware

remote = shell_aware(machine, shell="auto")  # an existing SshMachine/ParamikoMachine
uv = remote["uv"]
print(uv["--version"]())
```

The adapter returns standard Plumbum `RemoteCommand` and `BoundCommand` objects.
Argument binding, `.run(retcode=..., timeout=...)`, `.with_env(...)`, and
`.with_cwd(...)` use Plumbum's command APIs. Only command discovery and shell
execution are adapted for this connection; no Plumbum classes or other SSH
connections are modified. Reapplying `shell_aware()` returns the same adapter.
The original connection remains available as `remote.machine`, including its
file-transfer and tunneling APIs.

Native Windows connections should still start with `SSHConnection`, since an
ordinary Plumbum connection may run POSIX probes before it can be adapted.
`SSHConnection.connect()` returns the same adapter type, `ShellAwareMachine`.
The earlier `RemoteShell` name remains an alias for compatibility.

### Proxy gateways

`proxyprocess` can group workers locally (`host=None`) or deploy that group on an
SSH gateway. Gateway setup now uses the same `NetworkRunner` and shell adapter
as ordinary SSH workers; RPC ports are allocated automatically.

```python
from recursiverpc import localprocess, proxyprocess

gateway = proxyprocess(
    1, "gateway.example.com", 22,
    [localprocess(2)],
    ("worker", None),  # SSH user/password; None allows key authentication
    {},
)
```

The proxy's first number is retained for compatibility; its capacity is the sum
of its child workers. Child network descriptors use
`networkprocess(number, SSHConnection(...))` or an existing RPC `(host, port)`
tuple. SSH hostnames, key paths, and known_hosts for these children are resolved
on the gateway. Live local SSH machine objects cannot be copied to a gateway.
Local proxy groups can also contain nested proxy groups.

Run the isolated regression tests (without SSH credentials):

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s test -p "test_cross_shell.py" -v
```

On Windows, the Linux Bash tests use the `Ubuntu-22.04` WSL distribution. The
tests launch a small bootstrap, verify script bytes and shell detection, and
check startup and cleanup; they do not run the distributed workload.
