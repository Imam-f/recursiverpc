"""Enable library diagnostics while leaving unrelated loggers at WARNING."""

import logging

from recursiverpc import Recursive_RPC, localprocess


def square(value):
    return value * value


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("recursiverpc").setLevel(logging.DEBUG)
    with Recursive_RPC([localprocess(1)]) as pool:
        print(pool.apply(square, 9))
