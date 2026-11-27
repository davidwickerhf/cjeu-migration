#!/usr/bin/env python3
"""One-shot historical backfill for empty-scl ECtHR -> ECtHR fulltext citations.

Uses the exact same extractor & target resolver as case-law-explorer's weekly
enrichment_sync_etl task (data_extraction.linking.echr_text_echr + echr_edge_v2)
over cle_v2.case_text via sql-runner.

Default mode is read-only (--dry-run); pass --apply only after migration 0020
(citation_source_dataset registration for echr_text_echr) is applied and owner
go-ahead is confirmed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migration.backfill_cjeu_text_treaty import SqlRunnerClient, resolve_cle_dags  # noqa: E402


_TARGET_CANDIDATES_POS_SQL = """
    WITH matched_cases AS (
        SELECT DISTINCT d.case_id
          FROM cle_v2.echr_document_appno a
          JOIN cle_v2.echr_document d ON d.item_id = a.item_id
         WHERE a.source = 'appno'
           AND a.appno = ANY(%s)
           AND d.case_id IS NOT NULL
        UNION
        SELECT DISTINCT ci.case_id
          FROM cle_v2.case_identifier ci
         WHERE ci.scheme = 'echr_appno'
           AND ci.value_normalized = ANY(%s)
    )
    SELECT c.id,
           c.ecli,
           c.item_id,
           c.date_decision,
           bool_or(d.doctype ~* 'JUD') AS has_jud,
           bool_or(d.doctype ~* 'DEC') AS has_dec,
           bool_or(d.doctype ~* 'COM') AS has_com,
           bool_or(upper(COALESCE(d.doctype_branch, '')) = 'GRANDCHAMBER') AS is_gc,
           COALESCE(
               MAX(CASE WHEN d.doctype = 'HEJUD' THEN d.docname END),
               MAX(CASE WHEN d.doctype = 'HFJUD' THEN d.docname END),
               MAX(d.docname)
           ) AS docname,
           COALESCE(
               MAX(CASE WHEN d.doctype = 'HEJUD' THEN d.conclusion END),
               MAX(CASE WHEN d.doctype = 'HFJUD' THEN d.conclusion END),
               MAX(d.conclusion)
           ) AS conclusion,
           array_remove(array_agg(DISTINCT a.appno), NULL) AS owner_appnos,
           array_remove(array_agg(DISTINCT ci.value_normalized), NULL) AS ident_appnos
      FROM matched_cases mc
      JOIN cle_v2.cases c ON c.id = mc.case_id
      JOIN cle_v2.echr_document d ON d.case_id = c.id
      LEFT JOIN cle_v2.echr_document_appno a
        ON a.item_id = d.item_id AND a.source = 'appno'
      LEFT JOIN cle_v2.case_identifier ci
        ON ci.case_id = c.id AND ci.scheme = 'echr_appno'
     GROUP BY c.id, c.ecli, c.item_id, c.date_decision
"""


def fetch_target_candidates_http(
    client: SqlRunnerClient, norm_appnos: Set[str]
) -> Dict[str, List[Any]]:
    from data_extraction.linking import echr_edge_v2

    if not norm_appnos:
        return {}
    norm_sorted = sorted(norm_appnos)
    raw_variants = echr_edge_v2._appno_raw_variants(norm_sorted)
    rows = client.query(_TARGET_CANDIDATES_POS_SQL, [raw_variants, norm_sorted])
    appno_index: Dict[str, List[Any]] = defaultdict(list)
    for r in rows:
        kind = echr_edge_v2.classify_target_kind(
            bool(r["has_jud"]), bool(r["has_dec"]), bool(r["has_com"])
        )
        branch = "GRANDCHAMBER" if r["is_gc"] else None
        sec_kind = (
            echr_edge_v2.classify_secondary_judgment(r["docname"], r["conclusion"])
            if kind == "judgment"
            else None
        )
        cand = echr_edge_v2.TargetCandidate(
            case_id=int(r["id"]),
            ecli=r["ecli"] or None,
            item_id=r["item_id"] or None,
            date_decision=echr_edge_v2._as_date(r["date_decision"]),
            kind=kind,
            branch=branch,
            secondary_kind=sec_kind,
        )
        seen_ap: Set[str] = set()
        for raw_ap in list(r["owner_appnos"] or ()) + list(r["ident_appnos"] or ()):
            norm = echr_edge_v2.normalize_appno(raw_ap)
            if norm and norm not in seen_ap:
                seen_ap.add(norm)
                appno_index[norm].append(cand)
        if not seen_ap:
            fallback_ap = echr_edge_v2.appno_from_ecli(cand.ecli)
            if fallback_ap:
                appno_index[fallback_ap].append(cand)
    return dict(appno_index)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill echr_text_echr via sql-runner")
    ap.add_argument("--url", default="https://demo-psql.caselawexplorer.tech")
    ap.add_argument("--cle-checkout", default=None, help="Path to case-law-explorer checkout (or CLE_CHECKOUT env)")
    ap.add_argument("--batch-cases", type=int, default=8, help="Empty-scl ECHR cases per batch")
    ap.add_argument("--start-id", type=int, default=0, help="Resume scanning cases.id > start-id")
    ap.add_argument("--newest-first", action="store_true", help="Scan newest empty-scl cases first")
    ap.add_argument("--limit-cases", type=int, default=0, help="Max candidate cases (0 = all)")
    ap.add_argument("--sample", type=int, default=15, help="Number of sample edges to print")
    ap.add_argument("--apply", action="store_true", help="Write resolved rows into cle_v2.case_citation")
    args = ap.parse_args()

    resolve_cle_dags(args.cle_checkout)
    from data_extraction.linking import echr_edge_v2, echr_text_echr
    from data_extraction.linking.artifact import CaseKey

    token = os.getenv("SQL_RUNNER_TOKEN")
    if not token:
        print("SQL_RUNNER_TOKEN is required (run via anvil-secrets run)", file=sys.stderr)
        return 2

    client = SqlRunnerClient(args.url, token)

    stats: Counter = Counter()
    rule_counts: Counter = Counter()
    samples: List[Dict[str, Any]] = []
    cases_scanned = 0
    cases_with_hits = 0
    total_edges = 0
    total_inserted = 0

    cursor_id = args.start_id if not args.newest_first else 10**12
    while True:
        remaining = (args.limit_cases - cases_scanned) if args.limit_cases else args.batch_cases
        if args.limit_cases and remaining <= 0:
            break
        fetch_n = min(args.batch_cases, remaining) if args.limit_cases else args.batch_cases

        if args.newest_first:
            src_rows = client.query(
                "SELECT c.id, c.ecli, c.item_id, c.date_decision "
                "FROM cle_v2.cases c "
                "JOIN cle_v2.echr_document ed ON ed.case_id = c.id "
                "WHERE c.ecli LIKE 'ECLI:CE:ECHR:%%' "
                "  AND c.id < %s "
                "  AND (ed.scl IS NULL OR trim(ed.scl) = '') "
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM cle_v2.case_citation cc "
                "       WHERE cc.source_case_id = c.id "
                "         AND cc.source_dataset IN ('echr_edge_v2', 'echr_text_echr')"
                "  ) "
                "  AND EXISTS ("
                "      SELECT 1 FROM cle_v2.case_text ct "
                "       WHERE ct.case_id = c.id AND ct.fulltext IS NOT NULL"
                "  ) "
                "GROUP BY c.id, c.ecli, c.item_id, c.date_decision "
                "ORDER BY c.id DESC LIMIT %s",
                [cursor_id, fetch_n],
            )
        else:
            src_rows = client.query(
                "SELECT c.id, c.ecli, c.item_id, c.date_decision "
                "FROM cle_v2.cases c "
                "JOIN cle_v2.echr_document ed ON ed.case_id = c.id "
                "WHERE c.ecli LIKE 'ECLI:CE:ECHR:%%' "
                "  AND c.id > %s "
                "  AND (ed.scl IS NULL OR trim(ed.scl) = '') "
                "  AND NOT EXISTS ("
                "      SELECT 1 FROM cle_v2.case_citation cc "
                "       WHERE cc.source_case_id = c.id "
                "         AND cc.source_dataset IN ('echr_edge_v2', 'echr_text_echr')"
                "  ) "
                "  AND EXISTS ("
                "      SELECT 1 FROM cle_v2.case_text ct "
                "       WHERE ct.case_id = c.id AND ct.fulltext IS NOT NULL"
                "  ) "
                "GROUP BY c.id, c.ecli, c.item_id, c.date_decision "
                "ORDER BY c.id ASC LIMIT %s",
                [cursor_id, fetch_n],
            )
        if not src_rows:
            break

        batch_ids = [int(r["id"]) for r in src_rows]
        cursor_id = batch_ids[-1]
        cases_scanned += len(batch_ids)

        own_rows = client.query(
            "SELECT ed.case_id, a.appno "
            "FROM cle_v2.echr_document ed "
            "JOIN cle_v2.echr_document_appno a ON a.item_id = ed.item_id "
            "WHERE ed.case_id = ANY(%s) AND a.source = 'appno'",
            [batch_ids],
        )
        own_by_case: Dict[int, Set[str]] = {cid: set() for cid in batch_ids}
        for r in own_rows:
            norm = echr_edge_v2.normalize_appno(r["appno"])
            if norm:
                own_by_case.setdefault(int(r["case_id"]), set()).add(norm)

        text_rows = client.query(
            "SELECT case_id, language, fulltext "
            "FROM cle_v2.case_text "
            "WHERE case_id = ANY(%s) AND fulltext IS NOT NULL "
            "ORDER BY case_id, language, id",
            [batch_ids],
        )
        texts_by_case: Dict[int, List[Tuple[Optional[str], str]]] = {cid: [] for cid in batch_ids}
        for r in text_rows:
            texts_by_case.setdefault(int(r["case_id"]), []).append((r["language"], r["fulltext"]))

        needed_appnos: Set[str] = set()
        for r in src_rows:
            cid = int(r["id"])
            own = own_by_case[cid]
            ecli_app = echr_edge_v2.appno_from_ecli(r["ecli"])
            if ecli_app:
                own.add(ecli_app)
            for _lang, text in texts_by_case.get(cid, ()):
                for clause in echr_text_echr.extract_citation_clauses(text, own):
                    needed_appnos.update(clause.appnos)

        owner_index = fetch_target_candidates_http(client, needed_appnos)
        target_id_by_ecli: Dict[str, int] = {}
        target_id_by_item: Dict[str, int] = {}
        for cands in owner_index.values():
            for cand in cands:
                if cand.ecli:
                    target_id_by_ecli[cand.ecli] = cand.case_id
                if cand.item_id:
                    target_id_by_item[cand.item_id] = cand.case_id

        insert_payload: List[Dict[str, Any]] = []
        for r in src_rows:
            cid = int(r["id"])
            key = CaseKey(ecli=r["ecli"] or None, item_id=r["item_id"] or None)
            recs = echr_text_echr.resolve_case_fulltext(
                source_case_id=cid,
                source_key=key,
                source_date=echr_edge_v2._as_date(r["date_decision"]),
                own_appnos=own_by_case.get(cid, set()),
                texts=texts_by_case.get(cid, ()),
                owner_by_appno=owner_index,
                stats=stats,
            )
            if not recs:
                continue
            cases_with_hits += 1
            for rec in recs:
                tid = None
                if rec.target and rec.target.ecli:
                    tid = target_id_by_ecli.get(rec.target.ecli)
                if tid is None and rec.target and rec.target.item_id:
                    tid = target_id_by_item.get(rec.target.item_id)
                if tid is None or tid == cid:
                    continue
                total_edges += 1
                rule_counts[rec.match_method] += 1
                if len(samples) < args.sample:
                    samples.append({
                        "source_ecli": r["ecli"],
                        "target_ecli": rec.target.ecli if rec.target else None,
                        "appno": rec.target_ref_raw,
                        "method": rec.match_method,
                        "snippet": rec.mentions[0].raw_text if rec.mentions else "",
                    })
                insert_payload.append({
                    "source_case_id": cid,
                    "target_case_id": tid,
                    "target_ref_raw": rec.target_ref_raw,
                    "target_ref_scheme": rec.target_ref_scheme,
                    "relation_type": rec.relation_type,
                    "source_dataset": rec.source_dataset,
                    "weight": 1,
                    "is_cross_jurisdiction": False,
                    "extractor_version": rec.extractor_version,
                    "match_method": rec.match_method,
                    "confidence": rec.confidence,
                })

        if args.apply and insert_payload:
            res = client.execute(
                "INSERT INTO cle_v2.case_citation ("
                "  source_case_id, target_case_id, target_ref_raw, target_ref_scheme,"
                "  relation_type, source_dataset, weight, is_cross_jurisdiction,"
                "  extractor_version, match_method, confidence"
                ") "
                "SELECT (x->>'source_case_id')::bigint, (x->>'target_case_id')::bigint,"
                "       x->>'target_ref_raw', x->>'target_ref_scheme',"
                "       x->>'relation_type', x->>'source_dataset', (x->>'weight')::int,"
                "       (x->>'is_cross_jurisdiction')::boolean,"
                "       x->>'extractor_version', x->>'match_method', (x->>'confidence')::real "
                "FROM jsonb_array_elements(%s::jsonb) AS x "
                "ON CONFLICT (source_case_id, target_case_id, relation_type, source_dataset) "
                "    WHERE target_case_id IS NOT NULL "
                "DO NOTHING",
                [json.dumps(insert_payload)],
            )
            total_inserted += int(res.get("row_count") or 0)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(json.dumps({
        "mode": mode,
        "cases_scanned": cases_scanned,
        "cases_with_hits": cases_with_hits,
        "edges": total_edges,
        "inserted": total_inserted,
        "last_cursor_id": cursor_id,
        "by_method": dict(rule_counts.most_common()),
        "stats": dict(stats),
        "samples": samples,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
