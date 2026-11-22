# Small RecursiveRPC examples

Each numbered file demonstrates one feature. Run scripts separately from the
repository root, using Python 3.10.16–3.10.x with this checkout installed:

```powershell
uv sync
.\.venv\Scripts\Activate.ps1
python examples/01_local_call.py
```

On Linux, activate with `source .venv/bin/activate` instead. You can also invoke
the venv's Python directly without activation. Paths to sample assets are resolved
relative to each script, so the examples also work from another working directory.

Examples 01–07 and 24 use the current environment. Examples 08–18 create isolated
temporary projects and may download packages or build tools with uv on their first
run. The controller needs this source checkout to build the framework wheel.
Every pool uses a `with` block to close workers and remove its temporary projects.

## Local calls and results

| Script | Demonstrates | Typical result |
| --- | --- | --- |
| [01_local_call.py](01_local_call.py) | Blocking call, positional and keyword arguments | `Hello, Alice!!!` |
| [02_async_call.py](02_async_call.py) | Submit, check readiness, retrieve a value | `49` |
| [03_ordered_map.py](03_ordered_map.py) | Batch results in input order | `[10, 11, 14, 19, 26]` |
| [04_completion_order.py](04_completion_order.py) | Consume values as tasks finish | Usually `fast`, then `slow` |
| [05_async_map.py](05_async_map.py) | A future for every batch input | `[0, 1, 8, 27, 64]` |
| [06_task_errors.py](06_task_errors.py) | Catch an error and reuse the worker | Caught error, then `5.0` |
| [07_nested_local_pools.py](07_nested_local_pools.py) | Recursive groups of local workers | Values and worker process IDs |
| [24_logging.py](24_logging.py) | Enable library debug logging | Startup diagnostic and `81` |

Mapping methods take the iterable **before** the function. `map_async` and
`map_ordered_async` return futures in input order. `pool.as_completed(futures)`
yields **result values**, rather than futures. The synchronous `pool.map(...)`
returns a list of values in completion order; `map_ordered(...)` preserves input
order. Completion order and worker process IDs can vary between runs.

Task functions import the modules they need inside their bodies. Function
teleportation does not automatically copy the caller's module globals or an
application source tree. Keep the `if __name__ == "__main__"` guards when adapting
these examples, especially for Windows multiprocessing.

## Dependencies, project files, and setup

| Script | Demonstrates |
| --- | --- |
| [08_dependency_list.py](08_dependency_list.py) | A list of pinned package requirements |
| [09_dependency_string.py](09_dependency_string.py) | Comma-separated requirements with version ranges |
| [10_dependency_modules.py](10_dependency_modules.py) | Imported module objects; standard-library modules are skipped |
| [11_dependency_pyproject.py](11_dependency_pyproject.py) | Requirements read from a small pyproject.toml |
| [12_dependency_uv_file.py](12_dependency_uv_file.py) | A uv header read from the example's own file |
| [13_dependency_uv_inline.py](13_dependency_uv_inline.py) | A uv header passed as a string |
| [14_copy_project.py](14_copy_project.py) | Copy application code and read a relative data file |
| [15_copy_local_dependency.py](15_copy_local_dependency.py) | Bundle an editable local package and its application |
| [16_setup_inline.py](16_setup_inline.py) | Inline Python setup writes a file before work starts |
| [17_setup_file.py](17_setup_file.py) | A Python setup file generates worker configuration |
| [18_native_setup.py](18_native_setup.py) | Bash, PowerShell, or cmd setup hooks |

The tiny projects under [assets](assets) are bundled inputs, not additional
controller installations. Example 15 includes a local `example-helper` package;
you do not need to install it yourself. Example 14 prints
`Hello from copied application data!`, and example 15 prints `42`.

Choose a native setup language installed on the worker:

```powershell
python examples/18_native_setup.py --language powershell
python examples/18_native_setup.py --language cmd
python examples/18_native_setup.py --language bash
```

cmd requires Windows; Bash on Windows can use Git Bash. Setup hooks run once per
prepared environment before RPC startup. They create files in the temporary
worker project, leaving the original example assets untouched. Changes to a
setup process's environment variables do not persist into task processes.

`dependency=[]` means an isolated environment containing the framework and its
runtime dependencies. Omitting `dependency` uses the existing environment for
local workers. For your own app, combine `copy_project` with
`copy_local_dependencies=True` when it also uses local path dependencies.

## SSH and existing servers

| Script | Demonstrates |
| --- | --- |
| [19_ssh_worker.py](19_ssh_worker.py) | Managed SSH workers with an explicit dependency |
| [20_mixed_workers.py](20_mixed_workers.py) | Local and SSH workers sharing a scheduler |
| [21_ssh_gateway.py](21_ssh_gateway.py) | Nested worker groups deployed on an SSH gateway |
| [22_existing_rpc_server.py](22_existing_rpc_server.py) | Connect to an existing classic RPyC server |
| [23_remote_commands.py](23_remote_commands.py) | Plumbum command binding and environment overrides over SSH |

Examples 19–21 and 23 read these environment variables:

| Variable | Meaning |
| --- | --- |
| `RRPC_HOST` | Required SSH hostname |
| `RRPC_USER` | SSH username; otherwise use the SSH client's default |
| `RRPC_PORT` | SSH port; defaults to `22` |
| `RRPC_PASSWORD` | Optional password; omit for agent/key authentication |
| `RRPC_KEY` | Optional private-key path for 19, 20, and 23 |
| `RRPC_SHELL` | `auto`, `bash`, `powershell`, or `cmd` for 19, 20, and 23 |

PowerShell:

```powershell
$env:RRPC_HOST = "worker.example.com"
$env:RRPC_USER = "worker"
$env:RRPC_PORT = "22"
python examples/19_ssh_worker.py
```

Bash:

```bash
export RRPC_HOST=worker.example.com RRPC_USER=worker RRPC_PORT=22
python examples/19_ssh_worker.py
```

SSH host keys must already be trusted in `known_hosts`. Managed deployments need
remote Python and uv as described in the [main README](../README.md#ssh-login-shells).
Example 21's gateway descriptor uses user/password or agent/default keys and
automatic shell detection; it does not accept `RRPC_KEY` or `RRPC_SHELL`.

Compatible managed SSH workers automatically try to reuse a copied venv. An
incompatible OS, architecture, or Python runtime falls back to installing the
declared requirements with uv; no extra example flag is needed. Example 20 uses
weighted random scheduling, so a short batch may not visit every machine.

For example 22, start a classic server on a trusted local machine with the same
checkout installed, then run the client in another terminal:

```powershell
python -c "from rpyc.cli.rpyc_classic import main; main()" --host 127.0.0.1 --port 18812
python examples/22_existing_rpc_server.py --host 127.0.0.1 --port 18812
```

A classic server grants Python execution to its clients; keep it on a trusted
interface or behind an SSH tunnel. Existing-server connections do not provision
dependencies, copy projects, or run setup hooks.
