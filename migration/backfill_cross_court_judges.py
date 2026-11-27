#!/usr/bin/env python3
"""Historical backfill for cross-court judges & Advocates General (ECtHR + Rechtspraak).

Uses the exact same parsers as case-law-explorer's weekly enrichment_sync_etl task
(data_extraction.case_context.judges) over cle_v2.case_text via sql-runner, fetching
only LEFT(fulltext, 4000) for ECtHR and RIGHT(fulltext, 3000) for Rechtspraak so
batches of 150-200 cases run cleanly over HTTP.

Default mode is read-only (--dry-run); pass --apply only after owner go-ahead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migration.backfill_cjeu_text_treaty import SqlRunnerClient, resolve_cle_dags  # noqa: E402


_CORPUS_SPECS: Dict[str, Tuple[Optional[str], str, str]] = {
    # corpus -> (court.code or None for rs_document, case_text.source, slice_expr)
    "echr": ("ECTHR", "HUDOC", "LEFT(ct.fulltext, 4000)"),
    "rs-phr": (
        "RS:Parket bij de Hoge Raad",
        "RECHTSPRAAK",
        "LEFT(ct.fulltext, 1500) || E'\\n\\n' || RIGHT(ct.fulltext, 3000)",
    ),
    "rs-hr": ("RS:Hoge Raad", "RECHTSPRAAK", "RIGHT(ct.fulltext, 3000)"),
    "rs-all": (
        None,
        "RECHTSPRAAK",
        "CASE WHEN c.ecli LIKE 'ECLI:NL:PHR:%%' "
        "THEN LEFT(ct.fulltext, 1500) || E'\\n\\n' || RIGHT(ct.fulltext, 3000) "
        "ELSE RIGHT(ct.fulltext, 3000) END",
    ),
}


def run_corpus(
    client: SqlRunnerClient,
    judges_mod: Any,
    corpus: str,
    *,
    batch_cases: int,
    start_id: int,
    newest_first: bool,
    limit_cases: int,
    sample_limit: int,
    apply: bool,
) -> Dict[str, Any]:
    court_code, text_source, slice_expr = _CORPUS_SPECS[corpus]
    is_echr = corpus == "echr"

    court_id: Optional[int] = None
    if court_code is not None:
        crows = client.query("SELECT id FROM cle_v2.court WHERE code = %s LIMIT 1", [court_code])
        if not crows:
            raise RuntimeError(f"Court code {court_code!r} not found in cle_v2.court")
        court_id = int(crows[0]["id"])

    cases_scanned = 0
    cases_with_hits = 0
    total_records = 0
    total_judges_inserted = 0
    total_links_inserted = 0
    by_dataset: Counter = Counter()
    samples: List[Dict[str, Any]] = []

    cursor_id = start_id if not newest_first else 10**12
    cmp_op = "<" if newest_first else ">"
    order_dir = "DESC" if newest_first else "ASC"

    while True:
        remaining = (limit_cases - cases_scanned) if limit_cases else batch_cases
        if limit_cases and remaining <= 0:
            break
        fetch_n = min(batch_cases, remaining) if limit_cases else batch_cases

        # Step 1: fast indexed keyset page of candidate case IDs (materialized court_id
        # lookup prevents Postgres from walking cases_pkey across 1.5M unrelated rows).
        if court_id is not None:
            id_rows = client.query(
                "WITH pool AS MATERIALIZED ("
                "    SELECT c.id, c.ecli "
                "      FROM cle_v2.cases c "
                "     WHERE c.court_id = %s "
                f"      AND c.id {cmp_op} %s"
                ") "
                "SELECT p.id, p.ecli "
                "  FROM pool p "
                " WHERE NOT EXISTS (SELECT 1 FROM cle_v2.case_judge cj WHERE cj.case_id = p.id) "
                "   AND EXISTS ("
                "       SELECT 1 FROM cle_v2.case_text ct "
                "        WHERE ct.case_id = p.id AND ct.source = %s AND ct.fulltext IS NOT NULL"
                "   ) "
                f"ORDER BY p.id {order_dir} "
                "LIMIT %s",
                [court_id, cursor_id, text_source, fetch_n],
            )
        else:
            id_rows = client.query(
                "SELECT c.id, c.ecli "
                "  FROM cle_v2.rs_document rd "
                "  JOIN cle_v2.cases c ON c.id = rd.case_id "
                f"WHERE rd.case_id {cmp_op} %s "
                "  AND NOT EXISTS (SELECT 1 FROM cle_v2.case_judge cj WHERE cj.case_id = rd.case_id) "
                "  AND EXISTS ("
                "      SELECT 1 FROM cle_v2.case_text ct "
                "       WHERE ct.case_id = rd.case_id AND ct.source = %s AND ct.fulltext IS NOT NULL"
                "  ) "
                f"ORDER BY rd.case_id {order_dir} "
                "LIMIT %s",
                [cursor_id, text_source, fetch_n],
            )

        if not id_rows:
            break

        batch_ids = [int(r["id"]) for r in id_rows]
        ecli_by_id = {int(r["id"]): (r["ecli"] or "") for r in id_rows}
        cursor_id = batch_ids[-1]
        cases_scanned += len(batch_ids)

        # Step 2: fetch only the leading/trailing text slice for this exact batch of IDs.
        if is_echr:
            text_rows = client.query(
                f"SELECT DISTINCT ON (ct.case_id) ct.case_id, {slice_expr} AS snippet "
                "  FROM cle_v2.case_text ct "
                " WHERE ct.case_id = ANY(%s) "
                "   AND ct.source = %s "
                "   AND ct.fulltext IS NOT NULL "
                " ORDER BY ct.case_id, (ct.language = 'en') DESC, ct.id",
                [batch_ids, text_source],
            )
        else:
            text_rows = client.query(
                f"SELECT DISTINCT ON (ct.case_id) ct.case_id, {slice_expr} AS snippet "
                "  FROM cle_v2.case_text ct "
                "  JOIN cle_v2.cases c ON c.id = ct.case_id "
                " WHERE ct.case_id = ANY(%s) "
                "   AND ct.source = %s "
                "   AND ct.fulltext IS NOT NULL "
                " ORDER BY ct.case_id, ct.id",
                [batch_ids, text_source],
            )

        batch_names: set[str] = set()
        link_payload: List[Dict[str, Any]] = []

        for r in text_rows:
            cid = int(r["case_id"])
            ecli = ecli_by_id.get(cid, "")
            snippet = r["snippet"] or ""
            recs = (
                judges_mod.extract_echr_judges(snippet, ecli)
                if is_echr
                else judges_mod.extract_rs_judges(snippet, ecli)
            )
            if not recs:
                continue
            cases_with_hits += 1
            for rec in recs:
                total_records += 1
                by_dataset[rec.source_dataset] += 1
                batch_names.add(rec.name)
                if len(samples) < sample_limit:
                    samples.append({
                        "ecli": ecli,
                        "name": rec.name,
                        "role": rec.role,
                        "source_dataset": rec.source_dataset,
                    })
                link_payload.append({
                    "case_id": cid,
                    "name": rec.name,
                    "role": rec.role,
                    "source_dataset": rec.source_dataset,
                    "extractor_version": rec.extractor_version,
                    "confidence": rec.confidence,
                })

        if apply and link_payload:
            res_j = client.execute(
                "INSERT INTO cle_v2.judge (full_name) "
                "SELECT DISTINCT v "
                "FROM jsonb_array_elements_text(%s::jsonb) AS v "
                "WHERE NOT EXISTS ("
                "    SELECT 1 FROM cle_v2.judge j WHERE lower(j.full_name) = lower(v)"
                ")",
                [json.dumps(sorted(batch_names), ensure_ascii=False)],
            )
            total_judges_inserted += int(res_j.get("row_count") or 0)

            res_cj = client.execute(
                "INSERT INTO cle_v2.case_judge ("
                "  case_id, judge_id, role, source_dataset, extractor_version, confidence"
                ") "
                "SELECT (x->>'case_id')::bigint, j.id, x->>'role', x->>'source_dataset', "
                "       x->>'extractor_version', (x->>'confidence')::real "
                "FROM jsonb_array_elements(%s::jsonb) AS x "
                "JOIN LATERAL ("
                "    SELECT id FROM cle_v2.judge j2 "
                "     WHERE lower(j2.full_name) = lower(x->>'name') "
                "     ORDER BY id LIMIT 1"
                ") j ON true "
                "ON CONFLICT (case_id, judge_id, role) DO NOTHING",
                [json.dumps(link_payload, ensure_ascii=False)],
            )
            total_links_inserted += int(res_cj.get("row_count") or 0)

    return {
        "corpus": corpus,
        "cases_scanned": cases_scanned,
        "cases_with_hits": cases_with_hits,
        "records": total_records,
        "judges_inserted": total_judges_inserted,
        "case_judges_inserted": total_links_inserted,
        "last_cursor_id": cursor_id,
        "by_dataset": dict(by_dataset.most_common()),
        "samples": samples,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill ECtHR & Rechtspraak judges/AGs via sql-runner")
    ap.add_argument("--url", default="https://demo-psql.caselawexplorer.tech")
    ap.add_argument("--cle-checkout", default=None, help="Path to case-law-explorer checkout (or CLE_CHECKOUT env)")
    ap.add_argument(
        "--corpus",
        choices=("all", "echr", "rs-phr", "rs-hr", "rs-all"),
        default="all",
        help="Target corpus ('all' runs echr, rs-phr, rs-hr in sequence)",
    )
    ap.add_argument("--batch-cases", type=int, default=150, help="Cases fetched per HTTP batch (LEFT/RIGHT slice)")
    ap.add_argument("--start-id", type=int, default=0, help="Resume scanning cases.id > start-id (single corpus)")
    ap.add_argument("--newest-first", action="store_true", help="Scan newest cases first (for sampling/quick sweep)")
    ap.add_argument("--limit-cases", type=int, default=0, help="Max cases per corpus (0 = all)")
    ap.add_argument("--sample", type=int, default=10, help="Number of sample hits to print per corpus")
    ap.add_argument("--apply", action="store_true", help="Write rows into cle_v2.judge and cle_v2.case_judge")
    args = ap.parse_args()

    resolve_cle_dags(args.cle_checkout)
    from data_extraction.case_context import judges

    token = os.getenv("SQL_RUNNER_TOKEN")
    if not token:
        print("SQL_RUNNER_TOKEN is required (run via anvil-secrets run)", file=sys.stderr)
        return 2

    client = SqlRunnerClient(args.url, token)
    corpora = ["echr", "rs-phr", "rs-hr"] if args.corpus == "all" else [args.corpus]
    results = [
        run_corpus(
            client,
            judges,
            c,
            batch_cases=args.batch_cases,
            start_id=args.start_id,
            newest_first=args.newest_first,
            limit_cases=args.limit_cases,
            sample_limit=args.sample,
            apply=args.apply,
        )
        for c in corpora
    ]
    print(json.dumps({
        "mode": "APPLIED" if args.apply else "DRY-RUN",
        "results": results,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
