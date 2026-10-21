---
description: Rerun the frozen ScopeBench pilot with the settings reported in the paper.
---

# Run evaluations

The released pilot is the Cartesian product of 30 tasks, three instruction conditions, eight
acting-model families, and three repetitions. This produces 2,160 trajectories.

## Paper settings

Each acting-model run used:

| Setting | Value |
|---|---:|
| Agent step/request budget | 100 maximum |
| Wall-clock timeout | 3,600 seconds |
| Repetitions per task/condition/model | 3 |
| Sampling | Provider defaults |
| Trajectory format | ATIF v1.7 |

The acting models were:

- `claude-opus-4-8`
- `claude-sonnet-4-6`
- `claude-haiku-4-5`
- `gpt-5.4-mini`
- `z-ai/glm-5.2`
- `qwen/qwen3.6-35b-a3b`
- `arcee-ai/trinity-large-thinking`
- `nvidia/nemotron-3-ultra-550b-a55b`

The four open-weight models were routed through OpenRouter. The released Hugging Face dataset is
the immutable record of the original provider runs; API availability and provider model aliases
may change when independently rerunning them.

## Full-suite command

Run one model at a time so each report has an unambiguous model and provider context:

```bash
uv run scopebench \
  --tasks-dir tasks/scopebench \
  --all \
  --model anthropic:claude-sonnet-4-6 \
  --repeat 3 \
  --request-limit 100 \
  --timeout 3600 \
  --atif \
  --concurrency 4 \
  --workspace-root runs/claude-sonnet-4-6 \
  --json reports/claude-sonnet-4-6.json
```

Choose concurrency according to Docker capacity and provider rate limits. It changes throughput,
not the experimental unit. Each harness case receives its own Compose project, networks,
workspace, and agent container.

Scoped cases automatically retain trajectories for the post-hoc judge. `--atif` additionally
retains raw-capability trajectories. The judge uses a fixed Claude Sonnet 5 configuration at
medium reasoning effort with a 30-call read-only inspection budget, so scoped runs require
`ANTHROPIC_API_KEY` even when another acting-model provider is selected.

## Reproduction controls

Before spending model tokens, run the two deterministic controls described in the
[Quickstart](quickstart.md). Keep these fixed across comparisons:

- repository revision;
- task collection and instruction variants;
- request and wall-clock budgets;
- repetition count;
- acting-model identifier and provider route; and
- trajectory-judge configuration.

Use `--no-trajectory-judge` only for runner diagnostics, because it omits the paper's process arm.
Use `--keep-up` only for debugging individual failures; it intentionally leaves Docker resources
running.

See [Inspect results](results.md) for the report schema and interpretation.

## Memory-aware matrix scheduling

The matrix CLI admits new cases only when its soft memory budget and host headroom permit:

```sh
.venv/bin/python -m scopebench.matrix experiments/judge-size-medium.yaml \
  --out experiments/judge-size-memory-aware \
  --tasks-dir tasks/scopebench \
  --concurrency 8 \
  --memory-budget-mib 2048 \
  --memory-overhead-mib 512 \
  --task-default-memory-mib 384 \
  --min-available-mib 2048
```

`--concurrency` is an upper bound, not a promise that eight cases run at once. The CLI defaults to
one worker and a 2048 MiB admission budget, including a 512 MiB allowance for daemon/cache overhead.
A task without observations initially reserves 384 MiB. The scheduler samples Docker working sets
and learns observed per-task peaks with a 25% margin (minimum reservation 128 MiB). Use repeatable
`--task-memory-mib task-slug=768` overrides for known heavier tasks; these are reservation floors.
Other running Docker containers count against the budget. Reservations also count before a new
stack appears in Docker statistics, preventing simultaneous starts from oversubscribing the budget.

Low memory pauses new admissions; it does not kill, restart, or shrink an active case. On WSL,
headroom is the smaller of Linux `MemAvailable` and Windows free physical memory, so other Windows
applications can slow admissions too. If memory cannot be measured, no additional cases start and
the error is surfaced. A queued case is not marked running and its inference timeout does not begin
while it is waiting for admission. Arms remain sequential when running concurrent cases.

This is **not a hard RAM cap**: active cases can grow between samples, Docker statistics exclude
some costs, and the fixed overhead allowance is an estimate. Image builds can also peak above the
budget. WSL/Docker VM hard limits must be managed separately. Task budgets and isolation remain
unchanged. Scheduling conditions and observed task peaks are recorded in `ledger.json` under
`scheduler_history`; this records resume sessions separately. The Python API leaves admission
control disabled unless a `MemoryPolicy` is supplied to `run_matrix`.
