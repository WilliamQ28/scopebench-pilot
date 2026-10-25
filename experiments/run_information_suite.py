"""Run the information suite; avoid repeating an unavailable model's cold-start timeout."""

# ruff: noqa: INP001, T201 -- standalone operational CLI

import argparse
import json
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import cast

from dotenv import load_dotenv

from scopebench.admission import read_control
from scopebench.matrix import (
    ExecResult,
    ResolvedRun,
    build_executor,
    endpoints_for,
    load_spec,
    run_matrix,
)
from scopebench.memory import MemoryPolicy
from scopebench.warmup import WarmupTimeoutError


def main() -> int:
    """Run/resume all units, retaining readiness failures as retryable ledger errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after-pid", type=int)
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()
    if args.after_pid is not None:
        pid = cast("int", args.after_pid)
        process = Path(f"/proc/{pid}/stat")
        try:
            identity = process.read_text().split()[21]
        except FileNotFoundError:
            identity = None
        print(f"Waiting for old scheduler {pid} to finish its active units", flush=True)
        while process.exists():
            try:
                state = process.read_text().split()
            except FileNotFoundError:
                break
            if state[2] == "Z" or state[21] != identity:
                break
            time.sleep(5)
    load_dotenv(override=False)
    out = Path("experiments/judge-information-full-suite-20261022")
    old_ledger = out / "ledger.json"
    revision = None
    peaks: dict[str, float] = {}
    if old_ledger.exists():
        stored = json.loads(old_ledger.read_text())
        revision = cast("str", stored["code_revision"])
        for session in stored.get("scheduler_history", []):
            for task, peak in session.get("observed_task_peaks_mib", {}).items():
                peaks[task] = max(peaks.get(task, 0), float(peak))
        # Only scheduler/operational changes may share the original evaluation revision.
        check = subprocess.run(  # noqa: S603 - fixed git command, revision is a ledger commit ID
            [  # noqa: S607 - fixed git executable
                "git",
                "diff",
                "--quiet",
                revision,
                "--",
                "src/agent_harness",
                "src/scopejudge",
                "src/scopebench",
                "tasks",
                "briefs",
                "pyproject.toml",
                "uv.lock",
                ":!src/scopebench/matrix.py",
                ":!src/scopebench/admission.py",
                ":!src/scopebench/tests",
            ],
            check=False,
        )
        if check.returncode:
            raise RuntimeError("Evaluation code changed: use a new experiment instead of resuming")
    control_path = Path("experiments/judge-information-control.json")
    spec = load_spec(Path("experiments/judge-information-full-suite.yaml"))
    executor = build_executor(Path("tasks/scopebench"))
    failed_models: set[str] = set()
    lock = Lock()

    def guarded(run: ResolvedRun, out: Path) -> ExecResult:
        with lock:
            unavailable = failed_models.intersection(ep.model for ep in endpoints_for(run.options))
        if unavailable:
            raise RuntimeError(
                f"Deferred: model warm-up already timed out this session: {sorted(unavailable)}. "
                "No evaluation was run; retry on resume."
            )
        try:
            return executor(run, out)
        except WarmupTimeoutError as exc:
            # The exception's first field is the served model ID. Already in-flight units may
            # finish their own warm-up, but subsequent units fail promptly rather than all
            # spending another 30 minutes waiting for the same unavailable deployment.
            with lock:
                failed_models.add(str(exc).split()[0])
            raise

    ledger = run_matrix(
        spec,
        out,
        revision=revision,
        execute=guarded,
        concurrency=8,
        live_control=lambda: read_control(control_path),
        retry_errors=cast("bool", args.retry_errors),
        memory_policy=MemoryPolicy(
            budget_mib=2048, overhead_mib=512, min_available_mib=1024, peaks=peaks
        ),
    )
    statuses = {
        status: sum(r.status == status for r in ledger.runs.values())
        for status in ("completed", "error", "running", "pending")
    }
    print(f"Finished: {statuses}; ledger: {ledger.path}", flush=True)
    return int(bool(statuses["error"]))


if __name__ == "__main__":
    raise SystemExit(main())
