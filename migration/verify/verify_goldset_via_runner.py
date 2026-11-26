#!/usr/bin/env python3
"""Read-only E-01 gold-set verification against live cle_v2 via sql-runner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "sql"))

from sql_runner_client import SqlRunnerClient  # noqa: E402

THEMATIC_D1 = {
    ("ECLI:EU:C:2019:189", "ECLI:CE:ECHR:2016:0621JUD007613612"),
    ("ECLI:EU:C:2019:189", "ECLI:CE:ECHR:2017:0207DEC004238713"),
    ("ECLI:EU:C:2019:189", "ECLI:CE:ECHR:2020:1222JUD004393618"),
}


def load_goldset(data_js: Path) -> tuple[dict[str, dict], list[dict]]:
    text = data_js.read_text(encoding="utf-8")
    raw = text[text.index("{") : text.rstrip().rstrip(";").rindex("}") + 1]
    data = json.loads(raw)
    docs = {x["id"]: x for x in data["docs"]}
    links = [
        l
        for k, v in data.items()
        if k != "docs" and isinstance(v, list)
        for l in v
        if isinstance(l, dict) and "group" in l
    ]
    return docs, links


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-js",
        type=Path,
        default=Path("/home/agent/projects/cjeu-migration-roadmap/migration/verify/research-2026-11-24/goldset/data.js"),
    )
    args = parser.parse_args(argv)

    docs, links = load_goldset(args.data_js)
    client = SqlRunnerClient(read_only=True)
    eclis = sorted(docs)
    rows = client.query_rows(
        "SELECT id, ecli, celex_id, sources FROM cases WHERE ecli = ANY(%s::text[])",
        params=[eclis],
    )
    by_ecli = {r["ecli"]: r for r in rows}

    found_citation = 0
    total_citation = 0
    thematic_present = 0
    details = []

    for link in links:
        a, b = link["from"], link["to"]
        is_thematic = (a, b) in THEMATIC_D1 or (b, a) in THEMATIC_D1
        ca, cb = by_ecli.get(a), by_ecli.get(b)
        edges = []
        if ca and cb:
            erows = client.query_rows(
                """
                SELECT source_case_id = %s AS fwd, relation_type, source_dataset, match_method, confidence
                  FROM case_citation
                 WHERE (source_case_id = %s AND target_case_id = %s)
                    OR (source_case_id = %s AND target_case_id = %s)
                 ORDER BY source_dataset, relation_type
                """,
                params=[ca["id"], ca["id"], cb["id"], cb["id"], ca["id"]],
            )
            edges = [
                f"{'->' if r['fwd'] else '<-'} {r['relation_type']}/{r['source_dataset']}({r.get('match_method') or 'legacy'})"
                for r in erows
            ]
        if is_thematic:
            if edges:
                thematic_present += 1
        else:
            total_citation += 1
            if edges:
                found_citation += 1
        details.append(
            {
                "group": link["group"],
                "from": a,
                "to": b,
                "d1_thematic": is_thematic,
                "in_db": bool(ca and cb),
                "edges": edges,
            }
        )

    out = {
        "citation_links_found": found_citation,
        "citation_links_total": total_citation,
        "d1_thematic_false_positives": thematic_present,
        "details": details,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
