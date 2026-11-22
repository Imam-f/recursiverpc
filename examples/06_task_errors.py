"""A worker exception is raised when the controller retrieves the result."""

from recursiverpc import Recursive_RPC, localprocess


def divide(numerator, denominator):
    return numerator / denominator


if __name__ == "__main__":
    with Recursive_RPC([localprocess(1)]) as pool:
        future = pool.apply_async(divide, 10, 0)
        try:
            future.get()
        except ZeroDivisionError:
            print("Caught worker division by zero")
        print("Worker still usable:", pool.apply(divide, 10, 2))
