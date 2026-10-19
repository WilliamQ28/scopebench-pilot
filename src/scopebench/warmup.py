"""Ensure self-hosted models are serving before a run uses them.

The self-hosted (Vulcan) endpoint scales idle models to zero and answers HTTP 503 ``model_starting``
(ready in minutes) until a model is warm again. The ScopeJudge gate treats a failed judge call as a
fail-closed ``uncertain`` decision -- so a judge model that is cold for an entire run would block
every action and masquerade as a *perfect* monitor, a measurement artifact that would silently
confound a judge-size comparison. Warming each model until it answers, and failing loudly if it
never does, keeps that artifact out of the data. Models that are not ``openai-chat`` self-hosted
endpoints (e.g. a hosted Anthropic judge) do not scale to zero and are not warmed here.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_OPENAI_CHAT_PREFIX = "openai-chat:"


class ModelStarting(Exception):  # noqa: N818 - a transient state, not a failure
    """The endpoint reports the model is still spinning up (scaled to zero)."""


class WarmupTimeoutError(RuntimeError):
    """A model did not become ready within the allotted deadline."""


@dataclass(frozen=True)
class Endpoint:
    """A self-hosted model to warm: the served model name and its base URL."""

    model: str
    base_url: str


# A probe raises :class:`ModelStarting` while the model spins up, returns on ready, and raises
# anything else on a hard error (so a genuinely broken endpoint fails the run, not fail-closes it).
Probe = Callable[[str, str], None]


def served_name(model: str | None) -> str | None:
    """The served model name for an ``openai-chat:<name>`` id, or ``None`` for anything else."""
    if model is None or not model.startswith(_OPENAI_CHAT_PREFIX):
        return None
    return model[len(_OPENAI_CHAT_PREFIX) :]


def _dedup(endpoints: Iterable[Endpoint]) -> list[Endpoint]:
    seen: dict[tuple[str, str], Endpoint] = {}
    for ep in endpoints:
        _ = seen.setdefault((ep.model, ep.base_url), ep)
    return list(seen.values())


def warm_up(
    endpoints: Iterable[Endpoint],
    *,
    probe: Probe,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    deadline_s: float = 600.0,
    poll_s: float = 20.0,
) -> None:
    """Poll each distinct endpoint until it serves; raise if one never does within ``deadline_s``.

    A ready model returns immediately (one cheap request). A scaled-to-zero model raises
    :class:`ModelStarting`; we wait ``poll_s`` and retry until ``deadline_s`` elapses, then raise
    :class:`WarmupTimeoutError`. Any other probe exception propagates unchanged.
    """
    for ep in _dedup(endpoints):
        start = monotonic()
        while True:
            try:
                probe(ep.model, ep.base_url)
            except ModelStarting:
                if monotonic() - start > deadline_s:
                    raise WarmupTimeoutError(
                        f"{ep.model} at {ep.base_url} did not warm up within {deadline_s:.0f}s"
                    ) from None
                sleep(poll_s)
                continue
            break


_HTTP_SERVICE_UNAVAILABLE = 503


def http_status_is_starting(code: int, detail: str) -> bool:
    """Whether an HTTP error is the endpoint's scale-to-zero spin-up signal (keep polling)."""
    return code == _HTTP_SERVICE_UNAVAILABLE and "model_starting" in detail


def http_probe(api_key: str, *, timeout_s: float = 30.0) -> Probe:  # pragma: no cover - network I/O
    """A real probe: a tiny chat-completion request.

    A cold (scaled-to-zero) model signals spin-up in more than one way -- a fast 503
    ``model_starting``, or accepting the connection and then stalling (a read timeout), or being
    briefly unreachable -- so all of those raise :class:`ModelStarting` and warm-up keeps polling.
    Any other HTTP error is a real fault and propagates.
    """

    def probe(model: str, base_url: str) -> None:
        body = json.dumps(
            {"model": model, "messages": [{"role": "user", "content": "ready?"}], "max_tokens": 1}
        ).encode()
        req = urllib.request.Request(  # noqa: S310 - fixed https endpoint from config
            base_url.rstrip("/") + "/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        try:
            # Reaching the body means HTTP 200 (urlopen raises on any error status), so a readiness
            # probe need not read it.
            with urllib.request.urlopen(req, timeout=timeout_s):  # noqa: S310
                pass
        except urllib.error.HTTPError as exc:
            if http_status_is_starting(exc.code, exc.read().decode("utf-8", "replace")):
                raise ModelStarting(model) from None
            raise
        except (TimeoutError, urllib.error.URLError) as exc:
            raise ModelStarting(model) from exc

    return probe
