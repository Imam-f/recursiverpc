"""Pass an inline uv dependency header instead of a file."""

from recursiverpc import Recursive_RPC, localprocess

HEADER = """# /// script
# requires-python = ">=3.10.16,<3.11"
# dependencies = ["humanize==4.11.0"]
# ///
"""


def ordinal(value):
    import humanize  # ty: ignore[unresolved-import]  # Available in the worker environment.

    return humanize.ordinal(value)


if __name__ == "__main__":
    with Recursive_RPC([localprocess(1)], dependency=HEADER) as pool:
        print(pool.apply(ordinal, 23))
