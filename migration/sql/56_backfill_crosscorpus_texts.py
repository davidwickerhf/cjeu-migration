#!/usr/bin/env python3
"""One-off backfill: CELLAR Dutch fulltexts for the cross-corpus cases.

The original 50_load_cjeu.py built its ECLI map from cases.source='CJEU',
which silently skipped every fulltexts.parquet row belonging to the ~175
Dutch sector-8 decisions anchored to RS-sourced cases rows. For 55 of them
Rechtspraak provided no text either — this script loads those from the HF
parquet. Where an RS text exists it is kept (origin-wins policy, see
DATA_QUALITY.md — case_text): the guard `WHERE case_text.fulltext IS NULL`
makes the script idempotent and RS-preserving.

Applied to the caselaw staging DB on 2026-07-07 (55 rows). Fresh runs do
not need it — 50_load_cjeu.py now maps ECLIs via cjeu_document.

Deps beyond the loader env: duckdb (streams the 25 GB parquet remotely via
range requests instead of downloading it). Env: TARGET_DB_URL.
"""
import os
import duckdb
import psycopg2

HF_PARQUET = "hf://datasets/davidwickerhf/cjeu-opendata/fulltexts.parquet"

pg = psycopg2.connect(os.environ["TARGET_DB_URL"])
cur = pg.cursor()
cur.execute("SET search_path TO cle_v2, public")

cur.execute("""
    SELECT c.ecli, c.id FROM cases c
    JOIN cjeu_document d ON d.case_id = c.id
    JOIN rs_document r  ON r.case_id = c.id
    WHERE NOT EXISTS (SELECT 1 FROM case_text t
                      WHERE t.case_id = c.id AND t.language = 'nl'
                        AND t.fulltext IS NOT NULL)
""")
gap = dict(cur.fetchall())
print(f"{len(gap)} cross-corpus cases without Dutch fulltext")
if not gap:
    pg.close()
    raise SystemExit(0)

inlist = ",".join("'" + e + "'" for e in gap)
rows = duckdb.connect().execute(f"""
    SELECT upper(trim(ecli)), lower(text_language), text,
           text_source, text_format, missing_reasons
    FROM read_parquet('{HF_PARQUET}')
    WHERE upper(trim(ecli)) IN ({inlist})
      AND text IS NOT NULL AND length(text) > 0
""").fetchall()
print(f"{len(rows)} parquet texts fetched")

n = 0
for ecli, lang, text, src, fmt, miss in rows:
    cur.execute("""
        INSERT INTO case_text (case_id, language, fulltext, source,
                               text_format, missing_reasons)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (case_id, language) DO UPDATE
           SET fulltext = EXCLUDED.fulltext, source = EXCLUDED.source,
               text_format = EXCLUDED.text_format,
               missing_reasons = EXCLUDED.missing_reasons
         WHERE case_text.fulltext IS NULL
    """, (gap[ecli], lang or "nl", text, src, fmt, miss))
    n += cur.rowcount
pg.commit()
print(f"{n} case_text rows written")
pg.close()
