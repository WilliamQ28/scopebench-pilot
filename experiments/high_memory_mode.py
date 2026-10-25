"""Prepare a 10 GiB WSL ceiling and select an 8 GiB admission budget without shutting WSL down."""

# ruff: noqa: INP001, T201 -- standalone Windows/WSL operational CLI
import argparse
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from scopebench.admission import read_control, write_control


def configure_memory(text: str) -> str:
    """Preserve unrelated settings and comments; set one memory limit in the wsl2 section."""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if re.match(r"\s*\[wsl2\]\s*(?:[#;].*)?$", line, re.IGNORECASE)
        ),
        None,
    )
    if start is None:
        prefix = text + (newline if text and not text.endswith("\n") else "")
        return prefix + "[wsl2]" + newline + "memory=10GB" + newline
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines)
    )
    body = [
        line
        for line in lines[start + 1 : end]
        if not re.match(r"\s*memory\s*=", line, re.IGNORECASE)
    ]
    if not lines[start].endswith("\n"):
        lines[start] += newline
    return "".join(lines[: start + 1]) + "memory=10GB" + newline + "".join(body + lines[end:])


def prepare_config(path: Path) -> Path | None:
    """Back up existing settings before an atomic update; return the backup when changed."""
    old = path.read_bytes() if path.exists() else b""
    text = old.decode("utf-8-sig")
    desired = configure_memory(text).encode("utf-8")
    if desired == old:
        return None
    backup = None
    if path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + ".scopebench-backup-" + stamp)
        shutil.copy2(path, backup)
    temporary = path.with_name(path.name + ".scopebench.tmp")
    temporary.write_bytes(desired)
    temporary.replace(path)
    return backup


def main() -> None:
    """Only prepare settings; deliberately never restart WSL or kill active tasks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows-profile", required=True)
    args = parser.parse_args()
    profile = subprocess.check_output(  # noqa: S603 - Windows profile passed as a single argument
        ["wslpath", "-u", args.windows_profile],  # noqa: S607 - fixed conversion utility
        text=True,
    ).strip()
    config = Path(profile) / ".wslconfig"
    backup = prepare_config(config)
    control = Path(__file__).resolve().parent / "judge-information-control.json"
    write_control(control, "high-memory")
    print("Selected: up to 12 tasks, 8 GiB soft Docker admission budget, 1 GiB host reserve.")
    print(f"Prepared {config}: [wsl2] memory=10GB")
    if backup:
        print(f"Previous WSL settings backed up to {backup}")
    total_kib = int(
        next(
            line.split()[1]
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemTotal:")
        )
    )
    print(f"Current WSL-visible RAM: {total_kib / 1024**2:.2f} GiB.")
    print("The 10 GiB WSL ceiling applies after a WSL restart. No restart was performed.")
    print("Do NOT run wsl --shutdown while experiment tasks are active.")
    print("To apply the new ceiling: select drain, wait for the scheduler to exit, then")
    print("Restart WSL/Docker, then resume experiments/run_information_suite.py once.")
    print("Changing this profile does not reserve RAM; new tasks still require free host memory.")
    print(read_control(control))


if __name__ == "__main__":
    main()
