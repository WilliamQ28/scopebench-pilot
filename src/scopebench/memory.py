"""Memory observations and conservative admission estimates for Docker task workers.

The budget is a soft admission limit, not a container/VM hard limit. Docker's working-set metrics
exclude some cache and daemon costs; explicit overhead and host headroom leave room for those.
"""

from __future__ import annotations

import json
import math
import platform
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast


@dataclass(frozen=True)
class MemorySample:
    """A Docker working-set snapshot and available memory inside the Linux host/WSL VM."""

    total_mib: float
    available_mib: float
    by_run: dict[str, float] = field(default_factory=dict)


def _mib(value: str) -> float:
    match = re.fullmatch(r"([\d.]+)\s*(B|kB|KB|KiB|MB|MiB|GB|GiB|TB|TiB)", value.strip())
    if match is None:
        raise ValueError(f"Unrecognized Docker memory size: {value!r}")
    units = {
        "B": 1,
        "kB": 1000,
        "KB": 1000,
        "KiB": 1024,
        "MB": 1000**2,
        "MiB": 1024**2,
        "GB": 1000**3,
        "GiB": 1024**3,
        "TB": 1000**4,
        "TiB": 1024**4,
    }
    return float(match[1]) * units[match[2]] / 1024**2


def _docker(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed Docker CLI, no shell
        ["docker", *args],  # noqa: S607 - fixed executable
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return result.stdout


def _available_mib() -> float:
    available = next(
        float(line.split()[1]) / 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:")
    )
    if "microsoft" in platform.release().lower():
        powershell = shutil.which("powershell.exe")
        if powershell is None:
            raise RuntimeError("Cannot measure Windows memory headroom from WSL.")
        result = subprocess.run(  # noqa: S603 - fixed read-only Windows memory query
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        available = min(available, float(result.stdout.strip()) / 1024)
    return available


def sample_memory(active_paths: Mapping[str, Path]) -> MemorySample:
    """Measure all running containers; attribute task containers through mounts/Compose labels.

    A probe error propagates: unknown memory usage must not authorize another task. No Docker
    socket or host information is exposed to the actor. No inference requests are made.
    """
    available = _available_mib()
    rows = [
        cast("dict[str, str]", json.loads(line))
        for line in _docker("stats", "--no-stream", "--format", "{{json .}}").splitlines()
    ]
    total = sum(_mib(row["MemUsage"].split("/")[0]) for row in rows)
    by_run = dict.fromkeys(active_paths, 0.0)
    if rows and active_paths:
        try:
            details = _docker("inspect", *(r["Name"] for r in rows))
        except subprocess.CalledProcessError as exc:
            # --rm containers can finish between stats and inspect. Docker still returns an array
            # for surviving containers; retain the older total as a conservative upper estimate.
            details = cast("str", exc.stdout)
            if not details:
                raise
        containers = cast("list[dict[str, object]]", json.loads(details))
        usage = {row["Name"]: _mib(row["MemUsage"].split("/")[0]) for row in rows}
        for container in containers:
            config = cast("dict[str, object]", container["Config"])
            labels = cast("dict[str, str]", config.get("Labels") or {})
            sources = labels.get("com.docker.compose.project.config_files", "").split(",")
            mounts = cast("list[dict[str, object]]", container.get("Mounts", []))
            sources.extend(str(m["Source"]) for m in mounts)
            for rid, directory in active_paths.items():
                if any(source and Path(source).is_relative_to(directory) for source in sources):
                    by_run[rid] += usage[str(container["Name"]).lstrip("/")]
                    break
    return MemorySample(total, available, by_run)


@dataclass
class MemoryPolicy:
    """Soft admission budget with startup reservations for simultaneous workers."""

    budget_mib: int = 2048
    overhead_mib: int = 512
    default_task_mib: int = 384
    min_available_mib: int = 1024
    poll_seconds: float = 5.0
    task_mib: dict[str, int] = field(default_factory=dict)
    peaks: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject settings that cannot admit even one task or cannot be sampled safely."""
        if (
            self.budget_mib <= 0
            or self.overhead_mib < 0
            or self.default_task_mib <= 0
            or self.min_available_mib < 0
            or not math.isfinite(self.poll_seconds)
            or self.poll_seconds <= 0
            or any(v <= 0 for v in self.task_mib.values())
        ):
            raise ValueError("Memory settings must be positive (overhead/headroom may be zero).")
        if (
            self.overhead_mib + max([self.default_task_mib, *self.task_mib.values()])
            > self.budget_mib
        ):
            raise ValueError("Memory budget cannot fit overhead plus a task reservation.")

    def estimate(self, task: str) -> float:
        """Use overrides or observed peaks plus 25%; unprofiled tasks reserve the default."""
        if task in self.task_mib:
            return max(self.task_mib[task], self.peaks.get(task, 0) * 1.25)
        if task in self.peaks:
            return max(128.0, self.peaks[task] * 1.25)
        return float(self.default_task_mib)

    def observe(self, sample: MemorySample, active_tasks: Mapping[str, str]) -> None:
        """Retain task peaks across arms, ignoring unobserved/zero usage."""
        for rid, task in active_tasks.items():
            used = sample.by_run.get(rid, 0)
            if used > 0:
                self.peaks[task] = max(self.peaks.get(task, 0), used)

    def allows(self, task: str, sample: MemorySample, active_tasks: Mapping[str, str]) -> bool:
        """Account for other Docker workloads and pending startup reservations."""
        measured = sum(sample.by_run.get(rid, 0) for rid in active_tasks)
        reserved = sum(
            max(self.estimate(t), sample.by_run.get(rid, 0)) for rid, t in active_tasks.items()
        )
        candidate = self.estimate(task)
        other = max(0.0, sample.total_mib - measured)
        startup_gap = max(0.0, reserved - measured)
        return (
            other + reserved + candidate + self.overhead_mib <= self.budget_mib
            and sample.available_mib >= self.min_available_mib + startup_gap + candidate
        )
