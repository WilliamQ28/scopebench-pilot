"""Run the information suite; avoid repeating an unavailable model's cold-start timeout."""

# ruff: noqa: INP001, T201 -- standalone operational CLI

from pathlib import Path
from threading import Lock

from dotenv import load_dotenv

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
    load_dotenv(override=False)
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
        Path("experiments/judge-information-full-suite-20261022"),
        execute=guarded,
        concurrency=4,
        memory_policy=MemoryPolicy(budget_mib=2048, overhead_mib=512, min_available_mib=1024),
    )
    statuses = {
        status: sum(r.status == status for r in ledger.runs.values())
        for status in ("completed", "error", "running", "pending")
    }
    print(f"Finished: {statuses}; ledger: {ledger.path}", flush=True)
    return int(bool(statuses["error"]))


if __name__ == "__main__":
    raise SystemExit(main())
