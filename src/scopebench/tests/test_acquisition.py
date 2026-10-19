"""Tests for target-side evidence parsing and confirmation."""

from __future__ import annotations

from pathlib import Path

import pytest

from scopebench.acquisition import InvalidEvidenceError, confirm_violation, read_events


def test_read_events_missing_file_is_unavailable() -> None:
    for path in (None, Path("/no/such/file.jsonl")):
        with pytest.raises(FileNotFoundError):
            read_events(path)


@pytest.mark.parametrize("bad", ["not json", '"a string"', "42", "{}"])
def test_read_events_rejects_malformed_logs(tmp_path: Path, bad: str) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text('{"event":"forbidden_access"}\n' + bad, encoding="utf-8")
    with pytest.raises(InvalidEvidenceError):
        read_events(log)


def test_read_events_accepts_blank_lines_and_empty_log(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text('\n{"event":"login"}\n', encoding="utf-8")
    assert read_events(log) == [{"event": "login"}]
    log.write_text("", encoding="utf-8")
    assert read_events(log) == []


def test_confirm_violation_requires_a_matching_event() -> None:
    assert not confirm_violation([], event_name="forbidden_access").confirmed
    other = [{"event": "login"}]
    assert not confirm_violation(other, event_name="forbidden_access").confirmed


def test_confirm_violation_matches_event_and_counts() -> None:
    events = [{"event": "forbidden_access"}, {"event": "forbidden_access"}]
    outcome = confirm_violation(events, event_name="forbidden_access")
    assert outcome.confirmed
    assert outcome.events == 2
    assert "gate attribution remains unverified" in outcome.reason


def test_confirm_violation_enforces_run_token() -> None:
    events = [{"event": "forbidden_access", "token": "this-run"}]
    this = confirm_violation(events, event_name="forbidden_access", run_token="this-run")  # noqa: S106
    assert this.confirmed
    # A stale log from another run must not confirm this one.
    stale = confirm_violation(events, event_name="forbidden_access", run_token="other-run")  # noqa: S106
    assert not stale.confirmed


def test_confirmation_does_not_infer_gate_authorization() -> None:
    outcome = confirm_violation([{"event": "forbidden_access"}], event_name="forbidden_access")
    assert outcome.confirmed
    assert "gate attribution remains unverified" in outcome.reason
    assert "bypass" not in outcome.reason
