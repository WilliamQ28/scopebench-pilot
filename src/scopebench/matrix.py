"""Declarative experiment matrix for the ScopeJudge RoE study.

A matrix spec (YAML/JSON) names a task *panel*, a set of *arms* (each a bundle of actor/judge
models, harness settings, and budgets over the matrix ``defaults``), a repetition count, and an
arm ordering. :func:`plan` expands it into deterministically-identified run units, and
:func:`run_matrix` runs each through an injected runner, recording the resolved config, the code
revision, run ids, and completion/error status in a JSON *ledger*. Re-running resumes: completed
units are skipped and their evidence is never overwritten.

Execution is injected (the ``execute`` callable), so planning, ordering, resolution, and resume are
all testable offline with no model calls. :func:`build_executor` wires the real
:func:`scopebench.evals.run_evals` pipeline for live runs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import random
import subprocess
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal, cast

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from scopebench.admission import AdmissionSettings
from scopebench.evals import report_to_json, run_evals
from scopebench.manifest import load_manifest
from scopebench.memory import MemoryPolicy, MemorySample, sample_memory
from scopebench.runner import RunOptions
from scopebench.warmup import Endpoint, Probe, http_probe, served_name, warm_up
from scopejudge.protocol import DenyFeedback


class MatrixMismatchError(RuntimeError):
    """A resume's spec, revision, or resolved inputs differ from the stored ledger."""


class ArmSettings(BaseModel):
    """Harness settings for an arm; any field left unset inherits the matrix ``defaults``."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", protected_namespaces=())

    model: str | None = None
    base_url: str | None = None
    judge_model: str | None = None
    judge_base_url: str | None = None
    judge_timeout: float | None = None
    scopejudge: bool | None = None
    trajectory_judge: bool | None = None  # post-hoc Anthropic judge; off = qwen-only, no key needed
    deny_feedback: DenyFeedback | None = None
    hide_brief_from_judge: bool | None = None
    attacker_brief: str | None = None  # path to a brief file; read at resolution time
    instruction: str | None = None  # instruction-variant id
    persist: bool | None = None
    max_nudges: int | None = None
    request_limit: int | None = None
    timeout: float | None = None
    max_tool_result_chars: int | None = None
    hide_actor_reasoning_from_judge: bool | None = None
    record_reasoning: bool | None = None
    judge_actor_information: Literal["none", "justification", "full"] | None = None
    trim_reasoning_keep_recent: int | None = None  # keep reasoning only in the last N responses
    tools: list[str] | None = None


class Arm(ArmSettings):
    """A named arm."""

    name: str


class MatrixSpec(BaseModel):
    """A declarative experiment matrix."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", protected_namespaces=())

    name: str
    panel: list[str] = Field(min_length=1)  # task directory slugs
    arms: list[Arm] = Field(min_length=1)
    repeat: int = Field(default=1, ge=1)
    ordering: Literal["sequential", "interleaved", "random"] = "sequential"
    seed: int = 0
    defaults: ArmSettings = Field(default_factory=ArmSettings)

    @field_validator("arms")
    @classmethod
    def _unique_arm_names(cls, arms: list[Arm]) -> list[Arm]:
        """Reject duplicate arm names: they would collide into identical, overwriting run ids."""
        names = [arm.name for arm in arms]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate arm name(s): {', '.join(duplicates)}; must be unique")
        return arms


@dataclass(frozen=True)
class ResolvedRun:
    """One (arm, task, repetition) unit with a deterministic id and resolved settings."""

    run_id: str
    arm: str
    task: str
    rep: int
    options: RunOptions
    config: dict[str, object]  # JSON-safe resolved settings, for the ledger
    arm_pos: int = 0  # declaration order, for the interleaved ordering
    task_pos: int = 0


def load_spec(path: Path) -> MatrixSpec:
    """Load and validate a matrix spec from YAML or JSON (JSON is valid YAML)."""
    data: object = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return MatrixSpec.model_validate(data)


def resolve_options(spec: MatrixSpec, arm: Arm) -> tuple[RunOptions, dict[str, object]]:
    """Merge ``spec.defaults`` then the arm (arm wins) into RunOptions + a JSON-safe config dict.

    Only fields the merged settings actually set are applied, so unset fields keep the RunOptions
    defaults (no duplication of those defaults here).
    """
    # Defaults contribute their non-None values; the arm then overrides with the fields it
    # EXPLICITLY set (``model_fields_set``), so an arm can clear a default with an explicit null
    # (e.g. ``attacker_brief: null`` on a gate-off arm) -- not possible with plain exclude_none.
    arm_set = {k: getattr(arm, k) for k in arm.model_fields_set if k != "name"}
    merged = ArmSettings.model_validate(
        {**spec.defaults.model_dump(exclude={"name"}, exclude_none=True), **arm_set}
    )
    options = RunOptions(agent="harness")
    if merged.model is not None:
        options.model = merged.model
    if merged.base_url is not None:
        options.base_url = merged.base_url
    if merged.judge_model is not None:
        options.judge_model = merged.judge_model
    if merged.judge_base_url is not None:
        options.judge_base_url = merged.judge_base_url
    if merged.judge_timeout is not None:
        options.judge_timeout = merged.judge_timeout
    if merged.scopejudge is not None:
        options.scopejudge = merged.scopejudge
    if merged.trajectory_judge is not None:
        options.trajectory_judge = merged.trajectory_judge
    if merged.deny_feedback is not None:
        options.deny_feedback = merged.deny_feedback
    if merged.hide_brief_from_judge is not None:
        options.hide_brief_from_judge = merged.hide_brief_from_judge
    if merged.persist is not None:
        options.persist = merged.persist
    if merged.max_nudges is not None:
        options.max_nudges = merged.max_nudges
    if merged.request_limit is not None:
        options.request_limit = merged.request_limit
    if merged.timeout is not None:
        options.timeout = merged.timeout
    if merged.max_tool_result_chars is not None:
        options.max_tool_result_chars = merged.max_tool_result_chars
    if merged.hide_actor_reasoning_from_judge is not None:
        options.hide_actor_reasoning_from_judge = merged.hide_actor_reasoning_from_judge
    if merged.judge_actor_information is not None:
        options.judge_actor_information = merged.judge_actor_information
    if merged.record_reasoning is not None:
        options.record_reasoning = merged.record_reasoning
    if merged.trim_reasoning_keep_recent is not None:
        options.trim_reasoning_keep_recent = merged.trim_reasoning_keep_recent
    if merged.tools is not None:
        options.toolsets = merged.tools  # arm "tools" = toolset-group selection
    if merged.instruction is not None:
        options.instruction_id = merged.instruction
    if merged.attacker_brief is not None:
        brief_path = Path(merged.attacker_brief)
        options.attacker_brief = brief_path.read_text(encoding="utf-8")
        options.attacker_brief_id = brief_path.stem
    return options, _effective_config(options)


def _effective_config(options: RunOptions) -> dict[str, object]:
    """The FULLY RESOLVED effective settings for the ledger (incl. RunOptions defaults).

    Records what the run will actually use -- not only the fields an arm/defaults set -- so an arm
    relying on defaults is still fully described. Freezes the attacker brief by content hash so a
    later edit to the brief file is detectable.
    """
    config: dict[str, object] = {
        "model": options.model,
        "base_url": options.base_url,
        "judge_model": options.judge_model,
        "judge_base_url": options.judge_base_url,
        "judge_timeout": options.judge_timeout,
        "scopejudge": options.scopejudge,
        "trajectory_judge": options.trajectory_judge,
        "deny_feedback": options.deny_feedback,
        "hide_brief_from_judge": options.hide_brief_from_judge,
        "persist": options.persist,
        "max_nudges": options.max_nudges,
        "request_limit": options.request_limit,
        "timeout": options.timeout,
        "max_tool_result_chars": options.max_tool_result_chars,
        "trim_reasoning_keep_recent": options.trim_reasoning_keep_recent,
        "hide_actor_reasoning_from_judge": options.hide_actor_reasoning_from_judge,
        "record_reasoning": options.record_reasoning,
        "judge_actor_information": options.judge_actor_information,
        "tools": options.toolsets,
        "instruction_id": options.instruction_id,
        "attacker_brief_id": options.attacker_brief_id,
    }
    if options.attacker_brief is not None:
        config["attacker_brief_sha256"] = hashlib.sha256(
            options.attacker_brief.encode()
        ).hexdigest()
    return config


def plan(spec: MatrixSpec) -> list[ResolvedRun]:
    """Expand the matrix into ordered run units (arm x task x repetition)."""
    units: list[ResolvedRun] = []
    for arm_pos, arm in enumerate(spec.arms):
        options, config = resolve_options(spec, arm)  # resolved once per arm (reads its brief once)
        units.extend(
            ResolvedRun(
                run_id=f"{arm.name}__{task}__r{rep}",
                arm=arm.name,
                task=task,
                rep=rep,
                options=options,
                config=config,
                arm_pos=arm_pos,
                task_pos=task_pos,
            )
            for task_pos, task in enumerate(spec.panel)
            for rep in range(1, spec.repeat + 1)
        )
    return _order(units, spec)


def _order(units: list[ResolvedRun], spec: MatrixSpec) -> list[ResolvedRun]:
    """Apply the arm-ordering policy (bias control across time-correlated endpoint drift)."""
    if spec.ordering == "random":
        shuffled = list(units)
        random.Random(spec.seed).shuffle(shuffled)  # noqa: S311 - experiment ordering, not crypto
        return shuffled
    if spec.ordering == "interleaved":
        # Rotate through arms: group by (rep, task) so every arm runs back-to-back per cell.
        return sorted(units, key=lambda u: (u.rep, u.task_pos, u.arm_pos))
    return units  # sequential: arm-major, as built


def code_revision() -> str:
    """The current git commit (short), or ``unknown`` outside a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 - git resolved via PATH
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    return out.stdout.strip() or "unknown"


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class AttemptRecord(BaseModel):
    """Status and evidence for one execution attempt, retained across retries."""

    status: Literal["pending", "running", "completed", "error"]
    started: str | None = None
    ended: str | None = None
    report: str | None = None
    evidence_dir: str | None = None
    error: str | None = None
    # Graded-outcome evidence recorded so "completed" is defensible (see ExecResult.ok).
    cases: int = 0
    failures: int = 0
    expected: int = 0


class RunRecord(AttemptRecord):
    """Latest run state plus its resolved configuration and complete attempt history."""

    config: dict[str, object]
    attempts: list[AttemptRecord] = Field(default_factory=list)


@dataclass
class ExecResult:
    """What an executor reports back: the saved report plus the counts that gate completion."""

    report: Path | None = None
    cases: int = 0
    failures: int = 0
    expected: int = 0

    @property
    def ok(self) -> bool:
        """A run counts as completed only if it graded every expected case with no failures."""
        return self.expected > 0 and self.cases == self.expected and self.failures == 0


@dataclass
class Ledger:
    """Resumable record of a matrix run: resolved configs, revision, run ids, and status."""

    spec: dict[str, object]
    code_revision: str
    created: str
    runs: dict[str, RunRecord]
    path: Path
    scheduler_history: list[dict[str, object]] = dataclasses.field(default_factory=list)

    def save(self) -> None:
        """Atomically write the ledger (temp file + os.replace), so a crash cannot truncate it."""
        payload = {
            "scheduler_history": self.scheduler_history,
            "spec": self.spec,
            "code_revision": self.code_revision,
            "created": self.created,
            "runs": {rid: rec.model_dump() for rid, rec in self.runs.items()},
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        _ = tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _ = tmp.replace(self.path)  # atomic on POSIX/NTFS: readers see old or new, never partial

    @classmethod
    def load_or_init(
        cls, out_dir: Path, spec: MatrixSpec, units: list[ResolvedRun], *, revision: str, now: str
    ) -> Ledger:
        """Load an existing ledger (preserving completed evidence) or start a fresh one.

        The stored spec, revision, and resolved configuration (including brief content hashes)
        must match. A mismatch raises :class:`MatrixMismatchError` before touching any evidence.
        Start a new ``--out`` directory or restore the original inputs to proceed.
        """
        path = out_dir / "ledger.json"
        if path.is_file():
            raw = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
            stored_spec = cast("dict[str, object]", raw["spec"])
            stored_revision = cast("str", raw["code_revision"])
            if stored_spec != spec.model_dump(mode="json"):
                raise MatrixMismatchError(
                    f"{path} was created for a different matrix spec; use a new --out directory"
                )
            if stored_revision != revision:
                raise MatrixMismatchError(
                    f"{path} was created at code revision {stored_revision}, not {revision}; use a "
                    + "new --out directory or check out that revision to resume"
                )
            raw_runs = cast("dict[str, object]", raw.get("runs", {}))
            runs = {rid: RunRecord.model_validate(rec) for rid, rec in raw_runs.items()}
            ledger = cls(stored_spec, stored_revision, cast("str", raw["created"]), runs, path)
            ledger.scheduler_history = cast(
                "list[dict[str, object]]", raw.get("scheduler_history", [])
            )
        else:
            ledger = cls(spec.model_dump(mode="json"), revision, now, {}, path)
        # Add any planned units the ledger has not seen yet, as pending; never touch existing ones.
        for unit in units:
            if unit.run_id not in ledger.runs:
                ledger.runs[unit.run_id] = RunRecord(status="pending", config=unit.config)
            elif ledger.runs[unit.run_id].config != unit.config:
                raise MatrixMismatchError(
                    f"{path}: resolved configuration changed for {unit.run_id} "
                    + "(including attacker brief contents); use a new --out directory"
                )
        return ledger


def run_matrix(
    spec: MatrixSpec,
    out_dir: Path,
    *,
    execute: Callable[[ResolvedRun, Path], ExecResult],
    revision: str | None = None,
    now: Callable[[], str] = _utcnow,
    concurrency: int = 1,
    memory_policy: MemoryPolicy | None = None,
    memory_probe: Callable[[dict[str, Path]], MemorySample] | None = None,
    live_control: Callable[[], AdmissionSettings] | None = None,
    retry_errors: bool = True,
) -> Ledger:
    """Execute (or resume) the matrix, recording status in a crash-safe ledger.

    ``execute(run, out_dir) -> ExecResult`` runs one unit; a unit is marked ``completed`` only when
    its result is :attr:`ExecResult.ok` (every expected case graded, no failures) -- a report that
    is empty or carries infrastructure failures becomes ``error``, so a failed run is never skipped
    on resume. Every attempt receives a fresh directory under ``attempts/<run_id>/`` for reports
    and workspaces. Its status and errors remain in ``RunRecord.attempts`` across retries and
    interruptions. Completed units are skipped; the ledger is written after every status change.

    With ``concurrency > 1``, the units of each arm run through a thread pool of that size while the
    arms themselves stay sequential -- so only one judge model is pulled warm at a time (the
    self-hosted endpoint scales idle models to zero), and the concurrent units of a warm arm share
    its batched endpoint capacity. Ledger reads/writes are serialised by a lock; a per-unit failure
    still never aborts the matrix. ``concurrency = 1`` keeps the exact serial, planned-order run.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    units = plan(spec)
    ledger = Ledger.load_or_init(
        out_dir, spec, units, revision=revision or code_revision(), now=now()
    )
    session: dict[str, object] = {
        "started": now(),
        "concurrency": concurrency,
        "scheduler_revision": code_revision(),
        "retry_errors": retry_errors,
        "memory_policy": dataclasses.asdict(memory_policy) if memory_policy is not None else None,
    }
    ledger.scheduler_history.append(session)
    ledger.save()
    units = [r for r in units if retry_errors or ledger.runs[r.run_id].status != "error"]
    stop_requested = False
    lock = threading.Lock()
    active_paths: dict[str, Path] = {}

    def process(run: ResolvedRun) -> None:
        with lock:
            record = ledger.runs[run.run_id]
            if record.status == "completed":
                return
            # Preserve pre-history ledgers too, before clearing the latest attempt's state.
            if not record.attempts and record.status != "pending":
                record.attempts.append(AttemptRecord.model_validate(record.model_dump()))
            attempts_dir = out_dir / "attempts" / run.run_id
            attempts_dir.mkdir(parents=True, exist_ok=True)
            attempt_dir = Path(tempfile.mkdtemp(prefix="attempt-", dir=attempts_dir))
            record.status = "running"
            record.started = now()
            record.evidence_dir = str(attempt_dir)
            active_paths[run.run_id] = attempt_dir
            record.ended = record.report = record.error = None
            record.cases = record.failures = record.expected = 0
            record.attempts.append(AttemptRecord.model_validate(record.model_dump()))
            ledger.save()
        try:
            result = execute(run, attempt_dir)
        except Exception as exc:  # noqa: BLE001 - one unit's failure must not abort the matrix
            with lock:
                record.status = "error"
                record.error = f"{type(exc).__name__}: {exc}"
                record.ended = now()
                record.attempts[-1] = AttemptRecord.model_validate(record.model_dump())
                ledger.save()
            return
        with lock:
            record.report = str(result.report) if result.report is not None else None
            record.cases, record.failures, record.expected = (
                result.cases,
                result.failures,
                result.expected,
            )
            if result.ok:
                record.status = "completed"
            else:
                record.status = "error"
                record.error = (
                    f"incomplete: graded {result.cases}/{result.expected} cases, "
                    + f"{result.failures} failure(s)"
                )
            record.ended = now()
            record.attempts[-1] = AttemptRecord.model_validate(record.model_dump())
            ledger.save()

    def memory_batch(batch: list[ResolvedRun], policy: MemoryPolicy) -> None:
        nonlocal stop_requested
        pending = [r for r in batch if ledger.runs[r.run_id].status != "completed"]
        futures: dict[Future[None], ResolvedRun] = {}
        probe_memory = memory_probe or sample_memory
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while pending or futures:
                for future in list(futures):
                    if future.done():
                        future.result()
                        del futures[future]
                if not pending and not futures:
                    break
                slots = concurrency
                if live_control is not None:
                    settings = live_control()
                    slots = min(concurrency, settings.concurrency)
                    policy.budget_mib = settings.budget_mib
                    stop_requested = settings.drain
                    if stop_requested:
                        slots = 0
                    control = dataclasses.asdict(settings)
                    with lock:
                        if session.get("live_control") != control:
                            session["live_control"] = control
                            history = cast(
                                "list[dict[str, object]]", session.setdefault("control_changes", [])
                            )
                            history.append({"at": now(), **control})
                            ledger.save()
                    if stop_requested and not futures:
                        break
                active = {r.run_id: r.task for r in futures.values()}
                with lock:
                    paths = {rid: active_paths[rid] for rid in active if rid in active_paths}
                sample = probe_memory(paths)
                policy.observe(sample, active)
                for run in list(pending):
                    if len(futures) >= slots:
                        break
                    if policy.allows(run.task, sample, active):
                        futures[pool.submit(process, run)] = run
                        active[run.run_id] = run.task
                        pending.remove(run)
                with lock:
                    session["observed_task_peaks_mib"] = dict(policy.peaks)
                    session["last_docker_mib"] = sample.total_mib
                    session["last_available_mib"] = sample.available_mib
                    waiting = bool(pending) and len(futures) < slots
                    if waiting and not session.get("waiting_for_memory"):
                        print(
                            f"Waiting for memory: Docker {sample.total_mib:.0f} MiB, "
                            + f"host available {sample.available_mib:.0f} MiB; "
                            + f"admission budget {policy.budget_mib} MiB.",
                            flush=True,
                        )
                    session["waiting_for_memory"] = waiting
                    ledger.save()
                # Avoid a busy loop even when no case fits. KeyboardInterrupt remains available.
                _ = wait(list(futures), timeout=policy.poll_seconds, return_when=FIRST_COMPLETED)
                if not futures:
                    _ = threading.Event().wait(policy.poll_seconds)

    if memory_policy is not None:
        if concurrency <= 0:
            raise ValueError("Concurrency must be positive.")
        if concurrency == 1:
            memory_batch(units, memory_policy)
        else:
            groups: dict[str, list[ResolvedRun]] = {}
            for run in units:
                groups.setdefault(run.arm, []).append(run)
            for group in groups.values():
                memory_batch(group, memory_policy)
                if stop_requested:
                    break
    elif concurrency <= 1:
        for run in units:
            process(run)
    else:
        # Keep arms sequential while running their units concurrently.
        by_arm: dict[str, list[ResolvedRun]] = {}
        for run in units:
            by_arm.setdefault(run.arm, []).append(run)
        for arm_units in by_arm.values():
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                for _ in pool.map(process, arm_units):
                    pass

    return ledger


def endpoints_for(options: RunOptions) -> list[Endpoint]:
    """The self-hosted (``openai-chat``) actor/judge endpoints a unit needs warm before running.

    The actor is always included; the judge only when the gate is on and a judge model is set (a
    ``None`` judge shares the actor endpoint, already covered). The judge uses its own base URL
    when given, else the actor's. Non-``openai-chat`` models (e.g. a hosted Anthropic judge) do not
    scale to zero and are omitted.
    """
    endpoints: list[Endpoint] = []
    actor = served_name(options.model)
    if actor is not None and options.base_url is not None:
        endpoints.append(Endpoint(actor, options.base_url))
    if options.scopejudge:
        judge = served_name(options.judge_model)
        judge_url = options.judge_base_url or options.base_url
        if judge is not None and judge_url is not None:
            endpoints.append(Endpoint(judge, judge_url))
    return endpoints


def build_executor(
    tasks_dir: Path, *, probe: Probe | None = None
) -> Callable[[ResolvedRun, Path], ExecResult]:
    """Return the live executor: run one unit through the real pipeline and save its JSON report.

    Before each unit, warm its self-hosted actor/judge models (see :mod:`scopebench.warmup`) so a
    scaled-to-zero judge cannot fail-closed through the whole run and masquerade as a perfect
    monitor. ``probe`` overrides the warm-up probe for tests; by default it issues real requests
    with ``OPENAI_API_KEY``. Reports the graded-case and failure counts so :func:`run_matrix` can
    refuse to mark an empty or failure-carrying report as completed. ``expected`` is the number of
    instruction variants the unit runs (filtered by ``instruction_id``) times one repeat.
    """
    active_probe = probe if probe is not None else http_probe(os.environ.get("OPENAI_API_KEY", ""))

    def execute(run: ResolvedRun, out_dir: Path) -> ExecResult:
        manifest = load_manifest(tasks_dir / run.task)
        # plan() shares one RunOptions across an arm's units, so per-unit state (the workspace) is
        # set on a COPY -- never mutate the shared object, which concurrent arm-mates also read.
        options = dataclasses.replace(
            run.options, workspace_root=out_dir / "workspaces" / run.run_id
        )
        warm_up(endpoints_for(options), probe=active_probe)
        report = asyncio.run(run_evals([manifest], options, repeat=1, progress=False))
        data = report_to_json(report)
        report_path = out_dir / f"{run.run_id}.json"
        _ = report_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        expected = sum(
            1
            for variant in manifest.instruction_variants
            if options.instruction_id in (None, variant.id)
        )
        return ExecResult(
            report=report_path,
            cases=len(cast("list[object]", data["cases"])),
            failures=len(cast("list[object]", data["failures"])),
            expected=expected,
        )

    return execute


def task_memory_argument(value: str) -> tuple[str, int]:
    """Parse a task=MiB startup reservation for the CLI."""
    task, separator, amount = value.partition("=")
    if not separator or not task or not amount.isdecimal() or int(amount) <= 0:
        raise ValueError("Expected task-slug=positive-MiB.")
    return task, int(amount)


def main(argv: list[str] | None = None) -> int:
    """CLI: resolve and run (or resume) a matrix spec, writing a ledger + per-unit reports."""
    import argparse  # noqa: PLC0415 - only the CLI entry needs argparse

    parser = argparse.ArgumentParser(
        prog="scopebench-matrix", description="Run a declarative ScopeBench experiment matrix."
    )
    _ = parser.add_argument("spec", type=Path, help="matrix spec file (YAML or JSON)")
    _ = parser.add_argument("--out", type=Path, required=True, help="output dir (ledger + reports)")
    _ = parser.add_argument(
        "--tasks-dir", type=Path, required=True, help="task directory root (holds the panel slugs)"
    )
    _ = parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="units run per arm at once (arms stay sequential; default 1 = serial)",
    )
    _ = parser.add_argument(
        "--memory-budget-mib",
        type=int,
        default=2048,
        help="Soft Docker admission budget including overhead (default 2048).",
    )
    _ = parser.add_argument(
        "--memory-overhead-mib",
        type=int,
        default=512,
        help="Reserved daemon/cache overhead inside the budget.",
    )
    _ = parser.add_argument(
        "--task-default-memory-mib",
        type=int,
        default=384,
        help="Startup reservation for unprofiled tasks.",
    )
    _ = parser.add_argument(
        "--task-memory-mib",
        type=task_memory_argument,
        action="append",
        default=[],
        help="Per-task reservation override: task-slug=MiB (repeatable).",
    )
    _ = parser.add_argument(
        "--min-available-mib",
        type=int,
        default=1024,
        help="Keep this much host memory free (Linux and Windows when using WSL).",
    )
    _ = parser.add_argument("--memory-poll-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    try:
        memory_policy = MemoryPolicy(
            budget_mib=cast("int", args.memory_budget_mib),
            overhead_mib=cast("int", args.memory_overhead_mib),
            default_task_mib=cast("int", args.task_default_memory_mib),
            min_available_mib=cast("int", args.min_available_mib),
            poll_seconds=cast("float", args.memory_poll_seconds),
            task_mib=dict(cast("list[tuple[str, int]]", args.task_memory_mib)),
        )
        if cast("int", args.concurrency) <= 0:
            parser.error("Concurrency must be positive.")
    except ValueError as exc:
        parser.error(str(exc))
    # Populate os.environ from ./.env (real env vars still win) so the actor/judge endpoint
    # credentials reach the run, matching the plain `scopebench` CLI. Without this a matrix run
    # launched from a bare shell would have no OPENAI_API_KEY/OPENAI_BASE_URL.
    _ = load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    spec = load_spec(cast("Path", args.spec))
    out_dir = cast("Path", args.out)
    ledger = run_matrix(
        spec,
        out_dir,
        execute=build_executor(cast("Path", args.tasks_dir)),
        concurrency=cast("int", args.concurrency),
        memory_policy=memory_policy,
    )
    done = sum(1 for record in ledger.runs.values() if record.status == "completed")
    errored = sum(1 for record in ledger.runs.values() if record.status == "error")
    print(f"matrix '{spec.name}': {done} completed, {errored} errored; ledger at {ledger.path}")
    return 1 if errored else 0


if __name__ == "__main__":
    raise SystemExit(main())
