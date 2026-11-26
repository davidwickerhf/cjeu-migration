#!/usr/bin/env python3
"""Backfill Phases 3-5 context tables (EU legislation, law refs, authorities, outcomes) via sql-runner.

Writes strictly to additive/new rows (`ON CONFLICT ... DO NOTHING`), never
touching existing rows or live citation counts. DRY_RUN=1 by default.

Modes (`--mode`):
  eu_legislation    R3.1: seed 63 curated EU/CoE/UN acts + aliases (requires 0011)
  rs_eu_law         R3.3: RS fulltext/summary -> EU law references (requires 0012, 0014)
  echr_eu_law       R3.4: HUDOC fulltext/summary -> EU law references (requires 0012, 0014)
  rs_authorities    R4.1/4.4/4.5: RS texts -> authority_document + case_authority_reference
  echr_authorities  R4.4/4.5: HUDOC texts -> authority_document + case_authority_reference
  cjeu_outcomes     R5.1: parse cjeu_document.proc_type -> case_outcome
  echr_outcomes     R5.1: parse echr_document violation/nonviolation/conclusion -> case_outcome
  rs_dictum         R5.1 (optional): parse RS Beslissing/Dictum -> case_outcome

Env:
  SQL_RUNNER_URL      e.g. https://demo-psql.caselawexplorer.tech
  SQL_RUNNER_TOKEN    HMAC token (never printed)
  SQL_RUNNER_CONFIRM  default execute-cle-v2
  DRY_RUN             default 1; set DRY_RUN=0 to write
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "sql"))

from sql_runner_client import SqlRunnerClient  # noqa: E402

INSERT_LEGISLATION_SQL = """
INSERT INTO legislation (
    scheme, identifier, jurisdiction_id, document_type, title,
    enacted_date, enacted_year, eli, source_dataset, extractor_version
)
SELECT
    u.scheme, u.identifier, j.id, u.document_type, u.title,
    u.enacted_date, u.enacted_year, u.eli, u.source_dataset, u.extractor_version
  FROM unnest(
      %s::text[], %s::text[], %s::text[], %s::text[], %s::text[],
      %s::date[], %s::smallint[], %s::text[], %s::text[], %s::text[]
  ) AS u(
      scheme, identifier, jurisdiction_code, document_type, title,
      enacted_date, enacted_year, eli, source_dataset, extractor_version
  )
  JOIN jurisdiction j ON j.iso_code = u.jurisdiction_code
ON CONFLICT (scheme, identifier)
WHERE scheme IN ('celex', 'coe_treaty', 'un_treaty')
DO NOTHING
"""

INSERT_ALIASES_SQL = """
INSERT INTO legislation_alias (legislation_id, alias, source)
SELECT l.id, u.alias, 'curated'
  FROM unnest(%s::text[], %s::text[], %s::text[]) AS u(scheme, identifier, alias)
  JOIN legislation l ON l.scheme = u.scheme AND l.identifier = u.identifier
ON CONFLICT (legislation_id, alias) DO NOTHING
"""

INSERT_LAW_REF_RESOLVED_SQL = """
INSERT INTO case_law_reference (
    case_id, legislation_id, provision_id, raw_scheme, raw_resource, raw_subdivision,
    raw_reference, role, source_dataset, extractor_version, match_method, confidence
)
SELECT *
  FROM unnest(
      %s::bigint[], %s::bigint[], %s::bigint[], %s::text[], %s::text[], %s::text[],
      %s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::real[]
  )
ON CONFLICT (case_id, raw_scheme, raw_resource, COALESCE(raw_subdivision, ''), role, source_dataset)
WHERE raw_resource IS NOT NULL
  AND (legislation_id IS NOT NULL OR provision_id IS NOT NULL)
  AND source_dataset IN ('cellar_sparql', 'cellar_law_ref', 'rs_text_eu_law', 'echr_text_eu_law')
DO NOTHING
"""

INSERT_LAW_REF_UNRESOLVED_SQL = """
INSERT INTO case_law_reference (
    case_id, legislation_id, provision_id, raw_scheme, raw_resource, raw_subdivision,
    raw_reference, role, source_dataset, extractor_version, match_method, confidence
)
SELECT *
  FROM unnest(
      %s::bigint[], %s::bigint[], %s::bigint[], %s::text[], %s::text[], %s::text[],
      %s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::real[]
  )
ON CONFLICT (case_id, raw_scheme, raw_resource, COALESCE(raw_subdivision, ''), role, source_dataset)
WHERE provision_id IS NULL AND legislation_id IS NULL AND raw_resource IS NOT NULL
DO NOTHING
"""

INSERT_AUTHORITY_DOCS_SQL = """
INSERT INTO authority_document (kind, identifier_scheme, identifier, title, issuer, date, url)
SELECT *
  FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::date[], %s::text[])
ON CONFLICT (kind, COALESCE(identifier_scheme, ''), identifier)
WHERE identifier IS NOT NULL
DO NOTHING
"""

INSERT_AUTHORITY_REFS_SQL = """
INSERT INTO case_authority_reference (
    case_id, authority_document_id, raw_reference, pinpoint,
    text_field, language, char_start, char_end, context,
    source_dataset, extractor_version, match_method, confidence
)
SELECT
    u.case_id, d.id, u.raw_reference, u.pinpoint,
    u.text_field, u.language, u.char_start, u.char_end, u.context,
    u.source_dataset, u.extractor_version, u.match_method, u.confidence
  FROM unnest(
      %s::bigint[], %s::text[], %s::text[], %s::text[],
      %s::text[], %s::text[], %s::text[], %s::text[],
      %s::int[], %s::int[], %s::text[],
      %s::text[], %s::text[], %s::text[], %s::real[]
  ) AS u(
      case_id, doc_kind, doc_scheme, doc_identifier,
      raw_reference, pinpoint, text_field, language,
      char_start, char_end, context,
      source_dataset, extractor_version, match_method, confidence
  )
  LEFT JOIN authority_document d
    ON d.kind = u.doc_kind
   AND COALESCE(d.identifier_scheme, '') = COALESCE(u.doc_scheme, '')
   AND d.identifier = u.doc_identifier
ON CONFLICT (case_id, md5(raw_reference), COALESCE(text_field, ''), COALESCE(language, ''),
             COALESCE(char_start, -1), source_dataset)
DO NOTHING
"""

INSERT_OUTCOMES_SQL = """
INSERT INTO case_outcome (
    case_id, scope, article, outcome, raw_value, source_dataset, extractor_version
)
SELECT *
  FROM unnest(
      %s::bigint[], %s::text[], %s::text[], %s::text[],
      %s::text[], %s::text[], %s::text[]
  )
ON CONFLICT (case_id, scope, COALESCE(article, ''), outcome, source_dataset)
DO NOTHING
"""


def _parse_date(val: Any) -> dt.date | None:
    if not val:
        return None
    if isinstance(val, dt.date):
        return val
    try:
        return dt.date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


def _load_cle(cle_checkout: Path):
    dags_dir = str(cle_checkout / "airflow" / "dags")
    if dags_dir not in sys.path:
        sys.path.insert(0, dags_dir)
    from data_extraction.case_context import cjeu_outcomes, common, echr_outcomes, rs_dictum
    from data_extraction.linking import (
        artifact,
        authority_text,
        echr_eu_law,
        eu_law_text,
        rs_eu_law,
        text_common,
    )
    from data_loading import legislation_loader

    return {
        "artifact": artifact,
        "text_common": text_common,
        "legislation_loader": legislation_loader,
        "eu_law_text": eu_law_text,
        "rs_eu_law": rs_eu_law,
        "echr_eu_law": echr_eu_law,
        "authority_text": authority_text,
        "cjeu_outcomes": cjeu_outcomes,
        "echr_outcomes": echr_outcomes,
        "rs_dictum": rs_dictum,
        "context_common": common,
    }


def run_eu_legislation(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    dry_run: bool,
    chunk_size: int,
    explain_sql: bool,
) -> dict[str, Any]:
    acts = list(cle["legislation_loader"].acts_module.curated_acts())
    act_rows = [
        (
            a.scheme, a.identifier, a.jurisdiction, a.document_type, a.title,
            a.enacted_date, a.enacted_year, a.eli, a.source_dataset, a.extractor_version,
        )
        for a in acts
    ]
    alias_rows = [
        (a.scheme, a.identifier, alias)
        for a in acts
        for alias in (a.aliases or ())
        if alias and alias.strip()
    ]
    ins_acts = 0
    ins_aliases = 0
    if act_rows:
        ins_acts = client.execute_unnest_chunks(
            INSERT_LEGISLATION_SQL, list(zip(*act_rows)),
            chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql),
        )
    if alias_rows:
        ins_aliases = client.execute_unnest_chunks(
            INSERT_ALIASES_SQL, list(zip(*alias_rows)),
            chunk_size=chunk_size, dry_run=dry_run,
        )
    return {
        "mode": "eu_legislation",
        "acts_prepared": len(act_rows),
        "acts_inserted": ins_acts,
        "aliases_prepared": len(alias_rows),
        "aliases_inserted": ins_aliases,
    }


def _load_eu_legislation_lookup(
    client: SqlRunnerClient,
) -> tuple[dict[tuple[str, str], int], dict[tuple[int, str], int]]:
    """Preload curated EU/CoE/UN legislation IDs (+ aliases) and provisions."""
    leg_map: dict[tuple[str, str], int] = {}
    leg_rows = client.query_rows(
        """
        SELECT id, scheme, identifier
          FROM legislation
         WHERE scheme IN ('celex', 'coe_treaty', 'un_treaty')
         ORDER BY id
         LIMIT 900
        """
    )
    leg_ids: list[int] = []
    for r in leg_rows:
        lid = int(r["id"])
        leg_ids.append(lid)
        leg_map.setdefault((r["scheme"], r["identifier"]), lid)
    if leg_ids:
        alias_rows = client.query_rows(
            """
            SELECT legislation_id, alias
              FROM legislation_alias
             WHERE legislation_id = ANY(%s::bigint[])
             ORDER BY legislation_id
             LIMIT 900
            """,
            params=[leg_ids],
        )
        for r in alias_rows:
            leg_map.setdefault(("celex", r["alias"]), int(r["legislation_id"]))
    prov_map: dict[tuple[int, str], int] = {}
    if leg_ids:
        last_pid = 0
        while True:
            prows = client.query_rows(
                """
                SELECT id, legislation_id, lower(article_label) AS label
                  FROM legal_provision
                 WHERE legislation_id = ANY(%s::bigint[])
                   AND element_type = 'article'
                   AND article_label IS NOT NULL
                   AND id > %s
                 ORDER BY id
                 LIMIT 900
                """,
                params=[leg_ids, last_pid],
            )
            if not prows:
                break
            for r in prows:
                prov_map.setdefault((int(r["legislation_id"]), r["label"]), int(r["id"]))
            last_pid = int(prows[-1]["id"])
            if len(prows) < 900:
                break
    return leg_map, prov_map


def run_eu_law_text(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    corpus: str,
    from_id: int,
    to_id: int | None,
    page_size: int,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
    max_cases: int | None,
) -> dict[str, Any]:
    mod = cle["rs_eu_law"] if corpus == "rs" else cle["echr_eu_law"]
    eu_mod = cle["eu_law_text"]
    art = cle["artifact"]
    tc = cle["text_common"]
    corpus_tag = "RS" if corpus == "rs" else "ECHR"

    leg_map, prov_map = _load_eu_legislation_lookup(client)
    if corpus == "echr":
        bounds = client.query_rows("SELECT min(case_id) AS lo, max(case_id) AS hi FROM echr_document")[0]
    else:
        bounds = client.query_rows(
            "SELECT min(id) AS lo, max(id) AS hi FROM cases WHERE %s = ANY(sources)",
            params=[corpus_tag],
        )[0]
    if bounds["lo"] is None:
        return {"mode": f"{corpus}_eu_law", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(int(bounds["lo"]) - 1, from_id - 1) if from_id > 0 else int(bounds["lo"]) - 1

    stats: Counter[str] = Counter()
    scanned = 0
    ins_res = 0
    ins_unres = 0
    explained = False

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        if corpus == "echr":
            id_rows = client.query_rows(
                """
                SELECT DISTINCT case_id
                  FROM echr_document
                 WHERE case_id > %s AND case_id <= %s
                 ORDER BY case_id
                 LIMIT %s
                """,
                params=[cursor_id, hi, lim],
            )
            if not id_rows:
                break
            page_ids = [int(r["case_id"]) for r in id_rows]
            batch_count = len(page_ids)
            cursor_id = page_ids[-1]
            rows = client.query_rows(
                """
                SELECT c.id, c.ecli, c.celex_id, c.item_id, c.date_decision,
                       ct.language, ct.fulltext, ct.summary
                  FROM cases c
                  JOIN case_text ct ON ct.case_id = c.id AND ct.source = %s
                 WHERE c.id = ANY(%s::bigint[])
                   AND (ct.fulltext IS NOT NULL OR ct.summary IS NOT NULL)
                 ORDER BY c.id
                """,
                params=[mod.TEXT_SOURCE, page_ids],
            )
        else:
            rows = client.query_rows(
                """
                SELECT c.id, c.ecli, c.celex_id, c.item_id, c.date_decision,
                       ct.language, ct.fulltext, ct.summary
                  FROM cases c
                  JOIN case_text ct ON ct.case_id = c.id AND ct.source = %s
                 WHERE %s = ANY(c.sources)
                   AND c.id > %s AND c.id <= %s
                   AND (ct.fulltext IS NOT NULL OR ct.summary IS NOT NULL)
                 ORDER BY c.id
                 LIMIT %s
                """,
                params=[mod.TEXT_SOURCE, corpus_tag, cursor_id, hi, lim],
            )
            if not rows:
                break
            batch_count = len(rows)
            cursor_id = int(rows[-1]["id"])
        scanned += batch_count

        by_case: dict[int, tuple[Any, list[Any]]] = {}
        for r in rows:
            cid = int(r["id"])
            if cid not in by_case:
                src = eu_mod.TextSource(
                    art.CaseKey(
                        ecli=r.get("ecli") or None,
                        celex=r.get("celex_id") or None,
                        item_id=r.get("item_id") or None,
                    ),
                    _parse_date(r.get("date_decision")),
                )
                by_case[cid] = (src, [])
            units = by_case[cid][1]
            for field_name in ("fulltext", "summary"):
                if r.get(field_name):
                    units.append(tc.TextUnit(r[field_name], field_name, r.get("language"), mod.TEXT_SOURCE))

        res_rows: list[tuple] = []
        unres_rows: list[tuple] = []
        for cid, (src, units) in by_case.items():
            for rec in eu_mod.records_for(src, units, mod.DATASET, mod.PROFILE):
                leg_id = leg_map.get((rec.target.scheme, rec.target.identifier))
                if leg_id is None:
                    for alt in rec.target.alternatives or ():
                        leg_id = leg_map.get((rec.target.scheme, alt))
                        if leg_id is not None:
                            break
                prov_id = None
                if leg_id is not None and rec.article:
                    prov_id = prov_map.get((leg_id, rec.article.lower()))
                row = (
                    cid, leg_id, prov_id, rec.raw_scheme, rec.raw_resource, rec.raw_subdivision,
                    rec.raw_reference, rec.role, rec.source_dataset,
                    rec.extractor_version, rec.match_method, rec.confidence,
                )
                if leg_id is not None or prov_id is not None:
                    res_rows.append(row)
                    stats["resolved"] += 1
                else:
                    unres_rows.append(row)
                    stats["unresolved"] += 1

        if res_rows:
            ins_res += client.execute_unnest_chunks(
                INSERT_LAW_REF_RESOLVED_SQL, list(zip(*res_rows)),
                chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql and not explained),
            )
            explained = True
        if unres_rows:
            ins_unres += client.execute_unnest_chunks(
                INSERT_LAW_REF_UNRESOLVED_SQL, list(zip(*unres_rows)),
                chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql and not explained),
            )
            explained = True

        if scanned % 1000 < batch_count:
            print(
                f"[{corpus}_eu_law] scanned={scanned} last_id={cursor_id} res={ins_res} unres={ins_unres}",
                file=sys.stderr,
                flush=True,
            )
        if batch_count < lim:
            break

    return {
        "mode": f"{corpus}_eu_law",
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "inserted_resolved": ins_res,
        "inserted_unresolved": ins_unres,
        "stats": dict(sorted(stats.items())),
    }


def run_authorities(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    corpus: str,
    from_id: int,
    to_id: int | None,
    page_size: int,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
    max_cases: int | None,
) -> dict[str, Any]:
    auth_mod = cle["authority_text"]
    art = cle["artifact"]
    text_source, dataset = auth_mod.CORPORA[corpus]
    corpus_tag = "RS" if corpus == "rs" else "ECHR"
    groups = auth_mod.scan_groups()

    bounds = client.query_rows(
        "SELECT min(id) AS lo, max(id) AS hi FROM cases WHERE %s = ANY(sources)",
        params=[corpus_tag],
    )[0]
    if bounds["lo"] is None:
        return {"mode": f"{corpus}_authorities", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(0, from_id - 1) if from_id > 0 else 0

    stats: Counter[str] = Counter()
    scanned = 0
    ins_docs = 0
    ins_refs = 0
    explained = False

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        rows = client.query_rows(
            """
            SELECT c.id, c.ecli, c.celex_id, c.item_id, ct.language, ct.fulltext, ct.summary
              FROM cases c
              JOIN case_text ct ON ct.case_id = c.id AND ct.source = %s
             WHERE %s = ANY(c.sources)
               AND c.id > %s AND c.id <= %s
               AND (ct.fulltext IS NOT NULL OR ct.summary IS NOT NULL)
             ORDER BY c.id
             LIMIT %s
            """,
            params=[text_source, corpus_tag, cursor_id, hi, lim],
        )
        if not rows:
            break
        scanned += len(rows)
        cursor_id = int(rows[-1]["id"])

        docs_dedup: dict[tuple[str, str, str], tuple] = {}
        ref_rows: list[tuple] = []
        for r in rows:
            cid = int(r["id"])
            key = art.CaseKey(
                ecli=r.get("ecli") or None,
                celex=r.get("celex_id") or None,
                item_id=r.get("item_id") or None,
            )
            for field_name in ("fulltext", "summary"):
                text = r.get(field_name)
                if not text:
                    continue
                for rec in auth_mod.records_for_text(
                    key, text, field_name, r.get("language"), text_source, dataset, groups, stats
                ):
                    doc = rec.authority
                    if doc.identifier:
                        dkey = (doc.kind, doc.identifier_scheme or "", doc.identifier)
                        docs_dedup.setdefault(
                            dkey,
                            (
                                doc.kind, doc.identifier_scheme, doc.identifier,
                                doc.title, doc.issuer, doc.date, doc.url,
                            ),
                        )
                    m = rec.mention
                    ref_rows.append(
                        (
                            cid, doc.kind, doc.identifier_scheme, doc.identifier,
                            rec.raw_reference, rec.pinpoint, m.text_field, m.language,
                            m.char_start, m.char_end, m.context,
                            rec.source_dataset, rec.extractor_version, rec.match_method, rec.confidence,
                        )
                    )

        if docs_dedup:
            ins_docs += client.execute_unnest_chunks(
                INSERT_AUTHORITY_DOCS_SQL, list(zip(*docs_dedup.values())),
                chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql and not explained),
            )
            explained = True
        if ref_rows:
            ins_refs += client.execute_unnest_chunks(
                INSERT_AUTHORITY_REFS_SQL, list(zip(*ref_rows)),
                chunk_size=chunk_size, dry_run=dry_run,
            )

        if scanned % 1000 < len(rows):
            print(
                f"[{corpus}_authorities] scanned={scanned} last_id={cursor_id} docs={ins_docs} refs={ins_refs}",
                file=sys.stderr,
                flush=True,
            )
        if len(rows) < lim:
            break

    return {
        "mode": f"{corpus}_authorities",
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "docs_inserted": ins_docs,
        "refs_inserted": ins_refs,
        "stats": dict(sorted(stats.items())),
    }


def run_cjeu_outcomes(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    from_id: int,
    to_id: int | None,
    page_size: int,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
    max_cases: int | None,
) -> dict[str, Any]:
    mod = cle["cjeu_outcomes"]
    hi = to_id
    if hi is None:
        b = client.query_rows("SELECT max(case_id) AS hi FROM cjeu_document")[0]["hi"]
        if b is None:
            return {"mode": "cjeu_outcomes", "scanned": 0}
        hi = int(b)
    cursor_id = max(0, from_id - 1) if from_id > 0 else 0

    stats: Counter[str] = Counter()
    scanned = 0
    prepared = 0
    inserted = 0
    explained = False

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        rows = client.query_rows(
            """
            SELECT case_id, celex_id, proc_type
              FROM cjeu_document
             WHERE proc_type IS NOT NULL
               AND case_id > %s AND case_id <= %s
             ORDER BY case_id
             LIMIT %s
            """,
            params=[cursor_id, hi, lim],
        )
        if not rows:
            break
        scanned += len(rows)
        cursor_id = int(rows[-1]["case_id"])
        batch = []
        for r in rows:
            if not mod.is_decision_celex(r.get("celex_id")):
                stats["non_decision_skipped"] += 1
                continue
            outcome, rule = mod.parse_proc_type(r.get("proc_type"))
            stats[f"rule:{rule}"] += 1
            if outcome:
                batch.append(
                    (
                        int(r["case_id"]), "overall", None, outcome,
                        r["proc_type"], mod.SOURCE_DATASET, mod.EXTRACTOR_VERSION,
                    )
                )
        prepared += len(batch)
        if batch:
            inserted += client.execute_unnest_chunks(
                INSERT_OUTCOMES_SQL, list(zip(*batch)),
                chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql and not explained),
            )
            explained = True
        if len(rows) < lim:
            break

    return {
        "mode": "cjeu_outcomes",
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "prepared": prepared,
        "inserted": inserted,
        "stats": dict(sorted(stats.items())),
    }


def run_echr_outcomes(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    from_id: int,
    to_id: int | None,
    page_size: int,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
    max_cases: int | None,
) -> dict[str, Any]:
    mod = cle["echr_outcomes"]
    hi = to_id
    if hi is None:
        b = client.query_rows("SELECT max(case_id) AS hi FROM echr_document")[0]["hi"]
        if b is None:
            return {"mode": "echr_outcomes", "scanned": 0}
        hi = int(b)
    cursor_id = max(0, from_id - 1) if from_id > 0 else 0

    stats: Counter[str] = Counter()
    scanned = 0
    prepared = 0
    inserted = 0
    explained = False

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        rows = client.query_rows(
            """
            SELECT case_id, doctype, conclusion, violation, nonviolation
              FROM echr_document
             WHERE case_id > %s AND case_id <= %s
               AND (doctype ILIKE '%%JUD%%' OR doctype ILIKE '%%DEC%%')
             ORDER BY case_id
             LIMIT %s
            """,
            params=[cursor_id, hi, lim],
        )
        if not rows:
            break
        scanned += len(rows)
        cursor_id = int(rows[-1]["case_id"])
        by_case: dict[int, list[tuple]] = {}
        for r in rows:
            cid = int(r["case_id"])
            by_case.setdefault(cid, []).append(
                (r.get("conclusion"), r.get("violation"), r.get("nonviolation"), [], r.get("doctype"))
            )
        batch = []
        for cid, docs in by_case.items():
            recs = mod.outcomes_for_documents(mod.case_key(ecli=f"ECLI:CE:ECHR:2000:0101JUD{cid:09d}"), docs, stats)
            for rec in recs:
                batch.append(
                    (
                        cid, rec.scope, rec.article, rec.outcome,
                        rec.raw_value, rec.source_dataset, rec.extractor_version,
                    )
                )
        prepared += len(batch)
        if batch:
            inserted += client.execute_unnest_chunks(
                INSERT_OUTCOMES_SQL, list(zip(*batch)),
                chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql and not explained),
            )
            explained = True
        if len(rows) < lim:
            break

    return {
        "mode": "echr_outcomes",
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "prepared": prepared,
        "inserted": inserted,
        "stats": dict(sorted(stats.items())),
    }


def run_rs_dictum(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    from_id: int,
    to_id: int | None,
    page_size: int,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
    max_cases: int | None,
) -> dict[str, Any]:
    mod = cle["rs_dictum"]
    art = cle["artifact"]
    bounds = client.query_rows(
        "SELECT min(id) AS lo, max(id) AS hi FROM cases WHERE 'RS' = ANY(sources)"
    )[0]
    if bounds["lo"] is None:
        return {"mode": "rs_dictum", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(0, from_id - 1) if from_id > 0 else 0

    stats: Counter[str] = Counter()
    scanned = 0
    prepared = 0
    inserted = 0
    explained = False

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        rows = client.query_rows(
            """
            SELECT c.id, c.ecli, ct.fulltext
              FROM cases c
              JOIN case_text ct ON ct.case_id = c.id AND ct.source = 'RECHTSPRAAK'
             WHERE 'RS' = ANY(c.sources)
               AND c.id > %s AND c.id <= %s
               AND ct.fulltext IS NOT NULL
             ORDER BY c.id
             LIMIT %s
            """,
            params=[cursor_id, hi, lim],
        )
        if not rows:
            break
        scanned += len(rows)
        cursor_id = int(rows[-1]["id"])
        batch = []
        for r in rows:
            cid = int(r["id"])
            key = art.CaseKey(ecli=r.get("ecli") or f"ECLI:NL:XX:2000:{cid}")
            for rec in mod.outcome_records(key, r["fulltext"], stats):
                batch.append(
                    (
                        cid, rec.scope, rec.article, rec.outcome,
                        rec.raw_value, rec.source_dataset, rec.extractor_version,
                    )
                )
        prepared += len(batch)
        if batch:
            inserted += client.execute_unnest_chunks(
                INSERT_OUTCOMES_SQL, list(zip(*batch)),
                chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql and not explained),
            )
            explained = True
        if len(rows) < lim:
            break

    return {
        "mode": "rs_dictum",
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "prepared": prepared,
        "inserted": inserted,
        "stats": dict(sorted(stats.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=(
            "eu_legislation",
            "rs_eu_law",
            "echr_eu_law",
            "rs_authorities",
            "echr_authorities",
            "cjeu_outcomes",
            "echr_outcomes",
            "rs_dictum",
        ),
        required=True,
    )
    parser.add_argument("--cle-checkout", type=Path, default=Path("/home/agent/projects/cle-law-and-context"))
    parser.add_argument("--from-id", type=int, default=0)
    parser.add_argument("--to-id", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=200)
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args(argv)

    dry_run = os.environ.get("DRY_RUN", "1").strip().lower() not in {"0", "false", "no"}
    client = SqlRunnerClient(read_only=dry_run)
    cle = _load_cle(args.cle_checkout)

    started = time.monotonic()
    if args.mode == "eu_legislation":
        summary = run_eu_legislation(
            client, cle, dry_run=dry_run, chunk_size=args.chunk_size, explain_sql=args.explain
        )
    elif args.mode in ("rs_eu_law", "echr_eu_law"):
        summary = run_eu_law_text(
            client, cle, corpus="rs" if args.mode == "rs_eu_law" else "echr",
            from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain, max_cases=args.max_cases,
        )
    elif args.mode in ("rs_authorities", "echr_authorities"):
        summary = run_authorities(
            client, cle, corpus="rs" if args.mode == "rs_authorities" else "echr",
            from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain, max_cases=args.max_cases,
        )
    elif args.mode == "cjeu_outcomes":
        summary = run_cjeu_outcomes(
            client, cle, from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain, max_cases=args.max_cases,
        )
    elif args.mode == "echr_outcomes":
        summary = run_echr_outcomes(
            client, cle, from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain, max_cases=args.max_cases,
        )
    else:
        summary = run_rs_dictum(
            client, cle, from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain, max_cases=args.max_cases,
        )

    summary["dry_run"] = dry_run
    summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
