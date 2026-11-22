"""Example application code, copied with its data file."""

from pathlib import Path


def message():
    return Path("data/message.txt").read_text(encoding="utf-8").strip()
