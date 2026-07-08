#!/usr/bin/env bash
# Matched-day eval batch (cost-optimization epic, step C).
#
# Launches one run.py PROCESS PER CANDIDATE **in parallel** (repetitions stay
# sequential inside each process, so every candidate still yields ONE eval-run
# record with proper n>1 mean/stdev aggregation). Parallel-across-candidates is
# deliberate methodology, not just speed: news is a moving target, and a
# 3-4 hour sequential batch would let candidates see different news states --
# simultaneous launch means every candidate researches the SAME news snapshot
# with the SAME priors, so score/cost differences are attributable to the
# candidate, not the hour. Concurrency stays bounded at len(candidates)
# sessions (+their judge phases), well below anything that has rate-limited
# this account.
#
# Usage (from deploy/eval-harness/):
#   export ANTHROPIC_API_KEY=$(cat ~/.anthropic-managed-agents/ant-api-key.txt)
#   ./run_matched_batch.sh                          # default step-C trio, REPS=3
#   REPS=2 ./run_matched_batch.sh slug-a slug-b     # custom set / repetitions
#
# The recent-briefs signing key resolves from
# ~/.anthropic-managed-agents/recent-briefs-signing-key.txt (harness/local_config.py),
# so no other env is needed. Exit code is non-zero if ANY candidate's run failed.
set -euo pipefail
cd "$(dirname "$0")"

REPS="${REPS:-3}"
BATCH_NAME="${BATCH_NAME:-matched-$(date +%Y-%m-%d)}"
CANDIDATES=("$@")
if [ ${#CANDIDATES[@]} -eq 0 ]; then
  CANDIDATES=(production-baseline haiku-digest-sonnet-select haiku-swap-hardened)
fi
# INVARIANT (do not break): CANDIDATES is guaranteed non-empty from here on --
# macOS ships bash 3.2, where "${arr[@]}" under `set -u` ERRORS on an empty
# array. PIDS is safe only because it is populated by iterating this non-empty
# CANDIDATES. If you ever add an early exit / empty-set path, guard the
# expansions (e.g. ${arr[@]+"${arr[@]}"}) or bump the shebang requirement.
: "${ANTHROPIC_API_KEY:?export ANTHROPIC_API_KEY first (never hardcode it)}"

LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/matched-batch-${BATCH_NAME}.XXXX")"
echo "matched batch '${BATCH_NAME}': ${#CANDIDATES[@]} candidates x ${REPS} repetition(s), logs in ${LOG_DIR}"

PIDS=()
for slug in "${CANDIDATES[@]}"; do
  log="${LOG_DIR}/${slug}.log"
  PYTHONUNBUFFERED=1 .venv/bin/python run.py "$slug" \
    --name "${BATCH_NAME}" --repetitions "${REPS}" --timeout 3300 \
    >"$log" 2>&1 &
  PIDS+=("$!")
  echo "  launched ${slug} (pid $!)"
done

FAIL=0
for i in "${!PIDS[@]}"; do
  if wait "${PIDS[$i]}"; then
    echo "OK    ${CANDIDATES[$i]}"
  else
    echo "FAILED ${CANDIDATES[$i]} -- see ${LOG_DIR}/${CANDIDATES[$i]}.log"
    FAIL=1
  fi
done

echo ""
for slug in "${CANDIDATES[@]}"; do
  echo "--- ${slug} (tail) ---"
  tail -n 6 "${LOG_DIR}/${slug}.log"
done
exit "${FAIL}"
