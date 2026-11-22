"""Copy application code and data; run inside the copied working directory."""

from pathlib import Path

from recursiverpc import Recursive_RPC, localprocess


def report():
    import demo_app  # ty: ignore[unresolved-import]  # Available in the worker environment.

    return demo_app.message()


if __name__ == "__main__":
    app = Path(__file__).parent / "assets/application"
    with Recursive_RPC([localprocess(1)], dependency=[], copy_project=app) as pool:
        print(pool.apply(report))
