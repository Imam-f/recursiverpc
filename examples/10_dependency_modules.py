"""Resolve imported modules to distributions; ignore standard-library modules."""

import json

import tomlkit

from recursiverpc import Recursive_RPC, localprocess


def read_toml():
    import tomlkit

    return tomlkit.parse("answer = 42")["answer"]


if __name__ == "__main__":
    # tomlkit is already a dependency of recursiverpc on the controller.
    with Recursive_RPC([localprocess(1)], dependency=[tomlkit, json]) as pool:
        print(pool.apply(read_toml))
