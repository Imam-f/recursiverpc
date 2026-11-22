"""Distribute a batch across local and SSH workers in the same pool."""

import os

from recursiverpc import Recursive_RPC, SSHConnection, localprocess, networkprocess


def identify(value):
    import os
    import socket

    return value * value, socket.gethostname(), os.getpid()


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
        [localprocess(1), networkprocess(2, connection)], dependency=[]
    ) as pool:
        # Scheduling is weighted random; a short batch may not visit every host.
        print(pool.map_ordered(range(8), identify))
