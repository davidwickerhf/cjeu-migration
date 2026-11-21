#!/usr/bin/env python3
"""Backfill: CELLAR Dutch fulltexts for the cross-corpus cases (D12).

The original 50_load_cjeu.py built its ECLI map from cases.source='CJEU',
which silently skipped every fulltexts.parquet row belonging to the ~175
Dutch sector-8 decisions anchored to RS-sourced cases rows. Under the D12
dual-source model every CELLAR rendition is stored as its own case_text row
(unique on case_id × language × source); where Rechtspraak also provides
the text, both rows coexist and case_text_canonical prefers the origin.

Idempotent: rows already present (same case/language/source triple) are
left alone, except that a summary-only row from the loader's summary pass
gets its fulltext filled in.

Applied to the caselaw staging DB on 2026-07-07 (55 gap texts, then the
119 remaining renditions after D12). Fresh runs do not need it —
50_load_cjeu.py now maps ECLIs via cjeu_document and keys its resume set
on the full triple.

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
""")
xover = dict(cur.fetchall())
print(f"{len(xover)} cross-corpus cases")

inlist = ",".join("'" + e + "'" for e in xover)
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
        ON CONFLICT (case_id, language, source) DO UPDATE
           SET fulltext = EXCLUDED.fulltext,
               text_format = EXCLUDED.text_format,
               missing_reasons = EXCLUDED.missing_reasons
         WHERE case_text.fulltext IS NULL
    """, (xover[ecli], lang or "nl", text, src or "UNKNOWN", fmt, miss))
    n += cur.rowcount
pg.commit()
print(f"{n} case_text rows written")
pg.close()
