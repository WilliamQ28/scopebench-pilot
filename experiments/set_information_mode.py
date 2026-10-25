"""Switch the running suite without restarting it: working, idle, high-memory, or drain."""

# ruff: noqa: INP001, T201 -- standalone operational CLI
import argparse
from pathlib import Path
from typing import cast

from scopebench.admission import RunMode, read_control, write_control

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["working", "idle", "high-memory", "drain"])
    args = parser.parse_args()
    path = Path(__file__).resolve().parent / "judge-information-control.json"
    write_control(path, cast("RunMode", args.mode))
    print(read_control(path))
