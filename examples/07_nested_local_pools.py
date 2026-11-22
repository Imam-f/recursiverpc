"""Compose a tree of local worker groups using proxy descriptors."""

from recursiverpc import Recursive_RPC, localprocess, proxyprocess


def identify(value):
    import os

    return value, os.getpid()


if __name__ == "__main__":
    nested = proxyprocess(1, None, None, [localprocess(1)], (), {})
    group = proxyprocess(1, None, None, [localprocess(1), nested], (), {})
    with Recursive_RPC([group]) as pool:
        print(pool.map_ordered(range(6), identify))
