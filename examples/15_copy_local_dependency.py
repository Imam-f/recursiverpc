"""Bundle an editable path dependency along with its application."""

from pathlib import Path

from recursiverpc import Recursive_RPC, localprocess


def calculate(value):
    import example_helper  # ty: ignore[unresolved-import]  # Available in the worker environment.

    return example_helper.double(value)


if __name__ == "__main__":
    project = Path(__file__).parent / "assets/local_dependency_app/pyproject.toml"
    with Recursive_RPC(
        [localprocess(1)],
        dependency=project,
        copy_local_dependencies=True,
        copy_project=True,
    ) as pool:
        print(pool.apply(calculate, 21))
