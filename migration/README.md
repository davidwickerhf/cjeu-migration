# CLE migration pipeline

Loads all three corpora into the unified `cle_v2` schema
([../docs/postgres-schema/schema_full.sql](../docs/postgres-schema/schema_full.sql)),
per the mapping in [MIGRATION_MAPPING.md](../docs/postgres-schema/MIGRATION_MAPPING.md).

- **RS + ECHR**: SQL over `postgres_fdw` from the legacy caselawexplorer DB
  (server-to-server; the orchestrating machine only issues statements).
- **CJEU**: Python loader from the HF parquet corpus (streamed COPY).

## Usage

```bash
export TARGET_DB_URL="postgres://…"        # where cle_v2 lives (Neon staging / Coolify)
export LEGACY_DB_URL="postgres://…"        # legacy caselawexplorer (read-only use)
./run.sh
```

Sampled run (rehearsal on small instances):

```bash
RS_FILTER="d.date_decision >= '2026-04-01'" \
ECHR_FILTER="d.judgementdate >= '2026-01-01'" \
LEGIS_FILTER="le.bwb_id LIKE 'BWBR0001%'" \
LEGIS_FILTER_STUB="le.bwb_resource LIKE 'BWBR0001%'" \
CJEU_FILTER_YEAR_GTE=2026 \
./run.sh
```

Full run adds: `PORT_SEGMENTS=true` (32 GB legacy embeddings) and
`PORT_LIDO=true` (10.2M LIDO registry fold-in).

## Properties

- **Resumable**: every step records itself in `cle_v2.migration_manifest`;
  completed steps are skipped. `FORCE=1 STEPS="20_rs_cases 40_citations"`
  reruns specific steps.
- **Bulk-load layout**: `00_schema_core` creates bare tables; all 67 indexes,
  61 FKs, triggers and views land in `90_post_load` AFTER the data
  (~10× faster load). `90` also dedupes citation/law-reference rows first —
  bulk steps run without the dedup indexes, so a partially-failed-then-
  resumed load may have double-inserted.
- **Citations last**: `40_citations` must run after ALL corpora are in
  `cases` so the 161,922 cross-corpus targets (RS→ECHR, RS→CJEU) resolve.
  `41_counts` rebuilds `case_citation_counts` in one pass before `90`
  installs the maintaining trigger.
- **Generated columns** (`fulltext_tsv`, `judgement_year`, …) and indexes
  are computed by the target — never ported. The only derived data shipped
  as bytes is the embeddings (`25_rs_segments`).

## Step order

| Step | What | Notes |
|---|---|---|
| 00_schema_core | tables + PKs (cle_v2) | generated from schema_full.sql |
| 01_fdw_setup | FDW link to legacy | `import_generated 'false'` |
| 10_lookups | languages (HUDOC map), courts, doctypes | RS `instance` = 1,230 court names |
| 11_legislation | BWB catalog + stubs | snapshot-dedupe (latest per lido_id) |
| 20_rs_cases | RS cases/satellites/texts/domains/law refs | |
| 25_rs_segments | 32 GB embeddings (optional) | `PORT_SEGMENTS=true` |
| 30_echr_cases | ECHR variant grouping → cases + satellites | canonical: ENG→FRE→any; date from ECLI |
| 50_load_cjeu.py | CJEU from HF parquets | streamed texts, staging COPY |
| 40_citations | all edges → case_citation | cross-corpus resolution |
| 41_counts | counts rebuild | |
| 42_lido_registry | LIDO fold-in (optional) | `PORT_LIDO=true` |
| 90_post_load | dedupe + indexes + FKs + triggers + views | idempotent |
| verify/reconcile.sql | row counts + orphan checks | run automatically at end |

## Cutover to Coolify (once staging is verified)

```bash
pg_dump "$NEON_URL" -n cle_v2 -Fc -f cle_v2.dump     # data + definitions, no index bytes
pg_restore -d "$COOLIFY_URL" -j 4 cle_v2.dump         # rebuilds indexes/tsv on target
```

Run the restore from a one-shot container inside the Coolify network (the
DB is not reachable from outside; Neon is reachable from inside).
