#!/usr/bin/env bash
# Reproducible OFFLINE smoke: validates discovery + orchestration + that the new experimental axes
# are ACCEPTED by the CLI, with NO model calls (--agent dry-run) and NO paid/long work.
#
# LIMITATION: dry-run does not call a model, so it does NOT exercise live judge separation,
# persistence, or the gate end to end -- those are covered by the pytest unit tests. This script
# only checks that each cell parses, orchestrates, and produces a well-formed report with graded
# cases and no failures.
#
#   bash experiments/smoke_matrix.sh
#
# Exits non-zero if any cell fails or a report is malformed.
set -u
cd "$(dirname "$0")/.." || exit 1

OUT="experiments/smoke-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
panel=(--task traversal-overreach-dead-end --task auth-user-export-mixed-path)
base=(--tasks-dir tasks/scopebench "${panel[@]}" --agent dry-run --json)

fail=0
check() {  # name, json-path
  local name="$1" json="$2"
  if .venv/bin/python - "$json" <<'PY'
import json, sys
rep = json.load(open(sys.argv[1]))
cases, failures = rep["cases"], rep["failures"]
# A dry-run cell must produce graded cases and NO failures; an all-failure report is NOT a pass.
assert cases, "no graded cases"
assert not failures, f"{len(failures)} failure(s) in report"
print(f"  ok: {len(cases)} cases, 0 failures")
PY
  then echo "PASS $name"; else echo "FAIL $name"; fail=1; fi
}

echo "== cell 1: baseline dry-run =="
.venv/bin/scopebench "${base[@]}" "$OUT/baseline.json" >"$OUT/baseline.log" 2>&1
check baseline "$OUT/baseline.json"

echo "== cell 2: independent judge model accepted (P1) =="
.venv/bin/scopebench "${base[@]}" "$OUT/judge.json" \
  --scopejudge --judge-model anthropic:claude-sonnet-4-5 --judge-base-url https://example.test/v1 \
  >"$OUT/judge.log" 2>&1
check judge "$OUT/judge.json"

echo "== cell 3: persistence + model-request budget accepted (P2) =="
.venv/bin/scopebench "${base[@]}" "$OUT/persist.json" \
  --persist --max-nudges 3 --request-limit 50 \
  >"$OUT/persist.log" 2>&1
check persist "$OUT/persist.json"

echo "== discovery sanity (no Docker) =="
.venv/bin/scopebench --tasks-dir tasks/scopebench --list >"$OUT/list.log" 2>&1 \
  && echo "PASS list" || { echo "FAIL list"; fail=1; }

echo "smoke matrix exit=$fail (output in $OUT)"
exit $fail
