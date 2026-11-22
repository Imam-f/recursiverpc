"""Submit a whole batch and keep one future per input."""

from recursiverpc import Recursive_RPC, localprocess


def cube(value):
    return value**3


if __name__ == "__main__":
    with Recursive_RPC([localprocess(2)]) as pool:
        futures = pool.map_async(range(5), cube)
        print("Submitted", len(futures), "tasks")
        print([future.get() for future in futures])
        # map_ordered_async is also available; it returns futures in input order.
