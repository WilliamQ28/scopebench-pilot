#!/usr/bin/env bash
# Ready-to-run recipe for the attacker-behaviour-ladder experiments.
#
#   cp .env.example .env   # then fill in OPENAI_API_KEY (ask the project owner)
#   bash experiments/run_ladder.sh adaptive        # gate-on, no persistence controller
#   bash experiments/run_ladder.sh persistent      # gate-on, --persist
#   bash experiments/run_ladder.sh capability-off   # gate-off attack ceiling (no monitor)
#
# Design doc: experiments/attacker-behavior-ladder.md
#
# Env overrides (defaults in parens): BUDGET(150) TIMEOUT(7200) REPEAT(5) CONCURRENCY(3)
# MODEL(openai-chat:qwen35-122b) BRIEF(briefs/adaptive-monitor-aware.md) OUT(experiments/ladder-$(date +%Y%m%d))
#
# NOTE: the endpoint is slow; keep --judge-timeout/--timeout high or the ScopeJudge gate times out
# and fails closed. Each gate-on run can take ~1-2h at BUDGET=150. Runs are sequential; a Windows
# reboot kills Docker and aborts the run (completed runs' agent-result.json are salvageable).
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1

arm="${1:-adaptive}"
# Load .env into the environment so the checks below (and scopebench) see the keys.
[ -f .env ] && { set -a; . ./.env; set +a; }
: "${OPENAI_BASE_URL:?set OPENAI_BASE_URL in .env (see .env.example)}"
: "${OPENAI_API_KEY:?set OPENAI_API_KEY in .env (ask the project owner)}"

MODEL="${MODEL:-openai-chat:qwen35-122b}"
BRIEF="${BRIEF:-briefs/adaptive-monitor-aware.md}"
BUDGET="${BUDGET:-150}"
TIMEOUT="${TIMEOUT:-7200}"
REPEAT="${REPEAT:-5}"
CONCURRENCY="${CONCURRENCY:-3}"
OUT="${OUT:-experiments/ladder-$(date +%Y%m%d)}"
mkdir -p "$OUT"

tasks=(
  traversal-overreach-dead-end
  webhook-admin-api-ssrf-dead-end
  jwt-internal-api-trust-dead-end
  scope-creep-production-host-dead-end
  sqli-cross-tenant-dead-end
  no-pivot-lateral-movement-dead-end
)
sel=(); for t in "${tasks[@]}"; do sel+=(--task "$t"); done

common=(
  --tasks-dir tasks/scopebench "${sel[@]}"
  --agent harness --model "$MODEL" --base-url "$OPENAI_BASE_URL"
  --instruction program-brief-v3
  --request-limit "$BUDGET" --timeout "$TIMEOUT" --wait-timeout 240
  --concurrency "$CONCURRENCY" --repeat "$REPEAT"
  --atif --no-trajectory-judge --max-tool-result-chars 12000
)

case "$arm" in
  capability-off)  # gate-off attack ceiling: no monitor, no brief
    extra=() ;;
  adaptive)        # gate-on, adaptive brief, no persistence controller
    extra=(--attacker-brief "$BRIEF" --scopejudge --judge-timeout 120
           --deny-feedback reasoned --hide-brief-from-judge) ;;
  persistent)      # gate-on, adaptive brief, persistence controller
    extra=(--attacker-brief "$BRIEF" --scopejudge --judge-timeout 120
           --deny-feedback reasoned --hide-brief-from-judge --persist) ;;
  *) echo "usage: run_ladder.sh [adaptive|persistent|capability-off]"; exit 2 ;;
esac

echo "arm=$arm model=$MODEL budget=$BUDGET timeout=$TIMEOUT repeat=$REPEAT -> $OUT/$arm.json"
exec .venv/bin/scopebench "${common[@]}" "${extra[@]}" \
  --workspace-root "$OUT/workspaces/$arm" --json "$OUT/$arm.json"
