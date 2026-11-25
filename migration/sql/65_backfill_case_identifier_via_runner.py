#!/usr/bin/env python3
"""Backfill cle_v2.case_identifier (roadmap R1.2a) through the Coolify sql-runner.

Follows the proven runner-transport pattern of 60/62:
- DRY_RUN=1 by default (read-only /query; refuses /execute outright).
- Keyset-paginates cases by id (LIMIT 500) over POST /query.
- Normalizes identifiers using case-law-explorer's exact `identifiers.py` rules
  (single code path for weekly ETL and historical backfill).
- Inserts into cle_v2.case_identifier via batched
  `INSERT ... SELECT FROM unnest(...) ON CONFLICT (case_id, scheme, value_normalized) DO NOTHING`
  over POST /execute, with automatic chunk bisection on timeouts.
- Resumable via --from-id / --to-id.

Env:
  SQL_RUNNER_URL      e.g. https://demo-psql.caselawexplorer.tech
  SQL_RUNNER_TOKEN    HMAC token (never printed)
  SQL_RUNNER_CONFIRM  default execute-cle-v2
  DRY_RUN             default 1; set DRY_RUN=0 to write
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "sql"))

from sql_runner_client import SqlRunnerClient  # noqa: E402

INSERT_IDENTIFIERS_SQL = """
INSERT INTO case_identifier (case_id, scheme, value, value_normalized, source, is_primary)
SELECT *
  FROM unnest(
      %s::bigint[],
      %s::text[],
      %s::text[],
      %s::text[],
      %s::text[],
      %s::boolean[]
  )
ON CONFLICT (case_id, scheme, value_normalized) DO NOTHING
"""


def _load_cle_modules(cle_checkout: Path):
    dags_dir = str(cle_checkout / "airflow" / "dags")
    if dags_dir not in sys.path:
        sys.path.insert(0, dags_dir)
    from data_extraction.linking import identifiers
    from data_loading import identifier_loader

    return identifiers, identifier_loader


def _normalize_row(
    mod,
    case_id: int,
    scheme: str,
    raw_value: str | None,
    source: str,
    is_primary: bool,
    stats: Counter[str],
) -> tuple[int, str, str, str, str, bool] | None:
    if not raw_value or not str(raw_value).strip():
        return None
    raw = str(raw_value).strip()
    norm = mod.normalize(scheme, raw)
    if not norm:
        stats[f"rejected:{scheme}"] += 1
        return None
    stats[f"candidates:{scheme}"] += 1
    return (case_id, scheme, raw, norm, source, is_primary)


def collect_for_case_page(
    client: SqlRunnerClient,
    mod,
    loader_mod,
    cases_page: list[dict[str, Any]],
    stats: Counter[str],
) -> list[tuple[int, str, str, str, str, bool]]:
    if not cases_page:
        return []
    ids = [int(r["id"]) for r in cases_page]
    case_primary_num = {int(r["id"]): (r.get("case_number") or "").strip() for r in cases_page}
    dedup: dict[tuple[int, str, str], tuple[int, str, str, str, str, bool]] = {}

    def add(row: tuple[int, str, str, str, str, bool] | None) -> None:
        if row is None:
            return
        key = (row[0], row[1], row[3])
        if key not in dedup or (row[5] and not dedup[key][5]):
            dedup[key] = row

    for row in cases_page:
        cid = int(row["id"])
        sources = row.get("sources") or []
        add(_normalize_row(mod, cid, "ecli", row.get("ecli"), "cases.ecli", True, stats))
        add(
            _normalize_row(
                mod, cid, "celex", row.get("celex_id"), "cases.celex_id", "CJEU" in sources, stats
            )
        )
        if row.get("case_number") and "CJEU" in sources:
            for idx, num in enumerate(loader_mod.split_cjeu_case_numbers(row.get("case_number"))):
                add(
                    _normalize_row(
                        mod, cid, "cjeu_case_number", num, "cases.case_number", idx == 0, stats
                    )
                )

    # Satellite identifiers for the exact id list of this page (always <= 500 cases).
    cjeu_rows = client.query_rows(
        """
        SELECT case_id, celex_id, ecli, case_number
          FROM cjeu_document
         WHERE case_id = ANY(%s::bigint[])
        """,
        params=[ids],
    )
    for r in cjeu_rows:
        cid = int(r["case_id"])
        add(_normalize_row(mod, cid, "celex", r.get("celex_id"), "cjeu_document.celex_id", False, stats))
        add(_normalize_row(mod, cid, "ecli", r.get("ecli"), "cjeu_document.ecli", False, stats))
        for idx, num in enumerate(loader_mod.split_cjeu_case_numbers(r.get("case_number"))):
            add(
                _normalize_row(
                    mod, cid, "cjeu_case_number", num, "cjeu_document.case_number", idx == 0, stats
                )
            )

    rs_rows = client.query_rows(
        """
        SELECT case_id, zaaknummer, replaces_identifier
          FROM rs_document
         WHERE case_id = ANY(%s::bigint[])
           AND (zaaknummer IS NOT NULL OR replaces_identifier IS NOT NULL)
        """,
        params=[ids],
    )
    for r in rs_rows:
        cid = int(r["case_id"])
        for znum in loader_mod.split_zaaknummers(r.get("zaaknummer")):
            for variant in mod.zaaknummer_variants(znum):
                stats["candidates:nl_zaaknummer"] += 1
                add((cid, "nl_zaaknummer", znum, variant, "rs_document.zaaknummer", False))
        rep = r.get("replaces_identifier")
        if rep:
            ecli_norm = mod.normalize_ecli(rep)
            if ecli_norm:
                stats["candidates:ecli"] += 1
                add((cid, "ecli", str(rep).strip(), ecli_norm, "rs_document.replaces_identifier", False))
            else:
                for ljn in loader_mod.split_ljns(rep):
                    add(
                        _normalize_row(
                            mod, cid, "ljn", ljn, "rs_document.replaces_identifier", False, stats
                        )
                    )

    appno_rows = client.query_rows(
        """
        SELECT ed.case_id, a.appno
          FROM echr_document ed
          JOIN echr_document_appno a ON a.item_id = ed.item_id
         WHERE ed.case_id = ANY(%s::bigint[])
           AND a.source = 'appno'
        """,
        params=[ids],
    )
    for r in appno_rows:
        cid = int(r["case_id"])
        appno = r.get("appno")
        norm_primary = mod.normalize_echr_appno(case_primary_num.get(cid))
        norm_appno = mod.normalize_echr_appno(appno)
        is_primary = bool(norm_appno and norm_primary and norm_appno == norm_primary)
        add(_normalize_row(mod, cid, "echr_appno", appno, "echr_document_appno", is_primary, stats))

    return list(dedup.values())


def run_backfill(
    client: SqlRunnerClient,
    cle_checkout: Path,
    *,
    from_id: int = 0,
    to_id: int | None = None,
    page_size: int = 500,
    chunk_size: int = 1000,
    dry_run: bool = True,
    explain_sql: bool = False,
    max_cases: int | None = None,
) -> dict[str, Any]:
    mod, loader_mod = _load_cle_modules(cle_checkout)

    if not dry_run:
        exists = client.query_rows(
            "SELECT to_regclass('case_identifier') IS NOT NULL AS ok"
        )[0]["ok"]
        if not exists:
            raise RuntimeError("cle_v2.case_identifier does not exist (apply migration 0009 first)")

    bounds = client.query_rows("SELECT min(id) AS lo, max(id) AS hi FROM cases")[0]
    if bounds["lo"] is None:
        return {"cases_scanned": 0, "rows_prepared": 0, "rows_inserted": 0, "stats": {}}

    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(0, from_id - 1) if from_id > 0 else 0

    stats: Counter[str] = Counter()
    cases_scanned = 0
    rows_prepared = 0
    rows_inserted = 0
    explained = False
    started = time.monotonic()

    while True:
        remaining = (max_cases - cases_scanned) if max_cases is not None else page_size
        if remaining <= 0:
            break
        limit = min(page_size, remaining)
        page = client.query_rows(
            """
            SELECT id, ecli, celex_id, case_number, sources
              FROM cases
             WHERE id > %s AND id <= %s
             ORDER BY id
             LIMIT %s
            """,
            params=[cursor_id, hi, limit],
        )
        if not page:
            break
        cases_scanned += len(page)
        cursor_id = int(page[-1]["id"])

        batch = collect_for_case_page(client, mod, loader_mod, page, stats)
        rows_prepared += len(batch)
        if batch:
            cols = list(zip(*batch))
            written = client.execute_unnest_chunks(
                INSERT_IDENTIFIERS_SQL,
                cols,
                chunk_size=chunk_size,
                dry_run=dry_run,
                explain_first=(dry_run and explain_sql and not explained),
            )
            explained = True
            rows_inserted += written

        if len(page) < limit:
            break

    return {
        "dry_run": dry_run,
        "from_id": from_id,
        "to_id": hi,
        "last_id_scanned": cursor_id,
        "cases_scanned": cases_scanned,
        "rows_prepared": rows_prepared,
        "rows_inserted": rows_inserted,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "stats": dict(sorted(stats.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cle-checkout",
        type=Path,
        default=Path("/home/agent/projects/cle-law-and-context"),
    )
    parser.add_argument("--from-id", type=int, default=0)
    parser.add_argument("--to-id", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args(argv)

    dry_run = os.environ.get("DRY_RUN", "1").strip().lower() not in {"0", "false", "no"}
    client = SqlRunnerClient(read_only=dry_run)
    summary = run_backfill(
        client,
        args.cle_checkout,
        from_id=args.from_id,
        to_id=args.to_id,
        page_size=args.page_size,
        chunk_size=args.chunk_size,
        dry_run=dry_run,
        explain_sql=args.explain,
        max_cases=args.max_cases,
    )
    import json

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
