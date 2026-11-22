"""Consume asynchronous task values as they complete."""

from recursiverpc import Recursive_RPC, localprocess


def delayed(value, delay):
    import time

    time.sleep(delay)
    return value


if __name__ == "__main__":
    with Recursive_RPC([localprocess(2)]) as pool:
        futures = [
            pool.apply_async(delayed, "slow", 0.6),
            pool.apply_async(delayed, "fast", 0.05),
        ]
        # as_completed yields VALUES, not future objects.
        for result in pool.as_completed(futures):
            print(result)
