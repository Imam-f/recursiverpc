"""Connect to an already running RPyC classic server without SSH provisioning."""

import argparse

from recursiverpc import Recursive_RPC, networkprocess


def identify():
    import socket

    return socket.gethostname()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=18812)
    args = parser.parse_args()
    # The server must already have recursiverpc installed. No dependency/setup options.
    with Recursive_RPC([networkprocess(1, (args.host, args.port))]) as pool:
        print(pool.apply(identify))
