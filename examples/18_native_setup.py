"""Select a Bash, PowerShell, or cmd setup hook with --language."""

import argparse
import os

from recursiverpc import Recursive_RPC, SetupScript, localprocess


def read_marker():
    from pathlib import Path

    return Path("ready.txt").read_text().strip()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--language",
        choices=["bash", "powershell", "cmd"],
        default="cmd" if os.name == "nt" else "bash",
    )
    language = parser.parse_args().language
    scripts = {
        "bash": "printf ready > ready.txt",
        "powershell": '[IO.File]::WriteAllText((Join-Path (Get-Location) "ready.txt"), "ready")',
        "cmd": "echo ready>ready.txt",
    }
    with Recursive_RPC(
        [localprocess(1)],
        dependency=[],
        setup_script=SetupScript(scripts[language], language=language),
    ) as pool:
        print(pool.apply(read_marker))
