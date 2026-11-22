"""Use the shell-aware connection with Plumbum command binding and environments."""

import json
import os

from recursiverpc import SSHConnection, shell_aware

if __name__ == "__main__":
    connection = SSHConnection(
        os.environ["RRPC_HOST"],
        user=os.getenv("RRPC_USER"),
        port=int(os.getenv("RRPC_PORT", "22")),
        password=os.getenv("RRPC_PASSWORD"),
        key_filename=os.getenv("RRPC_KEY"),
        shell=os.getenv("RRPC_SHELL", "auto"),
    )
    remote = connection.connect()
    try:
        # Adapting an already adapted connection is harmless.
        assert shell_aware(remote) is remote
        executable = json.loads(
            remote.python_run("import sys,json; print(json.dumps(sys.executable))")
        )
        python = remote[executable]
        command = python["-c", 'import os; print(os.environ["DEMO_MESSAGE"])']
        print(command.with_env(DEMO_MESSAGE="hello from a remote command")().strip())
    finally:
        remote.close()
