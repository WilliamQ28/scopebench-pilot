"""Status discovery must follow real controllers rather than old running ledger rows."""

import json
import os
import runpy
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "experiments/experiment_status.py"
_MODULE = runpy.run_path(str(_SCRIPT))
discover = cast("Callable[[Path, Path], list[dict[str, object]]]", _MODULE["discover_processes"])
select = cast(
    "Callable[[Path, list[dict[str, object]]], tuple[Path, str]]", _MODULE["select_ledger"]
)


def _process(
    proc_root: Path,
    cwd: Path,
    args: list[str],
    *,
    pid: int = 123,
    ticks: int = 10,
    state: str = "S",
) -> None:
    proc = proc_root / str(pid)
    proc.mkdir(parents=True)
    _ = (proc / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")
    _ = (proc / "stat").write_text(
        f"{pid} (python) " + " ".join([state, *(["0"] * 18), str(ticks)])
    )
    (proc / "cwd").symlink_to(cwd, target_is_directory=True)


def _ledger(path: Path, *, name: str = "selected") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(
        json.dumps(
            {
                "code_revision": "test",
                "created": datetime.now(UTC).isoformat(),
                "spec": {"name": name},
                "runs": {"unit": {"status": "completed"}},
                "scheduler_history": [],
            }
        )
    )


@pytest.mark.parametrize("kind", ["custom", "legacy", "module", "entrypoint"])
def test_discovery_recognizes_controllers_and_their_own_outputs(tmp_path: Path, kind: str) -> None:
    proc = tmp_path / "proc"
    project = tmp_path / "project"
    project.mkdir()
    if kind == "custom":
        args = ["python", "-u", "experiments/current/run.py"]
        expected = project / "experiments/current/runs/ledger.json"
    elif kind == "legacy":
        args = ["python", "experiments/run_information_suite.py"]
        expected = project / "experiments/judge-information-full-suite-20261022/ledger.json"
    else:
        args = ["python", "-m", "scopebench.matrix"] if kind == "module" else ["scopebench-matrix"]
        args += ["spec.yaml", "--out=experiments/current"]
        expected = project / "experiments/current/ledger.json"
    _process(proc, project, args)
    found = discover(project, proc)
    assert len(found) == 1
    assert found[0]["ledger"] == str(expected)
    assert found[0]["pid"] == 123


def test_live_controller_wins_over_newer_stale_ledger(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    project = tmp_path / "project"
    current = project / "experiments/current/runs/ledger.json"
    stale = project / "experiments/old-full-suite/ledger.json"
    _ledger(current)
    _ledger(stale)
    os.utime(current, (1, 1))
    os.utime(stale, (100, 100))
    before = current.read_bytes(), stale.read_bytes()
    _process(proc, project, ["python", "experiments/current/run.py"])
    selected, reason = select(project, discover(project, proc))
    assert selected == current
    assert "active controller PID 123" in reason
    assert (current.read_bytes(), stale.read_bytes()) == before


def test_preparing_controller_does_not_fall_back_to_old_ledger(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    project = tmp_path / "project"
    _ledger(project / "experiments/old/ledger.json")
    _process(proc, project, ["python", "-m", "scopebench.matrix", "s.yaml", "--out", "new-run"])
    selected, reason = select(project, discover(project, proc))
    assert selected == project / "new-run/ledger.json"
    assert not selected.exists()
    assert "active" in reason


def test_latest_active_controller_wins_even_with_lower_pid(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc = tmp_path / "proc"
    _process(proc, project, ["python", "experiments/first/run.py"], pid=900, ticks=1)
    _process(proc, project, ["python", "experiments/second/run.py"], pid=100, ticks=2)
    selected, _reason = select(project, discover(project, proc))
    assert selected == project / "experiments/second/runs/ledger.json"


@pytest.mark.parametrize("kind", ["shell", "checker", "zombie", "other-project", "exited"])
def test_discovery_excludes_noncontrollers_and_dead_processes(tmp_path: Path, kind: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc = tmp_path / "proc"
    args = ["python", "experiments/current/run.py"]
    cwd = project
    if kind == "shell":
        args = ["/bin/bash", "-c", "python experiments/current/run.py"]
    elif kind == "checker":
        args = ["python", "check_run.py"]
    elif kind == "other-project":
        cwd = tmp_path
        args = ["python", "-m", "scopebench.matrix", "s.yaml", "--out", "elsewhere"]
    _process(proc, cwd, args, state="Z" if kind == "zombie" else "S")
    if kind == "exited":
        (proc / "123/cmdline").unlink()
    assert discover(project, proc) == []


def test_no_active_controller_shows_latest_with_explicit_fallback_label(tmp_path: Path) -> None:
    old = tmp_path / "experiments/old/ledger.json"
    new = tmp_path / "experiments/new/runs/ledger.json"
    _ledger(old)
    _ledger(new)
    os.utime(old, (1, 1))
    os.utime(new, (100, 100))
    chosen, reason = select(tmp_path, [])
    assert chosen == new
    assert "no active controller" in reason
    assert "most recently updated" in reason
    with pytest.raises(FileNotFoundError, match="No experiment ledger"):
        select(tmp_path / "empty", [])


def test_checker_explicit_override_and_human_identity(tmp_path: Path) -> None:
    ledger = tmp_path / "explicit/ledger.json"
    _ledger(ledger, name="explicit-check")
    before = ledger.read_bytes()
    command = [sys.executable, str(_SCRIPT.parent.parent / "check_run.py"), "--ledger", str(ledger)]
    result = subprocess.run(command, capture_output=True, text=True, check=True)  # noqa: S603 - fixed checker
    assert "Experiment: explicit-check" in result.stdout
    assert str(ledger) in result.stdout
    assert "explicit --ledger override" in result.stdout
    assert "Process: NOT RUNNING" in result.stdout
    assert ledger.read_bytes() == before


def test_checker_follows_real_live_controller_instead_of_newer_historical_ledgers() -> None:
    """Exercise /proc and the actual launcher without running any benchmark/model calls."""
    project = _SCRIPT.parent.parent
    with tempfile.TemporaryDirectory(
        prefix="status-discovery-test-", dir=project / "experiments"
    ) as directory:
        experiment = Path(directory)
        ledger = experiment / "runs/ledger.json"
        _ledger(ledger, name="live-controller-test")
        os.utime(ledger, (1, 1))  # any historical ledger is newer; process identity must win
        script = experiment / "run.py"
        _ = script.write_text("import time\ntime.sleep(60)\n")
        controller = subprocess.Popen([sys.executable, str(script)], cwd=project)  # noqa: S603 - inert local fixture
        try:
            command = [sys.executable, str(project / "check_run.py"), "--json"]
            result = subprocess.run(command, capture_output=True, text=True, check=True)  # noqa: S603 - fixed checker
            snapshot = cast("dict[str, object]", json.loads(result.stdout))
            assert snapshot["ledger"] == str(ledger)
            assert snapshot["experiment_name"] == "live-controller-test"
            assert f"PID {controller.pid}" in str(snapshot["selection_reason"])
            processes = cast("list[dict[str, object]]", snapshot["processes"])
            assert [p["pid"] for p in processes] == [controller.pid]
        finally:
            controller.terminate()
            _ = controller.wait(timeout=5)
