"""Map over inputs and return values in input order."""

from recursiverpc import Recursive_RPC, localprocess


def square(value, offset=0):
    return value * value + offset


if __name__ == "__main__":
    with Recursive_RPC([localprocess(2)]) as pool:
        # This API takes the iterable before the function.
        print(pool.map_ordered(range(5), square, offset=10))
