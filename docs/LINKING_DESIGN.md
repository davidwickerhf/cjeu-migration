# Cross-dataset linking: Phase 1 design

Status: v2, 2026-11-24, after the first independent review (see "Review log").
Companion to [CROSS_DATASET_ROADMAP.md](CROSS_DATASET_ROADMAP.md) (items
R1.1–R1.5 and the data path every Phase 2 extractor uses). Nothing here is
applied anywhere yet.

## Corrections to the roadmap found while designing

- **The migration numbers are `0009` and `0010`, not `0008`.** caselaw-coolify
  `main` already has two `0008` files: `0008_case_cited_by.sql` (the
  `case_cited_by` materialized view, refreshed by the promoter after each
  promotion) and `0008_case_text_document_kind.sql`.
- **`case_cited_by` is created in `0008_case_cited_by.sql`** (R1.4 asked where).
  It counts distinct citing units (an ECtHR application, otherwise the case)
  per target over every resolved `case_citation` row, whatever its
  `relation_type` or `source_dataset`, and leaves out sources with more than 300
  rows, counted over *all* their rows, unresolved and every dataset included
  (`0008_case_cited_by.sql:55-61`). Two consequences: inverse `cited_by`,
  `is_about_concept`, `joins` and identity rows are counted today; and any new
  link dataset can push a real, heavily citing judgment over 300 rows and
  silently remove all its citations from the counts. citations-api reads
  `case_cited_by` on the collection endpoints (`utils/citation_counts.py`) and
  `case_citation_counts` in `api/combined/index.py` and
  `api/combined/expand/index.py`.
- **`case_citation_counts` changes the moment a row is inserted**
  (`trg_case_citation_counts`, `0001_schema_full.sql:79-150`), unresolved rows
  included. So new link data in production changes numbers users see unless the
  counts are guarded first (see "Order of work").
- **The existing CJEU loader rewrites RS rows in place.** `cjeu_loader.py`
  `SQL_LINK_ECLI` updates every unresolved row whose `source_dataset` starts with
  `rs_` (or is `echr_edge`) on each `cellar_etl` load, setting `target_case_id`
  and clearing `target_ecli_raw`. New `rs_text_*` datasets would be mutated by it.
  It must be narrowed to the legacy datasets before any `rs_text_*` row exists.
- **Schema files.** The deployed databases, db-dev included, are built by the
  migrator from caselaw-coolify `db/migrations/` (`docker-compose.yml`
  `migrator-dev`). case-law-explorer `db/schema.sql` is used only by that repo's
  local `cle-postgres`; unit tests use a fake hook. Its unique indexes on
  `case_citation` are not partial (coolify's are). New objects are mirrored into
  `db/schema.sql` with the same predicates, and its three `case_citation` unique
  indexes are brought in line with coolify (local dev only).
- **The promoter replays artifacts, not rows.** `etl/promoter.py`
  `replay_artifacts` re-runs the case-law-explorer loaders against production on
  the verified files of a batch, with fixed paths per month and one file per
  role. New link data must travel as artifacts the promoter knows.
- **The migrator applies each file in one transaction** (`migrate.sh`,
  `psql -1`), rewriting only quoted `"public"` to the deploy schema, and sets the
  search path only after all files ran. So every object in a migration is written
  `"public"."name"`, and locks taken early in a file are held to its end: an
  `ALTER TABLE case_citation` followed by an index build in the same file would
  hold ACCESS EXCLUSIVE (blocking API reads) for the whole scan. Index builds on
  `case_citation` therefore go in their own file.
- **Promoter privileges.** In production the promoter role has INSERT and UPDATE
  on the schema; on db-dev only on the promotion tables
  (`configure-promoter-role.sh:62-77`). New functions it calls are
  `SECURITY DEFINER` with an explicit grant, like `refresh_case_cited_by`.

## Order of work (what may reach production when)

1. `0009` (new columns and empty tables; milliseconds of locking) and `0010`
   (indexes on `case_citation`; one scan under a SHARE lock, reads continue,
   writes wait; applied outside the Monday ETL and Thursday promotion windows).
   Both strictly additive; nothing users see changes.
2. Code: narrow `cjeu_loader.py` `SQL_LINK_ECLI`/`SQL_LINK_CELEX` to the legacy
   datasets; `case_identifier` maintained by the RS, CJEU and ECtHR loaders;
   link extractors and the link loader (Phase 2), all writing to db-dev only.
3. **Count and API guard (gated, owner's go-ahead):** a migration that makes the
   counts read only datasets marked `counted` in `citation_source_dataset`, with
   every existing dataset marked `counted` so today's numbers are unchanged
   (proved by a before/after diff of `case_cited_by` and `case_citation_counts`
   on db-dev and in production), and R7.1 in citations-api (active-dataset
   allowlist on every endpoint, `/api/combined/expand` included).
4. Only then do Phase 2 datasets reach production, dark (not `counted`, not
   active in the API); each is switched on after its precision sample and
   evaluation score, with a before/after report.
5. R1.4 proper (correct counts: no inverse/identity/concept rows, distinct citing
   cases) is a separate gated change with its own report.

## Data path for every new link (weekly and backfill)

One code path, two stages, joined on natural keys only.

1. **Extract** (case-law-explorer `data_extraction/linking/`): reads texts and
   metadata from the database it is pointed at (db-dev), finds mentions,
   resolves each to a target and writes JSONL, one line per link:

   ```json
   {"source": {"ecli": "ECLI:NL:RVS:2020:423"},
    "target": {"ecli": "ECLI:EU:C:2019:189"},
    "target_ref_raw": "C-221/17", "target_ref_scheme": "cjeu_case_number",
    "relation_type": "cites", "source_dataset": "rs_text_cjeu_casenum",
    "extractor_version": "rs_text/1.0.0", "match_method": "case_number",
    "confidence": 0.95,
    "mentions": [{"text_field": "fulltext", "language": "nl",
                  "text_source": "RECHTSPRAAK", "char_start": 10234,
                  "char_end": 10242, "raw_text": "C‑221/17",
                  "context": "...", "section": null, "segment_hash": null}]}
   ```

   Sources and targets are given by ECLI, else CELEX, else `item_id`; never by
   `cases.id`. `target_ref_raw` is the *normalised* reference
   (`identifiers.py`); the text as written is in each mention's `raw_text`.
   An unresolved reference has `"target": null`.
2. **Load** (`data_loading/link_loader.py`): maps natural keys to ids in its
   database and inserts with explicit conflict targets and the index predicate
   restated (the `upsert_citation` pattern in `clients/postgres.py`, valid
   against partial and non-partial indexes), then a `SELECT id` when nothing
   was returned, so mentions attach to an existing row. If the source case is
   missing, the line is counted and skipped. If the *target* is missing (a case
   in db-dev not yet promoted), the row is written unresolved with
   `target_ecli_raw`/`target_celex_raw` set from the target's natural key, so
   R2.9 resolves it later. It returns counts per `source_dataset`: inserted,
   already present, written unresolved, source missing, mentions inserted.

**Weekly runs.** Each DAG writes `processed/<month>/links/<source_dataset>.jsonl`
(empty files allowed) next to its other artifacts and loads them into db-dev.
Promoter changes (caselaw-coolify, separate PR): list these files in
`artifacts_for` with role `links` and `allow_empty=True`; keep a list of paths
per role in `replay_artifacts`; replay the link loader after the corpus loader;
add per-`source_dataset` citation and mention counts to `validation_report` and
`assert_promoted`. Link artifacts are rewritten by re-runs exactly like the
existing artifacts, so the existing hash check covers them.

**Historical backfill.** Not through the promoter (its sources, its Airflow-run
check and its read-only data mount don't fit a one-off). Run extract against
db-dev from cjeu-migration (`migration/sql/65_...` onwards, calling the same
case-law-explorer modules), load into db-dev, verify there (counts per
dataset, precision sample, evaluation set), record the artifact hashes and
expected counts in `migration/verify/`, then replay the same hashed files into
production with the link loader, with the owner's go-ahead and the expected
counts shown first, as a manually triggered task on the ETL worker.

Removing a dataset is one `DELETE FROM case_citation WHERE source_dataset = ...`
(mentions cascade), backed by an index on `source_dataset`.

## `0009_cross_dataset_links.sql` (caselaw-coolify)

Every object written `"public"."..."` in the file (shortened below), every
statement `IF NOT EXISTS`, `SET LOCAL lock_timeout = '5s'` at the top. Only
metadata changes on existing tables; everything else is new and empty.

### `case_citation`: provenance columns (R1.1)

```sql
ALTER TABLE case_citation
  ADD COLUMN IF NOT EXISTS match_method      text,  -- ecli | case_number | appno | celex | metadata | formal_relation | name_date | ljn | zaaknummer
  ADD COLUMN IF NOT EXISTS confidence        real,
  ADD COLUMN IF NOT EXISTS target_ref_raw    text,  -- normalised reference when it is not an ECLI/CELEX
  ADD COLUMN IF NOT EXISTS target_ref_scheme text;  -- cjeu_case_number | echr_appno | nl_zaaknummer | ljn | name_date
ALTER TABLE case_citation
  ADD CONSTRAINT case_citation_confidence_check
      CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1) NOT VALID,
  ADD CONSTRAINT case_citation_target_ref_scheme_check
      CHECK (target_ref_raw IS NULL OR target_ref_scheme IS NOT NULL) NOT VALID;
```

Nullable, no default: existing rows keep NULL and are not rewritten. The CHECKs
are `NOT VALID`, so adding them does not scan the existing rows (all NULL in
these columns anyway); they apply to every new or updated row. Constraint names
are guarded with a `pg_constraint` lookup so the file can be re-run.

### `case_citation_mention` (R1.1, R1.3)

```sql
CREATE TABLE case_citation_mention (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  citation_id          bigint NOT NULL REFERENCES case_citation(id) ON DELETE CASCADE,
  case_text_id         bigint REFERENCES case_text(id) ON DELETE SET NULL,
  text_field           text NOT NULL,      -- fulltext | summary | national_judgement_xml | metadata
  language             text,
  char_start           int,
  char_end             int,
  raw_text             text NOT NULL,      -- as written
  context              text,               -- up to ~300 chars around it
  segment_id           bigint REFERENCES case_segment(id) ON DELETE SET NULL,
  section              text,               -- e.g. ECtHR relevant domestic law (D4)
  treatment_label      text,               -- R5.4, later
  treatment_model      text,
  treatment_confidence real CHECK (treatment_confidence IS NULL OR treatment_confidence BETWEEN 0 AND 1),
  extractor_version    text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX case_citation_mention_uk ON case_citation_mention
  (citation_id, text_field, COALESCE(language, ''), COALESCE(char_start, -1), md5(raw_text));
CREATE INDEX case_citation_mention_idx_segment   ON case_citation_mention (segment_id)   WHERE segment_id IS NOT NULL;
CREATE INDEX case_citation_mention_idx_case_text ON case_citation_mention (case_text_id) WHERE case_text_id IS NOT NULL;
```

`citation_id` is local to each database; the loader resolves it, the artifact
carries natural keys only. `segment_id` is resolved from `segment_hash`
(`case_segment` is unique on `(case_id, segment_hash)`), `case_text_id` from
`(case_id, language, text_source)`.

### `case_identifier` (R1.2)

```sql
CREATE TABLE case_identifier (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id          bigint NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  scheme           text NOT NULL,   -- ecli | ljn | celex | cjeu_case_number | echr_appno | nl_zaaknummer | cellar_dossier_uuid
  value            text NOT NULL,   -- as found
  value_normalized text NOT NULL,   -- identifiers.py
  source           text NOT NULL,   -- cases.ecli | cjeu_document.case_number | cellar_joins | echr_document_appno | rs_document.zaaknummer | rs_document.replaces_identifier | ...
  is_primary       boolean NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX case_identifier_uk ON case_identifier (case_id, scheme, value_normalized);
CREATE INDEX case_identifier_idx_lookup ON case_identifier (scheme, value_normalized);
CREATE INDEX case_identifier_idx_value_trgm ON case_identifier USING gin (value_normalized gin_trgm_ops);
```

Not unique on `(scheme, value_normalized)`: a case number belongs to the
judgment, its orders and the AG opinion; an application number to several
ECtHR documents. Extractors choose among candidates (type, date, cues, and the
suffix kept in `value`, so an interim order `R` or a costs order `DEP` is not
taken for the judgment) and record it in `match_method`/`confidence`.
Normalisation lives in `data_extraction/linking/identifiers.py` with an SQL twin
for the backfill, tested against each other. Maintained going forward by the
RS, CJEU and ECtHR loaders (so promoter replay fills it in production), filled
for history by R1.2a.

### Lookups: relation types and source datasets

Documentation lookups, no foreign keys (existing data stays as is):

```sql
CREATE TABLE citation_relation_type (
  code        text PRIMARY KEY,
  description text NOT NULL,
  is_citation boolean NOT NULL,              -- a court relying on another decision
  is_inverse  boolean NOT NULL DEFAULT false, -- stored from the target's side (cellar cited_by)
  is_identity boolean NOT NULL DEFAULT false, -- same decision under another identifier
  counted     boolean NOT NULL               -- intended for citation counts once R1.4 lands
);
CREATE TABLE citation_source_dataset (
  code          text PRIMARY KEY,
  description   text NOT NULL,
  corpus        text,                        -- RS | CJEU | ECHR | cross
  counted       boolean NOT NULL DEFAULT false,
  active_in_api boolean NOT NULL DEFAULT false
);
```

`citation_relation_type` is seeded only with types whose meaning is known
(Cellar types, `cites`, `replaced_by` as an identity relation, and the new
procedural types, none of which count as citations). `citation_source_dataset`
starts empty. The count and API guard (step 3) registers every dataset and
relation type present in production, read from production when it ships, with
today's datasets `counted`, so it changes no number; new datasets start
uncounted. `active_in_api` is set only for datasets a collection endpoint shows
today (`rs_body_cite`, `rs_legacy_ddb`, `rs_formal_relation`, `echr_edge`,
`cellar_sparql`), never for `rs_lido_sqlite`: in citations-api (R7.1, branch
`feature/active-dataset-config`) a row marked active joins its collection's
endpoint, and `rs_lido_sqlite` is shown only by `/api/combined/expand` today,
which keeps it by default. New datasets start inactive. Seeding from `case_citation` inside `0010` was
dropped: two more full scans under its lock, and an empty db-dev would get
different rows than production. Nothing reads either table until step 3.

### Phase 4 and 5 tables (created now, filled later)

```sql
CREATE TABLE authority_document (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL,          -- nl_parliamentary_paper | nl_policy_rule | eu_preparatory_act | national_court_decision | efta_court | un_treaty_body | coe_body | literature
  identifier text, identifier_scheme text,
  title text, issuer text, date date, url text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX authority_document_uk ON authority_document
  (kind, COALESCE(identifier_scheme, ''), identifier) WHERE identifier IS NOT NULL;

CREATE TABLE case_authority_reference (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id bigint NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  authority_document_id bigint REFERENCES authority_document(id) ON DELETE SET NULL,
  raw_reference text NOT NULL, pinpoint text,
  text_field text, language text, char_start int, char_end int,
  context_segment_id bigint REFERENCES case_segment(id) ON DELETE SET NULL,
  source_dataset text NOT NULL, extractor_version text NOT NULL,
  match_method text,
  confidence real CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX case_authority_reference_uk ON case_authority_reference
  (case_id, raw_reference, COALESCE(text_field, ''), COALESCE(char_start, -1), source_dataset);
CREATE INDEX case_authority_reference_idx_document ON case_authority_reference (authority_document_id) WHERE authority_document_id IS NOT NULL;
CREATE INDEX case_authority_reference_idx_segment  ON case_authority_reference (context_segment_id)   WHERE context_segment_id IS NOT NULL;
CREATE INDEX case_authority_reference_idx_dataset  ON case_authority_reference (source_dataset);

CREATE TABLE case_outcome (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id bigint NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  scope text NOT NULL DEFAULT 'overall',  -- overall | article
  article text,                           -- ECtHR: '3', '8', 'P1-1'
  outcome text NOT NULL,                  -- violation | no_violation | dismissed | upheld | quashed | inadmissible | annulled | answered | ...
  raw_value text,
  source_dataset text NOT NULL, extractor_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX case_outcome_uk ON case_outcome
  (case_id, scope, COALESCE(article, ''), outcome, source_dataset);

CREATE TABLE case_status (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id bigint NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  status text NOT NULL,                   -- pending | struck_out | referred_to_grand_chamber | final | overturned
  status_date date,
  related_case_id bigint REFERENCES cases(id) ON DELETE SET NULL,
  raw_value text,
  source_dataset text NOT NULL, extractor_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX case_status_uk ON case_status
  (case_id, status, COALESCE(status_date, '-infinity'::date), source_dataset);
CREATE INDEX case_status_idx_related ON case_status (related_case_id) WHERE related_case_id IS NOT NULL;
```

### R6.1 support tables (created now, used only after the gate and approval)

```sql
CREATE TABLE case_citation_archive (LIKE case_citation INCLUDING DEFAULTS);
ALTER TABLE case_citation_archive
  ADD COLUMN archived_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN archive_reason text NOT NULL,   -- e.g. 'R6.1 echr_edge rebuild'
  ADD COLUMN archive_batch text NOT NULL,
  ADD PRIMARY KEY (archive_batch, id);

CREATE TABLE echr_edge_v2_staging (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_ecli text, source_item_id text,
  target_ecli text, target_item_id text,
  scl_reference text NOT NULL,     -- the scl entry the edge comes from
  appno text, rule text NOT NULL,  -- which disambiguation rule chose the target
  build_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (source_ecli IS NOT NULL OR source_item_id IS NOT NULL)
);
CREATE UNIQUE INDEX echr_edge_v2_staging_uk ON echr_edge_v2_staging
  (COALESCE(source_ecli, source_item_id), COALESCE(target_ecli, target_item_id, ''), scl_reference, build_version);
CREATE INDEX echr_edge_v2_staging_idx_source ON echr_edge_v2_staging (build_version, source_ecli);
CREATE INDEX echr_edge_v2_staging_idx_target ON echr_edge_v2_staging (build_version, target_ecli) WHERE target_ecli IS NOT NULL;
```

`LIKE ... INCLUDING DEFAULTS` copies columns, NOT NULL and defaults, not the
identity, indexes, foreign keys or triggers, so archive rows keep their original
`id` and nothing fires. The R6.1 archive and restore SQL lists columns
explicitly (no `SELECT *`), so a column added to `case_citation` later cannot
break it.

### Count refresh function (R1.4; created, not called)

```sql
CREATE FUNCTION refresh_case_citation_counts(p_case_ids bigint[] DEFAULT NULL)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = "public" ...
REVOKE ALL ON FUNCTION refresh_case_citation_counts(bigint[]) FROM PUBLIC;
```

Recomputes `cites_count` (distinct targets) and `cited_by_count` (distinct citing
cases) for the given cases (driven from `unnest(p_case_ids)`, or all `cases` when
NULL, so cases left with no counted edge are set to 0), from resolved rows only,
excluding self-loops, relation types with `counted = false` and datasets not
registered as counted (fail closed); upserts `case_citation_counts`; returns rows changed. Granted
to the promoter in `configure-promoter-role.sh` when it is switched on (step 5).

## `0010_case_citation_link_indexes.sql` (caselaw-coolify)

Separate file so its scans hold only a SHARE lock. `SET LOCAL lock_timeout = '5s'`.

```sql
CREATE UNIQUE INDEX IF NOT EXISTS case_citation_uk_unresolved_ref ON case_citation
  (source_case_id, target_ref_scheme, target_ref_raw, COALESCE(relation_type, ''), source_dataset)
  WHERE target_case_id IS NULL AND target_ref_raw IS NOT NULL;
CREATE INDEX IF NOT EXISTS case_citation_idx_target_ref ON case_citation
  (target_ref_scheme, target_ref_raw) WHERE target_case_id IS NULL AND target_ref_raw IS NOT NULL;
CREATE INDEX IF NOT EXISTS case_citation_idx_source_dataset ON case_citation (source_dataset);
```

Duration on production is not yet measured (needs the sql-runner); measure the
row count and a comparable index build on db-dev first.

## Late-binding resolution (R2.9)

A link-loader step after each load: for rows with `target_case_id IS NULL`
whose `target_ecli_raw`, `target_celex_raw` or `(target_ref_scheme,
target_ref_raw)` matches `case_identifier` of a case loaded in this batch,
insert the resolved row (same source, relation, dataset, method and confidence)
and copy its mentions to the new row, both `ON CONFLICT DO NOTHING`. The
unresolved row stays, so every run is insert-only; API and counts read resolved
rows only.

Side effect to handle: if a target case is later *deleted*,
`trg_cases_preserve_citation_raw` turns its resolved rows back into unresolved
ones, which collide with the surviving unresolved twin and make the delete fail
(`23505`). Case deletions are rare and manual; the runbook for one is to delete
the resolved rows that have an unresolved twin first. Not changed in `0009`.

## Changes to existing code (case-law-explorer)

- `cjeu_loader.py` `SQL_LINK_ECLI`: restrict to the legacy datasets
  (`rs_body_cite`, `rs_legacy_ddb`, `rs_formal_relation`, `rs_lido_sqlite`,
  `echr_edge`, after checking the full list in production).
- `db/schema.sql`: mirror `0008_case_text_document_kind`, `0009` and `0010`, and
  make its three `case_citation` unique indexes partial like coolify's.
- Loaders maintain `case_identifier` for the cases they load.

## Review log

- **v1 → v2, 2026-11-24, independent review (Gemini 4 Neon).** 8 blocking, 5
  should-fix, 1 suggestion; each code claim re-checked before adoption.
  Adopted: schema-qualify every object (the migrator rewrites only quoted
  `"public"`); split `case_citation` indexes into `0010` (ACCESS EXCLUSIVE from
  `ADD COLUMN` would otherwise be held through the index scan); counts and
  `/api/combined/expand` change as soon as new rows land, and `case_cited_by`'s
  >300-row cut-off counts all datasets, so Phase 2 data reaches production only
  after a count/API guard (`citation_source_dataset`, step 3); `cjeu_loader.py`
  `SQL_LINK_ECLI` rewrites `rs_*` rows in place; R2.9 copies mentions to the
  resolved row; the link loader degrades a missing target to an unresolved row;
  per-file link artifacts with list-valued roles and per-dataset verification
  counts in the promoter; `case_identifier` maintained by the weekly loaders;
  normalised `target_ref_raw` plus CHECK on its scheme; indexes for R2.9 and
  per-dataset deletes; mention fields for replay (`language`, `text_source`,
  `context`, `section`, `segment_hash`); explicit `ON CONFLICT` targets;
  `-infinity` instead of `epoch`; archive primary key and explicit column lists;
  staging keyed on ECLI or item id; FK indexes; `SECURITY DEFINER` refresh
  function that also resets to 0; confidence CHECKs; `cellar_dossier_uuid`
  scheme. Corrected my claim that db-dev is built from case-law-explorer
  `db/schema.sql` (it is built from the coolify migrations).
  Adjusted: the guard in step 3 keeps every existing dataset counted, so it
  changes no number today, rather than switching counts in the same step; R1.4
  proper stays a separate gated change. Not adopted: making
  `trg_cases_preserve_citation_raw` delete duplicates (a change to existing
  behaviour for a rare manual operation; documented instead).

- **v2 → SQL, 2026-11-24, second independent review (Gemini 4 Neon)** of the
  written `0009`/`0010`. 3 blocking, 6 should-fix, 3 suggestions; all re-checked
  on PostgreSQL 18. Adopted: `refresh_case_citation_counts` qualified every
  table and put `pg_temp` last (a caller with EXECUTE could otherwise shadow
  tables with temporary ones and zero the counts; reproduced, then confirmed
  fixed); separate full and targeted paths without full scans of
  `case_citation_counts`, writing only changed rows, `work_mem` 64MB; datasets
  fail closed; no seeding from `case_citation` in `0010` (wrongly classified
  `replaced_by` as a counted citation, two extra scans, db-dev/production
  divergence); `replaced_by` seeded as identity; `interprets_judgement` and
  `national_follow_up` not counted; `0009` takes the `case_citation` lock last;
  offset checks, `md5` key for long references, `language` in the authority key,
  dataset/filter indexes on outcome and status, `active_in_api` on relation
  types, `maintenance_work_mem` for `0010`; the promoter loses INSERT/UPDATE on
  the lookups and the archive (`configure-promoter-role.sh`). To do before
  deploying: check in production that none of the new table names exist, and
  measure `0010` on db-dev.

## Deploy order (from the implementation, 2026-11-24)

Each step needs the owner's go-ahead; none is done.

1. caselaw-coolify `0009`/`0010` (`feature/cross-dataset-link-schema`), applied to db-dev and production outside ETL windows, after checking the new table names don't exist and timing `0010` on db-dev.
2. case-law-explorer `feature/cross-dataset-linking` (extractors, link loader, identifier loader, DAG wiring), with every flag off: `CASE_IDENTIFIER_ENABLED`, `LINK_EXTRACTION_ENABLED`.
3. caselaw-coolify promoter (`feature/promoter-link-artifacts`). It must be deployed before `LINK_EXTRACTION_ENABLED` is turned on, or the old promoter's citation check fails on batches whose db-dev counts include link rows. Batches without link files are handled exactly as before.
4. R1.2a backfill on db-dev (dry run, then write), verify, then `CASE_IDENTIFIER_ENABLED=true`.
5. Count/API guard and R7.1 (gated, with the before/after report).
6. `LINK_EXTRACTION_ENABLED=true` on db-dev; historical link backfill on db-dev; precision samples and evaluation on real data; then promotion/replay to production with expected counts shown first.
