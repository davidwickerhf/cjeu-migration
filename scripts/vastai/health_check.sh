#!/usr/bin/env bash
set -u

log_file="${CJEU_HEALTH_LOG:-/workspace/cjeu-health.log}"
workspace_dir="${WORKSPACE_DIR:-/workspace/cjeu-data}"
venv_dir="${CJEU_VENV_DIR:-/workspace/cjeu-venv}"

{
  echo "=== $(date -Is) ==="
  supervisorctl status cjeu_full_rebuild || true
  WORKSPACE_DIR="${workspace_dir}" "${venv_dir}/bin/cjeu-migrate" status || true
  echo "recent_errors:"
  tail -n 400 /workspace/cjeu-full-rebuild.log 2>/dev/null \
    | grep -E "ERROR|Traceback|exhausted|mark_failed|attempt=[23]" \
    | tail -n 20 || true
  df -h /workspace
} >> "${log_file}" 2>&1
