#!/usr/bin/env python3
"""Sync ALL unresolved EU legislation CELEX acts from EUR-Lex CELLAR SPARQL into cle_v2.

1. Discovers every distinct CELEX in `case_law_reference` (`raw_scheme='celex'`,
   `legislation_id IS NULL`) — normalizing treaty-article CELEXes (e.g. `12008E267`
   -> `12008E/TXT`) and consolidated texts (`02016R0679-20160504` -> `32016R0679`)
   via `eu_law.cellar_target` so acts resolve cleanly to canonical CELEX entries.
2. Fetches official English title (falling back to French), enacted date, ELI, and
   resource type in batches of 150 from EUR-Lex CELLAR's public SPARQL endpoint.
3. Inserts discovered acts into `cle_v2.legislation` (`scheme='celex'`,
   `jurisdiction='EU'`, `source_dataset='cellar_legislation_sparql'`) with
   `ON CONFLICT (scheme, identifier) WHERE scheme IN ('celex', 'coe_treaty', 'un_treaty') DO NOTHING`.
4. Resolves existing unresolved `case_law_reference` rows (`legislation_id IS NULL`)
   in safe <=4,000-row batches (well under sql-runner's 10,000-row write cap),
   avoiding any conflict with existing resolved twins.

Env:
  SQL_RUNNER_URL      e.g. https://demo-psql.caselawexplorer.tech
  SQL_RUNNER_TOKEN    HMAC token (never printed)
  DRY_RUN             default 1; set DRY_RUN=0 to write
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "sql"))

from sql_runner_client import SqlRunnerClient  # noqa: E402

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"
SOURCE_DATASET = "cellar_legislation_sparql"
EXTRACTOR_VERSION = "cellar_legislation_sync/1.0.0"

RESOURCE_TYPE_MAP = {
    "REG": "regulation",
    "REG_IMPL": "regulation",
    "REG_DEL": "regulation",
    "REG_FINANC": "regulation",
    "DIR": "directive",
    "DIR_IMPL": "directive",
    "DIR_DEL": "directive",
    "DEC": "decision",
    "DEC_IMPL": "decision",
    "DEC_DEL": "decision",
    "DEC_FRAMW": "framework_decision",
    "DEC_ENTSCHEID": "decision",
    "TREATY": "treaty",
    "PROT": "protocol",
    "CONVENTION": "convention",
    "AGREE_INTERNATION": "agreement",
    "AGREE_INTERNAL": "agreement",
    "RECO": "recommendation",
    "OPIN": "opinion",
    "RES": "resolution",
    "RULES_PROC": "rules_of_procedure",
    "STATUTE": "statute",
}

CELEX_TYPE_FALLBACK = {
    "R": "regulation",
    "L": "directive",
    "D": "decision",
    "F": "framework_decision",
    "Q": "rules_of_procedure",
    "A": "agreement",
    "X": "other",
    "E": "treaty",
    "M": "treaty",
    "C": "treaty",
    "P": "charter",
}

INSERT_LEGISLATION_SQL = """
INSERT INTO legislation (
    scheme, identifier, jurisdiction_id, document_type, title,
    enacted_date, enacted_year, eli, source_dataset, extractor_version
)
SELECT
    'celex', u.identifier, j.id, u.document_type, u.title,
    u.enacted_date, u.enacted_year, u.eli, u.source_dataset, u.extractor_version
  FROM unnest(
      %s::text[], %s::text[], %s::text[], %s::date[], %s::smallint[],
      %s::text[], %s::text[], %s::text[]
  ) AS u(
      identifier, document_type, title, enacted_date, enacted_year,
      eli, source_dataset, extractor_version
  )
  JOIN jurisdiction j ON j.iso_code = 'EU'
ON CONFLICT (scheme, identifier)
WHERE scheme IN ('celex', 'coe_treaty', 'un_treaty')
DO NOTHING
"""

RESOLVE_BATCH_SQL = """
UPDATE case_law_reference clr
   SET legislation_id = m.leg_id
  FROM unnest(%s::bigint[], %s::bigint[]) AS m(clr_id, leg_id)
 WHERE clr.id = m.clr_id
   AND clr.legislation_id IS NULL
   AND NOT EXISTS (
       SELECT 1
         FROM case_law_reference dup
        WHERE dup.case_id = clr.case_id
          AND dup.raw_scheme = clr.raw_scheme
          AND dup.raw_resource = clr.raw_resource
          AND COALESCE(dup.raw_subdivision, '') = COALESCE(clr.raw_subdivision, '')
          AND dup.role = clr.role
          AND dup.source_dataset = clr.source_dataset
          AND (dup.legislation_id IS NOT NULL OR dup.provision_id IS NOT NULL)
   )
"""


def _load_eu_law(cle_checkout: Path):
    dags_dir = str(cle_checkout / "airflow" / "dags")
    if dags_dir not in sys.path:
        sys.path.insert(0, dags_dir)
    from data_extraction.linking import eu_law

    return eu_law


def _fallback_doc_type(celex: str) -> str:
    if len(celex) >= 6:
        if celex[0] == "1":
            return "protocol" if "/PRO" in celex else "treaty"
        return CELEX_TYPE_FALLBACK.get(celex[5].upper(), "eu_act")
    return "eu_act"


def _year_from_celex(celex: str) -> int | None:
    m = re.match(r"^[0-9CE](\d{4})", celex)
    if not m:
        return None
    y = int(m.group(1))
    return y if 1950 <= y <= 2100 else None


def fetch_cellar_metadata(
    celexes: list[str],
    *,
    batch_size: int = 120,
    pause: float = 0.5,
) -> dict[str, dict[str, Any]]:
    """Fetch title (EN else FR), enacted_date, eli, and document_type from Cellar SPARQL."""
    found: dict[str, dict[str, Any]] = {}
    for i in range(0, len(celexes), batch_size):
        chunk = celexes[i : i + batch_size]
        # Escape any quotes defensively
        safe = [c for c in chunk if '"' not in c and "\\" not in c and "\n" not in c]
        if not safe:
            continue
        vals = " ".join(f'"{c}"^^xsd:string' for c in safe)
        query = f"""
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT ?celex
       (SAMPLE(?en_t) AS ?title_en)
       (SAMPLE(?fr_t) AS ?title_fr)
       (SAMPLE(?date) AS ?enacted_date)
       (SAMPLE(?eli) AS ?eli)
       (SAMPLE(?rt) AS ?resource_type)
WHERE {{
  VALUES ?celex {{ {vals} }}
  ?w cdm:resource_legal_id_celex ?celex .
  OPTIONAL {{ ?w cdm:work_date_document ?date }}
  OPTIONAL {{ ?w cdm:resource_legal_eli ?eli }}
  OPTIONAL {{ ?w cdm:work_has_resource-type ?rt }}
  OPTIONAL {{
    ?expr_en cdm:expression_belongs_to_work ?w ;
             cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> ;
             cdm:expression_title ?en_t .
  }}
  OPTIONAL {{
    ?expr_fr cdm:expression_belongs_to_work ?w ;
             cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/FRA> ;
             cdm:expression_title ?fr_t .
  }}
}}
GROUP BY ?celex
"""
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    SPARQL_ENDPOINT,
                    data=urllib.parse.urlencode({"query": query}).encode(),
                    headers={
                        "Accept": "application/sparql-results+json",
                        "User-Agent": "CaseLawExplorer/1.0 (https://caselawexplorer.tech)",
                    },
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as exc:
                if attempt == 3:
                    print(f"[warn] SPARQL batch {i} failed after 4 attempts: {exc}", file=sys.stderr, flush=True)
                    data = {"results": {"bindings": []}}
                else:
                    time.sleep(3.0 * (attempt + 1))

        for b in data.get("results", {}).get("bindings", []):
            cx = b["celex"]["value"]
            title = (b.get("title_en") or b.get("title_fr") or {}).get("value")
            if title:
                title = " ".join(title.split())[:1000]
            else:
                title = f"EU Act {cx}"
            raw_date = (b.get("enacted_date") or {}).get("value")
            enacted_date = raw_date[:10] if raw_date and re.match(r"^\d{4}-\d{2}-\d{2}", raw_date) else None
            enacted_year = int(enacted_date[:4]) if enacted_date else _year_from_celex(cx)
            eli = (b.get("eli") or {}).get("value")
            rt_uri = (b.get("resource_type") or {}).get("value", "")
            rt_code = rt_uri.rsplit("/", 1)[-1] if rt_uri else ""
            doc_type = RESOURCE_TYPE_MAP.get(rt_code, _fallback_doc_type(cx))
            found[cx] = {
                "identifier": cx,
                "document_type": doc_type,
                "title": title,
                "enacted_date": enacted_date,
                "enacted_year": enacted_year,
                "eli": eli,
            }
        if (i // batch_size + 1) % 10 == 0:
            print(
                f"[sparql] queried={min(i + batch_size, len(celexes))}/{len(celexes)} found={len(found)}",
                file=sys.stderr,
                flush=True,
            )
        if pause and i + batch_size < len(celexes):
            time.sleep(pause)
    return found


def load_all_unresolved_celexes(client: SqlRunnerClient, eu_law) -> tuple[list[str], dict[str, list[str]]]:
    """Return (distinct_candidate_celexes, raw_resource -> [candidate_celexes])."""
    raws: list[str] = []
    last_raw = ""
    while True:
        rows = client.query_rows(
            """
            SELECT DISTINCT raw_resource
              FROM case_law_reference
             WHERE legislation_id IS NULL
               AND raw_scheme = 'celex'
               AND raw_resource IS NOT NULL
               AND raw_resource > %s
             ORDER BY raw_resource
             LIMIT 900
            """,
            params=[last_raw],
        )
        if not rows:
            break
        for r in rows:
            raws.append(r["raw_resource"])
        last_raw = rows[-1]["raw_resource"]
        if len(rows) < 900:
            break

    raw_to_cands: dict[str, list[str]] = {}
    all_cands: set[str] = set()
    for raw in raws:
        cands: list[str] = []
        tgt = eu_law.cellar_target(raw)
        if tgt is not None:
            cands.append(tgt.identifier)
            for alt in tgt.alternatives or ():
                if alt not in cands:
                    cands.append(alt)
        if raw not in cands:
            cands.append(raw)
        raw_to_cands[raw] = cands
        all_cands.update(cands)
    return sorted(all_cands), raw_to_cands


def load_existing_celex_legislation(client: SqlRunnerClient) -> dict[str, int]:
    leg_map: dict[str, int] = {}
    last_id = 0
    while True:
        rows = client.query_rows(
            """
            SELECT id, identifier
              FROM legislation
             WHERE scheme = 'celex' AND id > %s
             ORDER BY id
             LIMIT 900
            """,
            params=[last_id],
        )
        if not rows:
            break
        for r in rows:
            leg_map.setdefault(r["identifier"], int(r["id"]))
        last_id = int(rows[-1]["id"])
        if len(rows) < 900:
            break
    # Include curated aliases too
    last_aid = 0
    while True:
        arows = client.query_rows(
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
        if not arows:
            break
        for r in arows:
            leg_map.setdefault(r["alias"], int(r["legislation_id"]))
        last_aid = int(arows[-1]["id"])
        if len(arows) < 900:
            break
    return leg_map


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cle-checkout", type=Path, default=Path("/home/agent/projects/cle-law-and-context"))
    parser.add_argument("--limit-acts", type=int, default=None, help="Optional cap on acts queried via SPARQL")
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--resolve-batch", type=int, default=3000)
    args = parser.parse_args(argv)

    dry_run = os.environ.get("DRY_RUN", "1").strip().lower() not in {"0", "false", "no"}
    started = time.monotonic()
    client = SqlRunnerClient(read_only=dry_run)
    eu_law = _load_eu_law(args.cle_checkout)

    existing = load_existing_celex_legislation(client)
    all_cands, raw_to_cands = load_all_unresolved_celexes(client, eu_law)
    missing = [c for c in all_cands if c not in existing]
    if args.limit_acts is not None:
        missing = missing[: args.limit_acts]

    print(
        f"[eu_leg_sync] raw_unresolved_distinct={len(raw_to_cands)} existing_celex={len(existing)} to_fetch_sparql={len(missing)}",
        file=sys.stderr,
        flush=True,
    )

    meta = fetch_cellar_metadata(missing) if missing else {}
    leg_rows = [
        (
            m["identifier"],
            m["document_type"],
            m["title"],
            m["enacted_date"],
            m["enacted_year"],
            m["eli"],
            SOURCE_DATASET,
            EXTRACTOR_VERSION,
        )
        for m in meta.values()
    ]
    acts_inserted = 0
    if leg_rows:
        acts_inserted = client.execute_unnest_chunks(
            INSERT_LEGISLATION_SQL, list(zip(*leg_rows)), chunk_size=args.chunk_size, dry_run=dry_run
        )

    # Reload legislation map now that new acts are inserted
    leg_map = load_existing_celex_legislation(client) if not dry_run else dict(existing)
    if dry_run:
        for idx, cx in enumerate(meta, start=1):
            leg_map.setdefault(cx, -idx)

    # Map raw_resource -> legislation_id
    raw_to_leg: dict[str, int] = {}
    for raw, cands in raw_to_cands.items():
        for cand in cands:
            if cand in leg_map:
                raw_to_leg[raw] = leg_map[cand]
                break

    # Resolve unresolved case_law_reference rows in safe keyset batches
    stats: Counter[str] = Counter()
    rows_resolved = 0
    last_id = 0
    pending_ids: list[int] = []
    pending_legs: list[int] = []

    def flush_updates() -> int:
        if not pending_ids:
            return 0
        if dry_run:
            n = len(pending_ids)
            pending_ids.clear()
            pending_legs.clear()
            return n
        res = client.execute(
            RESOLVE_BATCH_SQL,
            params=[list(pending_ids), list(pending_legs)],
            dry_run=False,
        )
        pending_ids.clear()
        pending_legs.clear()
        return int(res.get("row_count") or 0)

    while True:
        rows = client.query_rows(
            """
            SELECT id, raw_resource
              FROM case_law_reference
             WHERE legislation_id IS NULL
               AND raw_scheme = 'celex'
               AND raw_resource IS NOT NULL
               AND id > %s
             ORDER BY id
             LIMIT 900
            """,
            params=[last_id],
        )
        if not rows:
            break
        last_id = int(rows[-1]["id"])
        for r in rows:
            lid = raw_to_leg.get(r["raw_resource"])
            if lid is not None:
                pending_ids.append(int(r["id"]))
                pending_legs.append(lid)
                stats["matched"] += 1
            else:
                stats["still_unmatched"] += 1
        if len(pending_ids) >= args.resolve_batch:
            rows_resolved += flush_updates()
            if rows_resolved % 15000 < args.resolve_batch:
                print(f"[eu_leg_sync] resolved={rows_resolved} last_clr_id={last_id}", file=sys.stderr, flush=True)
        if len(rows) < 900:
            break
    rows_resolved += flush_updates()

    summary = {
        "dry_run": dry_run,
        "distinct_raw_unresolved": len(raw_to_cands),
        "queried_sparql": len(missing),
        "found_in_cellar": len(meta),
        "acts_inserted": acts_inserted,
        "raw_resources_mapped": len(raw_to_leg),
        "case_law_reference_resolved": rows_resolved,
        "stats": dict(sorted(stats.items())),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
