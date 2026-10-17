"""Run a reproducible HTTP-only ScopeFirewall experiment."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from pydantic_ai.models import Model, infer_model
from pydantic_ai.providers.openai import OpenAIProvider

from agent_sandbox.models import read_api_key
from scopebench.firewall.experiment import llm_judge, permissive_stub, run_matrix
from scopebench.firewall.fixture import INSTRUCTION, ORACLE_POLICY
from scopebench.firewall.policy import compile_policy, policy_difference


class _Args(argparse.Namespace):
    actor_model: str | None = None
    judge_model: str | None = None
    compile_model: str | None = None
    base_url: str | None = None
    repeat: int = 1
    request_limit: int = 20
    operation_timeout: float = 30.0
    case_timeout: float = 120.0
    use_compiled_policy: bool = False
    output: Path | None = None


def _model(name: str, base_url: str | None) -> Model:
    if base_url is None:
        return infer_model(name)
    if not name.startswith("openai-chat:"):
        raise ValueError("--base-url requires openai-chat:<model> model identifiers")
    return infer_model(
        name,
        provider_factory=lambda _: OpenAIProvider(
            base_url=base_url, api_key=read_api_key(name) or "local-fixture-experiment"
        ),
    )


async def _run(args: _Args) -> dict[str, object]:
    actor_name: str | None = args.actor_model
    judge_name: str | None = args.judge_model
    compiler_name: str | None = args.compile_model
    base_url: str | None = args.base_url
    actor = _model(actor_name, base_url) if actor_name else None
    judge = _model(judge_name, base_url) if judge_name else None
    policy = ORACLE_POLICY
    compilation: dict[str, object] = {"source": "manual", "evaluated": False}
    if compiler_name:
        compiled = await compile_policy(INSTRUCTION, _model(compiler_name, base_url))
        compilation = {
            "source": "llm" if args.use_compiled_policy else "manual",
            "evaluated": True,
            "compiled_policy": compiled.model_dump(mode="json"),
            **policy_difference(ORACLE_POLICY, compiled),
        }
        if args.use_compiled_policy:
            policy = compiled
    report = await run_matrix(
        repeat=args.repeat,
        policy=policy,
        actor_model=actor,
        budget=args.request_limit,
        judge_factory=(lambda: llm_judge(judge)) if judge else (lambda: permissive_stub),
        semantic_kind="llm" if judge else "permissive-stub",
        operation_timeout=args.operation_timeout,
        case_timeout=args.case_timeout,
    )
    return {
        **report,
        "compilation": compilation,
        "engagement_instruction": INSTRUCTION,
        "models": {"actor": actor_name, "judge": judge_name, "compiler": compiler_name},
    }


def main(argv: list[str] | None = None) -> int:
    """Default to offline controls; explicit model flags opt into provider calls."""
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--actor-model")
    _ = parser.add_argument("--judge-model")
    _ = parser.add_argument("--compile-model")
    _ = parser.add_argument("--use-compiled-policy", action="store_true")
    _ = parser.add_argument("--base-url")
    _ = parser.add_argument("--repeat", type=int, default=1)
    _ = parser.add_argument("--request-limit", type=int, default=20)
    _ = parser.add_argument("--operation-timeout", type=float, default=30.0)
    _ = parser.add_argument("--case-timeout", type=float, default=120.0)
    _ = parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv, namespace=_Args())
    if (
        args.repeat < 1
        or args.request_limit < 1
        or args.operation_timeout <= 0
        or args.case_timeout <= 0
    ):
        parser.error("repeat, request-limit and timeouts must be positive")
    if args.use_compiled_policy and not args.compile_model:
        parser.error("--use-compiled-policy requires --compile-model")
    if args.actor_model and not args.judge_model:
        parser.error("adaptive LLM experiments require an explicit --judge-model")
    output: Path = args.output or Path(".scopefirewall-runs") / f"http-{uuid.uuid4().hex[:12]}.json"
    if output.exists():
        parser.error("output already exists; choose a fresh report path")
    report = asyncio.run(_run(args))
    output.parent.mkdir(parents=True, exist_ok=True)
    _ = output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
