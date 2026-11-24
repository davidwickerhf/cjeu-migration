# Cross-dataset linking: Phase 1 design

Status: draft for review, 2026-11-24. Companion to
[CROSS_DATASET_ROADMAP.md](CROSS_DATASET_ROADMAP.md) (items R1.1–R1.5, and the
data paths every Phase 2 extractor uses). Nothing here is applied anywhere yet.

## Corrections to the roadmap found while designing

- **The migration number is `0009`, not `0008`.** caselaw-coolify `main` already
  has two `0008` files: `0008_case_cited_by.sql` (the `case_cited_by`
  materialized view, refreshed by the promoter after each promotion) and
  `0008_case_text_document_kind.sql` (`case_text.document_kind`, the
  `case_text_canonical` view).
- **`case_cited_by` is where it was created** (R1.4 asked): `0008_case_cited_by.sql`.
  It counts distinct citing *units* (an ECtHR application or a case) per target
  over every `case_citation` row with both ends resolved, whatever its
  `relation_type`, and drops sources with more than 300 rows. So it also counts
  inverse `cited_by` rows (in the wrong direction), `is_about_concept`, `joins`
  and identity `replaced_by` rows. citations-api reads `case_cited_by` on the
  collection endpoints (`utils/citation_counts.py`) but still reads
  `case_citation_counts` in `api/combined/index.py` and
  `api/combined/expand/index.py`.
- **Two schema files.** The pipeline's tests and db-dev bootstrap use
  case-law-explorer `db/schema.sql`; the deployed databases are built from
  caselaw-coolify `db/migrations/`. Their unique indexes differ (coolify's are
  partial, `WHERE target_case_id IS [NOT] NULL`; case-law-explorer's are not).
  Every new object goes into both, and every new insert uses a bare
  `ON CONFLICT DO NOTHING` (no arbiter columns), which works against either.
- **The promoter replays artifacts, not rows.** `etl/promoter.py`
  `replay_artifacts` re-runs the case-law-explorer loaders against production
  on the verified files of a batch. New link data must therefore travel as
  artifacts the promoter knows (see "Data path" below), or production never
  receives it.
- **The migrator applies each file in one transaction** (`psql -1`), so
  `CREATE INDEX CONCURRENTLY` is not available. Index builds on `case_citation`
  take a SHARE lock (reads continue, writes wait) for the length of one scan.

## Data path for every new link (weekly and backfill)

One code path, two stages, joined on natural keys only:

1. **Extract** (case-law-explorer `data_extraction/linking/`): reads texts and
   metadata from the database the task is pointed at (db-dev for the weekly
   run and the backfill), finds mentions, resolves each to a target and writes
   an artifact `links/<source_dataset>/<window>.jsonl`. One line per link:

   ```json
   {"source": {"ecli": "ECLI:NL:RVS:2020:423"},
    "target": {"ecli": "ECLI:EU:C:2019:189"},
    "target_ref_raw": "C-221/17", "target_ref_scheme": "cjeu_case_number",
    "relation_type": "cites", "source_dataset": "rs_text_cjeu_casenum",
    "extractor_version": "rs_text/1.0.0", "match_method": "case_number",
    "confidence": 0.95,
    "mentions": [{"text_field": "fulltext", "char_start": 10234,
                  "char_end": 10242, "raw_text": "C-221/17"}]}
   ```

   Sources and targets are identified by ECLI, else CELEX, else `item_id`;
   never by `cases.id`. An unresolved reference has `"target": null` and keeps
   `target_ref_raw`.
2. **Load** (`data_loading/link_loader.py`): maps natural keys to ids in the
   database it is pointed at, inserts `case_citation` rows with
   `ON CONFLICT DO NOTHING`, then the mentions of the rows it inserted or found,
   and returns counts per `source_dataset` (inserted, already present,
   unresolved, source missing).

The weekly DAGs run extract then load against db-dev and list the artifact in
the batch manifest; the promoter verifies its hash and replays the load step
against production (a small promoter change: an artifact role `links` and a
call to the link loader). The historical backfill runs the same two stages
from cjeu-migration over id ranges against db-dev; after verification on db-dev
the same artifacts are replayed into production with the owner's go-ahead and
the expected counts shown first. Deleting a dataset is one
`DELETE FROM case_citation WHERE source_dataset = ...` (mentions cascade).

Why an artifact rather than re-running extraction in production: the promoter
can verify and count exactly what will be written, the run is reproducible from
the file, and production never runs the regex scans.

## Migration `0009_cross_dataset_links.sql` (caselaw-coolify)

All additive. `SET LOCAL lock_timeout = '5s'` at the top so the migration fails
and is retried rather than queueing behind long API reads while it takes the
brief ACCESS EXCLUSIVE lock that `ALTER TABLE ... ADD COLUMN` needs. Mirror
in case-law-explorer `db/schema.sql`.

### `case_citation`: provenance columns (R1.1)

```sql
ALTER TABLE case_citation
  ADD COLUMN IF NOT EXISTS match_method      text,  -- ecli | case_number | appno | celex | metadata | formal_relation | name_date | ljn | zaaknummer
  ADD COLUMN IF NOT EXISTS confidence        real,  -- 0..1, extractor's own estimate
  ADD COLUMN IF NOT EXISTS target_ref_raw    text,  -- the reference as written when it is not an ECLI/CELEX
  ADD COLUMN IF NOT EXISTS target_ref_scheme text;  -- cjeu_case_number | echr_appno | nl_zaaknummer | ljn | name_date
CREATE UNIQUE INDEX IF NOT EXISTS case_citation_uk_unresolved_ref ON case_citation
  (source_case_id, target_ref_scheme, target_ref_raw, relation_type, source_dataset)
  WHERE target_case_id IS NULL AND target_ref_raw IS NOT NULL;
```

Nullable, no default: metadata-only for existing rows. Existing rows keep NULL
(no backfill of provenance onto old rows). An unresolved row with both
`target_ecli_raw` and `target_ref_raw` falls under two unique indexes, which is
harmless.

### `case_citation_mention` (R1.1, R1.3)

One row per occurrence of a link in a text.

```sql
CREATE TABLE case_citation_mention (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  citation_id          bigint NOT NULL REFERENCES case_citation(id) ON DELETE CASCADE,
  case_text_id         bigint REFERENCES case_text(id) ON DELETE SET NULL,
  text_field           text NOT NULL,      -- fulltext | summary | national_judgement_xml | metadata
  language             text,
  char_start           int,
  char_end             int,
  raw_text             text NOT NULL,      -- the matched string
  context              text,               -- up to ~300 chars around it
  segment_id           bigint REFERENCES case_segment(id) ON DELETE SET NULL,
  section              text,               -- e.g. ECtHR THE LAW / relevant domestic law (D4)
  treatment_label      text,               -- R5.4, later
  treatment_model      text,
  treatment_confidence real,
  extractor_version    text NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX case_citation_mention_uk ON case_citation_mention
  (citation_id, text_field, COALESCE(language, ''), COALESCE(char_start, -1));
CREATE INDEX case_citation_mention_idx_segment ON case_citation_mention (segment_id)
  WHERE segment_id IS NOT NULL;
```

Offsets refer to the text as stored when extracted; `extractor_version` and
`case_text.updated_at` say whether they are still valid.

### `case_identifier` (R1.2)

```sql
CREATE TABLE case_identifier (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id          bigint NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  scheme           text NOT NULL,   -- ecli | ljn | celex | cjeu_case_number | echr_appno | nl_zaaknummer
  value            text NOT NULL,   -- as found
  value_normalized text NOT NULL,   -- shared normalisation (below)
  source           text NOT NULL,   -- cases.ecli | cjeu_document.case_number | cellar_joins | echr_document_appno | rs_document.zaaknummer | rs_document.replaces_identifier | ...
  is_primary       boolean NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX case_identifier_uk ON case_identifier (case_id, scheme, value_normalized);
CREATE INDEX case_identifier_idx_lookup ON case_identifier (scheme, value_normalized);
CREATE INDEX case_identifier_idx_value_trgm ON case_identifier USING gin (value_normalized gin_trgm_ops);
```

Not unique on `(scheme, value_normalized)`: a CJEU case number belongs to the
judgment, its orders and the AG opinion; an application number to several
ECtHR documents. Choosing among them is the extractor's job (type, date, cues),
recorded in `match_method`/`confidence`. Normalisation (one Python module,
`data_extraction/linking/identifiers.py`, plus an SQL twin used by the
backfill, tested against each other):

- CJEU case numbers: Unicode hyphens (U+2010–U+2015, U+2212) and spaces to
  `-`; upper-case court letter; strip leading zeros of the number
  (`C-04/19` → `C-4/19`); two-digit year kept as written; suffixes `P`, `PPU`,
  `R`, `RX`, `REC`, `DEP`, `AJ`, `TO`, `INT`, `REV`, `OP`, `SA` kept in `value`,
  dropped in `value_normalized` (`C-221/17 P` → `C-221/17`); old cases without a
  court letter (`26/62`) kept as `26/62`.
- ECtHR application numbers: `nnnnn/yy` with leading zeros stripped.
- ECLIs: upper-case, `ECLI:` prefix added when written as a short ECLI
  (`EU:C:2019:189`).
- Dutch zaaknummers: whitespace collapsed; the `/1/` instance segment kept in
  `value` and both forms stored (`201012342/1/T1/V2` and `201012342/T1/V2`).

### Relation types (R1.1)

A documentation lookup, no foreign key (existing data stays as is):

```sql
CREATE TABLE citation_relation_type (
  code          text PRIMARY KEY,
  description   text NOT NULL,
  is_citation   boolean NOT NULL,  -- a court relying on another decision
  is_inverse    boolean NOT NULL DEFAULT false,  -- stored from the target's side (cellar cited_by)
  is_identity   boolean NOT NULL DEFAULT false,  -- same decision under another identifier
  counted       boolean NOT NULL   -- included in citation counts (R1.4)
);
```

Seeded with the existing types (`cites`, `cited_by`, `joins`,
`subject_to_appeal`, `reexamined_by`, `is_about_concept`,
`referred_for_preliminary_ruling`, `interprets_judgement`,
`logical_successor_of`, the RS formal-relation values found in production)
and the new ones (`preliminary_reference`, `national_follow_up`, `opinion_of`,
`appeal_of`, `earlier_instance`). Types missing from the table count as
citations, so an unknown value never silently disappears from counts. The RS
formal-relation values are read from production before the file is written
(`SELECT DISTINCT relation_type ...`), never guessed.

### Phase 4 and 5 tables (created now, filled later)

```sql
CREATE TABLE authority_document (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind text NOT NULL,          -- nl_parliamentary_paper | nl_policy_rule | eu_preparatory_act | national_court_decision | efta_court | un_treaty_body | coe_body | literature
  identifier text,             -- e.g. kst-34356-3, COM(2016) 467, CELEX 52016PC0467
  identifier_scheme text,
  title text, issuer text, date date, url text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX authority_document_uk ON authority_document (kind, identifier_scheme, identifier)
  WHERE identifier IS NOT NULL;

CREATE TABLE case_authority_reference (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id bigint NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
  authority_document_id bigint REFERENCES authority_document(id) ON DELETE SET NULL,
  raw_reference text NOT NULL, pinpoint text,
  text_field text, char_start int, char_end int,
  context_segment_id bigint REFERENCES case_segment(id) ON DELETE SET NULL,
  source_dataset text NOT NULL, extractor_version text NOT NULL,
  match_method text, confidence real,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX case_authority_reference_uk ON case_authority_reference
  (case_id, raw_reference, COALESCE(char_start, -1), source_dataset);

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
CREATE UNIQUE INDEX case_status_uk ON case_status (case_id, status, COALESCE(status_date, 'epoch'), source_dataset);
```

### R6.1 support tables (created now, used only after the gate and approval)

```sql
-- Exact copy of every column of case_citation, plus why and when it was archived.
CREATE TABLE case_citation_archive (LIKE case_citation INCLUDING DEFAULTS);
ALTER TABLE case_citation_archive
  ADD COLUMN archived_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN archive_reason text NOT NULL,   -- e.g. 'R6.1 echr_edge rebuild'
  ADD COLUMN archive_batch text NOT NULL;
CREATE INDEX case_citation_archive_idx_batch ON case_citation_archive (archive_batch);

CREATE TABLE echr_edge_v2_staging (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_ecli text NOT NULL, source_item_id text,
  target_ecli text, target_item_id text,
  scl_reference text NOT NULL,     -- the scl entry the edge comes from
  appno text, rule text NOT NULL,  -- which disambiguation rule chose the target
  build_version text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX echr_edge_v2_staging_uk ON echr_edge_v2_staging
  (source_ecli, COALESCE(target_ecli, ''), scl_reference, build_version);
```

`LIKE ... INCLUDING DEFAULTS` copies columns and defaults but not the identity,
indexes or triggers, so archive rows keep their original `id` and nothing
fires. The staging table uses natural keys only, so it can be built on db-dev
and replayed.

## Citation counts (R1.4)

In `0009`, add (not switch on):

```sql
CREATE FUNCTION refresh_case_citation_counts(p_case_ids bigint[] DEFAULT NULL) RETURNS integer ...
```

It recomputes `cites_count` (distinct targets) and `cited_by_count` (distinct
citing cases) for the given cases, or all cases when NULL, set-based, from
resolved rows only, excluding self-loops and relation types with
`counted = false` (`cited_by`, `is_about_concept`, `joins`, identity types),
and upserts `case_citation_counts`. It returns the number of rows changed.

Switching over is a separate, gated step (not in `0009`): a before/after
report per collection, then (with the owner's go-ahead) one migration that
replaces `trg_case_citation_counts` (per-row increments) with nothing, runs the
full refresh, and has the loaders and the promoter call the function for the
touched cases after each batch. The same report covers a `case_cited_by` revision that
excludes the same relation types (a new view definition changes numbers users
see, so it goes through the same gate). Until then nothing users see changes.

## Late-binding resolution (R2.9)

A link-loader step after each load: for rows with `target_case_id IS NULL`
whose `target_ecli_raw`, `target_celex_raw` or `(target_ref_scheme,
target_ref_raw)` now matches `case_identifier` for a case loaded in this batch,
insert the resolved row (same source, relation, dataset, method; confidence
copied) with `ON CONFLICT DO NOTHING`. The unresolved row stays, so every run
is insert-only. API and counts read resolved rows only, so the duplicate
unresolved row is invisible to them.

## Open questions for review

1. Is `target_ref_scheme` in the unique index right, or should one raw string
   only ever have one scheme?
2. Should `case_citation_mention` key on `citation_id` (environment-specific)
   or carry its own natural key? The loader resolves the citation per database,
   so `citation_id` is always local; the artifact holds natural keys only.
3. Index builds in one transaction: `case_citation_uk_unresolved_ref` scans the
   whole table once under a SHARE lock. Acceptable outside ETL windows, or split
   into its own migration applied at a quiet time?
