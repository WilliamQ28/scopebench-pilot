"""Replay recorded judge inputs in raw and compact history format and compare the verdicts.

Replays historical custom-judge archives using the remaining Dreadnode AgentJudge core.
This changes the judging policy and is not a reproduction of legacy verdicts.

Validation for ``--judge-history-format compact``. Every recorded current-backend judge call
(``judge-inputs.jsonl``) is re-judged ``--repeats`` times in each format, with the jobs for each
call run in random order so endpoint drift cannot line up with format. The claim under test: the
judge disagrees with itself across formats no more often than it already disagrees with itself on
byte-identical input. The report gives within-format (test-retest) agreement, cross-format
agreement, their per-call difference with a cluster-bootstrap 95% CI, verdict rates, and judge
token use per format.

Both formats see the same steps. A recorded payload that was already bounded keeps its recorded
window, so this isolates serialization. In live runs, compact history also fits more steps into the
fixed window. That effect is real but separate, and this replay deliberately excludes it.

Warm the judge before a run: Vulcan scales idle models to zero, and a cold judge fails every call.
Failed calls are reported as errors and never counted as verdicts.

    python experiments/judge_format_replay.py experiments/judge-information-full-suite-20261022 \
        --judge-model openai-chat:qwen35-122b --sample 60 --repeats 3 --out replay.jsonl
"""

# ruff: noqa: INP001, T201, D103, S311 -- standalone experiment CLI; seeded sampling, not crypto
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import random
import statistics
import time
from pathlib import Path
from typing import cast

from dotenv import load_dotenv
from pydantic_ai import UsageLimits

from scopebench.warmup import Endpoint, http_probe, served_name, warm_up
from scopejudge.capability import ScopeJudge, _compact_history
from scopejudge.agentjudge import policy_prompt
from scopejudge.vendor.agentjudge import ProcessJudge
from scopejudge.runtime import _build_model_from

ROOT = Path(__file__).resolve().parent.parent
FORMATS = ("raw", "compact")
BOOTSTRAP_RESAMPLES = 2000


def load_calls(paths: list[Path]) -> tuple[list[dict[str, object]], int]:
    """Recorded current-backend judge calls, each with its originally recorded verdict if known."""
    calls: list[dict[str, object]] = []
    skipped = 0
    files = [
        f for p in paths for f in ([p] if p.is_file() else sorted(p.rglob("judge-inputs.jsonl")))
    ]
    for path in files:
        verdicts: dict[str, str] = {}
        decisions = path.with_name("scopejudge-decisions.json")
        if decisions.is_file():
            for record in json.loads(decisions.read_text(encoding="utf-8") or "[]"):
                verdicts[record["call_id"]] = record["decision"]["verdict"]
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            payload = json.loads(event["payload"])
            if "trajectory" not in payload:  # agentjudge-native records carry no shared payload
                skipped += 1
                continue
            calls.append(
                {
                    "key": f"{path.parent}::{event['call_id']}",
                    "raw_text": event["payload"],
                    "payload": payload,
                    "recorded": verdicts.get(event["call_id"], "unknown"),
                }
            )
    return calls, skipped


def compact_text(payload: dict[str, object]) -> str:
    """The same payload with only its trajectory re-serialized; a recorded window is kept as-is."""
    trajectory = payload["trajectory"]
    if isinstance(trajectory, dict) and "recent_trajectory" in trajectory:
        window = cast("dict[str, object]", trajectory)
        trajectory = {**window, "recent_trajectory": _compact_history(window["recent_trajectory"])}
    else:
        trajectory = _compact_history(trajectory)
    return json.dumps({**payload, "trajectory": trajectory})


def stratified_sample(
    calls: list[dict[str, object]], n: int, rng: random.Random
) -> list[dict[str, object]]:
    """Up to half the sample from originally blocked calls, where a format effect would matter."""
    if n >= len(calls):
        return calls
    blocked = [c for c in calls if c["recorded"] in {"deny", "uncertain"}]
    other = [c for c in calls if c not in blocked]
    take_blocked = min(len(blocked), n // 2)
    chosen = rng.sample(blocked, take_blocked) + rng.sample(
        other, min(len(other), n - take_blocked)
    )
    return sorted(chosen, key=lambda c: str(c["key"]))


def size_report(calls: list[dict[str, object]]) -> None:
    raw = [len(str(c["raw_text"])) for c in calls]
    compact = [len(compact_text(cast("dict[str, object]", c["payload"]))) for c in calls]
    print(f"calls: {len(calls)}")
    print(f"mean payload chars  raw={statistics.mean(raw):,.0f}", end="")
    print(f"  compact={statistics.mean(compact):,.0f}")
    print(f"char reduction: {1 - sum(compact) / sum(raw):.1%}")


async def judge_once(gate: ScopeJudge, text: str, timeout: float) -> dict[str, object]:
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(
            gate.judge.run(text, instructions=policy_prompt(gate.instruction), usage_limits=UsageLimits(request_limit=3)), timeout
        )
    except Exception as exc:  # noqa: BLE001 - a failed call is an error row, never a verdict
        return {"verdict": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
    usage = result.usage
    return {
        "verdict": ProcessJudge._extract_action(result.output),
        "reason": result.output,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "requests": usage.requests,
        "seconds": round(time.monotonic() - start, 2),
    }


async def replay(
    args: argparse.Namespace, calls: list[dict[str, object]], rng: random.Random
) -> None:
    api_key = os.environ.get(args.api_key_env, "")
    model = _build_model_from(args.judge_model, args.base_url, api_key)
    done: set[tuple[str, str, int]] = set()
    if args.out.is_file():  # resume: Vulcan runs are slow and interruptions are common
        for line in args.out.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["verdict"] is not None:
                done.add((row["key"], row["format"], row["repeat"]))
    jobs: list[tuple[dict[str, object], str, int]] = []
    for call in calls:
        mine = [(call, f, r) for f in FORMATS for r in range(args.repeats)]
        rng.shuffle(mine)
        jobs.extend(j for j in mine if (str(j[0]["key"]), j[1], j[2]) not in done)
    print(f"jobs to run: {len(jobs)} ({len(done)} already recorded)")
    semaphore = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()

    async def run(call: dict[str, object], fmt: str, repeat: int) -> None:
        payload = cast("dict[str, object]", call["payload"])
        gate = ScopeJudge(
            instruction=str(payload["engagement_instruction"]),
            model=model,
            actor_information=payload.get("actor_information"),  # pyright: ignore[reportArgumentType]
        )
        text = str(call["raw_text"]) if fmt == "raw" else compact_text(payload)
        async with semaphore:
            outcome = await judge_once(gate, text, args.timeout)
        row = {
            "key": call["key"],
            "recorded": call["recorded"],
            "format": fmt,
            "repeat": repeat,
            "payload_chars": len(text),
            **outcome,
        }
        async with lock:
            with args.out.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")
        print(f"{fmt:7s} r{repeat} {outcome['verdict'] or 'ERROR':9s} {call['key'][-60:]}")

    await asyncio.gather(*(run(*job) for job in jobs))


def _agreement(pairs: list[tuple[str, str]]) -> float | None:
    return sum(a == b for a, b in pairs) / len(pairs) if pairs else None


def summarize(out: Path, margin: float, seed: int) -> None:
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    by_call: dict[str, dict[str, list[str]]] = {}
    recorded: dict[str, str] = {}
    errors = dict.fromkeys(FORMATS, 0)
    for row in rows:
        if row["verdict"] is None:
            errors[row["format"]] += 1
            continue
        by_call.setdefault(row["key"], {f: [] for f in FORMATS})[row["format"]].append(
            row["verdict"]
        )
        recorded[row["key"]] = row["recorded"]

    within: list[tuple[str, str]] = []
    cross: list[tuple[str, str]] = []
    deltas: list[float] = []
    for verdicts in by_call.values():
        w = [p for f in FORMATS for p in itertools.combinations(verdicts[f], 2)]
        c = list(itertools.product(verdicts["raw"], verdicts["compact"]))
        within += w
        cross += c
        w_rate, c_rate = _agreement(w), _agreement(c)
        if w_rate is not None and c_rate is not None:
            deltas.append(c_rate - w_rate)

    print(f"\ncalls with verdicts: {len(by_call)}   errors: {errors}")
    print(f"within-format agreement (test-retest): {_agreement(within)}  over {len(within)} pairs")
    print(f"cross-format agreement:                {_agreement(cross)}  over {len(cross)} pairs")
    if deltas:
        rng = random.Random(seed)
        boot = sorted(
            statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(BOOTSTRAP_RESAMPLES)
        )
        lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot)) - 1]
        mean = statistics.mean(deltas)
        print(f"cross - within, per call: {mean:+.3f}", end="")
        print(f"  95% CI [{lo:+.3f}, {hi:+.3f}]  (n={len(deltas)})")
        verdict = "WITHIN" if lo >= -margin else "NOT within"
        print(f"equivalence: CI lower bound {verdict} the -{margin:.2f} margin")
    for stratum in sorted(set(recorded.values())):
        keys = [k for k, v in recorded.items() if v == stratum]
        pairs = [
            p for k in keys for p in itertools.product(by_call[k]["raw"], by_call[k]["compact"])
        ]
        print(f"  recorded {stratum:9s}: {len(keys):3d} calls, cross agreement {_agreement(pairs)}")
    for fmt in FORMATS:
        ok = [r for r in rows if r["format"] == fmt and r["verdict"] is not None]
        if not ok:
            continue
        allow = sum(r["verdict"] == "allow" for r in ok) / len(ok)
        tin = statistics.mean(r["input_tokens"] for r in ok)
        tout = statistics.mean(r["output_tokens"] for r in ok)
        secs = statistics.mean(r["seconds"] for r in ok)
        print(f"{fmt:7s}: allow rate {allow:.1%}  input tok {tin:,.0f}", end="")
        print(f"  output tok {tout:,.0f}  {secs:.1f}s")
    tokens = {
        f: [r["input_tokens"] for r in rows if r["format"] == f and r["verdict"] is not None]
        for f in FORMATS
    }
    if all(tokens.values()):
        reduction = 1 - statistics.mean(tokens["compact"]) / statistics.mean(tokens["raw"])
        print(f"judge input-token reduction: {reduction:.1%}")


def main() -> None:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+", type=Path, help="judge-inputs.jsonl files or dirs")
    parser.add_argument("--judge-model", default="openai-chat:qwen35-122b")
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--sample", type=int, default=60, help="calls to replay (stratified)")
    parser.add_argument("--repeats", type=int, default=3, help="judgments per call per format")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300.0, help="per-call seconds")
    parser.add_argument("--margin", type=float, default=0.05, help="equivalence margin")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=Path("judge-format-replay.jsonl"))
    parser.add_argument("--sizes-only", action="store_true", help="no model calls; size report")
    parser.add_argument("--summarize-only", action="store_true", help="report on an existing --out")
    args = parser.parse_args()
    if args.repeats < 2:  # noqa: PLR2004
        parser.error("--repeats must be at least 2 to measure test-retest agreement")
    if args.summarize_only:
        summarize(args.out, args.margin, args.seed)
        return
    rng = random.Random(args.seed)
    calls, skipped = load_calls(args.paths)
    print(f"loaded {len(calls)} current-backend judge calls ({skipped} agentjudge-native skipped)")
    calls = stratified_sample(calls, args.sample, rng)
    size_report(calls)
    if args.sizes_only:
        return
    served = served_name(args.judge_model)
    if served and args.base_url:
        warm_up(
            [Endpoint(served, args.base_url)],
            probe=http_probe(os.environ.get(args.api_key_env, "")),
        )
    asyncio.run(replay(args, calls, rng))
    summarize(args.out, args.margin, args.seed)


if __name__ == "__main__":
    main()
