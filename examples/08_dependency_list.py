"""Install explicit requirements in an isolated temporary worker project."""

from recursiverpc import Recursive_RPC, localprocess


def format_number(value):
    import humanize  # ty: ignore[unresolved-import]  # Available in the worker environment.

    return humanize.intcomma(value)


if __name__ == "__main__":
    with Recursive_RPC([localprocess(1)], dependency=["humanize==4.11.0"]) as pool:
        print(pool.apply(format_number, 1234567))
