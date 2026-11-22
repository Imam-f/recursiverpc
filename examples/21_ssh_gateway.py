"""Deploy a nested worker group on an SSH gateway."""

import os

from recursiverpc import Recursive_RPC, localprocess, proxyprocess


def identify(value):
    import os
    import socket

    return value, socket.gethostname(), os.getpid()


if __name__ == "__main__":
    # This gateway API accepts user/password; SSH agent or default keys work too.
    gateway = proxyprocess(
        1,
        os.environ["RRPC_HOST"],
        int(os.getenv("RRPC_PORT", "22")),
        [localprocess(1), proxyprocess(1, None, None, [localprocess(1)], (), {})],
        (os.getenv("RRPC_USER"), os.getenv("RRPC_PASSWORD")),
        {},
    )
    with Recursive_RPC([gateway], dependency=[]) as pool:
        print(pool.map_ordered(range(6), identify))
