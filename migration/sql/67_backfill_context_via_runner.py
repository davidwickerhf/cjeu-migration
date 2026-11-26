#!/usr/bin/env python3
"""Backfill Phases 3-5 context tables (EU legislation, outcomes) via sql-runner.

Writes strictly to additive/new rows (`ON CONFLICT ... DO NOTHING`), never
touching existing rows or live citation counts. DRY_RUN=1 by default.

Modes (`--mode`):
  eu_legislation   R3.1: seed 63 curated EU/CoE/UN acts + aliases into
                   legislation / legislation_alias (requires migration 0011)
  cjeu_outcomes    R5.1: parse cjeu_document.proc_type -> case_outcome (requires 0009)
  echr_outcomes    R5.1: parse echr_document violation/nonviolation/conclusion -> case_outcome

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
      %s::date[], %s::int[], %s::text[], %s::text[], %s::text[]
  ) AS u(
      scheme, identifier, jurisdiction_code, document_type, title,
      enacted_date, enacted_year, eli, source_dataset, extractor_version
  )
  JOIN jurisdiction j ON j.code = u.jurisdiction_code
ON CONFLICT (scheme, identifier)
WHERE scheme IN ('celex', 'coe_treaty', 'un_treaty')
DO NOTHING
"""

INSERT_ALIASES_SQL = """
INSERT INTO legislation_alias (legislation_id, alias)
SELECT l.id, u.alias
  FROM unnest(%s::text[], %s::text[], %s::text[]) AS u(scheme, identifier, alias)
  JOIN legislation l ON l.scheme = u.scheme AND l.identifier = u.identifier
ON CONFLICT (legislation_id, alias) DO NOTHING
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


def _load_cle(cle_checkout: Path):
    dags_dir = str(cle_checkout / "airflow" / "dags")
    if dags_dir not in sys.path:
        sys.path.insert(0, dags_dir)
    from data_extraction.case_context import cjeu_outcomes, echr_outcomes
    from data_loading import legislation_loader

    return {
        "legislation_loader": legislation_loader,
        "cjeu_outcomes": cjeu_outcomes,
        "echr_outcomes": echr_outcomes,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("eu_legislation", "cjeu_outcomes", "echr_outcomes"), required=True)
    parser.add_argument("--cle-checkout", type=Path, default=Path("/home/agent/projects/cle-law-and-context"))
    parser.add_argument("--from-id", type=int, default=0)
    parser.add_argument("--to-id", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=500)
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
    elif args.mode == "cjeu_outcomes":
        summary = run_cjeu_outcomes(
            client, cle, from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain, max_cases=args.max_cases,
        )
    else:
        summary = run_echr_outcomes(
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
