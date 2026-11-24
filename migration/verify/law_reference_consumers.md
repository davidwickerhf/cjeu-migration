# Law-reference consumers and provision-only rows (R3.6)

Date: 2026-11-24

## Question

`case_law_reference` points at an act in two ways: directly through
`legislation_id`, or through `provision_id` -> `legal_provision.legislation_id`.
In production, 786,846 rows with `source_dataset = 'rs_lido-both'` have
`legislation_id` NULL, and 786,746 of those have `provision_id` set, so for them
the act is reachable only through the provision (the other 100 have neither id).
R3.6 asks which consumers that join `case_law_reference` to `legislation`, or
filter or count by `legislation_id`, also follow `provision_id`, and which ones
miss these rows. Consumers that match through `raw_resource` / `raw_label_id`
are listed with what they cover.

## Method

Read-only. No database, no network. Files were read from git refs with
`git show <ref>:<path>` and searched with `git grep -n <pattern> <ref>`.

Patterns: `case_law_reference`, `legislation_id`, `provision_id`,
`legal_provision`, `legislation`, `raw_label_id`, `bwb_label_id`,
`raw_resource`, `law_reference`, `law_element`, `case_law`, `/api/links/laws`,
`/api/links/cases`.

| Repo | Ref read | Short SHA |
|---|---|---|
| citations-api | `origin/main` (working tree is on `feature/cjeu-metadata-filters`, not read) | d3ba960 |
| citations-api | `origin/feature/active-dataset-config` (= `origin/main` + 3 commits; none touch the files below, same 23 grep hits) | 6f8449f |
| case-law-explorer | `origin/feature/cross-dataset-linking` | 79b67c6 |
| case-law-explorer | `origin/main` (for comparison only) | d35d5d7 |
| coolify-eu-legislation (caselaw-coolify) | checked-out `HEAD` = `origin/feature/cross-dataset-link-schema` | cdf4c89 |
| coolify-eu-legislation | `origin/main` (differs from HEAD only by migrations 0009/0010, which do not touch `case_law_reference`; the RS views are identical) | 84df2bb |
| caselaw-ui | `HEAD` (`main`) | 67e90be |
| cjeu-law-reference-consumers (this repo) | `HEAD`, for the legacy load SQL | 1a51500 |

What the `rs_lido-both` rows carry was taken from this repo's legacy load,
`migration/sql/20_rs_cases.sql:88-107`: rows from legacy
`rs_document_law_reference` are written with `raw_scheme = 'bwb'`,
`raw_resource = bwb_resource`, `raw_subdivision = article`,
`raw_label_id = bwb_label_id`, `raw_reference = opschrift`, and
`source_dataset = 'rs_' || source` (so legacy `lido-both` becomes
`rs_lido-both`). `legislation_id` comes from a LEFT JOIN on
`legislation.identifier = bwb_resource` and `provision_id` from a LEFT JOIN on
`legal_provision.bwb_label_id = bwb_label_id`. A NULL `legislation_id` with a
set `provision_id` therefore means: no `legislation` row with
`identifier = raw_resource` existed at load time, but the label id resolved to
a provision. The comment in case-law-explorer
`airflow/dags/lido/tasks/merge_into_cle.py:135-143` agrees (legacy rows
identify the target by BWB id and article and carry a `provision_id` for about
80 percent). This was not checked against the database.

Rows written later by the LIDO merge (`merge_into_cle.py` step 5a, 5b) have no
raw columns at all: 5a writes `legislation_id` only, 5b writes `provision_id`
only (`raw_scheme`, `raw_resource`, `raw_label_id` NULL). LIDO promotion to
production is gated by `ETL_PROMOTION_LIDO_ENABLED`
(`airflow/dags/promotion_client.py:160-173`).

## Consumers

"Act reached by" values: `legislation_id only`, `provision_id->legal_provision`,
`raw_resource=identifier`, `raw_label_id=bwb_label_id`, `several`, or `legacy DB`
(the separate linkextractor database, `LINKEXTRACTOR_DB_URL`, tables
`law_element` / `case_law` / `legal_case`, which cle_v2 lists as not ported).

| # | Repo / ref | File:line | What it does | Act reached by | Misses provision-only rows | Fix needed |
|---|---|---|---|---|---|---|
| 1 | citations-api `origin/main` | `api/cjeu/queryhelper.py:233-241` | `/api/cjeu` `euLaw` filter | several: `raw_resource` (CELEX) UNION `legislation_id` -> `legislation.identifier` | Not for `rs_lido-both` (BWB, outside the CELEX filter). For CJEU: yes in structure, but no CJEU row has a `provision_id` today (`cjeu_loader.py:204` writes raw columns only) | Add a UNION arm `JOIN legal_provision lp ON lp.id = r.provision_id JOIN legislation l ON l.id = lp.legislation_id`. |
| 2 | citations-api `origin/main` | `api/cjeu/queryhelper.py:642-650` | `/api/cjeu` `eu_law` metadata on result nodes | `legislation_id only` (LEFT JOIN), plus `raw_resource`; requires `raw_scheme = 'celex'` | Same as 1; also drops resolved rows that have no `raw_scheme` | Join `legislation` on `COALESCE(r.legislation_id, lp.legislation_id)` and accept resolved rows without `raw_scheme`. |
| 3 | citations-api `origin/main` | `api/cjeu/options.py:72-79` | `euLaw` filter options with case counts | `legislation_id only` (LEFT JOIN), plus `raw_resource`; requires `raw_scheme = 'celex'` | Same as 2 (counts would be low) | Same join change as 2. |
| 4 | citations-api `origin/main` | `api/combined/sqlbuilders.py:449-458` | combined search, CJEU `eu_law` condition | several: `raw_resource` UNION `legislation_id` | Same as 1 | Same extra UNION arm as 1. |
| 5 | citations-api `origin/main` | `api/combined/sqlbuilders.py:197-202` | combined search, RS `selectedLaws` (`BWB...|label`) | `legacy DB` via `linkextractor.utils.get_cases_by_bwb_and_label_id` | n.a. for cle_v2 (does not read `case_law_reference`); sees only what the legacy DB holds | Port to cle_v2: match `raw_label_id` OR `provision_id` -> `legal_provision.bwb_label_id`, with the act checked through `COALESCE(r.legislation_id, lp.legislation_id)` or `raw_resource`. |
| 6 | citations-api `origin/main` | `api/combined/sqlbuilders.py:187-194` | combined search, RS `articles` text match | text: `case_text` tsvector and `rs_document.legal_provisions` text[] | n.a. (no id join) | None for R3.6. |
| 7 | citations-api `origin/main` | `api/rechtspraak/queryhelper.py:170-196`, used at `:270-281` | `/api/rechtspraak` `selectedLaws` ECLI whitelist | `legacy DB` (`law_element.bwb_id` + `bwb_label_id` -> `case_law`) | n.a. for cle_v2; same as 5 | Same port as 5. |
| 8 | citations-api `origin/main` | `api/rechtspraak/queryhelper.py:284-289` | `/api/rechtspraak` `bwbResources` filter | `raw_resource` only (`raw_scheme = 'bwb' AND raw_resource = ANY(...)`), no join to `legislation` | No for legacy `rs_lido-both` rows (they carry `raw_resource`). Yes for rows without raw columns (LIDO merge 5a/5b) | Add UNION arms through `legislation_id` -> `legislation.identifier` and `provision_id` -> `legal_provision` -> `legislation.identifier`. |
| 9 | citations-api `origin/main` | `api/rechtspraak/queryhelper.py:305-322` | `/api/rechtspraak` `articles` filter | text: `case_text` tsvector and `rs_document.legal_provisions` | n.a. | None for R3.6. |
| 10 | citations-api `origin/main` | `api/rechtspraak/queryhelper.py:819-856` | `legal_provisions` labels on RS nodes | reads view `rs_v_document_legal_provisions` (row 15) | as row 15 | Follows the fix to row 15. |
| 11 | citations-api `origin/main` | `api/links/laws.py:28-43` (POST), `:70-84` (GET); `linkextractor/utils.py:345-375` | `/api/links/laws`: law items found in text, with `amount_related_cases` | `legacy DB` (`COUNT(DISTINCT case_law.case_id)` per `law_element (bwb_id, bwb_label_id)`) | n.a. for cle_v2 | Port the count to `case_law_reference`, counting distinct `case_id` over `raw_label_id = :label` OR `provision_id IN (legal_provision WHERE bwb_label_id = :label)`. |
| 12 | citations-api `origin/main` | `api/links/cases.py:30`, `api/links/index.py:31`; `linkextractor/utils.py:306-343` | `/api/links/cases` and `/api/links`: ECLIs per BWB id + label id (LIMIT 5000) | `legacy DB` (`law_element` -> `case_law` -> `legal_case`) | n.a. for cle_v2 | Same port as 11, returning ECLIs. |
| 13 | citations-api `origin/main` | `api/network/queryhelper.py:147-175` | network endpoint `selectedLaws` (OpenSearch path) | `legacy DB` via `get_cases_by_bwb_and_label_id` | n.a. for cle_v2 | Uses the ported helper from 11/12. |
| 14 | caselaw-coolify `cdf4c89` | `db/migrations/0001_schema_full.sql:1061-1083` | view `rs_v_document_law_reference` (legacy RS law-reference shape, deeplinks) | `raw_resource` / `raw_label_id` columns only; `WHERE raw_scheme = 'bwb'`; no join to `legislation` or `legal_provision` | No for legacy `rs_lido-both` rows. Yes for rows without raw columns (merge 5a/5b): excluded from the view | Fill `bwb_resource` / `bwb_label_id` from `legislation.identifier` (via `COALESCE(legislation_id, lp.legislation_id)`) and `legal_provision.bwb_label_id` when the raw columns are NULL. No citations-api query reads this view on `origin/main` (docstring mention only). |
| 15 | caselaw-coolify `cdf4c89` | `db/migrations/0001_schema_full.sql:1087-1132` | view `rs_v_document_legal_provisions` (display labels) | several: arm 1 `raw_reference`; arm 2 `raw_label_id = legal_provision.bwb_label_id`; arms 3-6 `raw_resource = legislation.identifier` (+ article label); all require `raw_scheme = 'bwb'`; never `legislation_id` or `provision_id` | Partly. Legacy `rs_lido-both` rows still get arm 1 and arm 2 labels, but arms 3-6 give nothing when no `legislation` row has `identifier = raw_resource` (the likely reason `legislation_id` is NULL), so the act title is missing. Rows without raw columns: missed entirely | Add an arm on `provision_id` -> `legal_provision.title` and resolve the act title through `COALESCE(lr.legislation_id, lp.legislation_id)`. |
| 16 | case-law-explorer `79b67c6` | `db/schema.sql:663` | same view as 14, `cle_v2` schema copy | as 14 | as 14 | Keep in step with 14. |
| 17 | case-law-explorer `79b67c6` | `db/schema.sql:664` | same view as 15, `cle_v2` schema copy | as 15 | as 15 | Keep in step with 15. |
| 18 | case-law-explorer `79b67c6` | `airflow/dags/lido/tasks/merge_into_cle.py:145-170` | LIDO merge 5a, dedup before inserting an act-level row | several: `legislation_id` OR `raw_resource = identifier` | n.a. (writer; act-level target) | None for R3.6; see open point on dedup. |
| 19 | case-law-explorer `79b67c6` | `airflow/dags/lido/tasks/merge_into_cle.py:172-188` | LIDO merge 5b, dedup before inserting a provision-level row | several: `provision_id` OR (`raw_resource = identifier` AND `raw_subdivision = article_label`) | n.a. (writer) | None; note its rows carry no raw columns, which is why rows 8, 14, 15 miss them. |
| 20 | case-law-explorer `79b67c6` | `airflow/dags/data_loading/clients/postgres.py:501-531` | `has_lido_resolution`: does a case have any `source_dataset = 'LIDO'` row | none (existence by provenance) | n.a. | None. |
| 21 | caselaw-coolify `cdf4c89` | `etl/promoter.py:487-494`, `:515-521` | promotion metrics: count of `case_law_reference` rows per batch / per LIDO provenance | none (row counts) | n.a. | None. |

Writers seen but not consumers: `airflow/dags/data_loading/clients/postgres.py:533-560`
(`upsert_law_reference`, raw `bwb` columns only), `airflow/dags/data_loading/cjeu_loader.py:204`
(raw `celex` columns only), caselaw-coolify `etl/apply_loader_compatibility.py:27`
(index predicate).

caselaw-ui `67e90be`: no hits for any pattern, no database client; it does not
query the database or call `/api/links/*` by those names.

## Findings

- No consumer in any of the four repos follows `provision_id` ->
  `legal_provision.legislation_id` to reach the act. The only uses of
  `legal_provision` are by `bwb_label_id` (view row 15, arm 2) and by
  `legislation_id + article_label` (arm 3).
- The legacy `rs_lido-both` rows (786,846 with `legislation_id` NULL) carry
  `raw_scheme = 'bwb'`, `raw_resource` and `raw_label_id`, per the load SQL. So
  the `raw_resource` based consumers still see them: the `bwbResources` filter
  (row 8), `rs_v_document_law_reference` (rows 14, 16), and arms 1-2 of
  `rs_v_document_legal_provisions` (rows 15, 17). What they lose is the act
  title in `rs_v_document_legal_provisions` (arms 3-6 match `raw_resource` to
  `legislation.identifier`, the same match that failed when `legislation_id`
  was filled).
- No consumer that filters or counts by `case_law_reference.legislation_id`
  touches BWB rows. The `legislation_id` joins are all CJEU / CELEX (rows 1-4),
  where no row has a `provision_id` today, so nothing is missed there now; they
  would miss provision-only CELEX rows if a loader starts writing them.
- Rows written without raw columns, which is what the LIDO merge writes (5a:
  `legislation_id` only, 5b: `provision_id` only), are missed by every
  BWB consumer in cle_v2: the `bwbResources` filter and both RS views. This
  matters once LIDO promotion is enabled.
- The BWB law endpoints, `/api/links/laws` (counts), `/api/links/cases`,
  `/api/links`, and the `selectedLaws` filters in `/api/rechtspraak`,
  combined search and the network endpoint (rows 5, 7, 11-13), do not read
  `case_law_reference` at all. They query the separate linkextractor database
  (`law_element`, `case_law`, `legal_case`), which the cle_v2 schema marks as
  not ported. The `case_law_reference_idx_raw_label` index comment ("/api/links/laws
  + /api/links/cases") describes a port that has not happened on `origin/main`.
  Whether they miss rows depends on that database, not on cle_v2.

## New rows from Phase 3 (R3.5, R3.7)

How the rows written by case-law-explorer `feature/eu-legislation` meet the
consumers above:

- `rs_lido_sqlite` (R3.5, new rows only): `raw_scheme = 'bwb'` and
  `raw_resource` as before, plus `legislation_id` when the BWB id names exactly
  one act. Every `raw_resource` consumer (rows 8, 14, 15) keeps seeing them; a
  consumer that switches to the act-resolution join sees them too.
- `echr_article` (R3.7): `legislation_id` (the ECHR or a Protocol, scheme
  `coe_treaty`) and `provision_id` both set, `raw_scheme = 'echr_article'`,
  `raw_resource` NULL. No consumer above reads them today (none selects
  `coe_treaty` acts or ECHR provisions); a consumer using the join above
  reaches the act either way, and `COALESCE(r.legislation_id, lp.legislation_id)`
  gives the same act for these rows.
- CELEX acts (R3.1) are now `legislation` rows with `scheme = 'celex'`. Rows 1
  and 4 (`euLaw` / `eu_law`) already join `legislation_id` -> `legislation`
  with `scheme = 'celex'`; they start matching as soon as a loader (R3.2) sets
  `legislation_id` on CJEU law references.

## Fixes for the citations-api worker (Phase 7)

The shared building block is one act-resolution join, used everywhere a
consumer needs the act:

```sql
LEFT JOIN legal_provision lp ON lp.id = r.provision_id
LEFT JOIN legislation l ON l.id = COALESCE(r.legislation_id, lp.legislation_id)
```

1. `api/rechtspraak/queryhelper.py:284-289` (`bwbResources`): match
   `raw_resource = ANY(...)` OR `l.identifier = ANY(...)` with `l.scheme = 'bwb'`
   through the join above, so rows without raw columns are found.
2. `api/rechtspraak/queryhelper.py:170-196`, `api/combined/sqlbuilders.py:197-202`,
   `api/network/queryhelper.py:147-175`: replace `get_cases_by_bwb_and_label_id`
   with a cle_v2 query that matches `r.raw_label_id = :label` OR
   `lp.bwb_label_id = :label`, and checks the act with `r.raw_resource = :bwb`
   OR `l.identifier = :bwb`.
3. `api/links/laws.py` / `linkextractor/utils.py:345-375`
   (`get_amount_cases_by_bwb_and_label_ids`): count `DISTINCT case_id` in
   `case_law_reference` with the same label/act match as fix 2.
4. `api/links/cases.py:30`, `api/links/index.py:31` /
   `linkextractor/utils.py:306-343`: return ECLIs from `case_law_reference`
   with the same match as fix 2, keeping the LIMIT and the `source` list
   (`source_dataset`).
5. `api/cjeu/queryhelper.py:233-241` and `api/combined/sqlbuilders.py:449-458`
   (`euLaw` / `eu_law` filter): add a UNION arm through `provision_id` ->
   `legal_provision.legislation_id`.
6. `api/cjeu/queryhelper.py:642-650` and `api/cjeu/options.py:72-79`: join
   `legislation` on `COALESCE(r.legislation_id, lp.legislation_id)` and accept
   resolved rows whose `raw_scheme` is NULL.
7. Views (caselaw-coolify `db/migrations`, new migration; mirror in
   case-law-explorer `db/schema.sql`): `rs_v_document_legal_provisions` gets an
   arm `provision_id` -> `legal_provision.title` and resolves the act title
   through the join above; `rs_v_document_law_reference` fills `bwb_resource`
   and `bwb_label_id` from `legislation` / `legal_provision` when the raw
   columns are NULL. These are schema changes outside citations-api but fix
   row 10 for it.

## Not checked / open

- The database was not queried. That `rs_lido-both` rows carry `raw_scheme`,
  `raw_resource` and `raw_label_id` is inferred from `20_rs_cases.sql` and the
  `merge_into_cle.py` comment. A count of `rs_lido-both` rows with
  `legislation_id IS NULL AND provision_id IS NOT NULL AND raw_resource IS NULL`
  would confirm it (expected 0).
- Why `legislation_id` is NULL for these rows: check whether a
  `legislation (scheme = 'bwb', identifier = raw_resource)` row exists now (the
  LIDO merge step 1 may have added it since), and whether
  `legislation.identifier` of `lp.legislation_id` equals `raw_resource`. If they
  differ, the `bwb_label_id` -> provision pick (latest snapshot,
  `20_rs_cases.sql:102-104`) may point at a provision in a different act, and
  following `provision_id` would attach the wrong act.
- The 100 rows with neither `legislation_id` nor `provision_id` are reachable
  only through `raw_resource`; not examined.
- The linkextractor database behind `LINKEXTRACTOR_DB_URL` was not inspected;
  how far its `law_element` / `case_law` coverage differs from cle_v2 is
  unknown.
- `merge_into_cle.py:156-161` (5a dedup) skips an act-level row when any
  LIDO-sourced row for the case has `raw_resource` equal to the act's BWB id,
  which includes provision-level legacy rows. Outside R3.6; noted only.
- citations-api branch `feature/cjeu-metadata-filters` (the checked-out working
  tree) and other remote branches were not read.
- Saved queries stored in the database (`0003_saved_queries.sql` table rows)
  and the coolify `api/`, `frontend/`, `sql-runner*/` code had no matches in
  git; stored SQL text in the database was not checked.
