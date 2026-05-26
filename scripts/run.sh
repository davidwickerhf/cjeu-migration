#!/usr/bin/env bash
# Convenience launcher for Vast.ai-style boxes. Auto-restarts on crash up to N
# times so a transient network blip doesn't end the run prematurely. Idempotent
# manifest means re-runs pick up where they left off.

set -euo pipefail

MAX_RESTARTS="${MAX_RESTARTS:-5}"
SLEEP_BETWEEN="${SLEEP_BETWEEN:-30}"
RUN_LOG="${RUN_LOG:-./run.log}"

restarts=0
while (( restarts <= MAX_RESTARTS )); do
  echo "$(date -u +%FT%TZ) attempt $((restarts + 1)) / $((MAX_RESTARTS + 1))" | tee -a "$RUN_LOG"
  if cjeu-migrate run 2>&1 | tee -a "$RUN_LOG"; then
    echo "$(date -u +%FT%TZ) cjeu-migrate exited cleanly" | tee -a "$RUN_LOG"
    exit 0
  fi
  restarts=$((restarts + 1))
  echo "$(date -u +%FT%TZ) cjeu-migrate exited non-zero; sleeping ${SLEEP_BETWEEN}s before restart" | tee -a "$RUN_LOG"
  sleep "$SLEEP_BETWEEN"
done

echo "$(date -u +%FT%TZ) restart cap reached — bailing out" | tee -a "$RUN_LOG"
exit 1
