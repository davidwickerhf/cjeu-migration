#!/usr/bin/env python3
"""Backfill cle_v2.case_identifier (roadmap R1.2a) by case-id range.

The work is done by case-law-explorer's loader
(airflow/dags/data_loading/identifier_loader.py), the same code the weekly
RS, CJEU and ECtHR loads run behind CASE_IDENTIFIER_ENABLED, so history and
new data are filled by one code path (roadmap ground rule 4). This script
only pages through cases.id and calls

    python -m data_loading.identifier_loader --dsn-env VAR --from-id N --to-id M

once per range from the case-law-explorer checkout, appending each range's
counts per scheme (inserted, already_present, rejected, unresolved) to the
run log in migration/verify/case_identifier_backfill.md.

Where it runs: on the ETL worker, against db-dev, with a direct Postgres
connection. Unlike 59-64 it does not go through the sql-runner: the loader
reads several tables per range and inserts with execute_values, which the
runner's 1,000-row result cap and /query-/execute split do not fit. Only
db-dev is written; production gets the rows through the promoter or a replay
after the db-dev verification (see the verify file).

Safety:

- DRY_RUN=1 is the default: read-only, prints the ranges and how many cases
  each holds; nothing is written and the loader is not called.
- Insert-only (ON CONFLICT (case_id, scheme, value_normalized) DO NOTHING).
  Nothing is updated or deleted, so a range can be rerun at any time and an
  interrupted run resumes with --from-id.
- Refuses to write when cle_v2.case_identifier does not exist (migration
  0009 not deployed).
- One transaction per --batch-size window of ids inside the loader; a failed
  range stops the run and is logged, ranges already done stay committed.
- The DSN is read from the environment variable named by --dsn-env and never
  printed or logged.

Usage (ETL worker):

    export CLE_DEV_DSN=postgresql://...        # db-dev, never production
    DRY_RUN=1 python migration/sql/65_backfill_case_identifier.py \\
        --dsn-env CLE_DEV_DSN --cle-checkout /opt/case-law-explorer
    DRY_RUN=0 python migration/sql/65_backfill_case_identifier.py \\
        --dsn-env CLE_DEV_DSN --cle-checkout /opt/case-law-explorer \\
        --python /opt/airflow-venv/bin/python

Options: --from-id/--to-id (default: min/max cases.id), --range-size (ids
per loader call, default 50,000), --batch-size (ids per transaction inside
the loader, default 5,000), --log (default the verify file above).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG = ROOT / "migration" / "verify" / "case_identifier_backfill.md"
SCHEMES = ("ecli", "celex", "cjeu_case_number", "echr_appno", "nl_zaaknummer", "ljn")
LOG_HEADER = (
    "| started (UTC) | from_id | to_id | scheme | candidates | inserted | already_present "
    "| rejected | unresolved | seconds |\n"
    "|---|---|---|---|---|---|---|---|---|---|\n"
)
RUN_LOG_MARKER = "<!-- run log: 65_backfill_case_identifier.py appends below -->"


def connect(dsn_env: str):
    import psycopg2

    dsn = os.environ.get(dsn_env)
    if not dsn:
        sys.exit(f"environment variable {dsn_env} is not set")
    return psycopg2.connect(dsn)


def id_bounds(conn) -> tuple[int, int]:
    with conn.cursor() as cursor:
        cursor.execute("SELECT min(id), max(id) FROM cle_v2.cases")
        low, high = cursor.fetchone()
    conn.rollback()
    if low is None:
        sys.exit("cle_v2.cases is empty")
    return int(low), int(high)


def table_exists(conn) -> bool:
    with conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('cle_v2.case_identifier') IS NOT NULL")
        (exists,) = cursor.fetchone()
    conn.rollback()
    return bool(exists)


def cases_in(conn, low: int, high: int) -> int:
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM cle_v2.cases WHERE id BETWEEN %s AND %s", (low, high))
        (count,) = cursor.fetchone()
    conn.rollback()
    return int(count)


def ranges(low: int, high: int, size: int):
    start = low
    while start <= high:
        end = min(start + size - 1, high)
        yield start, end
        start = end + 1


def run_loader(args, low: int, high: int) -> dict:
    """One loader call; returns its JSON counts. Raises on failure."""
    dags = Path(args.cle_checkout) / "airflow" / "dags"
    if not (dags / "data_loading" / "identifier_loader.py").exists():
        sys.exit(f"{dags} has no data_loading/identifier_loader.py; wrong --cle-checkout?")
    command = [
        args.python,
        "-m",
        "data_loading.identifier_loader",
        "--dsn-env",
        args.dsn_env,
        "--from-id",
        str(low),
        "--to-id",
        str(high),
        "--batch-size",
        str(args.batch_size),
    ]
    completed = subprocess.run(
        command, cwd=dags, env=os.environ.copy(), capture_output=True, text=True
    )
    if completed.returncode != 0:
        tail = "\n".join(completed.stderr.strip().splitlines()[-20:])
        raise RuntimeError(
            f"loader failed for ids {low}-{high} (exit {completed.returncode}):\n{tail}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def ensure_log(path: Path) -> None:
    if not path.exists():
        path.write_text(f"# case_identifier backfill\n\n{RUN_LOG_MARKER}\n", encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if RUN_LOG_MARKER not in text:
        text = text.rstrip("\n") + f"\n\n{RUN_LOG_MARKER}\n"
        path.write_text(text, encoding="utf-8")


def append_run_header(path: Path, args, low: int, high: int) -> None:
    started = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            f"\n### Run {started} UTC: ids {low}-{high}, range {args.range_size}, "
            f"batch {args.batch_size}, DSN from ${args.dsn_env}\n\n{LOG_HEADER}"
        )


def append_range(path: Path, started: str, low: int, high: int, result: dict, seconds: float):
    lines = []
    schemes = result.get("schemes", {})
    for scheme in SCHEMES:
        c = schemes.get(scheme, {})
        lines.append(
            f"| {started} | {low} | {high} | {scheme} | {c.get('candidates', 0)} "
            f"| {c.get('inserted', 0)} | {c.get('already_present', 0)} "
            f"| {c.get('rejected', 0)} | {c.get('unresolved', 0)} | {seconds:.0f} |\n"
        )
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(lines)


def append_examples(path: Path, examples: dict) -> None:
    if not examples:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\nRejected and unresolved examples (first 20 per kind):\n\n```json\n")
        handle.write(json.dumps(examples, ensure_ascii=False, indent=1, sort_keys=True))
        handle.write("\n```\n")


def merge(total: dict, part: dict) -> None:
    for scheme, counts in part.get("schemes", {}).items():
        bucket = total["schemes"].setdefault(scheme, {})
        for name, value in counts.items():
            bucket[name] = bucket.get(name, 0) + value
    for key, examples in part.get("examples", {}).items():
        kept = total["examples"].setdefault(key, [])
        kept.extend(examples[: 20 - len(kept)])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dsn-env", required=True, help="env var holding the db-dev DSN")
    parser.add_argument("--cle-checkout", required=True, help="case-law-explorer checkout path")
    parser.add_argument("--python", default=sys.executable, help="interpreter with psycopg2")
    parser.add_argument("--from-id", type=int)
    parser.add_argument("--to-id", type=int)
    parser.add_argument("--range-size", type=int, default=50_000)
    parser.add_argument("--batch-size", type=int, default=5_000)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    args = parser.parse_args(argv)
    dry_run = os.environ.get("DRY_RUN", "1") != "0"

    conn = connect(args.dsn_env)
    try:
        low, high = id_bounds(conn)
        low = args.from_id if args.from_id is not None else low
        high = args.to_id if args.to_id is not None else high
        if high < low:
            sys.exit("--to-id is below --from-id")
        exists = table_exists(conn)
        plan = list(ranges(low, high, args.range_size))
        print(f"ids {low}-{high}: {len(plan)} ranges of {args.range_size}", flush=True)
        if dry_run:
            for start, end in plan:
                print(f"  {start}-{end}: {cases_in(conn, start, end)} cases")
            print(
                "DRY_RUN: nothing written. case_identifier "
                + ("exists" if exists else "is MISSING (deploy migration 0009 first)")
            )
            return 0
        if not exists:
            sys.exit("cle_v2.case_identifier does not exist; deploy migration 0009 first")
    finally:
        conn.close()

    ensure_log(args.log)
    append_run_header(args.log, args, low, high)
    total = {"schemes": {}, "examples": {}}
    for start, end in plan:
        started = dt.datetime.now(dt.timezone.utc)
        try:
            result = run_loader(args, start, end)
        except Exception as error:
            with args.log.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\nFAILED at ids {start}-{end}; rerun with --from-id {start}.\n\n```\n"
                    f"{error}\n```\n"
                )
            append_examples(args.log, total["examples"])
            print(error, file=sys.stderr)
            return 1
        seconds = (dt.datetime.now(dt.timezone.utc) - started).total_seconds()
        append_range(args.log, started.strftime("%Y-%m-%d %H:%M"), start, end, result, seconds)
        merge(total, result)
        print(f"  {start}-{end}: {json.dumps(result['schemes'], sort_keys=True)}", flush=True)
    with args.log.open("a", encoding="utf-8") as handle:
        handle.write("\nTotals:\n\n```json\n")
        handle.write(json.dumps(total["schemes"], indent=1, sort_keys=True))
        handle.write("\n```\n")
    append_examples(args.log, total["examples"])
    print(json.dumps(total["schemes"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
