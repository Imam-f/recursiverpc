# RecursiveRPC

RecursiveRPC is a Python library for running functions across local processes
and remote machines over SSH. Built on RPyC, it provides synchronous and
asynchronous calls, parallel mapping, and nested proxy gateways through a shared
worker-pool interface.

Workers can use isolated temporary uv projects with declared dependencies,
bundled local source code, and custom setup scripts. Compatible SSH workers reuse
copied virtual environments to avoid downloading packages remotely. Deployment
supports Linux Bash, Windows Git Bash, PowerShell, and cmd.

## Examples

Start with [the small example scripts](examples/README.md): 24 runnable examples
cover local and asynchronous calls, mapping, errors, nested pools, dependency
formats, source copying, setup hooks, SSH workers, gateways, and remote commands.

```powershell
.\.venv\Scripts\python.exe examples/01_local_call.py
```

## Worker dependencies

`Recursive_RPC(..., dependency=...)` creates isolated temporary uv projects for
local workers, SSH workers, and proxy gateways/children. Packages are prepared
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
uv sources/index configuration are used. By default, the caller's project itself
and dev dependency groups are not installed. Relative source paths are resolved
relative to that pyproject; enable `copy_local_dependencies` to bundle them instead
of requiring those original paths to be accessible on remote hosts.
The worker's Python requirement must be compatible with this library's supported
Python version. Dependency-resolution or installation errors stop pool creation.

The recursiverpc framework is bundled into each explicit dependency project as a
wheel, so SSH workers do not need the framework checkout at the caller's path.
The initial caller currently needs the source checkout to build that wheel.
Omitting `dependency` retains the caller-environment dependency selection;
`dependency=[]` requests an isolated project containing only recursiverpc and its
runtime dependencies.
Existing RPC `(host, port)` connections cannot be reconfigured with dependencies;
use a managed `SSHConnection` worker to create a new environment.

### Local projects and setup scripts

```python
with Recursive_RPC(
    clients,
    dependency=Path("pyproject.toml"),
    copy_local_dependencies=True,
    copy_project=True,
    setup_script=Path("setup_worker.py"),
) as pool:
    result = pool.apply(task, 123)
```

`copy_local_dependencies=True` bundles local path sources, `file://` requirements,
installed editable dependencies, and uv workspace members. Nested local sources
are included recursively, and paths are rewritten for the temporary project.
This also allows compatible copied venvs to use editable packages after the
controller's original source directories have been removed.

`copy_project=True` includes the project containing the dependency pyproject, or
the current directory for requirement-list inputs. A different source directory
can be selected with `copy_project=Path("application")`. Buildable projects are
installed from the copied source; plain/virtual projects are added to the worker's
import path. Tasks and setup scripts use the copied project as their working
directory, so relative data-file reads work. Git ignore rules are respected when
available; `.git`, venvs, bytecode, build output, and `.env*` files are excluded.
Use both copy flags when the application also has local project dependencies.

`setup_script` accepts inline Python or a `.py`, `.sh`, `.ps1`, `.cmd`, or `.bat`
file. Inline native scripts can specify their language:

```python
from recursiverpc import SetupScript

setup_script = "from pathlib import Path; Path('ready.txt').write_text('ready')"
setup_script = SetupScript("printf ready > ready.txt", language="bash")
```

Hooks run noninteractively once per prepared worker environment, including proxy gateways and
child environments, after dependencies are installed/copied and before RPC
startup. They do not run during preparation of the controller's cached venv
archive. Without a copied project, their working directory is the temporary
worker project. `RECURSIVERPC_PROJECT_DIR` points to that temporary root, and the
worker venv is active for subprocesses. Environment-variable changes in a hook
do not automatically persist into task processes; files and package installations
do persist. Native hooks require the corresponding interpreter on the target
(cmd requires Windows). A nonzero exit aborts startup and cleans up the project.

If these options are provided without `dependency`, the selected project's
`pyproject.toml` is used when present; otherwise only framework dependencies are
installed. Existing RPC-server connections cannot run these provisioning hooks.

### Copying compatible environments

Managed SSH workers compare OS and CPU architecture before installing packages.
On matching systems, they prefer copying the venv. Python implementation, major/
minor version, ABI, word size, Python constraints, and Linux libc compatibility
are also checked. An already installed compatible interpreter is required for
the copy path; it never downloads Python.

With `dependency=...`, an environment is prepared locally once for copying, and
its archive is reused for SSH workers and proxy children. Packages may need to be
downloaded on the controller during that first preparation, but the destination
does not run `uv sync` or download packages when copying succeeds. Without an
explicit dependency, the current venv is copied, including a bundled framework
wheel if recursiverpc is installed as editable source.

The destination venv is rebased onto its local interpreter, activation files and
console launchers are relocated, and installed versions, wheel compatibility,
and framework imports are validated before startup. OS/architecture mismatches,
missing compatible interpreters, unbundled external editable paths, or failed validation
fall back to the usual uv installation. A failed partial copy is removed first.

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

Arbitrary application source trees are not copied. External editable dependency
paths must be accessible remotely if setup falls back to installation.
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

### Development and diagnostics

The supported Python range is currently 3.10.16 through 3.10.x
(`requires-python = "~=3.10.16"`). Broader Python support needs separate validation.

The implementation is divided by responsibility:

- `config.py`: worker descriptors.
- `scheduler.py` and `futures.py`: the public scheduler and result handles.
- `pool.py` and `runners.py`: process dispatch and local, SSH, and proxy runners.
- `bootstrap.py` and `deployment.py`: server startup, output monitoring, and cleanup.
- `ssh.py`: shell adaptation; `dependencies.py`, `venv.py`, `sources.py`, and
  `setup.py`: worker environments and setup.

Public imports such as `from recursiverpc import Recursive_RPC` remain supported.
The original `recursiverpc.recursiverpc` module reexports its classes for existing
imports and serialized worker descriptors.

Library diagnostics use Python logging and are quiet by default. Task stdout and
stderr continue to be forwarded. To see startup and cleanup details:

```python
import logging

logging.basicConfig(level=logging.WARNING)
logging.getLogger("recursiverpc").setLevel(logging.DEBUG)
```

Run the automated suite from the repository root (the same command is used in CI):

```powershell
uv run --locked python -B -m unittest discover -v
```

The `test/` package contains only automated tests and shared fixtures. Discovery
does not load `.env` files or contact SSH/RPC infrastructure. Integration tests
start their own local workers and loopback servers, clean up temporary projects,
and may download packages/build tools with uv; an offline package cache is not
required. These tests do not require SSH credentials or a pre-existing server.

Shell tests exercise real command quoting, Unicode script upload, working
directories, environment variables, startup, and cleanup. Locally, unavailable
shells are skipped. On Windows, the optional Linux Bash check needs the
`Ubuntu-22.04` WSL distribution, not just the `wsl` executable.

[CI](.github/workflows/tests.yml) runs the full suite on Linux and Windows with
Python 3.10.16 and the latest 3.10 patch. Linux requires Bash; Windows requires
Git Bash, PowerShell, and cmd. `RRPC_TEST_REQUIRED_SHELLS` makes missing CI shells
fail instead of silently skipping them. CI never needs a remote SSH host.

Historical infrastructure experiments live in [experiments/](experiments/README.md).
Use [examples/](examples/README.md) for small, current library demonstrations.
