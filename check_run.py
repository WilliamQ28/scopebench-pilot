"""Readable read-only status checker. Add --watch 60 for periodic updates; --json for JSON."""

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    if "--json" in sys.argv:
        sys.argv.remove("--json")
    else:
        sys.argv.append("--human")
    if "--archives" in sys.argv:
        sys.argv.remove("--archives")
    else:
        sys.argv.append("--skip-archives")
    runpy.run_path(
        str(Path(__file__).resolve().parent / "experiments/experiment_status.py"),
        run_name="__main__",
    )
