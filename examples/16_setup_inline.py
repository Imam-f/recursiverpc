"""Create worker data with an inline Python setup hook before tasks start."""

from recursiverpc import Recursive_RPC, localprocess


def read_marker():
    from pathlib import Path

    return Path("ready.txt").read_text()


if __name__ == "__main__":
    setup = 'from pathlib import Path; Path("ready.txt").write_text("ready from inline setup")'
    with Recursive_RPC([localprocess(1)], dependency=[], setup_script=setup) as pool:
        print(pool.apply(read_marker))
