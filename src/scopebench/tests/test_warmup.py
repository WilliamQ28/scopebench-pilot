"""Tests for model warm-up (self-hosted scale-to-zero handling); no network I/O."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from scopebench.warmup import (
    Endpoint,
    ModelStarting,
    WarmupTimeoutError,
    http_status_is_starting,
    served_name,
    warm_up,
)


def test_http_status_is_starting_only_for_503_model_starting() -> None:
    assert http_status_is_starting(503, '{"code":"model_starting"}')
    assert not http_status_is_starting(503, "service unavailable")  # 503 for another reason
    assert not http_status_is_starting(500, "model_starting")  # not a 503
    assert not http_status_is_starting(200, "")


def test_served_name_only_for_openai_chat() -> None:
    assert served_name("openai-chat:qwen36-27b") == "qwen36-27b"
    assert served_name("anthropic:claude-x") is None
    assert served_name(None) is None


def test_warm_up_returns_when_model_is_ready() -> None:
    calls: list[str] = []

    def probe(model: str, _base_url: str) -> None:
        calls.append(model)  # ready immediately: no exception

    warm_up([Endpoint("m", "http://e")], probe=probe, sleep=_fail_sleep)
    assert calls == ["m"]


def test_warm_up_polls_until_ready() -> None:
    attempts = {"n": 0}
    slept: list[float] = []

    def probe(_model: str, _base_url: str) -> None:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ModelStarting("spinning up")

    warm_up(
        [Endpoint("m", "http://e")],
        probe=probe,
        sleep=slept.append,
        monotonic=_clock(),
        poll_s=5.0,
    )
    assert attempts["n"] == 3
    assert slept == [5.0, 5.0]  # waited between the two not-ready probes


def test_warm_up_raises_if_never_ready() -> None:
    def probe(_model: str, _base_url: str) -> None:
        raise ModelStarting("spinning up")

    with pytest.raises(WarmupTimeoutError, match="did not warm up"):
        warm_up(
            [Endpoint("m", "http://e")],
            probe=probe,
            sleep=lambda _s: None,
            monotonic=_clock(step=100.0),
            deadline_s=250.0,
        )


def test_warm_up_propagates_hard_errors() -> None:
    def probe(_model: str, _base_url: str) -> None:
        raise RuntimeError("endpoint down")

    with pytest.raises(RuntimeError, match="endpoint down"):
        warm_up([Endpoint("m", "http://e")], probe=probe, sleep=_fail_sleep)


def test_warm_up_dedups_endpoints() -> None:
    calls: list[tuple[str, str]] = []

    def probe(model: str, base_url: str) -> None:
        calls.append((model, base_url))

    warm_up(
        [Endpoint("m", "http://e"), Endpoint("m", "http://e"), Endpoint("m2", "http://e")],
        probe=probe,
        sleep=_fail_sleep,
    )
    assert calls == [("m", "http://e"), ("m2", "http://e")]  # the duplicate probed once


def _fail_sleep(_seconds: float) -> None:
    raise AssertionError("should not sleep when the model is ready")


def _clock(step: float = 1.0) -> Callable[[], float]:
    ticks = {"t": 0.0}

    def monotonic() -> float:
        now = ticks["t"]
        ticks["t"] += step
        return now

    return monotonic
