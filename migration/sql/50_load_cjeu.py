#!/usr/bin/env python3
"""CJEU loader: HF parquet corpus -> cle_v2.

Loads cases, cjeu_document, cjeu_ag_opinion, cjeu_national_document,
case_text, domain/case_domain, judge/case_judge, party/case_party,
case_citation, case_law_reference — per MIGRATION_MAPPING.md §3.

Pattern: COPY into TEMP staging tables, then set-based INSERT..SELECT with
lookup joins (same idempotency style as the SQL steps). fulltexts.parquet is
streamed via pyarrow so the 6 GB text column never sits in RAM.

Env:
  TARGET_DB_URL                              required
  CJEU_CASES_PARQUET / CJEU_FULLTEXTS_PARQUET  local paths (else HF download)
  CJEU_FILTER_YEAR_GTE                       optional sample filter (decision year)
"""
from __future__ import annotations

import os
import re
import sys

import pandas as pd
import pyarrow.parquet as pq
import psycopg

CDM_ROLE_COLUMNS = {
    "legal_resource": "legal_basis",
    "based_on_treaty": "based_on_treaty",
    "affecting_string": "affects",
    "case_law_amends_resource_legal": "amends",
    "case_law_amends_by_correction_resource_legal": "amends_by_correction",
    "case_law_confirms_resource_legal": "confirms",
    "case_law_declares_void_resource_legal": "declares_void",
    "case_law_declares_void_by_preliminary_ruling_resource_legal": "declares_void_by_preliminary_ruling",
    "case_law_incidentally_declares_void_resource_legal": "incidentally_declares_void",
    "case_law_declares_valid_resource_legal": "declares_valid",
    "case_law_declares_incidentally_valid_resource_legal": "declares_incidentally_valid",
    "case_law_states_failure_concerning_resource_legal": "states_failure",
    "case_law_suspends_application_of_resource_legal": "suspends_application",
    "case_law_immediately_enforces_resource_legal": "immediately_enforces",
    "resource_legal_incorporates_resource_legal": "incorporates",
    "resource_legal_corrects_resource_legal": "corrects",
}

CITE_COLUMNS = {
    "citing": "cites",
    "work_cites_work": "cites",
    "cited_by": "cited_by",
    "case_law_joins_case_court": "joins",
    "case_law_subject_to_appeal_in_case_court": "subject_to_appeal",
    "case_law_reexamined_by_case_court": "reexamined_by",
    "case_law_referred_to_for_preliminary_ruling_case_law": "referred_for_preliminary_ruling",
    "case_law_is_about_concept_case_law": "is_about_concept",
    "case_law_is_about_concept_new_case_law": "is_about_concept",
    "work_is_logical_successor_of_work": "logical_successor_of",
}

DOMAIN_COLUMNS = {
    "subject_matter": "cjeu_subject_matter",
    "eurovoc": "eurovoc",
    "keywords": "cjeu_keyword",
    "directory_codes": "cjeu_directory_code",
}

NATIONAL_COLUMNS = [
    ("case_law_delivered_by_court_national", "national_court_uri"),
    ("case_law_national_decision_internal_identifier", "national_decision_internal_id"),
    ("case_law_national_parties", "national_parties_raw"),
    ("case_law_national_keywords", "national_keywords"),
    ("case_law_national_reference_publication", "national_reference_publication"),
    ("case_law_national_reference_publication_conclusion", "national_reference_publication_conclusion"),
    ("case_law_national_follow_up", "national_follow_up"),
    ("case_law_national_judgement_reference", "national_judgement_reference"),
    ("case_law_national_act_reference_national", "national_act_reference_national"),
    ("case_law_national_act_reference_international", "national_act_reference_international"),
    ("case_law_national_act_reference_european", "national_act_reference_european"),
    ("case_law_national_based_on_resource_legal", "national_based_on_resource_legal"),
]


def toks(v):
    if v is None:
        return []
    try:
        if pd.isna(v):
            return []
    except (TypeError, ValueError):
        pass
    return [t.strip() for t in str(v).split(";") if t.strip()]


def first(v):
    t = toks(v)
    return t[0] if t else None


def celex_kind(celex):
    m = re.match(r"^[68]\d{4}([A-Z])([A-Z])", celex or "")
    if not m:
        return None, "other", False
    court = {"C": "CJEU", "T": "EGC", "F": "CST"}.get(m.group(1))
    dt = {"J": "judgment", "O": "order", "C": "opinion", "V": "ruling",
          "D": "decision"}.get(m.group(2), "other")
    return court, dt, m.group(2) == "C"


def case_number(celex):
    m = re.match(r"^[68](\d{4})([A-Z])[A-Z](\d{4})", celex or "")
    if not m:
        return None
    return f"{ {'C':'C','T':'T','F':'F'}.get(m.group(2), 'C') }-{int(m.group(3))}/{m.group(1)[2:]}"


FORMATION_WORDS = [
    ("GC", ("grand", "grande")), ("FC", ("full court", "plén", "assembl")),
    ("1C", ("first", "première")), ("2C", ("second", "deuxième")),
    ("3C", ("third", "troisième")), ("4C", ("fourth", "quatrième")),
    ("5C", ("fifth", "cinquième")), ("6C", ("sixth", "sixième")),
    ("7C", ("seventh", "septième")), ("8C", ("eighth", "huitième")),
    ("9C", ("ninth", "neuvième")), ("10C", ("tenth", "dixième")),
]


def formation_code(raw):
    f = (raw or "").lower()
    for code, words in FORMATION_WORDS:
        if any(w in f for w in words):
            return code
    return None


def importance(formation_raw):
    code = formation_code(formation_raw)
    if code in ("GC", "FC"):
        return 1
    if code in ("1C", "2C", "3C", "4C", "5C"):
        return 2
    if code:
        return 3
    return 4 if formation_raw else None


def copy_rows(cur, table, cols, rows):
    with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)


def main() -> int:
    url = os.environ["TARGET_DB_URL"]
    cases_p = os.environ.get("CJEU_CASES_PARQUET")
    ft_p = os.environ.get("CJEU_FULLTEXTS_PARQUET")
    year_gte = int(os.environ.get("CJEU_FILTER_YEAR_GTE", "0"))

    if not cases_p or not ft_p:
        from huggingface_hub import hf_hub_download
        repo = "davidwickerhf/cjeu-opendata"
        cases_p = cases_p or hf_hub_download(repo, "cases.parquet", repo_type="dataset")
        ft_p = ft_p or hf_hub_download(repo, "fulltexts.parquet", repo_type="dataset")

    df = pd.read_parquet(cases_p)
    df["x_celex"] = df["celex"].map(first)
    df["x_date"] = pd.to_datetime(
        df["date_publication"].map(lambda v: min(toks(v)) if toks(v) else None),
        errors="coerce", utc=True).dt.date
    df = df[df["ecli"].notna() & df["x_celex"].notna()]
    if year_gte:
        df = df[df["x_date"].map(lambda d: d is not None and d.year >= year_gte)]
    df = df.drop_duplicates(subset=["ecli"])
    df["x_ecli"] = df["ecli"].str.strip().str.upper()
    print(f"cjeu cases to load: {len(df):,}", flush=True)

    with psycopg.connect(url, autocommit=False) as conn:
        cur = conn.cursor()
        cur.execute("SET search_path TO cle_v2, public")
        cur.execute("SET temp_buffers = '512MB'")   # staging temp tables hold millions of rows; default 8MB starves

        # ---------- staging ----------
        cur.execute("""
        CREATE TEMP TABLE stg_case (ecli text, celex text, title text, date_decision date,
            court_code text, lang text, doctype text, proc text, case_number text,
            importance smallint, sector text, formation_code text, formation_raw text,
            proc_type_raw text, date_lodged date, journal_refs text, erecueil_ref text,
            local_identifier text, dossier_uri text,
            citations_extra_info text, national_judgement_xml text) ON COMMIT DROP;
        CREATE TEMP TABLE stg_kv (ecli text, k text, v text) ON COMMIT DROP;      -- domains/judges/parties/cites/lawrefs
        CREATE TEMP TABLE stg_ag (ecli text, ag text, opinion_uri text, parent_raw text) ON COMMIT DROP;
        CREATE TEMP TABLE stg_nat (ecli text, col text, val text) ON COMMIT DROP;
        """)

        case_rows, kv_rows, ag_rows, nat_rows = [], [], [], []
        for r in df.itertuples(index=False):
            d = r._asdict() if hasattr(r, "_asdict") else dict(zip(df.columns, r))
            ecli, celex = d["x_ecli"], d["x_celex"]
            court, dtcode, is_opinion = celex_kind(celex)
            form_raw = first(d.get("delivered_by_court_formation"))
            title = first(d.get("work_title"))
            cn = case_number(celex)
            case_rows.append((
                ecli, celex, title or (f"Case {cn}" if cn else None), d["x_date"],
                court or "CJEU", (first(d.get("language_procedure")) or "").lower() or None,
                dtcode, first(d.get("judicial_procedure_type")), cn,
                importance(form_raw), first(d.get("sector")), formation_code(form_raw),
                form_raw, first(d.get("type_procedure")),
                pd.to_datetime(first(d.get("date_of_request")), errors="coerce").date()
                    if first(d.get("date_of_request")) else None,
                first(d.get("references_journals")),
                first(d.get("case_law_published_in_erecueil")),
                first(d.get("local_identifier")), first(d.get("work_part_of_dossier")),
                d.get("citations_extra_info") if not pd.isna(d.get("citations_extra_info")) else None,
                d.get("national_judgement") if not pd.isna(d.get("national_judgement")) else None,
            ))
            for t in toks(d.get("case_law_is_about_case_law_subject_matter")):
                kv_rows.append((ecli, "dom:cjeu_is_about_subject", t))
            for t in toks(d.get("origin_country_or_role_qualifier")):
                kv_rows.append((ecli, "party:referring_state", t))   # union with origin_country; dedup downstream
            for col, scheme in DOMAIN_COLUMNS.items():
                for t in toks(d.get(col)):
                    kv_rows.append((ecli, f"dom:{scheme}", t))
            if (jr := first(d.get("judge_rapporteur"))):
                kv_rows.append((ecli, "judge:rapporteur", jr))
            for j in toks(d.get("case_law_delivered_by_judge")):
                kv_rows.append((ecli, "judge:judge", j))
            for col, role in (("case_law_defended_by_agent", "defendant_agent"),
                              ("case_law_requested_by_agent", "applicant_agent"),
                              ("commented_by_agent", "commenting_agent")):
                for p in toks(d.get(col)):
                    kv_rows.append((ecli, f"party:{role}", p))
            if (oc := first(d.get("origin_country"))):
                kv_rows.append((ecli, "party:referring_state", oc))
            for col, rel in CITE_COLUMNS.items():
                for t in toks(d.get(col)):
                    kv_rows.append((ecli, f"cite:{rel}:{col}", t))
            for col, role in CDM_ROLE_COLUMNS.items():
                for t in toks(d.get(col)):
                    kv_rows.append((ecli, f"lawref:{role}", t))
            if is_opinion:
                ag_rows.append((ecli, first(d.get("advocate_general")),
                                first(d.get("conclusions")),
                                first(d.get("opinion_advocate_general_joined_to_case_court"))))
            if first(d.get("sector")) == "8":
                for col, target in NATIONAL_COLUMNS:
                    if (v := first(d.get(col))):
                        nat_rows.append((ecli, target, v))

        copy_rows(cur, "stg_case", ("ecli","celex","title","date_decision","court_code","lang",
                  "doctype","proc","case_number","importance","sector","formation_code",
                  "formation_raw","proc_type_raw","date_lodged","journal_refs","erecueil_ref",
                  "local_identifier","dossier_uri","citations_extra_info","national_judgement_xml"),
                  case_rows)
        copy_rows(cur, "stg_kv", ("ecli","k","v"), kv_rows)
        copy_rows(cur, "stg_ag", ("ecli","ag","opinion_uri","parent_raw"), ag_rows)
        copy_rows(cur, "stg_nat", ("ecli","col","val"), nat_rows)
        print(f"staged: kv={len(kv_rows):,} ag={len(ag_rows):,} nat={len(nat_rows):,}", flush=True)

        # ---------- lookups ----------
        cur.execute("""
        INSERT INTO language (iso_code, name)
        SELECT DISTINCT lang, lang FROM stg_case WHERE lang IS NOT NULL
        ON CONFLICT (iso_code) DO NOTHING;
        INSERT INTO procedure_type (code, name)
        SELECT DISTINCT proc, proc FROM stg_case WHERE proc IS NOT NULL
        ON CONFLICT (code) DO NOTHING;
        """)

        # ---------- cases ----------
        cur.execute("""
        INSERT INTO cases (ecli, celex_id, source, title, date_decision, court_id,
                           language_iso, document_type_id, procedure_type_id,
                           case_number, importance)
        SELECT s.ecli, s.celex, 'CJEU', s.title, s.date_decision, c.id,
               s.lang, dt.id, pt.id, s.case_number, s.importance
        FROM stg_case s
        LEFT JOIN court c ON c.code = s.court_code
        LEFT JOIN document_type dt ON dt.code = s.doctype
        LEFT JOIN procedure_type pt ON pt.code = s.proc
        ON CONFLICT (ecli) DO NOTHING
        """)

        # ---------- cjeu_document ----------
        cur.execute("""
        INSERT INTO cjeu_document (case_id, celex_id, ecli, sector, case_number,
            formation_id, proc_type, date_lodged, journal_refs, erecueil_ref,
            local_identifier, dossier_uri, citations_extra_info, national_judgement_xml)
        SELECT k.id, s.celex, s.ecli, s.sector, s.case_number,
               f.id, s.proc_type_raw, s.date_lodged, s.journal_refs, s.erecueil_ref,
               s.local_identifier, s.dossier_uri, s.citations_extra_info, s.national_judgement_xml
        FROM stg_case s
        JOIN cases k ON k.ecli = s.ecli
        LEFT JOIN court_formation f ON f.code = s.formation_code
        ON CONFLICT (case_id) DO NOTHING
        """)

        # ---------- ag opinions (parent resolved by case number, best effort) ----------
        cur.execute("""
        INSERT INTO cjeu_ag_opinion (case_id, parent_case_id, advocate_general, opinion_uri, delivered_date)
        SELECT k.id, p.id, a.ag, a.opinion_uri, k.date_decision
        FROM stg_ag a
        JOIN cases k ON k.ecli = a.ecli
        LEFT JOIN LATERAL (
            SELECT c2.id FROM cases c2
            WHERE c2.source = 'CJEU'
              AND c2.case_number = replace((regexp_match(coalesce(a.parent_raw,''), '(?:case/)([CTF]-[0-9]+%2F[0-9]+|[CTF]-[0-9]+/[0-9]+)'))[1], '%2F', '/')
            LIMIT 1
        ) p ON true
        ON CONFLICT (case_id) DO NOTHING
        """)

        # ---------- national docs (pivot) ----------
        cur.execute("""
        INSERT INTO cjeu_national_document (case_id, national_court_uri,
            national_decision_internal_id, national_parties_raw, national_keywords,
            national_reference_publication, national_reference_publication_conclusion,
            national_follow_up, national_judgement_reference,
            national_act_reference_national, national_act_reference_international,
            national_act_reference_european, national_based_on_resource_legal)
        SELECT k.id,
            max(val) FILTER (WHERE col='national_court_uri'),
            max(val) FILTER (WHERE col='national_decision_internal_id'),
            max(val) FILTER (WHERE col='national_parties_raw'),
            max(val) FILTER (WHERE col='national_keywords'),
            max(val) FILTER (WHERE col='national_reference_publication'),
            max(val) FILTER (WHERE col='national_reference_publication_conclusion'),
            max(val) FILTER (WHERE col='national_follow_up'),
            max(val) FILTER (WHERE col='national_judgement_reference'),
            max(val) FILTER (WHERE col='national_act_reference_national'),
            max(val) FILTER (WHERE col='national_act_reference_international'),
            max(val) FILTER (WHERE col='national_act_reference_european'),
            max(val) FILTER (WHERE col='national_based_on_resource_legal')
        FROM stg_nat n JOIN cases k ON k.ecli = n.ecli
        GROUP BY k.id
        ON CONFLICT (case_id) DO NOTHING
        """)

        # ---------- domains ----------
        cur.execute("""
        INSERT INTO domain (scheme, name)
        SELECT DISTINCT substring(k from 5), v FROM stg_kv WHERE k LIKE 'dom:%'
          AND NOT EXISTS (SELECT 1 FROM domain d WHERE d.scheme = substring(stg_kv.k from 5) AND d.name = stg_kv.v);
        INSERT INTO case_domain (case_id, domain_id)
        SELECT DISTINCT c.id, d.id
        FROM stg_kv s JOIN cases c ON c.ecli = s.ecli
        JOIN domain d ON d.scheme = substring(s.k from 5) AND d.name = s.v
        WHERE s.k LIKE 'dom:%'
        ON CONFLICT (case_id, domain_id) DO NOTHING;
        """)

        # ---------- judges ----------
        cur.execute("""
        INSERT INTO judge (full_name)
        SELECT DISTINCT v FROM stg_kv WHERE k LIKE 'judge:%'
          AND NOT EXISTS (SELECT 1 FROM judge j WHERE j.full_name = stg_kv.v);
        INSERT INTO case_judge (case_id, judge_id, role)
        SELECT DISTINCT c.id, j.id, substring(s.k from 7)
        FROM stg_kv s JOIN cases c ON c.ecli = s.ecli JOIN judge j ON j.full_name = s.v
        WHERE s.k LIKE 'judge:%'
        ON CONFLICT (case_id, judge_id, role) DO NOTHING;
        """)

        # ---------- parties ----------
        cur.execute("""
        INSERT INTO party (canonical_name, role_class)
        SELECT DISTINCT v, 'agent' FROM stg_kv WHERE k LIKE 'party:%'
          AND NOT EXISTS (SELECT 1 FROM party p WHERE p.canonical_name = stg_kv.v);
        INSERT INTO case_party (case_id, party_id, role)
        SELECT DISTINCT c.id, p.id, substring(s.k from 7)
        FROM stg_kv s JOIN cases c ON c.ecli = s.ecli JOIN party p ON p.canonical_name = s.v
        WHERE s.k LIKE 'party:%'
        ON CONFLICT (case_id, party_id, role, ordinal) DO NOTHING;
        """)

        # ---------- citations (target resolved via celex; raw always kept) ----------
        cur.execute("""
        INSERT INTO case_citation (source_case_id, target_case_id, target_celex_raw,
            relation_type, source_dataset, is_cross_jurisdiction)
        SELECT DISTINCT c.id, t.id, s.v, split_part(s.k, ':', 2), 'cellar_sparql', false
        FROM stg_kv s
        JOIN cases c ON c.ecli = s.ecli
        LEFT JOIN cases t ON t.celex_id = s.v AND t.source = 'CJEU'
        WHERE s.k LIKE 'cite:%'
        ON CONFLICT DO NOTHING
        """)

        # ---------- law references (raw celex; EU catalog resolution is a later pass) ----------
        cur.execute("""
        INSERT INTO case_law_reference (case_id, raw_scheme, raw_resource, role, source_dataset)
        SELECT DISTINCT c.id, 'celex', s.v, substring(s.k from 8), 'cellar_sparql'
        FROM stg_kv s JOIN cases c ON c.ecli = s.ecli
        WHERE s.k LIKE 'lawref:%'
        ON CONFLICT DO NOTHING
        """)

        conn.commit()
        print("metadata committed", flush=True)

        # ---------- fulltexts: stream + filter in python, COPY straight in ----------
        # (no temp staging: the raw text is ~25 GB; we filter to loaded ECLIs
        #  and write case_text directly — (ecli, lang) is unique in the corpus)
        cur.execute("SET search_path TO cle_v2, public")
        cur.execute("SELECT ecli, id FROM cases WHERE source = 'CJEU'")
        ecli_to_id = dict(cur.fetchall())
        cur.execute("SELECT iso_code FROM language")
        known_langs = {r[0] for r in cur.fetchall()}
        cur.execute("""SELECT ct.case_id, ct.language FROM case_text ct
                       JOIN cases c ON c.id = ct.case_id WHERE c.source='CJEU'""")
        seen_pairs = set(cur.fetchall())   # resume-safe: skip already-loaded texts
        pf = pq.ParquetFile(ft_p)
        n = 0
        for batch in pf.iter_batches(batch_size=2000,
                columns=["ecli", "text", "text_source", "text_language", "text_format", "missing_reasons"]):
            rows = zip(batch.column("ecli").to_pylist(), batch.column("text_language").to_pylist(),
                       batch.column("text").to_pylist(), batch.column("text_source").to_pylist(),
                       batch.column("text_format").to_pylist(), batch.column("missing_reasons").to_pylist())
            out = []
            for e, l, t, s2, f2, m2 in rows:
                cid = ecli_to_id.get(str(e).strip().upper()) if e else None
                lang = (l or "").lower()
                if not cid or not lang or (cid, lang) in seen_pairs:
                    continue
                seen_pairs.add((cid, lang))
                if lang not in known_langs:
                    cur.execute("INSERT INTO language (iso_code, name) VALUES (%s,%s) ON CONFLICT DO NOTHING", (lang, lang))
                    known_langs.add(lang)
                out.append((cid, lang, t, s2, f2, m2))
            if out:
                copy_rows(cur, "case_text",
                          ("case_id","language","fulltext","source","text_format","missing_reasons"), out)
                n += len(out)
                if n % 50_000 < len(out):
                    print(f"  text rows loaded: {n:,}", flush=True)
                    conn.commit()
        conn.commit()
        print(f"fulltexts loaded ({n:,} rows)", flush=True)

        # summaries onto the procedure-language row
        cur.execute("SET search_path TO cle_v2, public")
        cur.execute("""CREATE TEMP TABLE stg_sum (ecli text, lang text, summary text, ssrc text)""")
        sums = [(r.x_ecli, (first(getattr(r, "language_procedure", None)) or "").lower() or "en",
                 first(getattr(r, "summary", None)), first(getattr(r, "summary_source", None)))
                for r in df.itertuples(index=False) if first(getattr(r, "summary", None))]
        copy_rows(cur, "stg_sum", ("ecli","lang","summary","ssrc"), sums)
        cur.execute("""
        UPDATE case_text ct SET summary = s.summary, summary_source = s.ssrc
        FROM stg_sum s JOIN cases c ON c.ecli = s.ecli
        WHERE ct.case_id = c.id AND ct.language = s.lang AND ct.summary IS NULL""")
        cur.execute("""
        INSERT INTO case_text (case_id, language, summary, summary_source, source)
        SELECT c.id, s.lang, s.summary, s.ssrc, 'CELLAR_ITEM'
        FROM stg_sum s JOIN cases c ON c.ecli = s.ecli
        ON CONFLICT (case_id, language) DO NOTHING""")

        cur.execute("""INSERT INTO migration_manifest (step) VALUES ('50_load_cjeu')
                       ON CONFLICT (step) DO UPDATE SET completed_at = now()""")
        conn.commit()
    print("cjeu load complete", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
