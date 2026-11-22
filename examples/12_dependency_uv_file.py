# /// script
# requires-python = ">=3.10.16,<3.11"
# dependencies = ["humanize==4.11.0"]
# ///
"""Read a uv script header from this file to provision workers."""

from pathlib import Path

from recursiverpc import Recursive_RPC, localprocess


def format_size():
    import humanize  # ty: ignore[unresolved-import]  # Available in the worker environment.

    return humanize.naturalsize(1000000)


if __name__ == "__main__":
    with Recursive_RPC([localprocess(1)], dependency=Path(__file__)) as pool:
        print(pool.apply(format_size))
