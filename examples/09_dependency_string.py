"""Declare comma-separated requirements, including a version range."""

from recursiverpc import Recursive_RPC, localprocess


def versions():
    from importlib.metadata import version

    return version("humanize"), version("packaging")


if __name__ == "__main__":
    with Recursive_RPC(
        [localprocess(1)], dependency="humanize>=4.11,<5, packaging>=24.2"
    ) as pool:
        print(pool.apply(versions))
