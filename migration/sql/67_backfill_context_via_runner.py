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

INSERT_STATUS_SQL = """
INSERT INTO case_status (
    case_id, status, status_date, related_case_id, raw_value, source_dataset, extractor_version
)
SELECT *
  FROM unnest(
      %s::bigint[], %s::text[], %s::date[], %s::bigint[],
      %s::text[], %s::text[], %s::text[]
  )
ON CONFLICT (case_id, status, COALESCE(status_date, '-infinity'::date), source_dataset)
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
    from data_extraction.case_context import case_status, cjeu_outcomes, common, echr_outcomes, rs_dictum
    from data_extraction.linking import (
        artifact,
        authority_text,
        cellar_authority,
        cellar_law_refs,
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
        "cellar_authority": cellar_authority,
        "cellar_law_refs": cellar_law_refs,
        "eu_law_text": eu_law_text,
        "rs_eu_law": rs_eu_law,
        "echr_eu_law": echr_eu_law,
        "authority_text": authority_text,
        "case_status": case_status,
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
    """Preload all EU/CoE/UN legislation IDs (+ aliases) and provisions (paginated)."""
    leg_map: dict[tuple[str, str], int] = {}
    leg_ids: list[int] = []
    last_lid = 0
    while True:
        leg_rows = client.query_rows(
            """
            SELECT id, scheme, identifier
              FROM legislation
             WHERE scheme IN ('celex', 'coe_treaty', 'un_treaty')
               AND id > %s
             ORDER BY id
             LIMIT 900
            """,
            params=[last_lid],
        )
        if not leg_rows:
            break
        for r in leg_rows:
            lid = int(r["id"])
            leg_ids.append(lid)
            leg_map.setdefault((r["scheme"], r["identifier"]), lid)
        last_lid = int(leg_rows[-1]["id"])
        if len(leg_rows) < 900:
            break
    if leg_ids:
        last_aid = 0
        while True:
            alias_rows = client.query_rows(
                """
                SELECT a.id, a.legislation_id, a.alias
                  FROM legislation_alias a
                  JOIN legislation l ON l.id = a.legislation_id
                 WHERE l.scheme IN ('celex', 'coe_treaty', 'un_treaty')
                   AND a.id > %s
                 ORDER BY a.id
                 LIMIT 900
                """,
                params=[last_aid],
            )
            if not alias_rows:
                break
            for r in alias_rows:
                leg_map.setdefault(("celex", r["alias"]), int(r["legislation_id"]))
            last_aid = int(alias_rows[-1]["id"])
            if len(alias_rows) < 900:
                break
    prov_map: dict[tuple[int, str], int] = {}
    if leg_ids:
        last_pid = 0
        while True:
            prows = client.query_rows(
                """
                SELECT p.id, p.legislation_id, lower(p.article_label) AS label
                  FROM legal_provision p
                  JOIN legislation l ON l.id = p.legislation_id
                 WHERE l.scheme IN ('celex', 'coe_treaty', 'un_treaty')
                   AND p.element_type = 'article'
                   AND p.article_label IS NOT NULL
                   AND p.id > %s
                 ORDER BY p.id
                 LIMIT 900
                """,
                params=[last_pid],
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

    if corpus == "echr":
        bounds = client.query_rows("SELECT min(case_id) AS lo, max(case_id) AS hi FROM echr_document")[0]
    else:
        bounds = client.query_rows(
            "SELECT min(id) AS lo, max(id) AS hi FROM cases WHERE %s = ANY(sources)",
            params=[corpus_tag],
        )[0]
    if bounds["lo"] is None:
        return {"mode": f"{corpus}_authorities", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(int(bounds["lo"]) - 1, from_id - 1) if from_id > 0 else int(bounds["lo"]) - 1

    stats: Counter[str] = Counter()
    scanned = 0
    ins_docs = 0
    ins_refs = 0
    explained = False

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        if corpus == "echr":
            id_rows = client.query_rows(
                "SELECT DISTINCT case_id FROM echr_document WHERE case_id > %s AND case_id <= %s ORDER BY case_id LIMIT %s",
                params=[cursor_id, hi, lim],
            )
            if not id_rows:
                break
            page_ids = [int(r["case_id"]) for r in id_rows]
            batch_count = len(page_ids)
            cursor_id = page_ids[-1]
            rows = client.query_rows(
                """
                SELECT c.id, c.ecli, c.celex_id, c.item_id, ct.language, ct.fulltext, ct.summary
                  FROM cases c
                  JOIN case_text ct ON ct.case_id = c.id AND ct.source = %s
                 WHERE c.id = ANY(%s::bigint[])
                   AND (ct.fulltext IS NOT NULL OR ct.summary IS NOT NULL)
                 ORDER BY c.id
                """,
                params=[text_source, page_ids],
            )
        else:
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
            batch_count = len(rows)
            cursor_id = int(rows[-1]["id"])
        scanned += batch_count

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

        if scanned % 1000 < batch_count:
            print(
                f"[{corpus}_authorities] scanned={scanned} last_id={cursor_id} docs={ins_docs} refs={ins_refs}",
                file=sys.stderr,
                flush=True,
            )
        if batch_count < lim:
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
        if scanned % 10000 < len(rows):
            print(
                f"[cjeu_outcomes] scanned={scanned} last_id={cursor_id} prepared={prepared} inserted={inserted}",
                file=sys.stderr,
                flush=True,
            )
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
        id_rows = client.query_rows(
            """
            SELECT DISTINCT case_id
              FROM echr_document
             WHERE case_id > %s AND case_id <= %s
               AND (doctype ILIKE '%%JUD%%' OR doctype ILIKE '%%DEC%%')
             ORDER BY case_id
             LIMIT %s
            """,
            params=[cursor_id, hi, min(lim, 250)],
        )
        if not id_rows:
            break
        page_ids = [int(r["case_id"]) for r in id_rows]
        scanned += len(page_ids)
        cursor_id = page_ids[-1]
        rows = []
        last_item = ""
        while True:
            sub = client.query_rows(
                """
                SELECT case_id, item_id, doctype, conclusion, violation, nonviolation
                  FROM echr_document
                 WHERE case_id = ANY(%s::bigint[])
                   AND (doctype ILIKE '%%JUD%%' OR doctype ILIKE '%%DEC%%')
                   AND item_id > %s
                 ORDER BY item_id
                 LIMIT 900
                """,
                params=[page_ids, last_item],
            )
            if not sub:
                break
            rows.extend(sub)
            last_item = sub[-1]["item_id"]
            if len(sub) < 900:
                break
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
        if scanned % 10000 < len(page_ids):
            print(
                f"[echr_outcomes] scanned={scanned} last_id={cursor_id} prepared={prepared} inserted={inserted}",
                file=sys.stderr,
                flush=True,
            )
        if len(page_ids) < min(lim, 250):
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
              JOIN rs_document d ON d.case_id = c.id
              JOIN case_text ct ON ct.case_id = c.id AND ct.source = 'RECHTSPRAAK'
             WHERE 'RS' = ANY(c.sources)
               AND c.id > %s AND c.id <= %s
               AND ct.fulltext IS NOT NULL
               AND coalesce(d.document_type, '') !~* 'conclusie'
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
        if scanned % 10000 < len(rows):
            print(
                f"[rs_dictum] scanned={scanned} last_id={cursor_id} prepared={prepared} inserted={inserted}",
                file=sys.stderr,
                flush=True,
            )
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


def run_cellar_law_refs(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
    max_cases: int | None,
) -> dict[str, Any]:
    mod = cle["cellar_law_refs"]
    art = cle["artifact"]
    leg_map, prov_map = _load_eu_legislation_lookup(client)

    stats: Counter[str] = Counter()
    last_cid = 0
    scanned = 0
    ins_res = 0
    ins_unres = 0
    explained = False

    while True:
        lim = 900 if max_cases is None else min(900, max_cases - scanned)
        if lim <= 0:
            break
        rows = client.query_rows(
            """
            SELECT cc.id, cc.source_case_id, c.ecli, c.celex_id, c.item_id, cc.target_celex_raw
              FROM case_citation cc
              JOIN cases c ON c.id = cc.source_case_id
             WHERE cc.source_dataset = 'cellar_sparql'
               AND cc.relation_type = 'cites'
               AND cc.target_case_id IS NULL
               AND cc.target_celex_raw IS NOT NULL
               AND cc.source_case_id IS NOT NULL
               AND cc.id > %s
             ORDER BY cc.id
             LIMIT %s
            """,
            params=[last_cid, lim],
        )
        if not rows:
            break
        scanned += len(rows)
        last_cid = int(rows[-1]["id"])

        res_rows: list[tuple] = []
        unres_rows: list[tuple] = []
        for r in rows:
            cid = int(r["source_case_id"])
            src = art.CaseKey(
                ecli=r.get("ecli") or None,
                celex=r.get("celex_id") or None,
                item_id=r.get("item_id") or None,
            )
            if not (src.ecli or src.celex or src.item_id):
                continue
            rec = mod.record_for(src, r["target_celex_raw"], stats)
            if rec is None:
                continue
            leg_id = leg_map.get((rec.target.scheme, rec.target.identifier))
            if leg_id is None:
                for alt in rec.target.alternatives or ():
                    leg_id = leg_map.get((rec.target.scheme, alt))
                    if leg_id is not None:
                        break
            prov_id = prov_map.get((leg_id, rec.article.lower())) if (leg_id is not None and rec.article) else None
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
        if scanned % 20000 < len(rows):
            print(f"[cellar_law_refs] scanned={scanned} res={ins_res} unres={ins_unres}", file=sys.stderr, flush=True)
        if len(rows) < lim:
            break

    return {
        "mode": "cellar_law_refs",
        "scanned": scanned,
        "inserted_resolved": ins_res,
        "inserted_unresolved": ins_unres,
        "stats": dict(sorted(stats.items())),
    }


def run_cjeu_status(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
) -> dict[str, Any]:
    mod = cle["case_status"]
    stats: Counter[str] = Counter()
    rows = client.query_rows(
        """
        SELECT a.id AS a_id, a.ecli AS a_ecli, COALESCE(ad.celex_id, a.celex_id) AS a_celex,
               a.date_decision AS a_date, ad.proc_type,
               u.id AS u_id, u.ecli AS u_ecli, COALESCE(ud.celex_id, u.celex_id) AS u_celex
          FROM case_citation cc
          JOIN cases a ON a.id = CASE WHEN cc.relation_type = 'appeal_of' THEN cc.source_case_id ELSE cc.target_case_id END
          JOIN cjeu_document ad ON ad.case_id = a.id
          JOIN cases u ON u.id = CASE WHEN cc.relation_type = 'appeal_of' THEN cc.target_case_id ELSE cc.source_case_id END
          LEFT JOIN cjeu_document ud ON ud.case_id = u.id
         WHERE cc.target_case_id IS NOT NULL
           AND cc.source_case_id <> cc.target_case_id
           AND (
                 (cc.relation_type = 'appeal_of' AND cc.source_dataset = 'cellar_relation_resolved')
              OR (cc.relation_type = 'subject_to_appeal' AND cc.source_dataset = 'cellar_sparql')
           )
           AND ad.proc_type ILIKE '%%successful%%'
        """
    )
    batch_dedup: dict[tuple, tuple] = {}
    for r in rows:
        stats["appeal_links"] += 1
        rec = mod.appeal_status(
            (r.get("a_ecli"), r.get("a_celex"), _parse_date(r.get("a_date")), r.get("proc_type")),
            (r.get("u_ecli"), r.get("u_celex")),
            stats,
        )
        if rec is None:
            continue
        key = (int(r["u_id"]), rec.status, rec.status_date, rec.source_dataset)
        batch_dedup.setdefault(
            key,
            (
                int(r["u_id"]), rec.status, rec.status_date, int(r["a_id"]),
                rec.raw_value, rec.source_dataset, rec.extractor_version,
            ),
        )
    inserted = 0
    if batch_dedup:
        inserted = client.execute_unnest_chunks(
            INSERT_STATUS_SQL, list(zip(*batch_dedup.values())),
            chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql),
        )
    return {
        "mode": "cjeu_status",
        "scanned": len(rows),
        "prepared": len(batch_dedup),
        "inserted": inserted,
        "stats": dict(sorted(stats.items())),
    }


def run_echr_status(
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
    mod = cle["case_status"]
    bounds = client.query_rows("SELECT min(case_id) AS lo, max(case_id) AS hi FROM echr_document")[0]
    if bounds["lo"] is None:
        return {"mode": "echr_status", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(int(bounds["lo"]) - 1, from_id - 1) if from_id > 0 else int(bounds["lo"]) - 1

    stats: Counter[str] = Counter()
    scanned = 0
    inserted = 0
    explained = False

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        id_rows = client.query_rows(
            "SELECT DISTINCT case_id FROM echr_document WHERE case_id > %s AND case_id <= %s ORDER BY case_id LIMIT %s",
            params=[cursor_id, hi, lim],
        )
        if not id_rows:
            break
        page_ids = [int(r["case_id"]) for r in id_rows]
        scanned += len(page_ids)
        cursor_id = page_ids[-1]

        drows = client.query_rows(
            """
            SELECT c.id, c.ecli, c.item_id AS case_item, d.item_id, d.doctype, d.doctype_branch,
                   d.docname, COALESCE((d.judgement_date AT TIME ZONE 'UTC')::date, c.date_decision) AS decided,
                   d.conclusion
              FROM cases c
              JOIN echr_document d ON d.case_id = c.id
             WHERE c.id = ANY(%s::bigint[])
            """,
            params=[page_ids],
        )
        batch_dedup: dict[tuple, tuple] = {}
        for r in drows:
            doc = mod.EchrDocument(
                int(r["id"]), r.get("ecli"), r.get("case_item"), r["item_id"],
                r.get("doctype"), r.get("doctype_branch"), r.get("docname"),
                _parse_date(r.get("decided")), r.get("conclusion"),
            )
            for rec in mod.own_statuses(doc, stats):
                key = (doc.case_id, rec.status, rec.status_date, rec.source_dataset)
                batch_dedup.setdefault(
                    key,
                    (
                        doc.case_id, rec.status, rec.status_date, None,
                        rec.raw_value, rec.source_dataset, rec.extractor_version,
                    ),
                )
        if batch_dedup:
            inserted += client.execute_unnest_chunks(
                INSERT_STATUS_SQL, list(zip(*batch_dedup.values())),
                chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql and not explained),
            )
            explained = True
        if scanned % 20000 < len(page_ids):
            print(f"[echr_status] scanned={scanned} last_id={cursor_id} inserted={inserted}", file=sys.stderr, flush=True)
        if len(page_ids) < lim:
            break

    return {
        "mode": "echr_status",
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "inserted": inserted,
        "stats": dict(sorted(stats.items())),
    }


def run_echr_gc_referrals(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
) -> dict[str, Any]:
    """Second pass of R5.2 ECtHR status: Chamber judgments referred to the Grand Chamber."""
    mod = cle["case_status"]
    gc_rows = client.query_rows(
        """
        SELECT c.id, c.ecli, c.item_id AS case_item, d.item_id, d.doctype, d.doctype_branch,
               d.docname, COALESCE((d.judgement_date AT TIME ZONE 'UTC')::date, c.date_decision) AS decided,
               d.conclusion
          FROM cases c
          JOIN echr_document d ON d.case_id = c.id
         WHERE d.doctype ~* 'JUD'
           AND (
                upper(regexp_replace(COALESCE(d.doctype_branch, ''), '[^A-Za-z]', '', 'g')) = 'GRANDCHAMBER'
             OR d.docname ~* '[\\[(]\\s*GC\\s*[\\])]'
           )
         ORDER BY d.judgement_date NULLS LAST, d.item_id
         LIMIT 950
        """
    )
    grand = []
    for r in gc_rows:
        doc = mod.EchrDocument(
            int(r["id"]), r.get("ecli"), r.get("case_item"), r["item_id"],
            r.get("doctype"), r.get("doctype_branch"), r.get("docname"),
            _parse_date(r.get("decided")), r.get("conclusion"),
        )
        if doc.is_judgment and doc.formation == "grand_chamber" and not mod._NOT_MERITS_DOCNAME.search(doc.docname or ""):
            grand.append(doc)

    stats: Counter[str] = Counter()
    batch_dedup: dict[tuple, tuple] = {}
    referred_cases: set[int] = set()

    for i in range(0, len(grand), 60):
        chunk = grand[i : i + 60]
        gc_items = [d.item_id for d in chunk]
        other_rows = client.query_rows(
            """
            SELECT DISTINCT c.id, c.ecli, c.item_id AS case_item, d.item_id, d.doctype, d.doctype_branch,
                   d.docname, COALESCE((d.judgement_date AT TIME ZONE 'UTC')::date, c.date_decision) AS decided,
                   d.conclusion
              FROM echr_document_appno g
              JOIN echr_document_appno o ON o.appno = g.appno AND o.source = 'appno' AND o.item_id <> g.item_id
              JOIN echr_document d ON d.item_id = o.item_id
              JOIN cases c ON c.id = d.case_id
             WHERE g.item_id = ANY(%s::text[]) AND g.source = 'appno' AND d.doctype ~* 'JUD'
             LIMIT 950
            """,
            params=[gc_items],
        )
        others = [
            mod.EchrDocument(
                int(r["id"]), r.get("ecli"), r.get("case_item"), r["item_id"],
                r.get("doctype"), r.get("doctype_branch"), r.get("docname"),
                _parse_date(r.get("decided")), r.get("conclusion"),
            )
            for r in other_rows
        ]
        all_items = sorted(set(gc_items) | {d.item_id for d in others})
        appnos: dict[str, list[str]] = {}
        for j in range(0, len(all_items), 150):
            arows = client.query_rows(
                "SELECT item_id, appno FROM echr_document_appno WHERE item_id = ANY(%s::text[]) AND source = 'appno' LIMIT 950",
                params=[all_items[j : j + 150]],
            )
            for ar in arows:
                appnos.setdefault(ar["item_id"], []).append(ar["appno"])
        by_case_key = {d.key: d.case_id for d in others if d.key is not None}
        for gc in chunk:
            for rec in mod.referral_statuses(gc, others, appnos):
                ch_id = by_case_key.get(rec.case)
                if ch_id is None or ch_id in referred_cases:
                    continue
                referred_cases.add(ch_id)
                stats["referred_to_grand_chamber"] += 1
                key = (ch_id, rec.status, rec.status_date, rec.source_dataset)
                batch_dedup.setdefault(
                    key,
                    (
                        ch_id, rec.status, rec.status_date, gc.case_id,
                        rec.raw_value, rec.source_dataset, rec.extractor_version,
                    ),
                )

    inserted = 0
    if batch_dedup:
        inserted = client.execute_unnest_chunks(
            INSERT_STATUS_SQL, list(zip(*batch_dedup.values())),
            chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql),
        )
    return {
        "mode": "echr_gc_referrals",
        "grand_chamber_merits": len(grand),
        "prepared": len(batch_dedup),
        "inserted": inserted,
        "stats": dict(sorted(stats.items())),
    }


def run_cjeu_status_sparql(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
) -> dict[str, Any]:
    """Query Cellar SPARQL for all fd_100 procedure types on linked CJEU appeals.

    Because cjeu_document.proc_type stores only a single procedure label (often
    'Action for annulment'), querying all fd_100 triples recovers 'Appeal - successful'
    (PVOI=OB) and 'Appeal against penalty - successful' (SANC=OB) for case_status
    ('overturned'), plus missing appeal outcomes for case_outcome.
    """
    import urllib.parse
    import urllib.request

    mod = cle["case_status"]
    out_mod = cle["cjeu_outcomes"]
    endpoint = "https://publications.europa.eu/webapi/rdf/sparql"

    # Fetch all resolved appeal_of links (paginated cleanly)
    appeal_links: list[dict[str, Any]] = []
    last_cc_id = 0
    while True:
        rows = client.query_rows(
            """
            SELECT cc.id, a.id AS a_id, a.ecli AS a_ecli, COALESCE(ad.celex_id, a.celex_id) AS a_celex,
                   a.date_decision AS a_date, u.id AS u_id, u.ecli AS u_ecli,
                   COALESCE(ud.celex_id, u.celex_id) AS u_celex
              FROM case_citation cc
              JOIN cases a ON a.id = cc.source_case_id
              LEFT JOIN cjeu_document ad ON ad.case_id = a.id
              JOIN cases u ON u.id = cc.target_case_id
              LEFT JOIN cjeu_document ud ON ud.case_id = u.id
             WHERE cc.source_dataset = 'cellar_relation_resolved'
               AND cc.relation_type = 'appeal_of'
               AND cc.target_case_id IS NOT NULL
               AND cc.id > %s
             ORDER BY cc.id
             LIMIT 900
            """,
            params=[last_cc_id],
        )
        if not rows:
            break
        appeal_links.extend(rows)
        last_cc_id = int(rows[-1]["id"])
        if len(rows) < 900:
            break

    celexes = sorted({r["a_celex"] for r in appeal_links if r.get("a_celex")})
    code_to_label = {
        "PVOI%3DOB": ("Appeal - successful", "successful", True),
        "SANC%3DOB": ("Appeal against penalty - successful", "successful", True),
        "PVOI%3DRF": ("Appeal - unfounded", "unfounded", False),
        "SANC%3DRF": ("Appeal against penalty - unfounded", "unfounded", False),
        "PVOI%3DRI": ("Appeal - inadmissible", "inadmissible", False),
        "SANC%3DRI": ("Appeal against penalty - inadmissible", "inadmissible", False),
        "PVOI%3DNL": ("Appeal - decision unnecessary", "decision_unnecessary", False),
    }
    celex_procs: dict[str, set[str]] = {}
    for i in range(0, len(celexes), 120):
        chunk = celexes[i : i + 120]
        vals = " ".join(f'"{x}"^^xsd:string' for x in chunk)
        q = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT ?celex ?proc WHERE {{
  VALUES ?celex {{ {vals} }}
  ?w cdm:resource_legal_id_celex ?celex ;
     cdm:case-law_has_type_procedure_concept_type_procedure ?proc .
}}
"""
        req = urllib.request.Request(
            endpoint,
            data=urllib.parse.urlencode({"query": q}).encode(),
            headers={"Accept": "application/sparql-results+json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        for b in data.get("results", {}).get("bindings", []):
            cx = b["celex"]["value"]
            code = b["proc"]["value"].split("/")[-1]
            if code in code_to_label:
                celex_procs.setdefault(cx, set()).add(code)

    stats: Counter[str] = Counter()
    status_dedup: dict[tuple, tuple] = {}
    outcome_dedup: dict[tuple, tuple] = {}

    for r in appeal_links:
        cx = r.get("a_celex")
        codes = celex_procs.get(cx, set()) if cx else set()
        for code in sorted(codes):
            label, outcome, is_overturned = code_to_label[code]
            stats[f"code:{code}"] += 1
            okey = (int(r["a_id"]), "overall", "", outcome, out_mod.SOURCE_DATASET)
            outcome_dedup.setdefault(
                okey,
                (
                    int(r["a_id"]), "overall", None, outcome,
                    label, out_mod.SOURCE_DATASET, out_mod.EXTRACTOR_VERSION,
                ),
            )
            if is_overturned:
                sdate = _parse_date(r.get("a_date"))
                skey = (int(r["u_id"]), "overturned", sdate, mod.CJEU_DATASET)
                status_dedup.setdefault(
                    skey,
                    (
                        int(r["u_id"]), "overturned", sdate, int(r["a_id"]),
                        label, mod.CJEU_DATASET, mod.EXTRACTOR_VERSION,
                    ),
                )

    ins_status = 0
    ins_outcome = 0
    if status_dedup:
        ins_status = client.execute_unnest_chunks(
            INSERT_STATUS_SQL, list(zip(*status_dedup.values())),
            chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql),
        )
    if outcome_dedup:
        ins_outcome = client.execute_unnest_chunks(
            INSERT_OUTCOMES_SQL, list(zip(*outcome_dedup.values())),
            chunk_size=chunk_size, dry_run=dry_run,
        )
    return {
        "mode": "cjeu_status_sparql",
        "appeal_links_scanned": len(appeal_links),
        "unique_appeal_celexes": len(celexes),
        "status_prepared": len(status_dedup),
        "status_inserted": ins_status,
        "outcomes_prepared": len(outcome_dedup),
        "outcomes_inserted": ins_outcome,
        "stats": dict(sorted(stats.items())),
    }


def run_cellar_authority(
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
    """Backfill R4.3 Cellar cited authorities (sector 5/8/E + national_judgement_xml literature)."""
    mod = cle["cellar_authority"]
    art = cle["artifact"]
    bounds = client.query_rows("SELECT min(case_id) AS lo, max(case_id) AS hi FROM cjeu_document")[0]
    if bounds["lo"] is None:
        return {"mode": "cellar_authority", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(int(bounds["lo"]) - 1, from_id - 1) if from_id > 0 else int(bounds["lo"]) - 1

    stats: Counter[str] = Counter()
    scanned = 0
    ins_docs = 0
    ins_refs = 0
    explained = False

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        docs = client.query_rows(
            """
            SELECT c.id, c.ecli, COALESCE(d.celex_id, c.celex_id) AS celex_id,
                   c.item_id, d.national_judgement_xml
              FROM cjeu_document d
              JOIN cases c ON c.id = d.case_id
             WHERE d.case_id > %s AND d.case_id <= %s
             ORDER BY d.case_id
             LIMIT %s
            """,
            params=[cursor_id, hi, lim],
        )
        if not docs:
            break
        page_ids = [int(r["id"]) for r in docs]
        scanned += len(page_ids)
        cursor_id = page_ids[-1]

        # Fetch unresolved cellar_sparql citations for these CJEU cases (in sub-batches if needed)
        targets_by_case: dict[int, list[tuple[str, str]]] = {}
        for j in range(0, len(page_ids), 80):
            sub_ids = page_ids[j : j + 80]
            crows = client.query_rows(
                """
                SELECT source_case_id, target_celex_raw, relation_type
                  FROM case_citation
                 WHERE source_case_id = ANY(%s::bigint[])
                   AND source_dataset = 'cellar_sparql'
                   AND target_case_id IS NULL
                   AND target_celex_raw IS NOT NULL
                   AND relation_type IN ('cites', 'cited_by')
                 LIMIT 950
                """,
                params=[sub_ids],
            )
            for cr in crows:
                targets_by_case.setdefault(int(cr["source_case_id"]), []).append(
                    (cr["target_celex_raw"], cr["relation_type"])
                )

        docs_dedup: dict[tuple[str, str, str], tuple] = {}
        ref_rows: list[tuple] = []
        for r in docs:
            cid = int(r["id"])
            key = art.CaseKey(
                ecli=r.get("ecli") or None,
                celex=r.get("celex_id") or None,
                item_id=r.get("item_id") or None,
            )
            if not (key.ecli or key.celex or key.item_id):
                continue
            recs = []
            if cid in targets_by_case:
                recs.extend(mod.citation_records(key, targets_by_case[cid], stats, decisions=False))
            if r.get("national_judgement_xml"):
                recs.extend(mod.notice_records(key, r["national_judgement_xml"], stats))
            for rec in recs:
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
        if scanned % 10000 < len(page_ids):
            print(f"[cellar_authority] scanned={scanned} last_id={cursor_id} docs={ins_docs} refs={ins_refs}", file=sys.stderr, flush=True)
        if len(page_ids) < lim:
            break

    return {
        "mode": "cellar_authority",
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "docs_inserted": ins_docs,
        "refs_inserted": ins_refs,
        "stats": dict(sorted(stats.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=(
            "eu_legislation",
            "cellar_law_refs",
            "rs_eu_law",
            "echr_eu_law",
            "rs_authorities",
            "echr_authorities",
            "cellar_authority",
            "cjeu_outcomes",
            "echr_outcomes",
            "rs_dictum",
            "cjeu_status",
            "cjeu_status_sparql",
            "echr_status",
            "echr_gc_referrals",
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
    elif args.mode == "cellar_law_refs":
        summary = run_cellar_law_refs(
            client, cle, chunk_size=args.chunk_size, dry_run=dry_run,
            explain_sql=args.explain, max_cases=args.max_cases,
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
    elif args.mode == "cellar_authority":
        summary = run_cellar_authority(
            client, cle, from_id=args.from_id, to_id=args.to_id,
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
    elif args.mode == "cjeu_status":
        summary = run_cjeu_status(
            client, cle, chunk_size=args.chunk_size, dry_run=dry_run, explain_sql=args.explain,
        )
    elif args.mode == "cjeu_status_sparql":
        summary = run_cjeu_status_sparql(
            client, cle, chunk_size=args.chunk_size, dry_run=dry_run, explain_sql=args.explain,
        )
    elif args.mode == "echr_status":
        summary = run_echr_status(
            client, cle, from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain, max_cases=args.max_cases,
        )
    elif args.mode == "echr_gc_referrals":
        summary = run_echr_gc_referrals(
            client, cle, chunk_size=args.chunk_size, dry_run=dry_run, explain_sql=args.explain,
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
