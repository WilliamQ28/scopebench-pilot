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
MAX_UNIT_DETAILS = 10


def discover_processes(root: Path = ROOT, proc_root: Path = Path("/proc")) -> list[dict]:
    """Associate live experiment controllers with their own ledgers, not stale status rows."""
    processes = []
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = [
                s.decode(errors="replace")
                for s in (proc / "cmdline").read_bytes().split(b"\0")
                if s
            ]
            stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            if not args or stat[0] == "Z":
                continue
            if "python" not in Path(args[0]).name and Path(args[0]).name != "scopebench-matrix":
                continue
            cwd = (proc / "cwd").resolve(strict=True)

            def resolve(value: str, base: Path = cwd) -> Path:
                path = Path(value)
                return (path if path.is_absolute() else base / path).resolve()

            script = next((resolve(a) for a in args[1:] if a.endswith(".py")), None)
            matrix = "scopebench.matrix" in args or any(
                Path(a).name == "scopebench-matrix" for a in args[:2]
            )
            legacy = script == root / "experiments/run_information_suite.py"
            custom = (
                script is not None
                and script.name == "run.py"
                and script.is_relative_to(root / "experiments")
            )
            if not ((matrix and cwd.is_relative_to(root)) or legacy or custom):
                continue
            out = next((a.split("=", 1)[1] for a in args if a.startswith("--out=")), None)
            if "--out" in args:
                out = args[args.index("--out") + 1]
            if out:
                ledger = resolve(out) / "ledger.json"
            elif legacy:
                ledger = root / "experiments/judge-information-full-suite-20261022/ledger.json"
            elif custom:
                direct = script.parent / "ledger.json"
                ledger = direct if direct.is_file() else script.parent / "runs/ledger.json"
            else:
                continue
            processes.append(
                {
                    "pid": int(proc.name),
                    "ledger": str(ledger.resolve()),
                    "started_ticks": int(stat[19]),
                    # Do not expose arbitrary command-line values/credentials in the status output.
                    "command": str(script) if script else "scopebench.matrix",
                }
            )
        except (OSError, ValueError, IndexError):
            continue  # processes can exit during the snapshot
    return processes


def select_ledger(root: Path = ROOT, processes: list[dict] | None = None) -> tuple[Path, str]:
    """Prefer a live controller; otherwise show the latest ledger and say it is a fallback."""
    active = discover_processes(root) if processes is None else processes
    if active:
        process = max(active, key=lambda p: (p["started_ticks"], p["pid"]))
        return Path(process["ledger"]), f"active controller PID {process['pid']}"
    candidates = []
    for base in (root / "experiments", root / ".scopebench-runs"):
        candidates.extend(base.glob("*/ledger.json"))
        candidates.extend(base.glob("*/runs/ledger.json"))
    if not candidates:
        raise FileNotFoundError(
            "No experiment ledger found; use --ledger PATH for a custom location"
        )
    return max(candidates, key=lambda p: p.stat().st_mtime_ns).resolve(), (
        "no active controller; showing the most recently updated experiment"
    )


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


def collect(
    ledger_path: Path, *, include_archives: bool = True, processes: list[dict] | None = None
) -> dict:
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
    unit_results = []
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
                unit_results.append(
                    {
                        "unit": unit,
                        "labels": case.get("labels", {}),
                        "metrics": case.get("metrics", {}),
                        "report": record["report"],
                    }
                )
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
    processes = discover_processes() if processes is None else processes
    matching = [p for p in processes if Path(p["ledger"]).resolve() == ledger_path.resolve()]
    history = ledger.get("scheduler_history", [])
    control_path = ROOT / "experiments/judge-information-control.json"
    legacy_ledger = ROOT / "experiments/judge-information-full-suite-20261022/ledger.json"
    control = (
        json.loads(control_path.read_text())
        if ledger_path == legacy_ledger and control_path.exists()
        else {}
    )
    return {
        "observed_at_utc": now.isoformat(),
        "ledger": str(ledger_path),
        "experiment_name": ledger.get("spec", {}).get("name", ledger_path.parent.name),
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
        "processes": matching,
        "other_active_ledgers": sorted({p["ledger"] for p in processes if p not in matching}),
        "unit_results": unit_results,
        "control_file": control,
        "latest_scheduler_session": history[-1] if history else {},
        "elapsed_hours": elapsed,
        "finished_units_per_hour": rate,
        "first_pass_eta_at_overall_rate_utc": (now + timedelta(hours=remaining / rate)).isoformat()
        if rate and remaining
        else None,
        "warnings": warnings,
        "note": (
            "Snapshot; errored units count as finished for ETA, not successful evaluations. "
            + "Archives include available partial/error runs. No model requests made."
        ),
    }


def display(result: dict) -> str:
    """Render counts without confusing errors, evidence gaps, or candidates with protection."""
    if result.get("state") == "preparing":
        return (
            "Active experiment is preparing; its ledger has not been created yet.\n"
            + "Ledger: "
            + result["ledger"]
            + "\nSelection: "
            + result["selection_reason"]
        )
    status = result["status"]
    process = (
        "ALIVE"
        if result["processes"]
        else (
            "NOT FOUND (running ledger entries may be stale)"
            if status.get("running")
            else "NOT RUNNING"
        )
    )
    session = result["latest_scheduler_session"]
    live = session.get("live_control", {})
    selected = result["control_file"].get("mode", "not applicable")
    budget = live.get("budget_mib", session.get("memory_policy", {}).get("budget_mib", "?"))
    finished = status.get("completed", 0) + status.get("error", 0)
    lines = [
        "ScopeJudge experiment status - " + result["observed_at_utc"],
        "Experiment: " + result["experiment_name"],
        "Ledger: " + result["ledger"],
        "Selection: " + result.get("selection_reason", "explicit ledger"),
        "Process: " + process,
        f"First-pass finished: {finished}/{result['planned_units']} "
        f"({finished / result['planned_units']:.1%})"
        if result["planned_units"]
        else "No units planned",
        f"Reports: {status.get('completed', 0)} | Errors: {status.get('error', 0)} | "
        f"Active: {status.get('running', 0)} | Pending: {status.get('pending', 0)}",
        f"Selected mode: {selected} | Scheduler mode: {live.get('mode', 'fixed')} | "
        f"Slots: {live.get('concurrency', session.get('concurrency'))} | "
        f"Budget: {budget} MiB | "
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
    if len(result.get("unit_results", [])) <= MAX_UNIT_DETAILS:
        for unit in result.get("unit_results", []):
            labels = unit["labels"]
            lines.append(
                f"  {unit['unit']}: {labels.get('AttackOutcome', 'unreported')} / "
                + f"{labels.get('JudgeBreach', 'unreported')} "
                + f"({unit['metrics'].get('model_requests', 0)} model requests)"
            )
    if result["active_units"]:
        lines += ["", "Active units:"]
        lines.extend(f"  {u['unit']} (started {u['started']})" for u in result["active_units"])
    if result["failures"]:
        lines += ["", "Failure categories:"]
        lines.extend(f"  {count}: {reason}" for reason, count in result["failures"].items())
    if finished == result["planned_units"]:
        lines += ["", "Run finished; no units remaining."]
    else:
        lines += [
            "",
            f"Historical first-pass throughput: {result['finished_units_per_hour']:.2f} units/hour",
            "ETA at that rate (UTC): " + str(result["first_pass_eta_at_overall_rate_utc"]),
            "ETA includes errors. Retries need more time; graded reports do not prove protection.",
        ]
    if result.get("other_active_ledgers"):
        lines += ["", "Other active experiment ledgers:", *result["other_active_ledgers"]]
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
        help="Override automatic selection of the active (or latest) experiment ledger.",
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
            processes = discover_processes()
            ledger, selection_reason = (
                (args.ledger.resolve(), "explicit --ledger override")
                if args.ledger
                else select_ledger(processes=processes)
            )
            if not ledger.exists() and selection_reason.startswith("active"):
                result = {"state": "preparing", "ledger": str(ledger)}
            else:
                result = collect(
                    ledger, include_archives=not args.skip_archives, processes=processes
                )
            result["selection_reason"] = selection_reason
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
