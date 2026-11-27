#!/usr/bin/env python3
"""Resolve raw CELLAR person URIs stored in cle_v2.judge.full_name via EUR-Lex SPARQL.

Legacy CELLAR metadata ingest stored raw `http://publications.europa.eu/resource/cellar/<uuid>`
URIs from `case_law_delivered_by_judge` directly in `cle_v2.judge.full_name` (248 rows /
37,559 `case_judge` links). This script resolves those URIs via CELLAR SPARQL
(`cdm:agent_name`, `skos:prefLabel`, or `owl:sameAs` `.../resource/agent/<Name>`):

  * If the resolved human name already exists in `cle_v2.judge` (case-insensitive),
    all `cle_v2.case_judge` links are merged onto the existing canonical `judge_id`
    (`ON CONFLICT (case_id, judge_id, role) DO NOTHING`) and the duplicate URI row is removed.
  * Otherwise, `cle_v2.judge.full_name` is updated in place to the resolved human name.

Default mode is read-only (--dry-run); pass --apply only after owner go-ahead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migration.backfill_cjeu_text_treaty import SqlRunnerClient  # noqa: E402

SPARQL_ENDPOINT = "https://publications.europa.eu/webapi/rdf/sparql"


def _label_from_agent_uri(uri: str) -> Optional[str]:
    marker = "/resource/agent/"
    idx = uri.find(marker)
    if idx == -1:
        return None
    tail = urllib.parse.unquote(uri[idx + len(marker) :]).replace("_", " ").strip()
    return tail or None


def resolve_cellar_uris_via_sparql(
    uris: List[str], *, batch_size: int = 40, timeout: int = 45
) -> Dict[str, str]:
    """Query EUR-Lex CELLAR SPARQL for human names of person/agent URIs."""
    resolved: Dict[str, str] = {}
    remaining: List[str] = []
    for u in uris:
        direct = _label_from_agent_uri(u)
        if direct:
            resolved[u] = direct
        elif u.startswith(("http://", "https://")):
            remaining.append(u)

    for i in range(0, len(remaining), batch_size):
        chunk = remaining[i : i + batch_size]
        values = " ".join(f"<{u}>" for u in chunk)
        query = (
            "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
            "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
            "PREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
            "SELECT ?s ?agentName ?prefLabel ?sameAs WHERE {\n"
            f"  VALUES ?s {{ {values} }}\n"
            "  OPTIONAL { ?s cdm:agent_name ?agentName . }\n"
            "  OPTIONAL { ?s skos:prefLabel ?prefLabel . FILTER(LANG(?prefLabel) = '' || LANG(?prefLabel) = 'en' || LANG(?prefLabel) = 'fr') }\n"
            "  OPTIONAL { ?s owl:sameAs ?sameAs . FILTER(CONTAINS(STR(?sameAs), '/resource/agent/')) }\n"
            "}"
        )
        data = urllib.parse.urlencode({"query": query}).encode("utf-8")
        req = urllib.request.Request(
            SPARQL_ENDPOINT,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/sparql-results+json",
                "User-Agent": "CaseLawExplorer/1.0 (+https://caselawexplorer.tech)",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        # Prefer cdm:agent_name, then skos:prefLabel, then /resource/agent/<Name>
        candidates: Dict[str, Dict[str, str]] = {}
        for row in payload.get("results", {}).get("bindings", []):
            s = row.get("s", {}).get("value")
            if not s:
                continue
            slot = candidates.setdefault(s, {})
            for key in ("agentName", "prefLabel", "sameAs"):
                val = (row.get(key) or {}).get("value", "").strip()
                if val and key not in slot:
                    slot[key] = val

        for u, slot in candidates.items():
            name = (
                slot.get("agentName")
                or slot.get("prefLabel")
                or (_label_from_agent_uri(slot["sameAs"]) if "sameAs" in slot else None)
            )
            if name and not name.lower().startswith(("http://", "https://")):
                resolved[u] = " ".join(name.split())

    return resolved


_PROVENANCE_COLS = ("source_dataset", "extractor_version", "confidence")
_DELETE_CHUNK_SIZE = 2000


def _detect_case_judge_extra_cols(client: SqlRunnerClient) -> List[str]:
    """Return whichever optional provenance columns exist on cle_v2.case_judge."""
    rows = client.query(
        "SELECT column_name "
        "  FROM information_schema.columns "
        " WHERE table_schema = 'cle_v2' "
        "   AND table_name = 'case_judge' "
        "   AND column_name = ANY(%s)",
        [list(_PROVENANCE_COLS)],
    )
    present = {r["column_name"] for r in rows}
    return [c for c in _PROVENANCE_COLS if c in present]


def _delete_case_judge_chunked(client: SqlRunnerClient, judge_ids: List[int]) -> int:
    """Delete case_judge rows for judge_ids in bounded chunks below sql-runner's 10k row limit."""
    total = 0
    for jid in judge_ids:
        while True:
            res = client.execute(
                "DELETE FROM cle_v2.case_judge "
                " WHERE ctid IN ("
                "     SELECT ctid FROM cle_v2.case_judge "
                "      WHERE judge_id = %s "
                "      LIMIT %s"
                " )",
                [jid, _DELETE_CHUNK_SIZE],
            )
            deleted = int(res.get("row_count") or 0)
            total += deleted
            if deleted < _DELETE_CHUNK_SIZE:
                break
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve raw CELLAR URIs in cle_v2.judge via EUR-Lex SPARQL")
    ap.add_argument("--url", default="https://demo-psql.caselawexplorer.tech")
    ap.add_argument("--batch-size", type=int, default=40, help="URIs per CELLAR SPARQL batch")
    ap.add_argument("--apply", action="store_true", help="Rename/merge resolved judge rows in cle_v2")
    args = ap.parse_args()

    token = os.getenv("SQL_RUNNER_TOKEN")
    if not token:
        print("SQL_RUNNER_TOKEN is required (run via anvil-secrets run)", file=sys.stderr)
        return 2

    client = SqlRunnerClient(args.url, token)

    # 1. Load all existing judges (small table: ~450-1000 rows).
    all_judges = client.query("SELECT id, full_name FROM cle_v2.judge ORDER BY id")
    existing_by_lower: Dict[str, Tuple[int, str]] = {}
    uri_rows: List[Tuple[int, str]] = []
    for r in all_judges:
        jid = int(r["id"])
        name = (r["full_name"] or "").strip()
        if name.lower().startswith(("http://", "https://")):
            uri_rows.append((jid, name))
        elif name:
            existing_by_lower.setdefault(name.lower(), (jid, name))

    if not uri_rows:
        print(json.dumps({"mode": "APPLIED" if args.apply else "DRY-RUN", "uri_judges": 0}, indent=2))
        return 0

    # 2. Resolve all URI rows via EUR-Lex CELLAR SPARQL.
    resolved_map = resolve_cellar_uris_via_sparql(
        [u for _jid, u in uri_rows], batch_size=args.batch_size
    )

    renames: List[Tuple[int, str, str]] = []  # (judge_id, old_uri, new_name)
    merges: List[Tuple[int, int, str, str]] = []  # (old_uri_id, canonical_id, old_uri, target_name)
    splits: List[Tuple[int, str, List[str]]] = []  # (old_uri_id, old_uri, [individual_names])
    unresolved: List[Tuple[int, str]] = []

    for jid, uri in uri_rows:
        name = resolved_map.get(uri)
        if not name:
            unresolved.append((jid, uri))
            continue
        parts = [" ".join(p.split()) for p in name.split(";") if p.strip()]
        if len(parts) > 1:
            splits.append((jid, uri, parts))
            continue
        single = parts[0]
        key = single.lower()
        if key in existing_by_lower:
            canonical_id, canonical_name = existing_by_lower[key]
            merges.append((jid, canonical_id, uri, canonical_name))
        else:
            renames.append((jid, uri, single))
            existing_by_lower[key] = (jid, single)

    applied_renames = 0
    applied_merges = 0
    applied_splits = 0
    if args.apply:
        extra_cols = _detect_case_judge_extra_cols(client)
        insert_cols_sql = ", ".join(["case_id", "judge_id", "role", *extra_cols])
        select_extra_sql = "".join(f", cj.{c}" for c in extra_cols)

        # 1. Apply in-place renames first so newly canonicalized rows exist before merges/splits.
        if renames:
            payload = [{"id": jid, "full_name": name} for jid, _uri, name in renames]
            res = client.execute(
                "UPDATE cle_v2.judge j "
                "   SET full_name = x->>'full_name' "
                "  FROM jsonb_array_elements(%s::jsonb) AS x "
                " WHERE j.id = (x->>'id')::bigint",
                [json.dumps(payload, ensure_ascii=False)],
            )
            applied_renames = int(res.get("row_count") or 0)

        # 2. Merge duplicate single-name URI judge rows into their canonical judge_id.
        if merges:
            for offset in range(0, len(merges), 20):
                batch = merges[offset : offset + 20]
                payload = [{"old_id": old_id, "new_id": new_id} for old_id, new_id, _u, _n in batch]
                pj = json.dumps(payload)
                client.execute(
                    f"INSERT INTO cle_v2.case_judge ({insert_cols_sql}) "
                    f"SELECT cj.case_id, (x->>'new_id')::bigint, cj.role{select_extra_sql} "
                    "  FROM jsonb_array_elements(%s::jsonb) AS x "
                    "  JOIN cle_v2.case_judge cj ON cj.judge_id = (x->>'old_id')::bigint "
                    "ON CONFLICT (case_id, judge_id, role) DO NOTHING",
                    [pj],
                )
                old_ids = [old_id for old_id, _new_id, _u, _n in batch]
                _delete_case_judge_chunked(client, old_ids)
                res_del = client.execute(
                    "DELETE FROM cle_v2.judge WHERE id = ANY(%s)",
                    [old_ids],
                )
                applied_merges += int(res_del.get("row_count") or 0)

        # 3. Split multi-person URIs ("Lecourt ; Hammes") into individual judges and drop the URI row.
        if splits:
            split_names = sorted({p for _jid, _u, parts in splits for p in parts})
            client.execute(
                "INSERT INTO cle_v2.judge (full_name) "
                "SELECT DISTINCT v "
                "FROM jsonb_array_elements_text(%s::jsonb) AS v "
                "WHERE NOT EXISTS (SELECT 1 FROM cle_v2.judge j WHERE lower(j.full_name) = lower(v))",
                [json.dumps(split_names, ensure_ascii=False)],
            )
            for offset in range(0, len(splits), 10):
                batch = splits[offset : offset + 10]
                split_pairs = [
                    {"old_id": jid, "name": p}
                    for jid, _u, parts in batch
                    for p in parts
                ]
                client.execute(
                    f"INSERT INTO cle_v2.case_judge ({insert_cols_sql}) "
                    f"SELECT cj.case_id, j.id, cj.role{select_extra_sql} "
                    "  FROM jsonb_array_elements(%s::jsonb) AS x "
                    "  JOIN cle_v2.case_judge cj ON cj.judge_id = (x->>'old_id')::bigint "
                    "  JOIN LATERAL ("
                    "      SELECT id FROM cle_v2.judge j2 "
                    "       WHERE lower(j2.full_name) = lower(x->>'name') "
                    "       ORDER BY id LIMIT 1"
                    "  ) j ON true "
                    "ON CONFLICT (case_id, judge_id, role) DO NOTHING",
                    [json.dumps(split_pairs, ensure_ascii=False)],
                )
                old_ids = [jid for jid, _u, _parts in batch]
                _delete_case_judge_chunked(client, old_ids)
                res_sdel = client.execute(
                    "DELETE FROM cle_v2.judge WHERE id = ANY(%s)",
                    [old_ids],
                )
                applied_splits += int(res_sdel.get("row_count") or 0)

    print(json.dumps({
        "mode": "APPLIED" if args.apply else "DRY-RUN",
        "uri_judges_found": len(uri_rows),
        "resolved": len(renames) + len(merges) + len(splits),
        "unresolved": len(unresolved),
        "planned_renames": len(renames),
        "planned_merges": len(merges),
        "planned_splits": len(splits),
        "applied_renames": applied_renames,
        "applied_merges": applied_merges,
        "applied_splits": applied_splits,
        "sample_renames": [{"id": j, "name": n, "uri": u} for j, u, n in renames[:10]],
        "sample_merges": [
            {"old_id": o, "canonical_id": c, "name": n, "uri": u} for o, c, u, n in merges[:10]
        ],
        "sample_splits": [{"id": j, "names": p, "uri": u} for j, u, p in splits[:10]],
        "unresolved_sample": [{"id": j, "uri": u} for j, u in unresolved[:10]],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
