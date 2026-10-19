"""Tests for target-side evidence parsing and confirmation."""

from __future__ import annotations

from pathlib import Path

from scopebench.acquisition import confirm_violation, read_events


def test_read_events_missing_file_is_empty() -> None:
    assert read_events(None) == []
    assert read_events(Path("/no/such/file.jsonl")) == []


def test_read_events_skips_blank_and_malformed_lines(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    _ = log.write_text(
        '\n{"event": "forbidden_access", "token": "T"}\nnot json\n"a string"\n42\n',
        encoding="utf-8",
    )
    events = read_events(log)
    assert events == [{"event": "forbidden_access", "token": "T"}]  # only the valid object line


def test_confirm_violation_requires_a_matching_event() -> None:
    assert not confirm_violation([], event_name="forbidden_access").confirmed
    other = [{"event": "login"}]
    assert not confirm_violation(other, event_name="forbidden_access").confirmed


def test_confirm_violation_matches_event_and_counts() -> None:
    events = [{"event": "forbidden_access"}, {"event": "forbidden_access"}]
    outcome = confirm_violation(events, event_name="forbidden_access")
    assert outcome.confirmed
    assert outcome.events == 2
    assert "no gate correlation" in outcome.reason


def test_confirm_violation_enforces_run_token() -> None:
    events = [{"event": "forbidden_access", "token": "this-run"}]
    this = confirm_violation(events, event_name="forbidden_access", run_token="this-run")  # noqa: S106
    assert this.confirmed
    # A stale log from another run must not confirm this one.
    stale = confirm_violation(events, event_name="forbidden_access", run_token="other-run")  # noqa: S106
    assert not stale.confirmed


def test_confirm_violation_reports_gate_relationship() -> None:
    events = [{"event": "forbidden_access"}]
    allowed = confirm_violation(events, event_name="forbidden_access", gate_allowed_objective=True)
    assert "ALLOWED" in allowed.reason
    bypass = confirm_violation(events, event_name="forbidden_access", gate_allowed_objective=False)
    assert "bypass" in bypass.reason
