"""Run a setup file once in each prepared worker environment."""

from pathlib import Path

from recursiverpc import Recursive_RPC, localprocess


def read_config():
    import json
    from pathlib import Path

    return json.loads(Path("worker_config.json").read_text())


if __name__ == "__main__":
    setup = Path(__file__).parent / "assets/setup_worker.py"
    with Recursive_RPC([localprocess(1)], dependency=[], setup_script=setup) as pool:
        print(pool.apply(read_config))
