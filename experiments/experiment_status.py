"""Read-only experiment snapshot for progress reports; never starts evaluations."""

# ruff: noqa: INP001, T201 -- standalone reporting CLI
import argparse
import collections
import hashlib
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_WATCH_SECONDS = 10
MAX_WATCH_SECONDS = 86400


def failure_category(reason: str) -> str:
    """Collapse noisy provider/Compose logs into actionable categories."""
    categories = (
        (
            "all predefined address pools have been fully subnetted",
            "Docker address-pool exhaustion",
        ),
        ("maximum context length", "Actor context overflow"),
        ("agent exceeded 7200s", "Task timeout (7200 seconds)"),
        ("Connection error", "Model connection failure"),
        ("status_code: 502", "Model gateway failure (502)"),
        ("Tool 'todo' exceeded", "Tool validation retry exhaustion"),
        ("Model token limit", "Provider output-token exhaustion"),
        ("PermissionError", "Task filesystem permission failure"),
        ("WarmupTimeoutError", "Model warm-up timeout"),
        ("Deferred:", "Deferred after model warm-up failure"),
        ("compose startup failed", "Docker Compose startup failure"),
    )
    return next(
        (name for needle, name in categories if needle in reason), reason.splitlines()[0][:180]
    )


def collect(ledger_path: Path, *, include_archives: bool = True) -> dict:
    """Read a coherent ledger snapshot; archive scanning is optional for fast polling."""
    raw = ledger_path.read_bytes()
    ledger = json.loads(raw)
    now = datetime.now(UTC)
    arms = collections.defaultdict(collections.Counter)
    labels = collections.defaultdict(collections.Counter)
    failures = collections.Counter()
    warnings = []
    candidates = []
    running = []
    archives = collections.Counter()
    for unit, record in ledger["runs"].items():
        arm = unit.split("__")[0]
        arms[arm][record["status"]] += 1
        if record["status"] == "running":
            running.append({"unit": unit, "started": record.get("started")})
        if record["status"] == "error" and not record.get("report"):
            failures[failure_category(record.get("error") or "Unknown pre-report failure")] += 1
        if record.get("report") and record["status"] in ("completed", "error"):
            try:
                report = json.loads(Path(record["report"]).read_text())
            except (OSError, ValueError) as exc:
                warnings.append(f"{unit}: cannot read report: {exc}")
                continue
            for failure in report.get("failures", []):
                reason = failure.get("error", "unknown")
                failures[failure_category(reason)] += 1
            for case in report.get("cases", []):
                for label, value in case.get("labels", {}).items():
                    labels[arm + "/" + label][value] += 1
                metrics = case.get("metrics", {})
                for key in (
                    "gate_allowed",
                    "gate_denied",
                    "gate_uncertain",
                    "model_requests",
                    "capability_pass",
                    "capability_fail",
                ):
                    arms[arm][key] += metrics.get(key, 0)
                arms[arm]["zero_gate_reports"] += not any(
                    metrics.get(key, 0) for key in ("gate_allowed", "gate_denied", "gate_uncertain")
                )
                if case.get("labels", {}).get("JudgeBreach") == "candidate-breach":
                    candidates.append(
                        {
                            "unit": unit,
                            "report": record["report"],
                            "evidence_dir": record["evidence_dir"],
                        }
                    )
        if include_archives and record.get("evidence_dir"):
            for path in Path(record["evidence_dir"]).rglob("model-responses.jsonl"):
                archives["files"] += 1
                archives["bytes"] += path.stat().st_size
                with path.open() as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            archives["incomplete_lines"] += 1
                            continue
                        role = event["role"]
                        archives[role + "_responses"] += 1
                        for part in event["response"].get("parts", []):
                            if part.get("part_kind") == "thinking":
                                archives[role + "_reasoning_chars"] += len(part.get("content", ""))
    statuses = collections.Counter(record["status"] for record in ledger["runs"].values())
    finished = statuses["completed"] + statuses["error"]
    elapsed = (now - datetime.fromisoformat(ledger["created"])).total_seconds() / 3600
    rate = finished / elapsed if elapsed > 0 else 0
    remaining = len(ledger["runs"]) - finished
    processes = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                cmd = (
                    (proc / "cmdline")
                    .read_bytes()
                    .replace(b"\0", b" ")
                    .decode(errors="replace")
                    .strip()
                )
                if (
                    "experiments/run_information_suite.py" in cmd
                    and "python" in cmd
                    and "/bin/bash" not in cmd
                ):
                    processes.append({"pid": int(proc.name), "command": cmd})
            except OSError:
                pass
    return {
        "observed_at_utc": now.isoformat(),
        "ledger": str(ledger_path),
        "ledger_sha256": hashlib.sha256(raw).hexdigest(),
        "evaluation_revision": ledger["code_revision"],
        "checkout_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - fixed reporting command
            text=True,
            cwd=ROOT,
        ).strip(),
        "planned_units": len(ledger["runs"]),
        "status": dict(statuses),
        "per_arm": dict(arms),
        "labels": dict(labels),
        "failures": dict(failures),
        "candidate_breaches": candidates,
        "active_units": running,
        "response_archives": dict(archives),
        "processes": processes,
        "control_file": json.loads(
            (ROOT / "experiments/judge-information-control.json").read_text()
        ),
        "latest_scheduler_session": ledger["scheduler_history"][-1],
        "elapsed_hours": elapsed,
        "finished_units_per_hour": rate,
        "first_pass_eta_at_overall_rate_utc": (now + timedelta(hours=remaining / rate)).isoformat()
        if rate
        else None,
        "warnings": warnings,
        "note": (
            "Snapshot; errored units count as finished for ETA, not successful evaluations. "
            + "Archives include available partial/error runs. No model requests made."
        ),
    }


def display(result: dict) -> str:
    """Render counts without confusing errors, evidence gaps, or candidates with protection."""
    status = result["status"]
    process = "ALIVE" if result["processes"] else "NOT FOUND (running ledger entries may be stale)"
    session = result["latest_scheduler_session"]
    live = session.get("live_control", {})
    selected = result["control_file"].get("mode", "unknown")
    finished = status.get("completed", 0) + status.get("error", 0)
    lines = [
        "ScopeJudge experiment status - " + result["observed_at_utc"],
        "Process: " + process,
        f"First-pass finished: {finished}/{result['planned_units']} "
        f"({finished / result['planned_units']:.1%})"
        if result["planned_units"]
        else "No units planned",
        f"Reports: {status.get('completed', 0)} | Errors: {status.get('error', 0)} | "
        f"Active: {status.get('running', 0)} | Pending: {status.get('pending', 0)}",
        f"Selected mode: {selected} | Scheduler mode: {live.get('mode', 'legacy')} | "
        f"Slots: {live.get('concurrency', session.get('concurrency'))} | "
        f"Budget: {live.get('budget_mib', '?')} MiB | "
        f"Waiting for memory: {session.get('waiting_for_memory', False)}",
        "",
        "Arm                                      Reports Errors Active Pending "
        + "Candidates MissingEvidence",
    ]
    for arm, counts in result["per_arm"].items():
        evidence = result["labels"].get(arm + "/JudgeBreach", {})
        lines.append(
            f"{arm:<40} {counts.get('completed', 0):>6} {counts.get('error', 0):>6} "
            f"{counts.get('running', 0):>6} {counts.get('pending', 0):>7} "
            f"{evidence.get('candidate-breach', 0):>10} "
            f"{evidence.get('insufficient-evidence', 0):>12}"
        )
    lines += [
        "",
        f"Candidate breaches: {len(result['candidate_breaches'])} (not independently confirmed)",
    ]
    lines.extend("  " + c["unit"] for c in result["candidate_breaches"])
    if result["active_units"]:
        lines += ["", "Active units:"]
        lines.extend(f"  {u['unit']} (started {u['started']})" for u in result["active_units"])
    if result["failures"]:
        lines += ["", "Failure categories:"]
        lines.extend(f"  {count}: {reason}" for reason, count in result["failures"].items())
    lines += [
        "",
        f"Historical first-pass throughput: {result['finished_units_per_hour']:.2f} units/hour",
        "ETA at that rate (UTC): " + str(result["first_pass_eta_at_overall_rate_utc"]),
        "ETA includes errors. Retries need more time; graded reports do not prove protection.",
    ]
    if result["failures"].get("Docker address-pool exhaustion"):
        lines.append(
            "ALERT: Docker network pool exhaustion recorded; failed startups need retries."
        )
    lines.extend("WARNING: " + warning for warning in result["warnings"])
    return "\n".join(lines)


def main() -> int:
    """Print once or watch without starting/stopping tasks or making model requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger",
        type=Path,
        default=ROOT / "experiments/judge-information-full-suite-20261022/ledger.json",
    )
    parser.add_argument("--out", type=Path, help="Write a JSON snapshot to this file")
    parser.add_argument("--human", action="store_true", help="Readable summary instead of JSON")
    parser.add_argument(
        "--skip-archives", action="store_true", help="Skip large reasoning archives for fast checks"
    )
    parser.add_argument(
        "--watch", type=float, metavar="SECONDS", help="Repeat until Ctrl+C; minimum 10 seconds"
    )
    args = parser.parse_args()
    if args.watch is not None and not MIN_WATCH_SECONDS <= args.watch <= MAX_WATCH_SECONDS:
        parser.error("--watch must be between 10 and 86400 seconds")
    try:
        while True:
            result = collect(args.ledger.resolve(), include_archives=not args.skip_archives)
            if args.out:
                args.out.write_text(json.dumps(result, indent=2) + "\n")
            if not args.out or args.human:
                print(display(result) if args.human else json.dumps(result, indent=2), flush=True)
            if args.watch is None:
                break
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("Stopped watching; the experiment was left running.")
    except (OSError, ValueError) as exc:
        print(f"Cannot read run state: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
