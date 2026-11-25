#!/usr/bin/env python3
"""Step 1 read-only pre-flight checks & real-text precision sampling via sql-runner.

Strictly read-only: creates SqlRunnerClient(read_only=True), so /execute can
never be called. Uses only GET /healthz and POST /query.

What it checks and produces:
1. Pre-apply schema & count-guard checks:
   - /healthz status (warns if writes_enabled=true on production)
   - Collision check for every new table/view introduced by migrations 0009-0015
   - Every existing source_dataset in case_citation and case_law_reference
     (verifies 0015 covers 100% of legacy citation datasets)
   - 0014 partial-unique-index preconditions (both counts must be 0)
   - Table sizes and baseline case_citation_counts / case_cited_by checksums
2. Real-text precision samples (run locally using case-law-explorer extractors):
   - RS texts (fulltext + summary): R2.1 ECLIs / CJEU case numbers / ECtHR
     appnos, R3.3 EU law, R4 authorities, R5.1 RS dictum
   - ECtHR texts & conclusions (HUDOC): R2.2 CJEU / RS citations, R3.4 EU law,
     R5.1 ECtHR article conclusions
   - CJEU texts & proc_type: R2.6 ECtHR citations, R5.1 CJEU outcomes
   - E-01 gold-set live texts (from tests/fixtures/linking_evaluation.json)

Usage:
  SQL_RUNNER_URL=https://demo-psql.caselawexplorer.tech \\
  anvil-secrets run --env SQL_RUNNER_TOKEN=cle-sql-runner-token -- \\
    /home/agent/.venvs/cle-tests/bin/python \\
      migration/verify/preflight_and_sample_via_runner.py \\
      --out-md migration/verify/step1_preflight_report.md \\
      --out-json migration/verify/step1_preflight_report.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "sql"))

from sql_runner_client import SqlRunnerClient  # noqa: E402

NEW_OBJECTS_0009_0015 = (
    "case_identifier",
    "case_citation_mention",
    "citation_relation_type",
    "citation_source_dataset",
    "authority_document",
    "case_authority_reference",
    "case_outcome",
    "case_status",
    "case_citation_archive",
    "echr_edge_v2_staging",
    "case_procedural_link",
)

LEGACY_SEEDED_IN_0015 = {
    "rs_body_cite",
    "rs_legacy_ddb",
    "rs_formal_relation",
    "rs_lido_sqlite",
    "rs_replaces",
    "echr_edge",
    "cellar_sparql",
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


def _snippet(text: str, start: int, end: int, radius: int = 110) -> str:
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    chunk = text[lo:hi].replace("\n", " ").replace("\r", " ")
    chunk = " ".join(chunk.split())
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return f"{prefix}{chunk}{suffix}"


def run_preflight(client: SqlRunnerClient) -> dict[str, Any]:
    health = client.healthz()

    existing_objects = client.query_rows(
        """
        SELECT table_name, table_type
          FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name = ANY(%s::text[])
         ORDER BY table_name
        """,
        params=[list(NEW_OBJECTS_0009_0015)],
    )

    citation_datasets = client.query_rows(
        """
        SELECT source_dataset, count(*)::bigint AS n,
               count(*) FILTER (WHERE target_case_id IS NOT NULL)::bigint AS resolved
          FROM case_citation
         GROUP BY source_dataset
         ORDER BY n DESC
        """
    )
    unseeded_legacy = [
        row["source_dataset"]
        for row in citation_datasets
        if row["source_dataset"] not in LEGACY_SEEDED_IN_0015
    ]

    law_ref_datasets = client.query_rows(
        """
        SELECT source_dataset, count(*)::bigint AS n,
               count(*) FILTER (WHERE legislation_id IS NOT NULL)::bigint AS with_leg,
               count(*) FILTER (WHERE provision_id IS NOT NULL)::bigint AS with_prov
          FROM case_law_reference
         GROUP BY source_dataset
         ORDER BY n DESC
        """
    )

    pre_0014_bwb = client.query_rows(
        """
        SELECT count(*)::bigint AS n
          FROM case_law_reference
         WHERE source_dataset = 'rs_lido_sqlite'
           AND legislation_id IS NOT NULL
        """
    )[0]["n"]

    pre_0014_eu = client.query_rows(
        """
        SELECT count(*)::bigint AS n
          FROM case_law_reference
         WHERE raw_resource IS NOT NULL
           AND (legislation_id IS NOT NULL OR provision_id IS NOT NULL)
           AND source_dataset IN ('cellar_sparql', 'cellar_law_ref', 'rs_text_eu_law', 'echr_text_eu_law')
        """
    )[0]["n"]

    table_sizes = client.query_rows(
        """
        SELECT relname AS table_name,
               reltuples::bigint AS est_rows,
               pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = current_schema()
           AND c.relname IN (
               'cases', 'case_text', 'case_citation', 'case_law_reference',
               'legislation', 'legal_provision', 'echr_document_article',
               'rs_document_publication', 'case_citation_counts', 'case_cited_by'
           )
         ORDER BY pg_total_relation_size(c.oid) DESC
        """
    )

    counts_baseline = client.query_rows(
        """
        SELECT count(*)::bigint AS rows,
               COALESCE(sum(cites_count), 0)::bigint AS sum_cites,
               COALESCE(sum(cited_by_count), 0)::bigint AS sum_cited_by
          FROM case_citation_counts
        """
    )[0]

    cited_by_present = client.query_rows(
        "SELECT to_regclass('case_cited_by') IS NOT NULL AS present"
    )[0]["present"]
    cited_by_baseline = None
    if cited_by_present:
        cited_by_baseline = client.query_rows(
            """
            SELECT count(*)::bigint AS rows,
                   COALESCE(sum(cited_by), 0)::bigint AS sum_cited_by,
                   count(*) FILTER (WHERE cited_by > 0)::bigint AS nonzero_cases
              FROM case_cited_by
            """
        )[0]

    return {
        "checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "healthz": health,
        "existing_new_objects": existing_objects,
        "citation_datasets": citation_datasets,
        "unseeded_legacy_citation_datasets": unseeded_legacy,
        "law_ref_datasets": law_ref_datasets,
        "pre_0014_rs_lido_with_legislation_id": pre_0014_bwb,
        "pre_0014_eu_law_resolved": pre_0014_eu,
        "table_sizes": table_sizes,
        "case_citation_counts_baseline": counts_baseline,
        "case_cited_by_baseline": cited_by_baseline,
    }


def _import_extractors(cle_checkout: Path):
    dags_dir = str(cle_checkout / "airflow" / "dags")
    if dags_dir not in sys.path:
        sys.path.insert(0, dags_dir)
    from data_extraction.case_context import cjeu_outcomes, echr_outcomes, rs_dictum
    from data_extraction.linking import authority_patterns, cjeu_text, echr_text, eu_law, rs_text

    return {
        "rs_text": rs_text,
        "echr_text": echr_text,
        "cjeu_text": cjeu_text,
        "eu_law": eu_law,
        "authority_patterns": authority_patterns,
        "rs_dictum": rs_dictum,
        "echr_outcomes": echr_outcomes,
        "cjeu_outcomes": cjeu_outcomes,
    }


def fetch_rs_sample(client: SqlRunnerClient, limit: int) -> list[dict[str, Any]]:
    # Mix cue-bearing recent decisions (so rare extractors reach >=20 hits)
    # with an unseeded deterministic stride across cases (to catch false positives).
    cued = client.query_rows(
        """
        SELECT c.id, c.ecli, c.date_decision, ct.language,
               LEFT(ct.fulltext, 35000) AS fulltext,
               LEFT(ct.summary, 6000) AS summary
          FROM cases c
          JOIN case_text ct ON ct.case_id = c.id
         WHERE 'RS' = ANY(c.sources)
           AND ct.source = 'RECHTSPRAAK'
           AND (ct.fulltext IS NOT NULL OR ct.summary IS NOT NULL)
           AND (
               ct.fulltext ILIKE '%%C-%%'
               OR ct.fulltext ILIKE '%%EHRM%%'
               OR ct.fulltext ILIKE '%%Richtlijn%%'
               OR ct.fulltext ILIKE '%%Kamerstukken%%'
           )
         ORDER BY c.id DESC
         LIMIT %s
        """,
        params=[max(10, limit // 2)],
    )
    seen = {r["id"] for r in cued}
    unbiased = client.query_rows(
        """
        SELECT c.id, c.ecli, c.date_decision, ct.language,
               LEFT(ct.fulltext, 35000) AS fulltext,
               LEFT(ct.summary, 6000) AS summary
          FROM cases c
          JOIN case_text ct ON ct.case_id = c.id
         WHERE 'RS' = ANY(c.sources)
           AND ct.source = 'RECHTSPRAAK'
           AND ct.fulltext IS NOT NULL
           AND (c.id %% 997) = 0
         ORDER BY c.id DESC
         LIMIT %s
        """,
        params=[max(10, limit // 2)],
    )
    return cued + [r for r in unbiased if r["id"] not in seen]


def fetch_echr_sample(client: SqlRunnerClient, limit: int) -> list[dict[str, Any]]:
    return client.query_rows(
        """
        SELECT c.id, c.ecli, c.date_decision, ed.item_id, ed.conclusion, ed.doctype_branch,
               ct.language, LEFT(ct.fulltext, 35000) AS fulltext
          FROM cases c
          JOIN echr_document ed ON ed.case_id = c.id
          LEFT JOIN case_text ct ON ct.case_id = c.id AND ct.source = 'HUDOC'
         WHERE 'ECHR' = ANY(c.sources)
           AND (
               ct.fulltext ILIKE '%%Court of Justice%%'
               OR ct.fulltext ILIKE '%%ECLI:%%'
               OR ct.fulltext ILIKE '%%Directive%%'
               OR ed.conclusion IS NOT NULL
           )
         ORDER BY c.id DESC
         LIMIT %s
        """,
        params=[limit],
    )


def fetch_cjeu_sample(client: SqlRunnerClient, limit: int) -> list[dict[str, Any]]:
    return client.query_rows(
        """
        SELECT c.id, c.ecli, c.celex_id, c.date_decision, cd.proc_type,
               ct.language, LEFT(ct.fulltext, 35000) AS fulltext
          FROM cases c
          JOIN cjeu_document cd ON cd.case_id = c.id
          LEFT JOIN case_text ct ON ct.case_id = c.id AND ct.language = 'en'
         WHERE 'CJEU' = ANY(c.sources)
           AND (
               ct.fulltext ILIKE '%%ECtHR%%'
               OR ct.fulltext ILIKE '%%ECHR%%'
               OR ct.fulltext ILIKE '%%Application no.%%'
               OR cd.proc_type IS NOT NULL
           )
         ORDER BY c.id DESC
         LIMIT %s
        """,
        params=[limit],
    )


def fetch_gold_set_texts(client: SqlRunnerClient, cle_checkout: Path) -> list[dict[str, Any]]:
    fixture_path = (
        cle_checkout / "airflow" / "dags" / "tests" / "fixtures" / "linking_evaluation.json"
    )
    if not fixture_path.exists():
        return []
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    eclis = sorted(
        {
            item["source"]["ecli"]
            for item in fixture.get("entries", [])
            if isinstance(item.get("source"), dict) and item["source"].get("ecli")
        }
    )
    if not eclis:
        return []
    return client.query_rows(
        """
        SELECT c.id, c.ecli, c.date_decision, c.sources, ct.source AS text_source,
               ct.language, LEFT(ct.fulltext, 40000) AS fulltext,
               LEFT(ct.summary, 8000) AS summary
          FROM cases c
          JOIN case_text ct ON ct.case_id = c.id
         WHERE c.ecli = ANY(%s::text[])
         ORDER BY c.ecli, ct.language
         LIMIT 200
        """,
        params=[eclis],
    )


def sample_extractors(
    client: SqlRunnerClient,
    cle_checkout: Path,
    *,
    docs_per_corpus: int = 80,
    max_examples_per_bucket: int = 25,
) -> dict[str, Any]:
    ext = _import_extractors(cle_checkout)
    buckets: dict[str, list[dict[str, Any]]] = {
        "rs_text_ecli": [],
        "rs_text_cjeu_casenum": [],
        "rs_text_echr_appno": [],
        "rs_text_eu_law": [],
        "rs_authorities": [],
        "rs_dictum": [],
        "echr_text_cjeu_rs": [],
        "echr_text_eu_law": [],
        "echr_outcomes": [],
        "cjeu_text_echr": [],
        "cjeu_outcomes": [],
    }
    totals: Counter[str] = Counter()

    def add(bucket: str, item: dict[str, Any]) -> None:
        totals[bucket] += 1
        if len(buckets[bucket]) < max_examples_per_bucket:
            buckets[bucket].append(item)

    rs_rows = fetch_rs_sample(client, docs_per_corpus)
    for row in rs_rows:
        decided = _parse_date(row.get("date_decision"))
        texts = []
        if row.get("fulltext"):
            texts.append(("fulltext", row.get("language") or "nl", row["fulltext"]))
        if row.get("summary"):
            texts.append(("summary", row.get("language") or "nl", row["summary"]))
        src = ext["rs_text"].SourceCase(
            ecli=row.get("ecli"), decision_date=decided, texts=texts
        )
        for hit in ext["rs_text"].scan_case(src):
            b = {
                "ecli": "rs_text_ecli",
                "cjeu_case_number": "rs_text_cjeu_casenum",
                "echr_appno": "rs_text_echr_appno",
            }.get(hit.scheme)
            if b:
                add(
                    b,
                    {
                        "source_ecli": row.get("ecli"),
                        "field": hit.text_field,
                        "raw": hit.raw_text,
                        "normalized": hit.value,
                        "cues": {
                            k: v
                            for k, v in (
                                ("opinion", hit.opinion_cue),
                                ("decision", hit.decision_cue),
                                ("gc", hit.grand_chamber),
                                ("date", str(hit.cited_date) if hit.cited_date else None),
                            )
                            if v
                        },
                        "context": hit.context,
                    },
                )
        for field_name, _lang, text in texts:
            for hit in ext["eu_law"].scan(text, decided=decided, profile="rs"):
                add(
                    "rs_text_eu_law",
                    {
                        "source_ecli": row.get("ecli"),
                        "field": field_name,
                        "celex": hit.celex,
                        "article": str(hit.article) if hit.article else None,
                        "method": hit.method,
                        "confidence": hit.confidence,
                        "context": _snippet(text, hit.start, hit.end),
                    },
                )
            for hit in ext["authority_patterns"].scan(text):
                add(
                    "rs_authorities",
                    {
                        "source_ecli": row.get("ecli"),
                        "field": field_name,
                        "kind": hit.doc.kind,
                        "identifier": hit.doc.identifier,
                        "pinpoint": hit.pinpoint,
                        "method": hit.method,
                        "context": _snippet(text, hit.start, hit.end),
                    },
                )
        if row.get("fulltext"):
            outcomes, reason = ext["rs_dictum"].parse_dictum(row["fulltext"])
            for outcome, raw_span, conf, rule in outcomes:
                add(
                    "rs_dictum",
                    {
                        "source_ecli": row.get("ecli"),
                        "outcome": outcome,
                        "rule": rule,
                        "confidence": conf,
                        "raw": raw_span[:240],
                    },
                )

    echr_rows = fetch_echr_sample(client, docs_per_corpus)
    for row in echr_rows:
        decided = _parse_date(row.get("date_decision"))
        text = row.get("fulltext") or ""
        if text:
            sc = ext["echr_text"].scan(text)
            for cite in sc.citations:
                for num in cite.numbers:
                    add(
                        "echr_text_cjeu_rs",
                        {
                            "source_ecli": row.get("ecli"),
                            "kind": "cjeu_case_number",
                            "normalized": getattr(num, "normalized", str(num)),
                            "ecli_in_cite": cite.ecli,
                            "opinion_cue": cite.opinion_cue,
                            "context": _snippet(
                                text,
                                getattr(num, "start", 0),
                                getattr(num, "end", 0),
                            ),
                        },
                    )
            for start, end, ecli in sc.nl_eclis:
                add(
                    "echr_text_cjeu_rs",
                    {
                        "source_ecli": row.get("ecli"),
                        "kind": "nl_ecli",
                        "normalized": ecli,
                        "context": _snippet(text, start, end),
                    },
                )
            for hit in ext["eu_law"].scan(text, decided=decided, profile="echr"):
                add(
                    "echr_text_eu_law",
                    {
                        "source_ecli": row.get("ecli"),
                        "celex": hit.celex,
                        "article": str(hit.article) if hit.article else None,
                        "method": hit.method,
                        "context": _snippet(text, hit.start, hit.end),
                    },
                )
        if row.get("conclusion"):
            for item in ext["echr_outcomes"].split_items(row["conclusion"]):
                art, outcome, rule = ext["echr_outcomes"].parse_conclusion_item(item)
                add(
                    "echr_outcomes",
                    {
                        "source_ecli": row.get("ecli"),
                        "article": art,
                        "outcome": outcome,
                        "rule": rule,
                        "raw": item[:200],
                    },
                )

    cjeu_rows = fetch_cjeu_sample(client, docs_per_corpus)
    for row in cjeu_rows:
        text = row.get("fulltext") or ""
        if text:
            sc = ext["cjeu_text"].scan(text, name_dates=False)
            for start, end, val in sc.eclis:
                add(
                    "cjeu_text_echr",
                    {
                        "source_ecli": row.get("ecli"),
                        "kind": "echr_ecli",
                        "normalized": val,
                        "context": _snippet(text, start, end),
                    },
                )
            for item in sc.appnos:
                add(
                    "cjeu_text_echr",
                    {
                        "source_ecli": row.get("ecli"),
                        "kind": "echr_appno",
                        "normalized": getattr(item, "value", str(item)),
                        "context": _snippet(
                            text, getattr(item, "start", 0), getattr(item, "end", 0)
                        ),
                    },
                )
        if row.get("proc_type"):
            parsed, rule = ext["cjeu_outcomes"].parse_proc_type(row["proc_type"])
            add(
                "cjeu_outcomes",
                {
                    "source_ecli": row.get("ecli"),
                    "proc_type": row["proc_type"],
                    "outcome": parsed,
                    "rule": rule,
                },
            )

    gold_rows = fetch_gold_set_texts(client, cle_checkout)
    gold_hits = []
    for row in gold_rows:
        texts = []
        if row.get("fulltext"):
            texts.append(("fulltext", row.get("language"), row["fulltext"]))
        if row.get("summary"):
            texts.append(("summary", row.get("language"), row["summary"]))
        decided = _parse_date(row.get("date_decision"))
        if "RS" in (row.get("sources") or []):
            src = ext["rs_text"].SourceCase(
                ecli=row.get("ecli"), decision_date=decided, texts=texts
            )
            hits = [
                {"scheme": h.scheme, "value": h.value, "field": h.text_field}
                for h in ext["rs_text"].scan_case(src)
            ]
            gold_hits.append({"ecli": row.get("ecli"), "corpus": "RS", "hits": hits})

    return {
        "docs_sampled": {
            "rs": len(rs_rows),
            "echr": len(echr_rows),
            "cjeu": len(cjeu_rows),
            "gold_set_rows": len(gold_rows),
        },
        "total_hits_in_sample": dict(totals),
        "examples": buckets,
        "gold_set_scan": gold_hits,
    }


def render_markdown(preflight: dict[str, Any], samples: dict[str, Any] | None) -> str:
    lines = [
        "# Step 1 Read-Only Pre-Flight & Precision Sample Report",
        "",
        f"Generated: `{preflight['checked_at_utc']}`",
        "",
        "## 1. Pre-apply schema & count-guard checks",
        "",
        f"- `/healthz`: `enabled={preflight['healthz'].get('enabled')}`, "
        f"`writes_enabled={preflight['healthz'].get('writes_enabled')}`, "
        f"`configured_keys={preflight['healthz'].get('configured_keys')}`, "
        f"`max_rows={preflight['healthz'].get('max_rows')}`",
    ]
    if preflight["healthz"].get("writes_enabled"):
        lines.append(
            "- **WARNING:** `writes_enabled` is `true` on sql-runner; flip `SQL_RUNNER_ALLOW_WRITES=false` outside write windows."
        )

    collisions = preflight["existing_new_objects"]
    lines.append(
        f"- New table/view collisions (must be 0 before `0009`): **{len(collisions)}** "
        + (f"({collisions})" if collisions else "(clean)")
    )
    unseeded = preflight["unseeded_legacy_citation_datasets"]
    lines.append(
        f"- Legacy `case_citation.source_dataset` values missing from `0015`: **{len(unseeded)}** "
        + (f"({unseeded})" if unseeded else "(all 6 covered)")
    )
    lines.append(
        f"- `0014` preconditions (both must be 0): "
        f"`rs_lido_sqlite` with `legislation_id` = **{preflight['pre_0014_rs_lido_with_legislation_id']}**, "
        f"EU-law resolved = **{preflight['pre_0014_eu_law_resolved']}**"
    )

    lines += [
        "",
        "### Existing `case_citation` datasets",
        "",
        "| `source_dataset` | rows | resolved |",
        "|---|---:|---:|",
    ]
    for r in preflight["citation_datasets"]:
        lines.append(f"| `{r['source_dataset']}` | {r['n']:,} | {r['resolved']:,} |")

    lines += [
        "",
        "### Table sizes & baseline count checksums",
        "",
        "| table | est_rows | total_size |",
        "|---|---:|---:|",
    ]
    for r in preflight["table_sizes"]:
        lines.append(f"| `{r['table_name']}` | {r['est_rows']:,} | {r['total_size']} |")

    cb = preflight["case_citation_counts_baseline"]
    lines.append(
        f"\n- `case_citation_counts` baseline: rows={cb['rows']:,}, "
        f"sum(cites_count)={cb['sum_cites']:,}, sum(cited_by_count)={cb['sum_cited_by']:,}"
    )
    if preflight["case_cited_by_baseline"]:
        mv = preflight["case_cited_by_baseline"]
        lines.append(
            f"- `case_cited_by` baseline: rows={mv['rows']:,}, "
            f"nonzero={mv['nonzero_cases']:,}, sum(cited_by)={mv['sum_cited_by']:,}"
        )

    if samples:
        lines += [
            "",
            "## 2. Real-text precision sample summary",
            "",
            f"Docs sampled: RS={samples['docs_sampled']['rs']}, "
            f"ECtHR={samples['docs_sampled']['echr']}, "
            f"CJEU={samples['docs_sampled']['cjeu']}, "
            f"Gold-set rows={samples['docs_sampled']['gold_set_rows']}",
            "",
            "| extractor bucket | total hits in sample | examples listed |",
            "|---|---:|---:|",
        ]
        for bucket, exs in samples["examples"].items():
            lines.append(
                f"| `{bucket}` | {samples['total_hits_in_sample'].get(bucket, 0):,} | {len(exs)} |"
            )

        for bucket, exs in samples["examples"].items():
            lines += ["", f"### Sample: `{bucket}` ({len(exs)} items)", ""]
            for idx, ex in enumerate(exs, 1):
                src = ex.get("source_ecli") or "?"
                ctx = ex.get("context") or ex.get("raw") or ex.get("proc_type") or ""
                target = (
                    ex.get("normalized")
                    or ex.get("celex")
                    or ex.get("identifier")
                    or ex.get("outcome")
                    or ""
                )
                extra = ex.get("article") or ex.get("kind") or ex.get("rule") or ex.get("method") or ""
                lines.append(f"{idx}. `{src}` → **`{target}`** ({extra}) — {ctx}")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cle-checkout",
        type=Path,
        default=Path("/home/agent/projects/cle-law-and-context"),
        help="Path to case-law-explorer checkout carrying Phase 2-5 extractors",
    )
    parser.add_argument("--docs-per-corpus", type=int, default=80)
    parser.add_argument("--examples", type=int, default=25)
    parser.add_argument("--skip-samples", action="store_true")
    parser.add_argument("--out-md", type=Path, default=None)
    parser.add_argument("--out-json", type=Path, default=None)
    args = parser.parse_args(argv)

    client = SqlRunnerClient(read_only=True)
    preflight = run_preflight(client)
    samples = (
        None
        if args.skip_samples
        else sample_extractors(
            client,
            args.cle_checkout,
            docs_per_corpus=args.docs_per_corpus,
            max_examples_per_bucket=args.examples,
        )
    )
    md = render_markdown(preflight, samples)
    if args.out_md:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        args.out_md.write_text(md, encoding="utf-8")
    else:
        print(md)
    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(
            json.dumps({"preflight": preflight, "samples": samples}, indent=2, default=str),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
