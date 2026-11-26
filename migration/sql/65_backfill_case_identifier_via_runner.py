#!/usr/bin/env python3
"""Backfill cle_v2.case_identifier (roadmap R1.2a) through the Coolify sql-runner.

Follows the proven runner-transport pattern of 60/62:
- DRY_RUN=1 by default (read-only /query; refuses /execute outright).
- Supports fast targeted passes (`--corpus`):
    ecli_sql       Range-batched pure-SQL insert of cases.ecli (~3.93M rows in ~1 min)
    cjeu           CJEU cases + cjeu_document (celex, ecli, cjeu_case_number ranges)
    echr           ECHR cases via echr_document_appno (source='appno' -> echr_appno)
    rs_replaces    rs_document.replaces_identifier -> ljn & alternate ecli
    rs_zaaknummer  rs_document.zaaknummer -> nl_zaaknummer variants
    all            Original full page-by-page pass across all cases + satellites
- Normalizes identifiers using case-law-explorer's exact `identifiers.py` rules
  (single code path for weekly ETL and historical backfill).
- Insert-only (`ON CONFLICT (case_id, scheme, value_normalized) DO NOTHING`),
  resumable via --from-id / --to-id.

Env:
  SQL_RUNNER_URL      e.g. https://demo-psql.caselawexplorer.tech
  SQL_RUNNER_TOKEN    HMAC token (never printed)
  SQL_RUNNER_CONFIRM  default execute-cle-v2
  DRY_RUN             default 1; set DRY_RUN=0 to write
"""

from __future__ import annotations

import argparse
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

INSERT_CASES_ECLI_RANGE_SQL = """
INSERT INTO case_identifier (case_id, scheme, value, value_normalized, source, is_primary)
SELECT id, 'ecli', btrim(ecli), upper(btrim(ecli)), 'cases.ecli', true
  FROM cases
 WHERE id > %s AND id <= %s
   AND ecli IS NOT NULL
   AND btrim(ecli) <> ''
   AND upper(btrim(ecli)) LIKE 'ECLI:%%'
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


def _dedup_rows(
    rows: list[tuple[int, str, str, str, str, bool] | None],
) -> list[tuple[int, str, str, str, str, bool]]:
    dedup: dict[tuple[int, str, str], tuple[int, str, str, str, str, bool]] = {}
    for row in rows:
        if row is None:
            continue
        key = (row[0], row[1], row[3])
        if key not in dedup or (row[5] and not dedup[key][5]):
            dedup[key] = row
    return list(dedup.values())


def _assert_table_exists(client: SqlRunnerClient) -> None:
    exists = client.query_rows("SELECT to_regclass('case_identifier') IS NOT NULL AS ok")[0]["ok"]
    if not exists:
        raise RuntimeError("cle_v2.case_identifier does not exist (apply migration 0009 first)")


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
    raw_rows: list[tuple[int, str, str, str, str, bool] | None] = []

    for row in cases_page:
        cid = int(row["id"])
        sources = row.get("sources") or []
        raw_rows.append(_normalize_row(mod, cid, "ecli", row.get("ecli"), "cases.ecli", True, stats))
        raw_rows.append(
            _normalize_row(
                mod, cid, "celex", row.get("celex_id"), "cases.celex_id", "CJEU" in sources, stats
            )
        )
        if row.get("case_number") and "CJEU" in sources:
            for idx, num in enumerate(loader_mod.split_cjeu_case_numbers(row.get("case_number"))):
                raw_rows.append(
                    _normalize_row(
                        mod, cid, "cjeu_case_number", num, "cases.case_number", idx == 0, stats
                    )
                )

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
        raw_rows.append(_normalize_row(mod, cid, "celex", r.get("celex_id"), "cjeu_document.celex_id", False, stats))
        raw_rows.append(_normalize_row(mod, cid, "ecli", r.get("ecli"), "cjeu_document.ecli", False, stats))
        for idx, num in enumerate(loader_mod.split_cjeu_case_numbers(r.get("case_number"))):
            raw_rows.append(
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
                raw_rows.append((cid, "nl_zaaknummer", znum, variant, "rs_document.zaaknummer", False))
        rep = r.get("replaces_identifier")
        if rep:
            ecli_norm = mod.normalize_ecli(rep)
            if ecli_norm:
                stats["candidates:ecli"] += 1
                raw_rows.append((cid, "ecli", str(rep).strip(), ecli_norm, "rs_document.replaces_identifier", False))
            else:
                for ljn in loader_mod.split_ljns(rep):
                    raw_rows.append(
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
        raw_rows.append(_normalize_row(mod, cid, "echr_appno", appno, "echr_document_appno", is_primary, stats))

    return _dedup_rows(raw_rows)


def run_ecli_sql(
    client: SqlRunnerClient,
    *,
    from_id: int = 0,
    to_id: int | None = None,
    step_size: int = 8000,
    dry_run: bool = True,
    explain_sql: bool = False,
) -> dict[str, Any]:
    if not dry_run:
        _assert_table_exists(client)
    bounds = client.query_rows("SELECT min(id) AS lo, max(id) AS hi FROM cases")[0]
    if bounds["lo"] is None:
        return {"corpus": "ecli_sql", "dry_run": dry_run, "rows_inserted": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(0, from_id - 1) if from_id > 0 else 0

    started = time.monotonic()
    batches = 0
    rows_inserted = 0
    explained = False

    while cursor_id < hi:
        next_id = min(cursor_id + step_size, hi)
        if dry_run:
            if explain_sql and not explained:
                client.query(f"EXPLAIN {INSERT_CASES_ECLI_RANGE_SQL}", params=[cursor_id, next_id])
                explained = True
            cnt = client.query_rows(
                """
                SELECT count(*)::bigint AS n
                  FROM cases
                 WHERE id > %s AND id <= %s
                   AND ecli IS NOT NULL AND upper(btrim(ecli)) LIKE 'ECLI:%%'
                """,
                params=[cursor_id, next_id],
            )[0]["n"]
            rows_inserted += int(cnt)
        else:
            res = client.execute(
                INSERT_CASES_ECLI_RANGE_SQL,
                params=[cursor_id, next_id],
                dry_run=False,
            )
            rows_inserted += int(res.get("row_count") or 0)
        batches += 1
        cursor_id = next_id
        if batches % 50 == 0:
            print(
                f"[ecli_sql] cursor_id={cursor_id}/{hi} batches={batches} rows={rows_inserted}",
                file=sys.stderr,
                flush=True,
            )

    return {
        "corpus": "ecli_sql",
        "dry_run": dry_run,
        "from_id": from_id,
        "to_id": hi,
        "batches": batches,
        "rows_inserted_or_matched": rows_inserted,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def run_targeted_corpus(
    client: SqlRunnerClient,
    cle_checkout: Path,
    corpus: str,
    *,
    from_id: int = 0,
    to_id: int | None = None,
    page_size: int = 900,
    chunk_size: int = 1000,
    dry_run: bool = True,
    explain_sql: bool = False,
    max_cases: int | None = None,
) -> dict[str, Any]:
    mod, loader_mod = _load_cle_modules(cle_checkout)
    if not dry_run:
        _assert_table_exists(client)

    started = time.monotonic()
    stats: Counter[str] = Counter()
    scanned = 0
    rows_prepared = 0
    rows_inserted = 0
    explained = False
    cursor_id = max(0, from_id - 1) if from_id > 0 else 0

    if corpus == "cjeu":
        bounds = client.query_rows("SELECT max(id) AS hi FROM cases WHERE 'CJEU' = ANY(sources)")[0]
        hi = to_id if to_id is not None else int(bounds["hi"] or 0)
        while cursor_id < hi:
            lim = min(page_size, max_cases - scanned) if max_cases is not None else page_size
            if lim <= 0:
                break
            rows = client.query_rows(
                """
                SELECT c.id, c.ecli, c.celex_id, c.case_number,
                       d.celex_id AS doc_celex, d.ecli AS doc_ecli, d.case_number AS doc_case_number
                  FROM cases c
                  LEFT JOIN cjeu_document d ON d.case_id = c.id
                 WHERE 'CJEU' = ANY(c.sources)
                   AND c.id > %s AND c.id <= %s
                 ORDER BY c.id
                 LIMIT %s
                """,
                params=[cursor_id, hi, lim],
            )
            if not rows:
                break
            scanned += len(rows)
            cursor_id = int(rows[-1]["id"])
            raw: list[tuple | None] = []
            for r in rows:
                cid = int(r["id"])
                raw.append(_normalize_row(mod, cid, "ecli", r.get("ecli"), "cases.ecli", True, stats))
                raw.append(_normalize_row(mod, cid, "celex", r.get("celex_id"), "cases.celex_id", True, stats))
                raw.append(_normalize_row(mod, cid, "celex", r.get("doc_celex"), "cjeu_document.celex_id", False, stats))
                raw.append(_normalize_row(mod, cid, "ecli", r.get("doc_ecli"), "cjeu_document.ecli", False, stats))
                for idx, num in enumerate(loader_mod.split_cjeu_case_numbers(r.get("case_number"))):
                    raw.append(_normalize_row(mod, cid, "cjeu_case_number", num, "cases.case_number", idx == 0, stats))
                for idx, num in enumerate(loader_mod.split_cjeu_case_numbers(r.get("doc_case_number"))):
                    raw.append(_normalize_row(mod, cid, "cjeu_case_number", num, "cjeu_document.case_number", idx == 0, stats))
            batch = _dedup_rows(raw)
            rows_prepared += len(batch)
            if batch:
                rows_inserted += client.execute_unnest_chunks(
                    INSERT_IDENTIFIERS_SQL, list(zip(*batch)),
                    chunk_size=chunk_size, dry_run=dry_run,
                    explain_first=(dry_run and explain_sql and not explained),
                )
                explained = True
            if scanned % 10000 < len(rows):
                print(f"[cjeu] scanned={scanned} last_id={cursor_id} inserted={rows_inserted}", file=sys.stderr, flush=True)
            if len(rows) < lim:
                break

    elif corpus == "echr":
        bounds = client.query_rows("SELECT max(case_id) AS hi FROM echr_document")[0]
        hi = to_id if to_id is not None else int(bounds["hi"] or 0)
        while cursor_id < hi:
            lim = min(page_size, max_cases - scanned) if max_cases is not None else page_size
            if lim <= 0:
                break
            case_rows = client.query_rows(
                """
                SELECT DISTINCT case_id
                  FROM echr_document
                 WHERE case_id > %s AND case_id <= %s
                 ORDER BY case_id
                 LIMIT %s
                """,
                params=[cursor_id, hi, lim],
            )
            if not case_rows:
                break
            page_ids = [int(r["case_id"]) for r in case_rows]
            scanned += len(page_ids)
            cursor_id = page_ids[-1]
            rows = []
            last_item = ""
            last_appno = ""
            while True:
                sub = client.query_rows(
                    """
                    SELECT DISTINCT ed.case_id, ed.item_id, a.appno, c.case_number
                      FROM echr_document ed
                      JOIN echr_document_appno a ON a.item_id = ed.item_id
                      JOIN cases c ON c.id = ed.case_id
                     WHERE ed.case_id = ANY(%s::bigint[])
                       AND a.source = 'appno'
                       AND (ed.item_id, a.appno) > (%s, %s)
                     ORDER BY ed.item_id, a.appno
                     LIMIT 900
                    """,
                    params=[page_ids, last_item, last_appno],
                )
                if not sub:
                    break
                rows.extend(sub)
                last_item = sub[-1]["item_id"]
                last_appno = sub[-1]["appno"]
                if len(sub) < 900:
                    break
            raw = []
            for r in rows:
                cid = int(r["case_id"])
                appno = r.get("appno")
                norm_p = mod.normalize_echr_appno(r.get("case_number"))
                norm_a = mod.normalize_echr_appno(appno)
                is_primary = bool(norm_a and norm_p and norm_a == norm_p)
                raw.append(_normalize_row(mod, cid, "echr_appno", appno, "echr_document_appno", is_primary, stats))
            batch = _dedup_rows(raw)
            rows_prepared += len(batch)
            if batch:
                rows_inserted += client.execute_unnest_chunks(
                    INSERT_IDENTIFIERS_SQL, list(zip(*batch)),
                    chunk_size=chunk_size, dry_run=dry_run,
                    explain_first=(dry_run and explain_sql and not explained),
                )
                explained = True
            if scanned % 10000 < len(page_ids):
                print(f"[echr] scanned={scanned} last_case_id={cursor_id} inserted={rows_inserted}", file=sys.stderr, flush=True)
            if len(page_ids) < lim:
                break

    elif corpus in ("rs_replaces", "rs_zaaknummer"):
        col = "replaces_identifier" if corpus == "rs_replaces" else "zaaknummer"
        bounds = client.query_rows(f"SELECT max(case_id) AS hi FROM rs_document WHERE {col} IS NOT NULL")[0]
        hi = to_id if to_id is not None else int(bounds["hi"] or 0)
        while cursor_id < hi:
            lim = min(page_size, max_cases - scanned) if max_cases is not None else page_size
            if lim <= 0:
                break
            rows = client.query_rows(
                f"""
                SELECT case_id, {col}
                  FROM rs_document
                 WHERE {col} IS NOT NULL
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
            raw = []
            for r in rows:
                cid = int(r["case_id"])
                if corpus == "rs_replaces":
                    rep = r.get("replaces_identifier")
                    ecli_norm = mod.normalize_ecli(rep)
                    if ecli_norm:
                        stats["candidates:ecli"] += 1
                        raw.append((cid, "ecli", str(rep).strip(), ecli_norm, "rs_document.replaces_identifier", False))
                    else:
                        for ljn in loader_mod.split_ljns(rep):
                            raw.append(_normalize_row(mod, cid, "ljn", ljn, "rs_document.replaces_identifier", False, stats))
                else:
                    for znum in loader_mod.split_zaaknummers(r.get("zaaknummer")):
                        for variant in mod.zaaknummer_variants(znum):
                            stats["candidates:nl_zaaknummer"] += 1
                            raw.append((cid, "nl_zaaknummer", znum, variant, "rs_document.zaaknummer", False))
            batch = _dedup_rows(raw)
            rows_prepared += len(batch)
            if batch:
                rows_inserted += client.execute_unnest_chunks(
                    INSERT_IDENTIFIERS_SQL, list(zip(*batch)),
                    chunk_size=chunk_size, dry_run=dry_run,
                    explain_first=(dry_run and explain_sql and not explained),
                )
                explained = True
            if scanned % 20000 < len(rows):
                print(f"[{corpus}] scanned={scanned} last_case_id={cursor_id} inserted={rows_inserted}", file=sys.stderr, flush=True)
            if len(rows) < lim:
                break
    else:
        raise ValueError(f"Unsupported corpus: {corpus}")

    return {
        "corpus": corpus,
        "dry_run": dry_run,
        "from_id": from_id,
        "to_id": hi,
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "rows_prepared": rows_prepared,
        "rows_inserted": rows_inserted,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "stats": dict(sorted(stats.items())),
    }


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
        _assert_table_exists(client)

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
        "--corpus",
        choices=("all", "ecli_sql", "cjeu", "echr", "rs_replaces", "rs_zaaknummer"),
        default="all",
    )
    parser.add_argument(
        "--cle-checkout",
        type=Path,
        default=Path("/home/agent/projects/cle-law-and-context"),
    )
    parser.add_argument("--from-id", type=int, default=0)
    parser.add_argument("--to-id", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--chunk-size", type=int, default=1000)
    parser.add_argument("--step-size", type=int, default=8000)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args(argv)

    dry_run = os.environ.get("DRY_RUN", "1").strip().lower() not in {"0", "false", "no"}
    client = SqlRunnerClient(read_only=dry_run)
    if args.corpus == "ecli_sql":
        summary = run_ecli_sql(
            client,
            from_id=args.from_id,
            to_id=args.to_id,
            step_size=args.step_size,
            dry_run=dry_run,
            explain_sql=args.explain,
        )
    elif args.corpus == "all":
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
    else:
        summary = run_targeted_corpus(
            client,
            args.cle_checkout,
            args.corpus,
            from_id=args.from_id,
            to_id=args.to_id,
            page_size=args.page_size,
            chunk_size=args.chunk_size,
            dry_run=dry_run,
            explain_sql=args.explain,
            max_cases=args.max_cases,
        )

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
