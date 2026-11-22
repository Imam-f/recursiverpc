"""Deploy an SSH worker with dependencies and automatic compatible-venv reuse."""

import os

from recursiverpc import Recursive_RPC, SSHConnection, networkprocess


def report(value):
    import platform
    import socket

    import humanize  # ty: ignore[unresolved-import]  # Available in the worker environment.

    return socket.gethostname(), platform.system(), humanize.intcomma(value)


if __name__ == "__main__":
    connection = SSHConnection(
        os.environ["RRPC_HOST"],
        user=os.getenv("RRPC_USER"),
        port=int(os.getenv("RRPC_PORT", "22")),
        password=os.getenv("RRPC_PASSWORD"),
        key_filename=os.getenv("RRPC_KEY"),
        shell=os.getenv("RRPC_SHELL", "auto"),
    )
    with Recursive_RPC(
        [networkprocess(2, connection)], dependency=["humanize==4.11.0"]
    ) as pool:
        print(pool.apply(report, 1234567))
