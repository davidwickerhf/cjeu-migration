# case_identifier backfill (R1.2a)

`migration/sql/65_backfill_case_identifier.py` fills `cle_v2.case_identifier`
for every existing case from data already in the database. The rows come from
case-law-explorer's `airflow/dags/data_loading/identifier_loader.py`, the code
the weekly loaders run behind `CASE_IDENTIFIER_ENABLED`, so history and new
loads share one code path.

| scheme | source | is_primary |
|---|---|---|
| `ecli` | `cases.ecli` | true |
| `celex` | `cases.celex_id` | true for CJEU cases |
| `cjeu_case_number` | `cjeu_document.case_number` | true for its first number |
| `cjeu_case_number` | `cellar_joins` (Cellar `joins` members) | false |
| `echr_appno` | `echr_document_appno`, `source = 'appno'` only | true when equal to `cases.case_number` |
| `nl_zaaknummer` | `rs_document.zaaknummer`, every variant | false |
| `ljn` | `rs_document.replaces_identifier` | false |

Not filled here: `cellar_dossier_uuid`, duplicate ECLIs (RVS:2012:BW3076 =
RVS:2012:4057), and joins members whose Cellar dossier has no document in the
database (they need Cellar's `cdm:dossier_identifier`, R2.5). The run counts
the latter as `unresolved` and lists examples.

## Where and how to run

On the ETL worker against **db-dev only**, with a direct Postgres connection.
It does not use the sql-runner: the loader reads several tables per range and
inserts with `execute_values`, which does not fit the runner's 1,000-row cap.
Production gets the rows through the promoter or a replay after verification.

Prerequisites:

1. Migration `0009` deployed on db-dev (`cle_v2.case_identifier` exists; the
   script refuses to write otherwise).
2. A case-law-explorer checkout that contains
   `airflow/dags/data_loading/identifier_loader.py`, and a Python with
   `psycopg2` (the Airflow environment's interpreter works).
3. The db-dev DSN in an environment variable (never on the command line).

```bash
export CLE_DEV_DSN=postgresql://...            # db-dev
# plan only (default): ranges and number of cases in each
python migration/sql/65_backfill_case_identifier.py \
    --dsn-env CLE_DEV_DSN --cle-checkout /path/to/case-law-explorer
# write
DRY_RUN=0 python migration/sql/65_backfill_case_identifier.py \
    --dsn-env CLE_DEV_DSN --cle-checkout /path/to/case-law-explorer \
    --python /path/to/airflow-venv/bin/python
```

Insert-only and idempotent: rerunning a range inserts nothing new. After a
failure the log names the range; rerun with `--from-id <that id>`.

A single range can also be run by hand from `airflow/dags` of the checkout:

```bash
python -m data_loading.identifier_loader --dsn-env CLE_DEV_DSN --from-id 1 --to-id 50000
```

## How to verify (db-dev, read-only)

### Counts per scheme and source

```sql
SELECT scheme, source, is_primary, count(*) AS rows, count(DISTINCT case_id) AS cases
  FROM cle_v2.case_identifier
 GROUP BY 1, 2, 3
 ORDER BY 1, 2, 3;
```

Compare with the inputs:

```sql
SELECT 'cases with ecli' AS k, count(*) FROM cle_v2.cases WHERE ecli IS NOT NULL
UNION ALL SELECT 'cases with celex', count(*) FROM cle_v2.cases WHERE celex_id IS NOT NULL
UNION ALL SELECT 'cjeu_document with case_number', count(*)
  FROM cle_v2.cjeu_document WHERE case_number IS NOT NULL AND case_number <> ''
UNION ALL SELECT 'joins rows', count(*) FROM cle_v2.case_citation WHERE relation_type = 'joins'
UNION ALL SELECT 'echr cases with an owner appno', count(DISTINCT d.case_id)
  FROM cle_v2.echr_document d
  JOIN cle_v2.echr_document_appno a ON a.item_id = d.item_id AND a.source = 'appno'
UNION ALL SELECT 'rs with zaaknummer', count(*) FROM cle_v2.rs_document
  WHERE zaaknummer IS NOT NULL AND zaaknummer <> ''
UNION ALL SELECT 'rs with replaces_identifier', count(*) FROM cle_v2.rs_document
  WHERE replaces_identifier IS NOT NULL AND replaces_identifier <> '';
```

Expected: `ecli` cases = cases with an ECLI minus the rejected ones in the
log; `celex` likewise; `cjeu_case_number` from `cjeu_document.case_number`
covers every `cjeu_document` row with a case number (one number each, as
`cjeu_mapping.case_number` derives it from the CELEX); `cellar_joins` rows
plus `unresolved` in the log account for the joins rows that name another
dossier (about half of Cellar's joins point at the case's own dossier and
add nothing new).

### Must be zero

```sql
-- an identifier of a case that is gone (FK cascade should prevent it)
SELECT count(*) FROM cle_v2.case_identifier ci
  LEFT JOIN cle_v2.cases k ON k.id = ci.case_id WHERE k.id IS NULL;
-- appnos that exist only as extractedappno (must never be loaded)
SELECT count(*) FROM cle_v2.case_identifier ci
  JOIN cle_v2.echr_document d ON d.case_id = ci.case_id
 WHERE ci.scheme = 'echr_appno'
   AND NOT EXISTS (SELECT 1 FROM cle_v2.echr_document d2
                     JOIN cle_v2.echr_document_appno a
                       ON a.item_id = d2.item_id AND a.source = 'appno'
                    WHERE d2.case_id = ci.case_id AND a.appno = ci.value);
-- a CJEU case without its primary CELEX row
SELECT count(*) FROM cle_v2.cases k
 WHERE k.sources @> '{CJEU}' AND k.celex_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM cle_v2.case_identifier ci
                    WHERE ci.case_id = k.id AND ci.scheme = 'celex' AND ci.is_primary);
```

### Spot checks (gold set and research files)

```sql
SELECT k.ecli, ci.scheme, ci.value, ci.value_normalized, ci.source, ci.is_primary
  FROM cle_v2.case_identifier ci JOIN cle_v2.cases k ON k.id = ci.case_id
 WHERE k.ecli IN ('ECLI:EU:C:2013:720',            -- X, Y and Z: C-199/12 own, C-200/12 and C-201/12 if joins resolve
                  'ECLI:EU:C:2019:189',            -- Tjebbes: C-221/17
                  'ECLI:NL:RVS:2012:BW3076',       -- 201012342/1/T1/V2 and 201012342/T1/V2; ljn BW3076 if replaces_identifier is set
                  'ECLI:NL:RVS:2017:1098',         -- four zaaknummers, each with and without /1/
                  'ECLI:CE:ECHR:2004:0622DEC001734103') -- F. v. UK: 17341/03
 ORDER BY 1, 2, 4;

-- lookups the extractors will make
SELECT scheme, value_normalized, count(*) AS cases
  FROM cle_v2.case_identifier
 WHERE (scheme, value_normalized) IN (('cjeu_case_number', 'C-200/12'),
                                      ('cjeu_case_number', 'C-4/19'),
                                      ('nl_zaaknummer', '201504577/A3'),
                                      ('echr_appno', '17341/03'),
                                      ('ljn', 'BW3076'))
 GROUP BY 1, 2;

-- 20 random rows per scheme, to read by eye
SELECT * FROM (
  SELECT ci.*, row_number() OVER (PARTITION BY scheme ORDER BY random()) AS n
    FROM cle_v2.case_identifier ci) s
 WHERE n <= 20 ORDER BY scheme, n;
```

Record the results below each run (counts per scheme and source, spot checks,
anything unexpected) before promoting.

## Run log

Each run appends its ranges (counts per scheme), totals and the first rejected
and unresolved examples.

<!-- run log: 65_backfill_case_identifier.py appends below -->
