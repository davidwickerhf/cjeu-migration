#!/usr/bin/env bash
set -euo pipefail

repo_dir="${CJEU_REPO_DIR:-/workspace/cjeu-migration}"
venv_dir="${CJEU_VENV_DIR:-/workspace/cjeu-venv}"
token_file="${HF_TOKEN_FILE:-/workspace/.hf_token}"

if [[ ! -s "${token_file}" ]]; then
  echo "HF token file is missing or empty: ${token_file}" >&2
  exit 2
fi

export HUGGINGFACE_TOKEN
HUGGINGFACE_TOKEN="$(tr -d '\r\n' < "${token_file}")"
export HF_DATASET_REPO="${HF_DATASET_REPO:-davidwickerhf/cjeu-opendata}"
export HF_HOME="${HF_HOME:-/workspace/.hf_home}"
export WORKSPACE_DIR="${WORKSPACE_DIR:-/workspace/cjeu-data}"
export START_DATE="${START_DATE:-1954-01-01}"
export END_DATE="${END_DATE:-}"
export WINDOW="${WINDOW:-month}"
export EXTRACTOR_THREADS="${EXTRACTOR_THREADS:-10}"
export MAX_WINDOW_RETRIES="${MAX_WINDOW_RETRIES:-3}"
# Build and validate before publishing. Set to 0 only after acceptance passes.
export SKIP_UPLOAD="${SKIP_UPLOAD:-1}"

mkdir -p "${WORKSPACE_DIR}" "${HF_HOME}"
cd "${repo_dir}"
exec "${venv_dir}/bin/cjeu-migrate" run
