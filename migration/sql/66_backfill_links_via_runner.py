#!/usr/bin/env python3
"""Backfill Phase 2 dark cross-dataset links into cle_v2.case_citation via sql-runner.

Safety gates enforced before any write:
1. DRY_RUN=1 is the default (read-only /query; refuses /execute outright).
2. Before writing any case_citation row, verifies that migration 0015 (count guard)
   is applied AND that every target source_dataset is registered in
   citation_source_dataset with counted = false AND active_in_api = false.
   Refuses to write if any dataset is counted or active.
3. Captures case_citation_counts checksums before and after the run and asserts
   zero unintended count movement.
4. Insert-only via batched `INSERT ... SELECT FROM unnest(...) ON CONFLICT ... DO NOTHING`
   matching the exact partial unique indexes of 0001 / 0010, with automatic chunk
   bisection on statement timeout.

Modes (`--mode`):
  ag_opinions   R2.4: AG opinion -> judgment (cjeu_ag_link / opinion_of)
  rs_text       R2.1: RS fulltext + summary -> ECLI / CJEU case number / ECtHR appno
  echr_text     R2.2: HUDOC fulltext -> CJEU case numbers & NL ECLIs
  cjeu_text     R2.6: CJEU fulltext -> ECtHR ECLIs & appnos

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
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "sql"))

from sql_runner_client import SqlRunnerClient  # noqa: E402

MODE_DATASETS = {
    "ag_opinions": ("cjeu_ag_link",),
    "rs_text": ("rs_text_ecli", "rs_text_cjeu_casenum", "rs_text_echr_appno"),
    "echr_text": ("echr_text_cjeu", "echr_text_rs"),
    "cjeu_text": ("cjeu_text_echr",),
}

INSERT_RESOLVED_SQL = """
INSERT INTO case_citation (
    source_case_id, target_case_id, relation_type, source_dataset,
    is_cross_jurisdiction, extractor_version, match_method, confidence
)
SELECT *
  FROM unnest(
      %s::bigint[],
      %s::bigint[],
      %s::text[],
      %s::text[],
      %s::boolean[],
      %s::text[],
      %s::text[],
      %s::real[]
  )
ON CONFLICT (source_case_id, target_case_id, relation_type, source_dataset)
WHERE target_case_id IS NOT NULL
DO NOTHING
"""

INSERT_UNRESOLVED_ECLI_SQL = """
INSERT INTO case_citation (
    source_case_id, target_ecli_raw, relation_type, source_dataset,
    is_cross_jurisdiction, extractor_version, match_method, confidence
)
SELECT *
  FROM unnest(
      %s::bigint[],
      %s::text[],
      %s::text[],
      %s::text[],
      %s::boolean[],
      %s::text[],
      %s::text[],
      %s::real[]
  )
ON CONFLICT (source_case_id, target_ecli_raw, relation_type, source_dataset)
WHERE target_case_id IS NULL AND target_ecli_raw IS NOT NULL
DO NOTHING
"""

INSERT_UNRESOLVED_REF_SQL = """
INSERT INTO case_citation (
    source_case_id, target_ref_scheme, target_ref_raw, relation_type, source_dataset,
    is_cross_jurisdiction, extractor_version, match_method, confidence
)
SELECT *
  FROM unnest(
      %s::bigint[],
      %s::text[],
      %s::text[],
      %s::text[],
      %s::text[],
      %s::boolean[],
      %s::text[],
      %s::text[],
      %s::real[]
  )
ON CONFLICT (source_case_id, target_ref_scheme, target_ref_raw, COALESCE(relation_type, ''), source_dataset)
WHERE target_case_id IS NULL AND target_ref_raw IS NOT NULL
DO NOTHING
"""


def _load_cle(cle_checkout: Path):
    dags_dir = str(cle_checkout / "airflow" / "dags")
    if dags_dir not in sys.path:
        sys.path.insert(0, dags_dir)
    from data_extraction.linking import ag_opinions, cjeu_text, echr_text, resolver, rs_text

    return {
        "ag_opinions": ag_opinions,
        "rs_text": rs_text,
        "echr_text": echr_text,
        "cjeu_text": cjeu_text,
        "resolver": resolver,
    }


def _parse_date(val: Any) -> dt.date | None:
    if not val:
        return None
    if isinstance(val, dt.date):
        return val
    try:
        return dt.date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


def assert_dark_guard(client: SqlRunnerClient, datasets: tuple[str, ...]) -> None:
    """Refuse to write unless 0015 is applied and all target datasets are dark."""
    has_0015 = client.query_rows(
        "SELECT EXISTS (SELECT 1 FROM public.schema_migrations WHERE filename = '0015_citation_count_guard.sql') AS ok"
    )[0]["ok"]
    if not has_0015:
        raise RuntimeError(
            "Migration 0015_citation_count_guard.sql is not applied; refusing to write to case_citation"
        )
    rows = client.query_rows(
        """
        SELECT code, counted, active_in_api
          FROM citation_source_dataset
         WHERE code = ANY(%s::text[])
        """,
        params=[list(datasets)],
    )
    by_code = {r["code"]: r for r in rows}
    for code in datasets:
        info = by_code.get(code)
        if not info:
            raise RuntimeError(f"Dataset {code!r} is not registered in citation_source_dataset")
        if info["counted"] or info["active_in_api"]:
            raise RuntimeError(
                f"Dataset {code!r} is NOT dark (counted={info['counted']}, active_in_api={info['active_in_api']})"
            )


def counts_checksum(client: SqlRunnerClient) -> dict[str, int]:
    row = client.query_rows(
        """
        SELECT count(*)::bigint AS rows,
               COALESCE(sum(cites_count), 0)::bigint AS sum_cites,
               COALESCE(sum(cited_by_count), 0)::bigint AS sum_cited_by
          FROM case_citation_counts
        """
    )[0]
    return {k: int(v) for k, v in row.items()}


def load_identifier_index(
    client: SqlRunnerClient,
    resolver_mod,
    schemes: tuple[str, ...],
) -> tuple[dict[str, int], dict[tuple[str, str], list[Any]]]:
    """Load target case_identifier lookup tables into memory via keyset /query."""
    ecli_to_id: dict[str, int] = {}
    candidates: dict[tuple[str, str], list[Any]] = defaultdict(list)

    for scheme in schemes:
        last_id = 0
        while True:
            rows = client.query_rows(
                """
                SELECT ci.id, ci.case_id, ci.value_normalized,
                       c.ecli, c.celex_id, c.item_id, c.date_decision, c.sources
                  FROM case_identifier ci
                  JOIN cases c ON c.id = ci.case_id
                 WHERE ci.scheme = %s
                   AND ci.id > %s
                 ORDER BY ci.id
                 LIMIT 900
                """,
                params=[scheme, last_id],
            )
            if not rows:
                break
            for r in rows:
                cid = int(r["case_id"])
                val = r["value_normalized"]
                if scheme == "ecli":
                    ecli_to_id[val] = cid
                else:
                    celex = r.get("celex_id") or ""
                    kind = celex[5:7] if len(celex) >= 7 else ("JUD" if "JUD" in (r.get("ecli") or "") else "DEC")
                    candidates[(scheme, val)].append(
                        resolver_mod.Candidate(
                            case_id=cid,
                            ecli=r.get("ecli"),
                            celex=r.get("celex_id"),
                            item_id=r.get("item_id"),
                            date=_parse_date(r.get("date_decision")),
                            kind=kind,
                            value=val,
                        )
                    )
            last_id = int(rows[-1]["id"])
            if len(rows) < 900:
                break
    return ecli_to_id, candidates


def run_ag_opinions(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    dry_run: bool,
    chunk_size: int,
    explain_sql: bool,
    max_cases: int | None,
) -> dict[str, Any]:
    ag = cle["ag_opinions"]
    # Build CELEX -> case_id and case_number -> [(case_id, celex, date)] for CJEU judgments/orders
    celex_map: dict[str, int] = {}
    last_id = 0
    while True:
        rows = client.query_rows(
            """
            SELECT id, celex_id
              FROM cases
             WHERE celex_id IS NOT NULL AND id > %s
             ORDER BY id LIMIT 900
            """,
            params=[last_id],
        )
        if not rows:
            break
        for r in rows:
            celex_map[str(r["celex_id"]).upper()] = int(r["id"])
        last_id = int(rows[-1]["id"])
        if len(rows) < 900:
            break

    resolved_rows: list[tuple] = []
    stats: Counter[str] = Counter()
    last_id = 0
    scanned = 0
    while True:
        lim = 500 if max_cases is None else min(500, max_cases - scanned)
        if lim <= 0:
            break
        rows = client.query_rows(
            """
            SELECT id, celex_id
              FROM cases
             WHERE celex_id ~ '^6[0-9]{4}CC'
               AND id > %s
             ORDER BY id LIMIT %s
            """,
            params=[last_id, lim],
        )
        if not rows:
            break
        scanned += len(rows)
        last_id = int(rows[-1]["id"])
        for r in rows:
            src_id = int(r["id"])
            celex = str(r["celex_id"]).upper()
            target_id = None
            method = None
            for letters in ("CJ", "CO"):
                for cand in ag.swapped_celex(celex, letters):
                    if cand in celex_map and celex_map[cand] != src_id:
                        target_id = celex_map[cand]
                        method = f"celex/swapped_cc_{letters.lower()}"
                        break
                if target_id is not None:
                    break
            if target_id is not None:
                stats["resolved"] += 1
                resolved_rows.append(
                    (
                        src_id,
                        target_id,
                        ag.RELATION,
                        ag.SOURCE_DATASET,
                        False,
                        ag.EXTRACTOR_VERSION,
                        method,
                        0.98,
                    )
                )
            else:
                stats["unresolved"] += 1
        if len(rows) < lim:
            break

    inserted = 0
    if resolved_rows:
        cols = list(zip(*resolved_rows))
        inserted = client.execute_unnest_chunks(
            INSERT_RESOLVED_SQL,
            cols,
            chunk_size=chunk_size,
            dry_run=dry_run,
            explain_first=(dry_run and explain_sql),
        )
    return {
        "mode": "ag_opinions",
        "scanned": scanned,
        "prepared_resolved": len(resolved_rows),
        "inserted_resolved": inserted,
        "stats": dict(stats),
    }


def run_rs_text(
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
    rs_mod = cle["rs_text"]
    res_mod = cle["resolver"]
    ecli_to_id, candidates = load_identifier_index(
        client, res_mod, ("ecli", "cjeu_case_number", "echr_appno")
    )

    bounds = client.query_rows(
        "SELECT min(id) AS lo, max(id) AS hi FROM cases WHERE 'RS' = ANY(sources)"
    )[0]
    if bounds["lo"] is None:
        return {"mode": "rs_text", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(0, from_id - 1) if from_id > 0 else 0

    stats: Counter[str] = Counter()
    scanned = 0
    inserted_res = 0
    inserted_ue = 0
    inserted_ur = 0
    explained = False

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        rows = client.query_rows(
            """
            SELECT c.id, c.ecli, c.date_decision, ct.language, ct.fulltext, ct.summary
              FROM cases c
              JOIN case_text ct ON ct.case_id = c.id AND ct.source = 'RECHTSPRAAK'
             WHERE 'RS' = ANY(c.sources)
               AND c.id > %s AND c.id <= %s
               AND (ct.fulltext IS NOT NULL OR ct.summary IS NOT NULL)
             ORDER BY c.id
             LIMIT %s
            """,
            params=[cursor_id, hi, lim],
        )
        if not rows:
            break
        scanned += len(rows)
        cursor_id = int(rows[-1]["id"])

        res_batch: dict[tuple, tuple] = {}
        ue_batch: dict[tuple, tuple] = {}
        ur_batch: dict[tuple, tuple] = {}

        for row in rows:
            src_id = int(row["id"])
            src_ecli = row.get("ecli")
            decided = _parse_date(row.get("date_decision"))
            texts = []
            if row.get("fulltext"):
                texts.append(("fulltext", row.get("language") or "nl", row["fulltext"]))
            if row.get("summary"):
                texts.append(("summary", row.get("language") or "nl", row["summary"]))
            src = rs_mod.SourceCase(ecli=src_ecli, decision_date=decided, texts=texts)
            for hit in rs_mod.scan_case(src):
                if hit.scheme == "ecli":
                    # Default cross-court only for backfill (skip NL->NL already in rs_body_cite)
                    if hit.value.startswith("ECLI:NL:") or hit.value == src_ecli:
                        continue
                    tid = ecli_to_id.get(hit.value)
                    if tid and tid != src_id:
                        key = (src_id, tid, "cites", rs_mod.DATASET_ECLI)
                        res_batch[key] = (
                            src_id, tid, "cites", rs_mod.DATASET_ECLI, True,
                            rs_mod.EXTRACTOR_VERSION, "ecli/text", 0.99,
                        )
                        stats["ecli:resolved"] += 1
                    elif not tid:
                        key = (src_id, hit.value, "cites", rs_mod.DATASET_ECLI)
                        ue_batch[key] = (
                            src_id, hit.value, "cites", rs_mod.DATASET_ECLI, True,
                            rs_mod.EXTRACTOR_VERSION, "ecli/text", 0.99,
                        )
                        stats["ecli:unresolved"] += 1
                elif hit.scheme == "cjeu_case_number":
                    cands = candidates.get(("cjeu_case_number", hit.value), [])
                    choice = res_mod.choose_cjeu(cands, decided, opinion_cue=hit.opinion_cue)
                    if choice and choice.candidate.case_id != src_id:
                        key = (src_id, choice.candidate.case_id, "cites", rs_mod.DATASET_CJEU)
                        res_batch[key] = (
                            src_id, choice.candidate.case_id, "cites", rs_mod.DATASET_CJEU, True,
                            rs_mod.EXTRACTOR_VERSION, f"case_number/{choice.rule}", choice.confidence,
                        )
                        stats["cjeu_casenum:resolved"] += 1
                    elif not cands:
                        key = (src_id, "cjeu_case_number", hit.value, "cites", rs_mod.DATASET_CJEU)
                        ur_batch[key] = (
                            src_id, "cjeu_case_number", hit.value, "cites", rs_mod.DATASET_CJEU, True,
                            rs_mod.EXTRACTOR_VERSION, "case_number/unresolved", 0.85,
                        )
                        stats["cjeu_casenum:unresolved"] += 1
                elif hit.scheme == "echr_appno":
                    cands = candidates.get(("echr_appno", hit.value), [])
                    choice = res_mod.choose_echr(
                        cands, decided,
                        decision_cue=hit.decision_cue,
                        cited_date=hit.cited_date,
                        cited_year=hit.cited_year,
                        grand_chamber=hit.grand_chamber,
                    )
                    if choice and choice.candidate.case_id != src_id:
                        key = (src_id, choice.candidate.case_id, "cites", rs_mod.DATASET_ECHR)
                        res_batch[key] = (
                            src_id, choice.candidate.case_id, "cites", rs_mod.DATASET_ECHR, True,
                            rs_mod.EXTRACTOR_VERSION, f"appno/{choice.rule}", choice.confidence,
                        )
                        stats["echr_appno:resolved"] += 1
                    elif not cands:
                        key = (src_id, "echr_appno", hit.value, "cites", rs_mod.DATASET_ECHR)
                        ur_batch[key] = (
                            src_id, "echr_appno", hit.value, "cites", rs_mod.DATASET_ECHR, True,
                            rs_mod.EXTRACTOR_VERSION, "appno/unresolved", 0.85,
                        )
                        stats["echr_appno:unresolved"] += 1

        if res_batch:
            inserted_res += client.execute_unnest_chunks(
                INSERT_RESOLVED_SQL, list(zip(*res_batch.values())),
                chunk_size=chunk_size, dry_run=dry_run, explain_first=(dry_run and explain_sql and not explained),
            )
            explained = True
        if ue_batch:
            inserted_ue += client.execute_unnest_chunks(
                INSERT_UNRESOLVED_ECLI_SQL, list(zip(*ue_batch.values())),
                chunk_size=chunk_size, dry_run=dry_run,
            )
        if ur_batch:
            inserted_ur += client.execute_unnest_chunks(
                INSERT_UNRESOLVED_REF_SQL, list(zip(*ur_batch.values())),
                chunk_size=chunk_size, dry_run=dry_run,
            )

        if len(rows) < lim:
            break

    return {
        "mode": "rs_text",
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "inserted_resolved": inserted_res,
        "inserted_unresolved_ecli": inserted_ue,
        "inserted_unresolved_ref": inserted_ur,
        "stats": dict(sorted(stats.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("ag_opinions", "rs_text"), required=True)
    parser.add_argument("--cle-checkout", type=Path, default=Path("/home/agent/projects/cle-law-and-context"))
    parser.add_argument("--from-id", type=int, default=0)
    parser.add_argument("--to-id", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args(argv)

    dry_run = os.environ.get("DRY_RUN", "1").strip().lower() not in {"0", "false", "no"}
    client = SqlRunnerClient(read_only=dry_run)
    cle = _load_cle(args.cle_checkout)

    before = counts_checksum(client)
    if not dry_run:
        assert_dark_guard(client, MODE_DATASETS[args.mode])

    started = time.monotonic()
    if args.mode == "ag_opinions":
        summary = run_ag_opinions(
            client, cle, dry_run=dry_run, chunk_size=args.chunk_size,
            explain_sql=args.explain, max_cases=args.max_cases,
        )
    else:
        summary = run_rs_text(
            client, cle, from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain, max_cases=args.max_cases,
        )

    after = counts_checksum(client)
    if not dry_run and before != after:
        raise RuntimeError(f"COUNT GUARD VIOLATION: before={before} after={after}")

    summary["dry_run"] = dry_run
    summary["elapsed_seconds"] = round(time.monotonic() - started, 2)
    summary["counts_checksum_before"] = before
    summary["counts_checksum_after"] = after
    summary["counts_unchanged"] = before == after
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
