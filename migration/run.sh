#!/usr/bin/env bash
# CLE migration runner. Idempotent: completed steps recorded in
# cle_v2.migration_manifest are skipped unless FORCE=1.
#
# Required env:
#   TARGET_DB_URL     — the cle_v2 host (Neon staging / Coolify at cutover)
#   LEGACY_DB_URL     — postgres://user:pass@host/db (read-only use)
# Optional env:
#   RS_FILTER         — SQL predicate on legacy.rs_document AS d   (default: true; qualify columns with d.)
#   ECHR_FILTER       — SQL predicate on legacy.echr_document AS d (default: true; qualify columns with d.)
#   PORT_SEGMENTS     — true|false: port 32GB ecli_segments   (default: false)
#   PORT_LIDO         — true|false: fold 10.2M LIDO registry  (default: false)
#   STEPS             — space list to run (default: all in order)
set -euo pipefail
cd "$(dirname "$0")"

: "${TARGET_DB_URL:?}"; : "${LEGACY_DB_URL:?}"

# Neon pooler endpoints (PgBouncer transaction mode) break session SET
# (search_path roulette) — migrations must use the direct endpoint.
if [[ "$TARGET_DB_URL" == *"-pooler."* ]]; then
  echo "!! TARGET_DB_URL uses a Neon pooler endpoint — switching to direct endpoint"
  TARGET_DB_URL="${TARGET_DB_URL/-pooler./.}"
fi
RS_FILTER="${RS_FILTER:-true}"
ECHR_FILTER="${ECHR_FILTER:-true}"
LEGIS_FILTER="${LEGIS_FILTER:-true}"          # predicate on rs_law_element AS le
LEGIS_FILTER_STUB="${LEGIS_FILTER_STUB:-true}" # predicate on rs_document_law_reference AS le
PORT_SEGMENTS="${PORT_SEGMENTS:-false}"
PORT_LIDO="${PORT_LIDO:-false}"

# parse legacy url ->  host/db/user/password psql vars
eval "$(python3 - "$LEGACY_DB_URL" <<'PY'
import sys, urllib.parse as u
p = u.urlparse(sys.argv[1])
print(f"LH={p.hostname}\nLD={p.path.lstrip('/')}\nLU={p.username}\nLP={p.password}")
PY
)"

PSQL=(psql "$TARGET_DB_URL" -v ON_ERROR_STOP=1
      -v legacy_host="$LH" -v legacy_db="$LD" -v legacy_user="$LU" -v legacy_password="$LP"
      -v rs_filter="$RS_FILTER" -v echr_filter="$ECHR_FILTER"
      -v port_segments="$PORT_SEGMENTS" -v port_lido="$PORT_LIDO"
      -v legis_filter="$LEGIS_FILTER" -v legis_filter_stub="$LEGIS_FILTER_STUB")

steps=(${STEPS:-00_schema_core 01_fdw_setup 10_lookups 11_legislation 20_rs_cases
       25_rs_segments 30_echr_cases 50_load_cjeu 40_citations 41_counts 42_lido_registry 90_post_load})

# runner owns the manifest (so even 00/01/90 are resumable)
"${PSQL[@]}" -q -c "CREATE SCHEMA IF NOT EXISTS cle_v2;
CREATE TABLE IF NOT EXISTS cle_v2.migration_manifest (
  step text PRIMARY KEY, completed_at timestamptz NOT NULL DEFAULT now(),
  rows_affected bigint, note text);"

done_steps=$("${PSQL[@]}" -tA -c "SELECT step FROM cle_v2.migration_manifest" 2>/dev/null || true)

for s in "${steps[@]}"; do
  if [[ "${FORCE:-0}" != "1" ]] && grep -qx "$s" <<<"$done_steps"; then
    echo "== skip $s (manifest)"; continue
  fi
  echo "== run  $s  $(date -u +%H:%M:%S)"
  if [[ -f "sql/$s.py" ]]; then
    PYBIN="${PYBIN:-$(dirname "$0")/../.venv/bin/python}"
    TARGET_DB_URL="$TARGET_DB_URL" "$PYBIN" "sql/$s.py"
  else
    "${PSQL[@]}" -f "sql/$s.sql"
  fi
  "${PSQL[@]}" -q -c "INSERT INTO cle_v2.migration_manifest (step) VALUES ('$s')
                      ON CONFLICT (step) DO UPDATE SET completed_at = now();"
done
echo "== verify"
"${PSQL[@]}" -f verify/reconcile.sql
