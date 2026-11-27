#!/usr/bin/env python3
"""One-shot historical backfill for ECtHR -> international treaty citations.

Uses the exact same scanner as case-law-explorer's weekly enrichment_sync_etl task
(data_extraction.linking.echr_treaty + treaty_matcher over treaties_catalog.json)
against cle_v2.case_text via sql-runner.

Default mode is read-only (--dry-run); pass --apply only after migrations 0020
(case_law_reference_uk_echr_treaty) and 0021 (international treaty catalog) are
applied and owner go-ahead is confirmed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migration.backfill_cjeu_text_treaty import (  # noqa: E402
    SqlRunnerClient,
    load_catalog_maps,
    resolve_cle_dags,
    select_tsquery_groups,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill echr_text_treaty via sql-runner")
    ap.add_argument("--url", default="https://demo-psql.caselawexplorer.tech")
    ap.add_argument("--cle-checkout", default=None, help="Path to case-law-explorer checkout (or CLE_CHECKOUT env)")
    ap.add_argument("--batch-cases", type=int, default=6, help="ECHR cases fetched per text query")
    ap.add_argument("--start-id", type=int, default=None, help="Resume scanning cases.id (> or < start-id)")
    ap.add_argument("--newest-first", action="store_true", help="Scan newest ECHR cases first (for sampling)")
    ap.add_argument("--group", type=int, default=None, help="Optional single tsquery group index (default: all groups)")
    ap.add_argument("--limit-cases", type=int, default=0, help="Max candidate cases (0 = all)")
    ap.add_argument("--sample", type=int, default=15, help="Number of sample hits to print")
    ap.add_argument("--allow-partial-catalog", action="store_true", help="Allow --apply even before migration 0021 seeds intl_treaty")
    ap.add_argument("--apply", action="store_true", help="Write resolved rows into cle_v2.case_law_reference")
    args = ap.parse_args()

    resolve_cle_dags(args.cle_checkout)
    from data_extraction.linking import echr_treaty, treaty_matcher

    token = os.getenv("SQL_RUNNER_TOKEN")
    if not token:
        print("SQL_RUNNER_TOKEN is required (run via anvil-secrets run)", file=sys.stderr)
        return 2

    client = SqlRunnerClient(args.url, token)

    # 1. Load catalog-scoped legislation & provision lookup maps across all non-ECHR treaty schemes.
    catalog_entries = [e for e in treaty_matcher.load_catalog() if not e.skip_in_echr]
    leg_map, prov_map = load_catalog_maps(client, catalog_entries)
    if args.apply and not args.allow_partial_catalog:
        has_intl = any(scheme == "intl_treaty" for scheme, _ in leg_map)
        if not has_intl:
            raise SystemExit(
                "cle_v2.legislation has no 'intl_treaty' rows yet. Apply migration "
                "0021_international_treaty_catalog.sql first, or pass --allow-partial-catalog."
            )

    # Resolve ECTHR court_id once for fast materialized pagination (avoids cases_pkey scan).
    crows = client.query("SELECT id FROM cle_v2.court WHERE code = 'ECTHR' LIMIT 1")
    echr_court_id = int(crows[0]["id"]) if crows else None

    # 2. Keyset-paginate ECHR cases in chunks of 2,000 and filter each chunk against
    #    case_text.fulltext_tsv across all selected tsquery groups.
    tsq_groups = select_tsquery_groups(
        treaty_matcher.candidate_tsquery_groups(skip_echr_core=True),
        args.group,
    )
    if args.start_id is not None:
        cursor_id = args.start_id
    else:
        cursor_id = 10**12 if args.newest_first else 0
    cmp_op = "<" if args.newest_first else ">"
    order_dir = "DESC" if args.newest_first else "ASC"

    candidate_ids: List[int] = []
    seen_cids: Set[int] = set()
    chunks_scanned = 0
    while True:
        if echr_court_id is not None:
            chunk_rows = client.query(
                "WITH pool AS MATERIALIZED ("
                f"  SELECT c.id FROM cle_v2.cases c WHERE c.court_id = %s AND c.id {cmp_op} %s"
                f") SELECT id FROM pool ORDER BY id {order_dir} LIMIT 2000",
                [echr_court_id, cursor_id],
            )
        else:
            chunk_rows = client.query(
                "SELECT c.id FROM cle_v2.cases c "
                f"WHERE c.ecli LIKE 'ECLI:CE:ECHR:%%' AND c.id {cmp_op} %s "
                f"ORDER BY c.id {order_dir} LIMIT 2000",
                [cursor_id],
            )
        if not chunk_rows:
            break
        chunks_scanned += 1
        chunk = [int(r["id"]) for r in chunk_rows]
        cursor_id = chunk[-1]
        chunk_matched: List[int] = []
        for tsq in tsq_groups:
            matched = client.query(
                "SELECT DISTINCT ct.case_id "
                "FROM cle_v2.case_text ct "
                "WHERE ct.case_id = ANY(%s) "
                "  AND ct.source = 'HUDOC' "
                "  AND ct.fulltext_tsv @@ to_tsquery('simple', %s)",
                [chunk, tsq],
            )
            for r in matched:
                cid = int(r["case_id"])
                if cid not in seen_cids:
                    seen_cids.add(cid)
                    chunk_matched.append(cid)
        chunk_matched.sort(reverse=args.newest_first)
        candidate_ids.extend(chunk_matched)
        if args.limit_cases and len(candidate_ids) >= args.limit_cases:
            candidate_ids = candidate_ids[: args.limit_cases]
            break

    print(
        f"Candidate ECHR cases matching treaty keywords: {len(candidate_ids)} "
        f"(groups={len(tsq_groups)}, chunks={chunks_scanned}, catalog_in_db={len(leg_map)}/{len(catalog_entries)})"
    )

    treaty_counts: Counter = Counter()
    pending_counts: Counter = Counter()
    samples: List[Dict[str, Any]] = []
    cases_with_hits = 0
    total_records = 0
    total_pending = 0
    total_inserted = 0

    for offset in range(0, len(candidate_ids), args.batch_cases):
        chunk_ids = candidate_ids[offset : offset + args.batch_cases]
        text_rows = client.query(
            "SELECT c.id, c.ecli, ct.language, ct.fulltext "
            "FROM cle_v2.case_text ct "
            "JOIN cle_v2.cases c ON c.id = ct.case_id "
            "WHERE c.id = ANY(%s) AND ct.source = 'HUDOC' AND ct.fulltext IS NOT NULL "
            "ORDER BY c.id, ct.language, ct.id",
            [chunk_ids],
        )
        by_case: Dict[int, Tuple[Optional[str], List[Tuple[Optional[str], str]]]] = {}
        for r in text_rows:
            cid = int(r["id"])
            entry = by_case.setdefault(cid, (r["ecli"], []))
            entry[1].append((r["language"], r["fulltext"]))

        insert_payload: List[Dict[str, Any]] = []
        for cid in sorted(by_case):
            ecli, texts = by_case[cid]
            recs = echr_treaty.records_for_case(ecli, texts)
            if not recs:
                continue
            cases_with_hits += 1
            for rec in recs:
                lid = leg_map.get((rec.scheme, rec.identifier))
                treaty_key = f"{rec.scheme}/{rec.identifier}"
                if lid is None:
                    total_pending += 1
                    pending_counts[treaty_key] += 1
                    if len(samples) < args.sample:
                        samples.append({
                            "ecli": ecli,
                            "treaty": treaty_key,
                            "subdivision": rec.raw_subdivision,
                            "in_db": False,
                            "raw_reference": rec.raw_reference,
                        })
                    continue
                pid = prov_map.get((lid, rec.article.lower())) if rec.article else None
                total_records += 1
                treaty_counts[treaty_key] += 1
                if len(samples) < args.sample:
                    samples.append({
                        "ecli": ecli,
                        "treaty": treaty_key,
                        "subdivision": rec.raw_subdivision,
                        "in_db": True,
                        "raw_reference": rec.raw_reference,
                    })
                insert_payload.append({
                    "case_id": cid,
                    "legislation_id": lid,
                    "provision_id": pid,
                    "raw_scheme": rec.scheme,
                    "raw_resource": rec.identifier,
                    "raw_subdivision": rec.raw_subdivision,
                    "raw_reference": rec.raw_reference,
                    "role": rec.role,
                    "source_dataset": rec.source_dataset,
                    "extractor_version": rec.extractor_version,
                    "match_method": rec.match_method,
                    "confidence": rec.confidence,
                })

        if args.apply and insert_payload:
            res = client.execute(
                "INSERT INTO cle_v2.case_law_reference ("
                "  case_id, legislation_id, provision_id,"
                "  raw_scheme, raw_resource, raw_subdivision, raw_reference,"
                "  role, source_dataset, extractor_version, match_method, confidence"
                ") "
                "SELECT (x->>'case_id')::bigint, (x->>'legislation_id')::bigint, "
                "       NULLIF(x->>'provision_id','')::bigint, "
                "       x->>'raw_scheme', x->>'raw_resource', NULLIF(x->>'raw_subdivision',''), "
                "       x->>'raw_reference', x->>'role', x->>'source_dataset', "
                "       x->>'extractor_version', x->>'match_method', (x->>'confidence')::real "
                "FROM jsonb_array_elements(%s::jsonb) AS x "
                "ON CONFLICT (case_id, raw_scheme, raw_resource, COALESCE(raw_subdivision, ''), role, source_dataset) "
                "WHERE raw_resource IS NOT NULL AND (legislation_id IS NOT NULL OR provision_id IS NOT NULL) "
                "  AND source_dataset = 'echr_text_treaty' "
                "DO NOTHING",
                [json.dumps(insert_payload)],
            )
            total_inserted += int(res.get("row_count") or 0)

    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(json.dumps({
        "mode": mode,
        "last_cursor_id": cursor_id,
        "candidates": len(candidate_ids),
        "cases_with_hits": cases_with_hits,
        "records_resolved_in_db": total_records,
        "records_pending_0021": total_pending,
        "inserted": total_inserted,
        "by_treaty_in_db": dict(treaty_counts.most_common()),
        "by_treaty_pending_0021": dict(pending_counts.most_common()),
        "samples": samples,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
