"""Offline tests for the declarative experiment matrix (no model calls)."""

from __future__ import annotations

import hashlib
import json
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
    load_spec,
    plan,
    resolve_options,
    run_matrix,
)
from scopebench.runner import RunOptions

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
                    "deny_feedback": "white",
                    "hide_brief_from_judge": True,
                    "attacker_brief": str(brief),
                    "instruction": "program-brief-v3",
                    "persist": True,
                    "max_nudges": 3,
                    "request_limit": 99,
                    "timeout": 123.0,
                    "max_tool_result_chars": 8000,
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
    assert o.deny_feedback == "white"
    assert o.hide_brief_from_judge is True
    assert o.persist is True
    assert o.max_nudges == 3
    assert o.request_limit == 99
    assert o.timeout == 123.0
    assert o.max_tool_result_chars == 8000
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


def test_build_executor_runs_pipeline_and_saves_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    class _Variant:
        id: str = "program-brief-v3"

    class _Manifest:
        instruction_variants: ClassVar[list[_Variant]] = [_Variant()]

    async def fake_run_evals(
        manifests: list[object], options: RunOptions, **_kw: object
    ) -> object:
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
    out = tmp_path / "out"
    rc = matrix.main([str(spec_path), "--out", str(out), "--tasks-dir", str(tmp_path / "tasks")])
    assert rc == 0
    assert (out / "ledger.json").is_file()
    assert "completed" in capsys.readouterr().out


def test_main_returns_nonzero_on_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spec_path = tmp_path / "m.yaml"
    _ = spec_path.write_text(_SPEC_YAML, encoding="utf-8")

    def fake_build(_tasks_dir: Path) -> _Executor:
        def execute(_run: ResolvedRun, _out: Path) -> ExecResult:
            raise RuntimeError("boom")

        return execute

    monkeypatch.setattr(matrix, "build_executor", fake_build)
    rc = matrix.main(
        [str(spec_path), "--out", str(tmp_path / "o"), "--tasks-dir", str(tmp_path / "t")]
    )
    assert rc == 1
