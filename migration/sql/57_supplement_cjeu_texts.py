#!/usr/bin/env python3
"""Supplement cle_v2.case_text with CJEU fulltext rows added to the HF
corpus after the bulk load (multilang topups).

Diffs fulltexts.parquet against the loaded (case_id, language, source)
triples and inserts only what is missing — append-only and idempotent, so
it can run after every topup. Mirrors the 50_load_cjeu.py text rules:
ECLIs map via cjeu_document (cross-corpus cases included, D13), language
rows without a text_language are skipped, unseen languages are upserted
into the language lookup.

Env:
  TARGET_DB_URL       required
  FULLTEXTS_PARQUET   local path; downloads from HF when unset
"""
import io
import csv
import os

import pyarrow.parquet as pq
import psycopg2

COLS = ("ecli", "text", "text_source", "text_language", "text_format",
        "missing_reasons")


def main() -> int:
    url = os.environ["TARGET_DB_URL"]
    path = os.environ.get("FULLTEXTS_PARQUET")
    if not path:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download("davidwickerhf/cjeu-opendata",
                               "fulltexts.parquet", repo_type="dataset")

    pg = psycopg2.connect(url)
    cur = pg.cursor()
    cur.execute("SET search_path TO cle_v2, public")

    cur.execute("""SELECT c.ecli, c.id FROM cases c
                   JOIN cjeu_document d ON d.case_id = c.id""")
    ecli_to_id = dict(cur.fetchall())
    cur.execute("""SELECT ct.case_id, ct.language, ct.source FROM case_text ct
                   JOIN cjeu_document d ON d.case_id = ct.case_id""")
    seen = set(cur.fetchall())
    cur.execute("SELECT iso_code FROM language")
    known_langs = {r[0] for r in cur.fetchall()}
    print(f"{len(ecli_to_id)} CJEU-corpus ECLIs, {len(seen)} text rows already loaded")

    inserted = 0
    scanned = 0
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=2000, columns=list(COLS)):
        cols = {c: batch.column(c).to_pylist() for c in COLS}
        out = []
        for e, t, s2, l, f2, m2 in zip(cols["ecli"], cols["text"],
                                       cols["text_source"], cols["text_language"],
                                       cols["text_format"], cols["missing_reasons"]):
            scanned += 1
            cid = ecli_to_id.get(str(e).strip().upper()) if e else None
            lang = (l or "").lower()
            src = s2 or "UNKNOWN"
            if not cid or not lang or (cid, lang, src) in seen:
                continue
            seen.add((cid, lang, src))
            if lang not in known_langs:
                cur.execute("INSERT INTO language (iso_code, name) VALUES (%s,%s) "
                            "ON CONFLICT DO NOTHING", (lang, lang))
                known_langs.add(lang)
            out.append((cid, lang, t, src, f2, m2))
        if out:
            cur.execute("""CREATE TEMP TABLE IF NOT EXISTS stg_text (
                case_id bigint, language text, fulltext text,
                source text, text_format text, missing_reasons text)""")
            cur.execute("TRUNCATE stg_text")
            buf = io.StringIO()
            w = csv.writer(buf)
            for r in out:
                w.writerow(["" if v is None else v for v in r])
            buf.seek(0)
            cur.copy_expert(
                "COPY stg_text FROM STDIN WITH (FORMAT csv)", buf)
            cur.execute("""
                INSERT INTO case_text (case_id, language, fulltext, source,
                                       text_format, missing_reasons)
                SELECT case_id, language, nullif(fulltext, ''), source,
                       nullif(text_format, ''), nullif(missing_reasons, '')
                FROM stg_text
                ON CONFLICT (case_id, language, source) DO NOTHING""")
            inserted += cur.rowcount
            pg.commit()
            print(f"  scanned {scanned:,} — inserted {inserted:,}", flush=True)

    pg.commit()
    print(f"done: scanned {scanned:,} parquet rows, inserted {inserted:,} new case_text rows")
    pg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
