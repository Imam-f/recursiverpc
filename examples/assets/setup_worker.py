"""Executed by the library inside a temporary worker project."""

import json
from pathlib import Path

Path("worker_config.json").write_text(json.dumps({"mode": "demo", "batch_size": 10}))
