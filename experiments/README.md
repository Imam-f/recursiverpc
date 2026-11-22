# Manual experiments

These are historical scratch scripts, separate from the runnable
[examples](../examples/README.md) and automated tests. They may require live
SSH/RPC servers and contain deliberately failing exploratory calls. They are
not run by unittest discovery or CI.

Run them explicitly from the repository root with the project venv active:

| Script | Purpose and prerequisites |
| --- | --- |
| `distributed_rpc.py` | Distributed workload using SSH credentials from the root `.env`: `HOSTNAME`, `USER`, `PORT`, `PASSWORD`. |
| `ssh_probe.py` | Old Plumbum/Paramiko shell probe using the same `.env` settings; assumes a POSIX login shell. |
| `rpyc_teleport.py` | Explore teleporting functions, classes, globals, and closures against a classic server on `localhost:18812`. |
| `multiprocess_pool.py` | Prototype pool driver that tries to launch classic servers on ports `18812` through `18819`; its old launcher may need updating. |
| `multiprocessor.py` | Historical helper implementation used by `multiprocess_pool.py`. |

For example:

```powershell
python experiments/rpyc_teleport.py
```

Imports do not initiate SSH/RPC connections; `rpyc_teleport.py` now opens and
closes its connection inside its guarded `main()`. The two SSH experiments load
the root `.env` only when run. Prefer the numbered examples for current usage.
