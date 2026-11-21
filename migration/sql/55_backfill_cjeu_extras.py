#!/usr/bin/env python3
"""Backfill the CJEU extra fields onto an ALREADY-LOADED cle_v2 database.

Covers the five fields the coverage proof caught (data-inspected 2026-07-07):
  citations_extra_info, national_judgement -> raw columns on cjeu_document
  case_law_national_based_on_resource_legal -> cjeu_national_document
  case_law_is_about_case_law_subject_matter -> domain scheme 'cjeu_is_about_subject'
  origin_country_or_role_qualifier -> union into referring_state party fanout

Idempotent; records itself as '55_backfill_cjeu_extras'.
Env: TARGET_DB_URL, CJEU_CASES_PARQUET (else HF download).
"""
import os, sys
import pandas as pd
import psycopg


def toks(v):
    try:
        if v is None or pd.isna(v): return []
    except (TypeError, ValueError): pass
    return [t.strip() for t in str(v).split(";") if t.strip()]


def main():
    url = os.environ["TARGET_DB_URL"]
    p = os.environ.get("CJEU_CASES_PARQUET")
    if not p:
        from huggingface_hub import hf_hub_download
        p = hf_hub_download("davidwickerhf/cjeu-opendata", "cases.parquet", repo_type="dataset")
    df = pd.read_parquet(p, columns=["ecli","citations_extra_info","national_judgement",
        "case_law_national_based_on_resource_legal",
        "case_law_is_about_case_law_subject_matter","origin_country_or_role_qualifier"])
    df["ecli"] = df["ecli"].str.strip().str.upper()
    df = df.drop_duplicates(subset=["ecli"])

    with psycopg.connect(url) as conn:
        cur = conn.cursor()
        cur.execute("SET search_path TO cle_v2, public")
        cur.execute("SET temp_buffers = '256MB'")
        cur.execute("ALTER TABLE cjeu_document ADD COLUMN IF NOT EXISTS citations_extra_info text")
        cur.execute("ALTER TABLE cjeu_document ADD COLUMN IF NOT EXISTS national_judgement_xml text")
        cur.execute("ALTER TABLE cjeu_national_document ADD COLUMN IF NOT EXISTS national_based_on_resource_legal text")

        cur.execute("""CREATE TEMP TABLE stg (ecli text, cei text, njx text, nbor text)""")
        cur.execute("""CREATE TEMP TABLE stg_kv (ecli text, k text, v text)""")
        rows, kv = [], []
        for r in df.itertuples(index=False):
            cei = str(r.citations_extra_info) if not pd.isna(r.citations_extra_info) else None
            njx = str(r.national_judgement) if not pd.isna(r.national_judgement) else None
            nbor = str(r.case_law_national_based_on_resource_legal) \
                if not pd.isna(r.case_law_national_based_on_resource_legal) else None
            if cei or njx or nbor:
                rows.append((r.ecli, cei, njx, nbor))
            for t in toks(r.case_law_is_about_case_law_subject_matter):
                kv.append((r.ecli, "dom", t))
            for t in toks(r.origin_country_or_role_qualifier):
                kv.append((r.ecli, "party", t))
        with cur.copy("COPY stg (ecli, cei, njx, nbor) FROM STDIN") as cp:
            for x in rows: cp.write_row(x)
        with cur.copy("COPY stg_kv (ecli, k, v) FROM STDIN") as cp:
            for x in kv: cp.write_row(x)
        print(f"staged: {len(rows):,} raw rows, {len(kv):,} kv", flush=True)

        cur.execute("""UPDATE cjeu_document d SET citations_extra_info = s.cei,
                       national_judgement_xml = s.njx
                       FROM stg s JOIN cases c ON c.ecli = s.ecli
                       WHERE d.case_id = c.id AND (s.cei IS NOT NULL OR s.njx IS NOT NULL)""")
        print(f"cjeu_document updated: {cur.rowcount:,}", flush=True)
        cur.execute("""UPDATE cjeu_national_document n SET national_based_on_resource_legal = s.nbor
                       FROM stg s JOIN cases c ON c.ecli = s.ecli
                       WHERE n.case_id = c.id AND s.nbor IS NOT NULL""")
        print(f"cjeu_national_document updated: {cur.rowcount:,}", flush=True)

        cur.execute("""INSERT INTO domain (scheme, name)
                       SELECT DISTINCT 'cjeu_is_about_subject', v FROM stg_kv WHERE k='dom'
                         AND NOT EXISTS (SELECT 1 FROM domain d WHERE d.scheme='cjeu_is_about_subject' AND d.name=stg_kv.v)""")
        cur.execute("""INSERT INTO case_domain (case_id, domain_id)
                       SELECT DISTINCT c.id, d.id FROM stg_kv s
                       JOIN cases c ON c.ecli = s.ecli
                       JOIN domain d ON d.scheme='cjeu_is_about_subject' AND d.name = s.v
                       WHERE s.k='dom' ON CONFLICT (case_id, domain_id) DO NOTHING""")
        print(f"is_about domain links: {cur.rowcount:,}", flush=True)

        cur.execute("""INSERT INTO party (canonical_name, role_class)
                       SELECT DISTINCT v, 'state' FROM stg_kv WHERE k='party'
                         AND NOT EXISTS (SELECT 1 FROM party p WHERE p.canonical_name=stg_kv.v AND p.role_class='state')""")
        cur.execute("""INSERT INTO case_party (case_id, party_id, role)
                       SELECT DISTINCT c.id, p.id, 'referring_state' FROM stg_kv s
                       JOIN cases c ON c.ecli = s.ecli
                       JOIN party p ON p.canonical_name = s.v AND p.role_class='state'
                       WHERE s.k='party' ON CONFLICT (case_id, party_id, role, ordinal) DO NOTHING""")
        print(f"qualifier party links: {cur.rowcount:,}", flush=True)

        cur.execute("""INSERT INTO migration_manifest (step) VALUES ('55_backfill_cjeu_extras')
                       ON CONFLICT (step) DO UPDATE SET completed_at = now()""")
        conn.commit()
    print("backfill complete", flush=True)


if __name__ == "__main__":
    sys.exit(main())
