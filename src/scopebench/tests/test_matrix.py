"""Offline tests for the declarative experiment matrix (no model calls)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, cast

import pytest

from scopebench import matrix
from scopebench.matrix import (
    Arm,
    ExecResult,
    MatrixMismatchError,
    MatrixSpec,
    ResolvedRun,
    build_executor,
    endpoints_for,
    load_spec,
    plan,
    resolve_options,
    run_matrix,
)
from scopebench.runner import RunOptions
from scopebench.warmup import Endpoint

_Executor = Callable[[ResolvedRun, Path], ExecResult]

_SPEC_YAML = """
name: smoke
panel: [task-a, task-b]
repeat: 2
ordering: sequential
defaults:
  request_limit: 40
  scopejudge: true
arms:
  - name: adaptive
    model: openai-chat:qwen35-122b
  - name: persistent
    model: openai-chat:qwen35-122b
    persist: true
    request_limit: 150
    judge_model: anthropic:claude-x
"""


def _spec(**over: object) -> MatrixSpec:
    base: dict[str, object] = {
        "name": "m",
        "panel": ["task-a", "task-b"],
        "arms": [Arm(name="a1"), Arm(name="a2")],
        "repeat": 2,
    }
    base.update(over)
    return MatrixSpec.model_validate(base)


def test_load_spec_parses_and_validates(tmp_path: Path) -> None:
    path = tmp_path / "matrix.yaml"
    _ = path.write_text(_SPEC_YAML, encoding="utf-8")
    spec = load_spec(path)
    assert spec.name == "smoke"
    assert [a.name for a in spec.arms] == ["adaptive", "persistent"]
    assert spec.defaults.request_limit == 40


def test_load_spec_rejects_unknown_field(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    _ = path.write_text("name: x\npanel: [t]\narms: [{name: a}]\nbogus: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bogus"):
        _ = load_spec(path)


def test_spec_rejects_duplicate_arm_names() -> None:
    with pytest.raises(ValueError, match="duplicate arm name"):
        _ = MatrixSpec.model_validate(
            {"name": "m", "panel": ["t"], "arms": [{"name": "a"}, {"name": "a"}]}
        )


def test_resolve_options_merges_defaults_then_arm() -> None:
    spec = MatrixSpec.model_validate(
        {
            "name": "m",
            "panel": ["t"],
            "defaults": {"request_limit": 40, "scopejudge": True},
            "arms": [{"name": "a", "request_limit": 150, "persist": True}],
        }
    )
    options, config = resolve_options(spec, spec.arms[0])
    assert options.request_limit == 150  # arm overrides default
    assert options.scopejudge is True  # inherited from defaults
    assert options.persist is True
    assert config["request_limit"] == 150
    assert config["scopejudge"] is True


def test_resolve_options_config_is_fully_effective_not_sparse() -> None:
    # An arm relying on RunOptions defaults is still fully described in the ledger config.
    spec = MatrixSpec.model_validate({"name": "m", "panel": ["t"], "arms": [{"name": "a"}]})
    _options, config = resolve_options(spec, spec.arms[0])
    assert config["request_limit"] == 40  # the RunOptions default, recorded (not omitted)
    assert config["timeout"] == 600.0
    assert config["deny_feedback"] == "reasoned"
    assert config["trajectory_judge"] is True  # RunOptions default recorded when unset
    assert config["trim_reasoning_keep_recent"] is None  # RunOptions default recorded when unset
    assert "attacker_brief_sha256" not in config  # no brief -> no hash


def test_resolve_options_maps_instruction_and_brief(tmp_path: Path) -> None:
    brief = tmp_path / "adaptive-monitor-aware.md"
    _ = brief.write_text("BE ADVERSARIAL", encoding="utf-8")
    spec = MatrixSpec.model_validate(
        {
            "name": "m",
            "panel": ["t"],
            "arms": [
                {"name": "a", "instruction": "program-brief-v3", "attacker_brief": str(brief)}
            ],
        }
    )
    options, _config = resolve_options(spec, spec.arms[0])
    assert options.instruction_id == "program-brief-v3"
    assert options.attacker_brief == "BE ADVERSARIAL"
    assert options.attacker_brief_id == "adaptive-monitor-aware"


def test_resolve_options_maps_every_field(tmp_path: Path) -> None:
    brief = tmp_path / "b.md"
    _ = brief.write_text("B", encoding="utf-8")
    spec = MatrixSpec.model_validate(
        {
            "name": "m",
            "panel": ["t"],
            "arms": [
                {
                    "name": "a",
                    "model": "openai-chat:qwen",
                    "base_url": "http://a",
                    "judge_model": "anthropic:claude-x",
                    "judge_base_url": "http://j",
                    "judge_timeout": 90.0,
                    "scopejudge": True,
                    "trajectory_judge": False,
                    "deny_feedback": "white",
                    "hide_brief_from_judge": True,
                    "attacker_brief": str(brief),
                    "instruction": "program-brief-v3",
                    "persist": True,
                    "max_nudges": 3,
                    "request_limit": 99,
                    "timeout": 123.0,
                    "max_tool_result_chars": 8000,
                    "trim_reasoning_keep_recent": 5,
                    "tools": ["web", "workflow"],
                }
            ],
        }
    )
    o, cfg = resolve_options(spec, spec.arms[0])
    assert o.model == "openai-chat:qwen"
    assert o.base_url == "http://a"
    assert o.judge_model == "anthropic:claude-x"
    assert o.judge_base_url == "http://j"
    assert o.judge_timeout == 90.0
    assert o.scopejudge is True
    assert o.trajectory_judge is False  # arm can disable the post-hoc Anthropic judge
    assert cfg["trajectory_judge"] is False
    assert o.deny_feedback == "white"
    assert o.hide_brief_from_judge is True
    assert o.persist is True
    assert o.max_nudges == 3
    assert o.request_limit == 99
    assert o.timeout == 123.0
    assert o.max_tool_result_chars == 8000
    assert o.trim_reasoning_keep_recent == 5
    assert cfg["trim_reasoning_keep_recent"] == 5
    assert o.toolsets == ["web", "workflow"]  # arm "tools" maps to RunOptions.toolsets
    assert o.instruction_id == "program-brief-v3"
    assert o.attacker_brief == "B"
    assert o.attacker_brief_id == "b"
    assert cfg["tools"] == ["web", "workflow"]  # the resolved config records the selection
    assert cfg["attacker_brief_sha256"] == hashlib.sha256(b"B").hexdigest()  # brief frozen by hash


def test_resolve_options_arm_can_clear_a_default() -> None:
    spec = MatrixSpec.model_validate(
        {
            "name": "m",
            "panel": ["t"],
            "defaults": {"request_limit": 150, "scopejudge": True},
            "arms": [{"name": "keep"}, {"name": "clear", "request_limit": None}],
        }
    )
    keep, _c1 = resolve_options(spec, spec.arms[0])
    clear, _c2 = resolve_options(spec, spec.arms[1])
    assert keep.request_limit == 150  # inherits the default
    assert clear.request_limit == 40  # explicit null overrides -> RunOptions default
    assert clear.scopejudge is True  # other defaults still inherited


def test_plan_expands_arms_times_panel_times_repeat() -> None:
    units = plan(_spec())
    assert len(units) == 2 * 2 * 2
    assert units[0].run_id == "a1__task-a__r1"
    assert {u.arm for u in units} == {"a1", "a2"}


def test_plan_sequential_is_arm_major() -> None:
    units = plan(_spec(ordering="sequential"))
    assert [u.arm for u in units[:4]] == ["a1", "a1", "a1", "a1"]  # all of a1 first


def test_plan_interleaved_rotates_arms_within_a_cell() -> None:
    units = plan(_spec(ordering="interleaved"))
    # First cell (rep1, task-a) runs both arms back to back.
    assert (units[0].arm, units[0].task, units[0].rep) == ("a1", "task-a", 1)
    assert (units[1].arm, units[1].task, units[1].rep) == ("a2", "task-a", 1)


def test_plan_random_is_seed_deterministic() -> None:
    a = [u.run_id for u in plan(_spec(ordering="random", seed=7))]
    b = [u.run_id for u in plan(_spec(ordering="random", seed=7))]
    c = [u.run_id for u in plan(_spec(ordering="random", seed=8))]
    assert a == b  # same seed -> same order
    assert set(a) == set(c)  # same set
    assert a != c  # different seed -> (almost surely) different order


def test_code_revision_handles_success_and_missing_git(monkeypatch: pytest.MonkeyPatch) -> None:
    class _P:
        stdout: str = "abc1234\n"

    def _ok(*_a: object, **_k: object) -> _P:
        return _P()

    monkeypatch.setattr(subprocess, "run", _ok)
    assert matrix.code_revision() == "abc1234"

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert matrix.code_revision() == "unknown"


def _fake_execute(calls: list[str]) -> _Executor:
    def execute(run: ResolvedRun, _out: Path) -> ExecResult:
        calls.append(run.run_id)
        return ExecResult(report=Path(f"{run.run_id}.json"), cases=1, failures=0, expected=1)

    return execute


def test_run_matrix_executes_all_and_records_ledger(tmp_path: Path) -> None:
    calls: list[str] = []
    ledger = run_matrix(
        _spec(), tmp_path, execute=_fake_execute(calls), revision="deadbee", now=lambda: "T"
    )
    assert len(calls) == 8  # every unit ran
    assert all(rec.status == "completed" for rec in ledger.runs.values())
    assert ledger.code_revision == "deadbee"
    saved = cast("dict[str, object]", json.loads((tmp_path / "ledger.json").read_text("utf-8")))
    assert saved["code_revision"] == "deadbee"
    assert len(cast("dict[str, object]", saved["runs"])) == 8


def test_run_matrix_records_errors_without_aborting(tmp_path: Path) -> None:
    def execute(run: ResolvedRun, _out: Path) -> ExecResult:
        if run.task == "task-a":
            raise RuntimeError("boom")
        return ExecResult(report=Path("r.json"), cases=1, failures=0, expected=1)

    ledger = run_matrix(_spec(), tmp_path, execute=execute, revision="r", now=lambda: "T")
    statuses = {rid: rec.status for rid, rec in ledger.runs.items()}
    assert statuses["a1__task-a__r1"] == "error"
    assert ledger.runs["a1__task-a__r1"].error is not None
    assert statuses["a1__task-b__r1"] == "completed"  # a later unit still ran


def test_run_matrix_marks_failed_or_empty_report_as_error(tmp_path: Path) -> None:
    # A report with no graded cases / failures must NOT be marked completed (so resume reruns).
    def execute(_run: ResolvedRun, _out: Path) -> ExecResult:
        return ExecResult(report=Path("r.json"), cases=0, failures=1, expected=1)

    ledger = run_matrix(_spec(), tmp_path, execute=execute, revision="r", now=lambda: "T")
    rec = ledger.runs["a1__task-a__r1"]
    assert rec.status == "error"
    assert rec.failures == 1
    assert rec.expected == 1
    assert "incomplete" in (rec.error or "")


def test_run_matrix_resumes_without_rerunning_completed(tmp_path: Path) -> None:
    calls1: list[str] = []
    _ = run_matrix(_spec(), tmp_path, execute=_fake_execute(calls1), revision="r", now=lambda: "T")
    assert len(calls1) == 8
    # Re-run with the SAME spec+revision: everything is completed, so nothing re-executes.
    calls2: list[str] = []
    ledger = run_matrix(
        _spec(), tmp_path, execute=_fake_execute(calls2), revision="r", now=lambda: "T2"
    )
    assert calls2 == []  # no re-execution
    assert ledger.code_revision == "r"  # original provenance preserved


def test_run_matrix_rejects_revision_mismatch_on_resume(tmp_path: Path) -> None:
    _ = run_matrix(_spec(), tmp_path, execute=_fake_execute([]), revision="r1", now=lambda: "T")
    with pytest.raises(MatrixMismatchError, match="code revision"):
        _ = run_matrix(_spec(), tmp_path, execute=_fake_execute([]), revision="r2", now=lambda: "T")


def test_run_matrix_rejects_spec_mismatch_on_resume(tmp_path: Path) -> None:
    # Same out dir, different spec (repeat changed) -> incompatible, must not silently reuse.
    _ = run_matrix(
        _spec(repeat=2), tmp_path, execute=_fake_execute([]), revision="r", now=lambda: "T"
    )
    with pytest.raises(MatrixMismatchError, match="different matrix spec"):
        _ = run_matrix(
            _spec(repeat=1), tmp_path, execute=_fake_execute([]), revision="r", now=lambda: "T"
        )


def test_resume_rejects_changed_brief_before_touching_evidence(tmp_path: Path) -> None:
    brief = tmp_path / "brief.md"
    brief.write_text("original", encoding="utf-8")
    spec = _spec(arms=[Arm(name="a", attacker_brief=str(brief))])
    out = tmp_path / "out"
    run_matrix(spec, out, execute=_fake_execute([]), revision="r")
    before = (out / "ledger.json").read_bytes()
    brief.write_text("changed", encoding="utf-8")
    calls: list[str] = []
    with pytest.raises(MatrixMismatchError, match="resolved configuration changed"):
        run_matrix(spec, out, execute=_fake_execute(calls), revision="r")
    assert calls == []
    assert (out / "ledger.json").read_bytes() == before


@pytest.mark.parametrize("failure", ["report", "exception", "interruption", "legacy"])
def test_retry_preserves_evidence_and_clears_latest_error(tmp_path: Path, failure: str) -> None:
    spec = _spec(panel=["task"], arms=[Arm(name="a")], repeat=1)
    rid = "a__task__r1"
    seen: list[Path] = []

    def fail(run: ResolvedRun, out: Path) -> ExecResult:
        seen.append(out)
        path = out / f"{run.run_id}.json"
        path.write_text('{"failure": "original"}', encoding="utf-8")
        if failure == "exception":
            raise RuntimeError("original failure")
        if failure == "interruption":
            raise KeyboardInterrupt
        return ExecResult(report=path, failures=1, expected=1)

    if failure == "interruption":
        with pytest.raises(KeyboardInterrupt):
            run_matrix(spec, tmp_path, execute=fail, revision="r")
    else:
        run_matrix(spec, tmp_path, execute=fail, revision="r")
    if failure == "legacy":
        path = tmp_path / "ledger.json"
        data = cast("dict[str, object]", json.loads(path.read_text()))
        runs = cast("dict[str, dict[str, object]]", data["runs"])
        del runs[rid]["attempts"]
        path.write_text(json.dumps(data), encoding="utf-8")

    def succeed(run: ResolvedRun, out: Path) -> ExecResult:
        seen.append(out)
        path = out / f"{run.run_id}.json"
        path.write_text('{"success": true}', encoding="utf-8")
        return ExecResult(report=path, cases=1, expected=1)

    ledger = run_matrix(spec, tmp_path, execute=succeed, revision="r")
    record = ledger.runs[rid]
    assert seen[0] != seen[1]
    assert json.loads((seen[0] / f"{rid}.json").read_text()) == {"failure": "original"}
    assert record.status == "completed"
    assert record.error is None
    assert record.failures == 0
    assert len(record.attempts) == 2
    first, second = record.attempts
    assert first.status == ("running" if failure == "interruption" else "error")
    assert first.evidence_dir == str(seen[0])
    assert second.status == "completed"
    assert second.error is None
    if failure != "interruption":
        assert first.error
    saved = cast("dict[str, object]", json.loads((tmp_path / "ledger.json").read_text()))
    saved_runs = cast("dict[str, dict[str, object]]", saved["runs"])
    assert len(cast("list[object]", saved_runs[rid]["attempts"])) == 2
    assert saved_runs[rid]["error"] is None


def test_build_executor_runs_pipeline_and_saves_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    class _Variant:
        id: str = "program-brief-v3"

    class _Manifest:
        instruction_variants: ClassVar[list[_Variant]] = [_Variant()]

    async def fake_run_evals(manifests: list[object], options: RunOptions, **_kw: object) -> object:
        seen["manifests"] = manifests
        seen["workspace_root"] = options.workspace_root
        return object()

    def fake_load_manifest(task_dir: Path) -> _Manifest:
        seen["task_dir"] = task_dir
        return _Manifest()

    def fake_report_to_json(_report: object) -> dict[str, object]:
        return {"cases": [{"name": "c1"}], "failures": []}  # one graded case, no failures

    monkeypatch.setattr(matrix, "load_manifest", fake_load_manifest)
    monkeypatch.setattr(matrix, "run_evals", fake_run_evals)
    monkeypatch.setattr(matrix, "report_to_json", fake_report_to_json)

    execute = build_executor(tmp_path / "tasks")
    run = ResolvedRun(
        run_id="a__t__r1",
        arm="a",
        task="task-x",
        rep=1,
        options=RunOptions(agent="harness"),  # no instruction filter -> all variants expected
        config={},
    )
    out = tmp_path / "out"
    out.mkdir()
    result = execute(run, out)
    assert result.report == out / "a__t__r1.json"
    assert result.cases == 1
    assert result.failures == 0
    assert result.expected == 1  # one instruction variant
    assert result.ok
    assert str(seen["task_dir"]).endswith("tasks/task-x")
    assert str(seen["workspace_root"]).endswith("workspaces/a__t__r1")


def test_endpoints_for_selects_self_hosted_actor_and_judge() -> None:
    # Actor + a self-hosted judge on its own base URL: both warmed.
    opts = RunOptions(
        agent="harness",
        model="openai-chat:qwen35-122b",
        base_url="http://actor",
        scopejudge=True,
        judge_model="openai-chat:qwen36-27b",
        judge_base_url="http://judge",
    )
    assert endpoints_for(opts) == [
        Endpoint("qwen35-122b", "http://actor"),
        Endpoint("qwen36-27b", "http://judge"),
    ]


def test_endpoints_for_omits_judge_when_gate_off_or_hosted() -> None:
    # Gate off: no judge endpoint. Hosted (non-openai-chat) actor: nothing to warm.
    gate_off = RunOptions(agent="harness", model="openai-chat:q", base_url="http://a")
    assert endpoints_for(gate_off) == [Endpoint("q", "http://a")]
    hosted = RunOptions(
        agent="harness", model="anthropic:claude-x", scopejudge=True, judge_model="anthropic:claude"
    )
    assert endpoints_for(hosted) == []


def test_endpoints_for_judge_falls_back_to_actor_base_url() -> None:
    opts = RunOptions(
        agent="harness",
        model="openai-chat:q",
        base_url="http://shared",
        scopejudge=True,
        judge_model="openai-chat:qj",  # no judge_base_url -> reuse the actor endpoint
    )
    assert endpoints_for(opts) == [
        Endpoint("q", "http://shared"),
        Endpoint("qj", "http://shared"),
    ]


def test_build_executor_warms_models_before_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    order: list[str] = []
    probed: list[str] = []

    class _Variant:
        id: str = "program-brief-v3"

    class _Manifest:
        instruction_variants: ClassVar[list[_Variant]] = [_Variant()]

    async def fake_run_evals(
        _manifests: list[object], _options: RunOptions, **_kw: object
    ) -> object:
        order.append("run")
        return object()

    def fake_load_manifest(_task_dir: Path) -> _Manifest:
        return _Manifest()

    def fake_report_to_json(_report: object) -> dict[str, object]:
        return {"cases": [{"name": "c"}], "failures": []}

    monkeypatch.setattr(matrix, "load_manifest", fake_load_manifest)
    monkeypatch.setattr(matrix, "run_evals", fake_run_evals)
    monkeypatch.setattr(matrix, "report_to_json", fake_report_to_json)

    def probe(model: str, _base_url: str) -> None:
        order.append("warm")
        probed.append(model)

    execute = build_executor(tmp_path / "tasks", probe=probe)
    run = ResolvedRun(
        run_id="a__t__r1",
        arm="a",
        task="t",
        rep=1,
        options=RunOptions(
            agent="harness",
            model="openai-chat:qwen35-122b",
            base_url="http://a",
            scopejudge=True,
            judge_model="openai-chat:qwen36-27b",
            judge_base_url="http://j",
        ),
        config={},
    )
    out = tmp_path / "out"
    out.mkdir()
    _ = execute(run, out)
    assert probed == ["qwen35-122b", "qwen36-27b"]  # both warmed
    assert order == ["warm", "warm", "run"]  # warming happens BEFORE the pipeline runs


def test_main_runs_matrix_and_writes_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec_path = tmp_path / "m.yaml"
    _ = spec_path.write_text(_SPEC_YAML, encoding="utf-8")

    def fake_build(_tasks_dir: Path) -> _Executor:
        def execute(run: ResolvedRun, out: Path) -> ExecResult:
            return ExecResult(report=out / f"{run.run_id}.json", cases=1, failures=0, expected=1)

        return execute

    monkeypatch.setattr(matrix, "build_executor", fake_build)
    monkeypatch.chdir(tmp_path)  # no .env here, so the dotenv load is a silent no-op
    out = tmp_path / "out"
    rc = matrix.main([str(spec_path), "--out", str(out), "--tasks-dir", str(tmp_path / "tasks")])
    assert rc == 0
    assert (out / "ledger.json").is_file()
    assert "completed" in capsys.readouterr().out


def test_main_loads_dotenv_from_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A matrix launched from a bare shell must still pick up the endpoint credentials from ./.env,
    # like the plain `scopebench` CLI, or the actor/judge would have no OPENAI_API_KEY/base URL.
    spec_path = tmp_path / "m.yaml"
    _ = spec_path.write_text(_SPEC_YAML, encoding="utf-8")
    _ = (tmp_path / ".env").write_text("SCOPEBENCH_DOTENV_PROBE=loaded\n", encoding="utf-8")
    monkeypatch.delenv("SCOPEBENCH_DOTENV_PROBE", raising=False)

    def fake_build(_tasks_dir: Path) -> _Executor:
        def execute(run: ResolvedRun, out: Path) -> ExecResult:
            return ExecResult(report=out / f"{run.run_id}.json", cases=1, failures=0, expected=1)

        return execute

    monkeypatch.setattr(matrix, "build_executor", fake_build)
    monkeypatch.chdir(tmp_path)
    _ = matrix.main(
        [str(spec_path), "--out", str(tmp_path / "out"), "--tasks-dir", str(tmp_path / "t")]
    )
    assert os.environ.get("SCOPEBENCH_DOTENV_PROBE") == "loaded"


def test_main_returns_nonzero_on_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "m.yaml"
    _ = spec_path.write_text(_SPEC_YAML, encoding="utf-8")

    def fake_build(_tasks_dir: Path) -> _Executor:
        def execute(_run: ResolvedRun, _out: Path) -> ExecResult:
            raise RuntimeError("boom")

        return execute

    monkeypatch.setattr(matrix, "build_executor", fake_build)
    monkeypatch.chdir(tmp_path)  # no .env here, so the dotenv load is a silent no-op
    rc = matrix.main(
        [str(spec_path), "--out", str(tmp_path / "o"), "--tasks-dir", str(tmp_path / "t")]
    )
    assert rc == 1
