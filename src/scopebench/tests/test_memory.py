"""Offline tests for memory observations and admission reservations."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from scopebench import memory
from scopebench.memory import MemoryPolicy, MemorySample


def test_sizes_and_bad_sizes() -> None:
    assert memory._mib("2 GiB") == 2048
    assert memory._mib("512MiB") == 512
    assert memory._mib("1024 B") == 1 / 1024
    with pytest.raises(ValueError, match="Unrecognized"):
        memory._mib("unknown")


@pytest.mark.parametrize(
    ("budget", "overhead", "task", "headroom", "poll", "overrides"),
    [
        (0, 0, 1, 0, 1.0, {}),
        (2048, -1, 1, 0, 1.0, {}),
        (2048, 0, 0, 0, 1.0, {}),
        (2048, 0, 1, -1, 1.0, {}),
        (2048, 0, 1, 0, 0.0, {}),
        (2048, 0, 1, 0, float("nan"), {}),
        (2048, 0, 1, 0, 1.0, {"heavy": -1}),
        (512, 512, 1, 0, 1.0, {}),
    ],
)
def test_invalid_policy(
    budget: int, overhead: int, task: int, headroom: int, poll: float, overrides: dict[str, int]
) -> None:
    with pytest.raises(ValueError, match="Memory"):
        MemoryPolicy(budget, overhead, task, headroom, poll, overrides)


def test_admission_counts_startups_other_containers_and_host_pressure() -> None:
    policy = MemoryPolicy(
        budget_mib=1024, overhead_mib=128, default_task_mib=384, min_available_mib=256
    )
    assert policy.allows("new", MemorySample(0, 4096), {})
    assert policy.allows("new", MemorySample(0, 4096), {"a": "first"})
    assert not policy.allows("new", MemorySample(0, 4096), {"a": "first", "b": "second"})
    assert not policy.allows("new", MemorySample(800, 4096), {})  # unrelated containers
    assert not policy.allows("new", MemorySample(0, 512), {})  # host apps need RAM
    assert not policy.allows("new", MemorySample(600, 4096, {"a": 600}), {"a": "first"})
    assert not policy.allows("new", MemorySample(0, 800), {"a": "first"})  # pending startup


def test_profiles_and_task_overrides() -> None:
    policy = MemoryPolicy(task_mib={"heavy": 900})
    assert policy.estimate("new") == 384
    assert policy.estimate("heavy") == 900
    policy.observe(MemorySample(0, 4096), {"new": "new"})
    assert "new" not in policy.peaks
    policy.observe(MemorySample(600, 4096, {"a": 400, "b": 50}), {"a": "new", "b": "small"})
    assert policy.estimate("new") == 500
    assert policy.estimate("small") == 128
    policy.observe(MemorySample(100, 4096, {"a": 100}), {"a": "new"})
    assert policy.estimate("new") == 500  # peaks do not fall during a run
    policy.observe(MemorySample(1000, 4096, {"a": 1000}), {"a": "heavy"})
    assert policy.estimate("heavy") == 1250


def test_sample_attributes_target_and_actor_and_counts_other_workloads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(memory, "_available_mib", lambda: 4096.0)
    rows = [
        {"Name": "actor", "MemUsage": "100MiB / 2GiB"},
        {"Name": "target", "MemUsage": "200MiB / 8GiB"},
        {"Name": "other", "MemUsage": "50MiB / 8GiB"},
    ]
    containers: list[dict[str, object]] = [
        {"Name": "/actor", "Config": {}, "Mounts": [{"Source": str(tmp_path / "a/work")}]},
        {
            "Name": "/target",
            "Config": {
                "Labels": {
                    "com.docker.compose.project.config_files": str(tmp_path / "a/work/compose.yaml")
                }
            },
        },
        {"Name": "/other", "Config": {}},
    ]

    def docker(*args: str) -> str:
        return (
            "\n".join(json.dumps(r) for r in rows) if args[0] == "stats" else json.dumps(containers)
        )

    monkeypatch.setattr(memory, "_docker", docker)
    sample = memory.sample_memory({"a": tmp_path / "a", "b": tmp_path / "b"})
    assert sample.total_mib == 350
    assert sample.by_run == {"a": 300, "b": 0}
    assert sample.available_mib == 4096
    assert memory.sample_memory({}).total_mib == 350

    def empty(*_a: str) -> str:
        return ""

    monkeypatch.setattr(memory, "_docker", empty)
    assert memory.sample_memory({}).total_mib == 0


def test_host_headroom_includes_windows_when_running_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    def meminfo(_p: Path) -> str:
        return "MemAvailable: 1048576 kB\n"

    monkeypatch.setattr(Path, "read_text", meminfo)
    monkeypatch.setattr(platform, "release", lambda: "linux")
    assert memory._available_mib() == 1024
    monkeypatch.setattr(platform, "release", lambda: "microsoft-standard-WSL2")

    def missing(_n: str) -> None:
        return None

    monkeypatch.setattr(shutil, "which", missing)
    with pytest.raises(RuntimeError, match="Windows"):
        memory._available_mib()

    def present(_n: str) -> str:
        return "/bin/powershell.exe"

    monkeypatch.setattr(shutil, "which", present)

    def run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, stdout="524288\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert memory._available_mib() == 512
    assert memory._docker("ps") == "524288\n"  # real subprocess wrapper, mocked I/O


@pytest.mark.parametrize("partial", [True, False])
def test_finished_container_race_keeps_conservative_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, partial: bool
) -> None:
    monkeypatch.setattr(memory, "_available_mib", lambda: 4096.0)

    def docker(*args: str) -> str:
        if args[0] == "stats":
            return json.dumps({"Name": "finished", "MemUsage": "100MiB / 1GiB"})
        raise subprocess.CalledProcessError(1, ["docker", *args], output="[]" if partial else "")

    monkeypatch.setattr(memory, "_docker", docker)
    if partial:
        sample = memory.sample_memory({"run": tmp_path})
        assert sample.total_mib == 100
        assert sample.by_run == {"run": 0}
    else:
        with pytest.raises(subprocess.CalledProcessError):
            memory.sample_memory({"run": tmp_path})
