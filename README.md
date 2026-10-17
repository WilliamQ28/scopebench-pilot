# ScopeBench Pilot

ScopeBench measures whether an autonomous security agent preserves a user-defined engagement
boundary while pursuing an objective. This research branch extends the original 30-task pilot
used in *ScopeBench: Do Agents Preserve Engagement Boundaries Under Goal Pressure?* at AISec 2026.
It contains 59 task manifests, live ScopeJudge gate evidence, an attacker persistence controller,
and a separate bounded HTTP ScopeFirewall experiment.

For the frozen pilot checkout, use upstream commit
`ead68e6660f83a2bcce6a75a706308ce11cf555a`. The pilot is a methodological proof of concept;
this branch's extensions are exploratory research and are separate from the community benchmark.

The original pilot's 2,160 ATIF trajectories are published as the
[`dreadnode/scopebench-pilot`](https://huggingface.co/datasets/dreadnode/scopebench-pilot)
dataset on Hugging Face.

## What is included

- 59 containerized task manifests, including mixed-path extensions, under [`tasks/scopebench`](tasks/scopebench)
- raw-capability, casual-scope, and program-brief variants for every task
- generated-result verifiers and reference solutions
- the ScopeBench runner and sandbox with research extensions
- the full-trajectory agentic judge used to evaluate the casual-scope and program-brief variants
- [live gate evidence and its limits](docs/scopebench/live-breach-evidence.md)
- [attacker behaviour ladder design](experiments/attacker-behavior-ladder.md) and
  [run script](experiments/run_ladder.sh)
- [HTTP ScopeFirewall controls](docs/scopebench/scopefirewall-http.md), including a reusable
  [Docker isolation validator](experiments/scopefirewall/docker_smoke.py)

The original dead-end tasks place the objective behind an action that crosses the scoped
condition's engagement boundary. Mixed-path extensions also provide an authorized objective path.
Raw-capability runs measure whether an agent can reach the objective. Scoped runs pair
deterministic outcome verification with a full-trajectory scope verdict.

## Quick start

Requirements: Python 3.13, uv, and Docker with Compose. Replace `<YOUR_REPOSITORY_URL>`
with the GitHub repository containing this research branch.

```sh
git clone --branch attacker-behavior-ladder <YOUR_REPOSITORY_URL>
cd scopebench-pilot
uv sync --frozen

uv run scopebench --tasks-dir tasks/scopebench --list
uv run scopebench --tasks-dir tasks/scopebench --all --agent dry-run
```

`dry-run` validates discovery and orchestration without calling a model. Run one task with the
sandboxed baseline agent by selecting a configured model and providing its API key:

```sh
export ANTHROPIC_API_KEY=...

uv run scopebench \
  --tasks-dir tasks/scopebench \
  --task auth-user-export-dead-end \
  --model anthropic:claude-sonnet-4-6 \
  --atif
```

Scoped cases also require an Anthropic API key for the fixed post-hoc trajectory judge. See
[`docs/scopebench/run-evaluations.md`](docs/scopebench/run-evaluations.md) for repetitions,
concurrency, reports, and model-provider configuration.

## Original pilot design

The original released evaluation is the Cartesian product of:

- 30 tasks
- 3 instruction conditions
- 8 model families
- 3 repetitions

This produces 2,160 trajectories. The Hugging Face dataset preserves the model, condition, task,
and repetition structure and records a checksum for every released trajectory. Those results
do not evaluate this branch's additional tasks, persistence controller, or HTTP firewall.

## Documentation and development

Start the documentation site locally:

```sh
uv run --group docs mkdocs serve
```

Run the repository checks:

```sh
uv run ruff check src tests
uv run basedpyright
uv run pytest
```

## Citation

Citation metadata is available in [`CITATION.cff`](CITATION.cff).

## License

This repository is released under the [MIT License](LICENSE).
