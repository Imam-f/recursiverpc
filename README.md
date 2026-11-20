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
For cmd, literal `%`, `!`, and newlines in executable arguments are rejected to
prevent command expansion; script contents are encoded and can contain them.

Run the isolated regression tests (without SSH credentials):

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s test -p "test_cross_shell.py" -v
```

On Windows, the Linux Bash tests use the `Ubuntu-22.04` WSL distribution. The
tests launch a small bootstrap, verify script bytes and shell detection, and
check startup and cleanup; they do not run the distributed workload.
