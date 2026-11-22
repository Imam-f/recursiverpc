"""Load worker requirements from a small pyproject.toml."""

from pathlib import Path

from recursiverpc import Recursive_RPC, localprocess


def format_number():
    import humanize  # ty: ignore[unresolved-import]  # Available in the worker environment.

    return humanize.intcomma(9876543)


if __name__ == "__main__":
    project = Path(__file__).parent / "assets/dependencies/pyproject.toml"
    with Recursive_RPC([localprocess(1)], dependency=project) as pool:
        print(pool.apply(format_number))
