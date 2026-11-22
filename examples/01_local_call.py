"""Call a function in a local worker; pass positional and keyword arguments."""

from recursiverpc import Recursive_RPC, localprocess


def greet(name, punctuation="!"):
    return f"Hello, {name}{punctuation}"


if __name__ == "__main__":
    with Recursive_RPC([localprocess(1)]) as pool:
        print(pool.apply(greet, "Alice", punctuation="!!!"))
