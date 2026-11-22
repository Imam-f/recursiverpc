"""Submit a task, do controller work, then retrieve its result."""

from recursiverpc import Recursive_RPC, localprocess


def slow_square(value):
    import time

    time.sleep(0.3)
    return value * value


if __name__ == "__main__":
    with Recursive_RPC([localprocess(1)]) as pool:
        future = pool.apply_async(slow_square, 7)
        print("Controller can keep working; done?", future.status())
        print("Result:", future.get())
