#!/usr/bin/env python3
"""Backfill R3.7 (echr_document_article -> case_law_reference) and
R4.6 (rs_document_publication -> authority_document + case_authority_reference)
through the Coolify sql-runner.

Uses case-law-explorer's exact `echr_articles` and `rs_publication` parsing
rules so historical backfills match weekly ETL output character-for-character:

  * R3.7 (`--task echr_article`):
      1. Reads all distinct `article_code` values from `cle_v2.echr_document_article`.
      2. Parses them with `data_extraction.legislation.echr_articles.parse_article_code`
         and `data_extraction.legislation.acts.echr_protocols()`.
      3. Inserts missing article/paragraph rows into `cle_v2.legal_provision`
         (`ON CONFLICT ... DO NOTHING`).
      4. Inserts deduplicated `(case_id, provision_id, role)` rows into
         `cle_v2.case_law_reference` (`source_dataset = 'echr_article'`,
         `extractor_version = 'echr_article/1.0.0'`) in case_id range batches.

  * R4.6 (`--task rs_publication`):
      1. Fast pure-SQL range-batched pass for the ~813k `rs_document_publication`
         rows with pre-parsed `(journal_abbr, year, locator)` columns matching
         `rs_publication.parse_publication`'s exact `publication_columns` rule.
      2. Python pass using `rs_publication.parse_publication` + `publication_doc`
         directly for all remaining non-`self_published` rows (e.g. trailing
         `met annotatie van` in locator, `other` rows, `updates_service` without
         columns).
      Both passes insert idempotently into `cle_v2.authority_document` and
      `cle_v2.case_authority_reference` (`source_dataset = 'rs_publication'`,
      `extractor_version = 'rs_publication/1.0.0'`).

Env:
  SQL_RUNNER_URL      e.g. https://demo-psql.caselawexplorer.tech
  SQL_RUNNER_TOKEN    HMAC token (never printed)
  SQL_RUNNER_CONFIRM  default execute-cle-v2
  DRY_RUN             default 1; pass --write or set DRY_RUN=0 to insert
  CLE_CHECKOUT        path to case-law-explorer checkout (default /home/agent/projects/cle-law-and-context)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "sql"))

from sql_runner_client import SqlRunnerClient  # noqa: E402

DEFAULT_CLE_CHECKOUT = Path(
    os.environ.get("CLE_CHECKOUT", "/home/agent/projects/cle-law-and-context")
)


def _load_cle_modules(cle_checkout: Path):
    dags_dir = str(cle_checkout / "airflow" / "dags")
    if dags_dir not in sys.path:
        sys.path.insert(0, dags_dir)
    from data_extraction.legislation import acts, echr_articles
    from data_extraction.linking import rs_publication

    return acts, echr_articles, rs_publication


# ---------------------------------------------------------------------------
# R3.7: echr_document_article -> legal_provision + case_law_reference
# ---------------------------------------------------------------------------


def backfill_echr_articles(
    client: SqlRunnerClient,
    cle_checkout: Path,
    *,
    dry_run: bool,
    case_batch_size: int = 5000,
) -> dict[str, Any]:
    t0 = time.monotonic()
    acts_mod, echr_mod, _ = _load_cle_modules(cle_checkout)
    protocols = acts_mod.echr_protocols()

    # 1. Load legislation.id for all CoE treaty acts
    leg_rows = client.query_rows(
        "SELECT id, scheme, identifier FROM cle_v2.legislation WHERE scheme = 'coe_treaty'"
    )
    act_id_by_key = {(r["scheme"], r["identifier"]): int(r["id"]) for r in leg_rows}

    # 2. Load all distinct article_code values (only ~475 distinct!)
    code_rows = client.query_rows(
        "SELECT DISTINCT article_code FROM cle_v2.echr_document_article ORDER BY article_code"
    )
    codes = [r["article_code"] for r in code_rows]
    print(f"[r3.7] Loaded {len(codes)} distinct article_code values", flush=True)

    # Parse every distinct code using CLE's exact parse_article_code
    wanted_articles: set[tuple[int, str]] = set()
    wanted_paragraphs: set[tuple[int, str, str]] = set()
    parsed_code_map: list[tuple[str, int, str, Optional[str], str]] = []
    stats: Counter[str] = Counter()

    for code in codes:
        refs, bad = echr_mod.parse_article_code(code)
        stats["codes_total"] += 1
        stats["bad_parts"] += len(bad)
        for ref in refs:
            act = protocols.get(ref.protocol)
            if act is None:
                stats["unknown_protocol"] += 1
                continue
            leg_id = act_id_by_key.get((act.scheme, act.identifier))
            if leg_id is None:
                stats["missing_legislation"] += 1
                continue
            wanted_articles.add((leg_id, ref.article))
            if ref.paragraph is not None:
                wanted_paragraphs.add((leg_id, ref.article, ref.paragraph))
            parsed_code_map.append((code, leg_id, ref.article, ref.paragraph, ref.part))

    print(
        f"[r3.7] Parsed {len(parsed_code_map)} (code -> provision) mappings across "
        f"{len(wanted_articles)} articles and {len(wanted_paragraphs)} paragraphs "
        f"(stats={dict(stats)})",
        flush=True,
    )

    # 3. Ensure all article & paragraph legal_provision rows exist
    prov_id_by_key: dict[tuple[int, str, Optional[str]], int] = {}
    if not dry_run:
        # Insert articles first
        art_list = sorted(wanted_articles)
        client.execute(
            """
            INSERT INTO cle_v2.legal_provision (
                legislation_id, parent_id, element_type, article_label, paragraph, title,
                source_dataset, extractor_version
            )
            SELECT leg_id, NULL::bigint, 'article', art, NULL::text, 'Article ' || art,
                   %s, %s
              FROM unnest(%s::bigint[], %s::text[]) AS t(leg_id, art)
            ON CONFLICT (legislation_id, element_type, article_label, COALESCE(paragraph, ''))
                WHERE source_dataset IS NOT NULL
            DO NOTHING
            """,
            [
                echr_mod.SOURCE_DATASET,
                echr_mod.EXTRACTOR_VERSION,
                [a[0] for a in art_list],
                [a[1] for a in art_list],
            ],
            dry_run=False,
        )
        # Fetch article provision IDs
        for leg_row in client.query_rows(
            """
            SELECT id, legislation_id, article_label
              FROM cle_v2.legal_provision
             WHERE element_type = 'article'
               AND source_dataset IS NOT NULL
               AND legislation_id = ANY(%s::bigint[])
            """,
            [sorted({a[0] for a in art_list})],
        ):
            prov_id_by_key[(int(leg_row["legislation_id"]), leg_row["article_label"], None)] = int(
                leg_row["id"]
            )

        # Insert paragraphs with parent_id pointing to the article row
        para_list = sorted(wanted_paragraphs)
        if para_list:
            client.execute(
                """
                INSERT INTO cle_v2.legal_provision (
                    legislation_id, parent_id, element_type, article_label, paragraph, title,
                    source_dataset, extractor_version
                )
                SELECT leg_id, parent_id, 'paragraph', art, para,
                       'Article ' || art || ' § ' || para,
                       %s, %s
                  FROM unnest(%s::bigint[], %s::bigint[], %s::text[], %s::text[])
                       AS t(leg_id, parent_id, art, para)
                ON CONFLICT (legislation_id, element_type, article_label, COALESCE(paragraph, ''))
                    WHERE source_dataset IS NOT NULL
                DO NOTHING
                """,
                [
                    echr_mod.SOURCE_DATASET,
                    echr_mod.EXTRACTOR_VERSION,
                    [p[0] for p in para_list],
                    [prov_id_by_key.get((p[0], p[1], None)) for p in para_list],
                    [p[1] for p in para_list],
                    [p[2] for p in para_list],
                ],
                dry_run=False,
            )
            # Fetch paragraph provision IDs (page by id to stay under 1,000-row cap)
            last_pid = 0
            leg_ids = sorted({p[0] for p in para_list})
            while True:
                prows = client.query_rows(
                    """
                    SELECT id, legislation_id, article_label, paragraph
                      FROM cle_v2.legal_provision
                     WHERE element_type = 'paragraph'
                       AND source_dataset IS NOT NULL
                       AND legislation_id = ANY(%s::bigint[])
                       AND id > %s
                     ORDER BY id
                     LIMIT 900
                    """,
                    [leg_ids, last_pid],
                )
                if not prows:
                    break
                for pr in prows:
                    last_pid = int(pr["id"])
                    prov_id_by_key[
                        (int(pr["legislation_id"]), pr["article_label"], pr["paragraph"])
                    ] = last_pid

        print(f"[r3.7] Resolved {len(prov_id_by_key):,} legal_provision rows", flush=True)

    # 4. Build parallel arrays for the code -> provision mapping
    map_codes: list[str] = []
    map_leg_ids: list[int] = []
    map_prov_ids: list[int] = []
    map_parts: list[str] = []
    for code, leg_id, art, para, part in parsed_code_map:
        pid = prov_id_by_key.get((leg_id, art, para))
        if not dry_run and pid is None:
            stats["provision_missing"] += 1
            continue
        map_codes.append(code)
        map_leg_ids.append(leg_id)
        map_prov_ids.append(pid or 0)
        map_parts.append(part)

    # 5. Range-batched pure-SQL insert over echr_document.case_id
    bounds = client.query_rows(
        "SELECT MIN(case_id) AS lo, MAX(case_id) AS hi FROM cle_v2.echr_document WHERE case_id IS NOT NULL"
    )[0]
    lo, hi = int(bounds["lo"]), int(bounds["hi"])
    print(f"[r3.7] Case ID range: {lo}..{hi} (step={case_batch_size})", flush=True)

    total_inserted = 0
    batches = 0
    if dry_run:
        print("[r3.7] DRY_RUN=1: skipping case_law_reference inserts.", flush=True)
    else:
        # Page by distinct ECHR case_ids in chunks of case_batch_size (denser than raw id range)
        cur = lo - 1
        while cur < hi:
            nxt_hi = min(hi, cur + 50_000)
            res = client.execute(
                """
                WITH code_map AS (
                    SELECT * FROM unnest(%s::text[], %s::bigint[], %s::bigint[], %s::text[])
                           AS m(article_code, legislation_id, provision_id, raw_subdivision)
                ),
                dedup AS (
                    SELECT DISTINCT ON (d.case_id, m.provision_id, a.kind)
                           d.case_id,
                           m.legislation_id,
                           m.provision_id,
                           %s::text AS raw_scheme,
                           m.raw_subdivision,
                           btrim(a.article_code) AS raw_reference,
                           a.kind AS role,
                           %s::text AS source_dataset,
                           %s::text AS extractor_version
                      FROM cle_v2.echr_document d
                      JOIN cle_v2.echr_document_article a ON a.item_id = d.item_id
                      JOIN code_map m ON m.article_code = a.article_code
                     WHERE d.case_id > %s AND d.case_id <= %s
                       AND a.kind IN ('applied', 'violation', 'nonviolation')
                     ORDER BY d.case_id, m.provision_id, a.kind,
                              btrim(a.article_code), m.raw_subdivision
                )
                INSERT INTO cle_v2.case_law_reference (
                    case_id, legislation_id, provision_id, raw_scheme, raw_subdivision,
                    raw_reference, role, source_dataset, extractor_version
                )
                SELECT case_id, legislation_id, provision_id, raw_scheme, raw_subdivision,
                       raw_reference, role, source_dataset, extractor_version
                  FROM dedup
                ON CONFLICT (case_id, provision_id, role, source_dataset)
                    WHERE provision_id IS NOT NULL AND raw_resource IS NULL
                DO NOTHING
                """,
                [
                    map_codes,
                    map_leg_ids,
                    map_prov_ids,
                    map_parts,
                    echr_mod.RAW_SCHEME,
                    echr_mod.SOURCE_DATASET,
                    echr_mod.EXTRACTOR_VERSION,
                    cur,
                    nxt_hi,
                ],
                dry_run=False,
            )
            rc = int(res.get("row_count") or res.get("rowcount") or 0)
            total_inserted += rc
            batches += 1
            cur = nxt_hi
            print(
                f"  [r3.7] batch {batches} up to case_id={nxt_hi:,}: +{rc:,} (total {total_inserted:,}, {time.monotonic()-t0:.1f}s)",
                flush=True,
            )

    final_count = int(
        client.query_rows(
            "SELECT COUNT(*) AS n FROM cle_v2.case_law_reference WHERE source_dataset = 'echr_article'"
        )[0]["n"]
    )
    out = {
        "task": "echr_article",
        "dry_run": dry_run,
        "provisions_resolved": len(prov_id_by_key),
        "inserted_this_run": total_inserted,
        "total_in_db": final_count,
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
    print(f"[r3.7] DONE: {json.dumps(out)}", flush=True)
    return out


# ---------------------------------------------------------------------------
# R4.6: rs_document_publication -> authority_document + case_authority_reference
# ---------------------------------------------------------------------------


def backfill_rs_publications(
    client: SqlRunnerClient,
    cle_checkout: Path,
    *,
    dry_run: bool,
    range_step: int = 100_000,
    from_id: int = 0,
) -> dict[str, Any]:
    t0 = time.monotonic()
    _, _, pub_mod = _load_cle_modules(cle_checkout)

    bounds = client.query_rows(
        "SELECT MIN(case_id) AS lo, MAX(case_id) AS hi FROM cle_v2.rs_document_publication"
    )[0]
    lo = max(int(bounds["lo"]), from_id + 1) if from_id > 0 else int(bounds["lo"])
    hi = int(bounds["hi"])
    print(f"[r4.6] rs_document_publication case_id range: {lo}..{hi} (step={range_step})", flush=True)

    # Pass 1: Pure-SQL range-batched pass for clean pre-parsed `publication_columns` rows.
    # Uses two statements per range inside one transaction (no unindexed `all_docs` CTE)
    # plus `SET LOCAL statement_timeout = '120s'`.
    sql_docs_inserted = 0
    sql_refs_inserted = 0
    if dry_run:
        print("[r4.6] DRY_RUN=1: skipping SQL and Python inserts.", flush=True)
    else:
        cur = lo - 1
        batch_num = 0
        parsed_cte_tpl = r"""
            WITH parsed AS (
                SELECT DISTINCT ON (p.case_id, btrim(p.raw))
                       p.case_id,
                       btrim(p.raw) AS raw_ref,
                       upper(regexp_replace(p.journal_abbr, '[\s.]+', '', 'g')) AS journal,
                       p.year::text AS yr,
                       upper(ltrim(regexp_replace(p.locator, '\s+', '', 'g'), '/,')) AS num,
                       NULLIF(
                           btrim(
                               regexp_replace(
                                   regexp_replace(
                                       COALESCE(p.annotator, ''),
                                       '(?i)\m(mr|prof|dr|drs|ir|ing|em|jhr)\.\s*',
                                       '',
                                       'g'
                                   ),
                                   '\s+',
                                   ' ',
                                   'g'
                               ),
                               ' ,;.:'
                           ),
                           ''
                       ) AS note
                  FROM cle_v2.rs_document_publication p
                 WHERE p.case_id > {lo} AND p.case_id <= {hi}
                   AND p.kind <> 'self_published'
                   AND p.raw IS NOT NULL
                   AND btrim(p.raw) <> ''
                   AND btrim(p.raw) !~* '^(?:www\.)?rechtspraak\.nl$'
                   AND p.journal_abbr IS NOT NULL
                   AND p.year IS NOT NULL
                   AND p.locator IS NOT NULL
                   AND upper(ltrim(regexp_replace(p.locator, '\s+', '', 'g'), '/,'))
                       ~ '^[0-9]{{1,6}}(\.[0-9]{{1,3}})?[A-Z]{{0,2}}$'
                   AND NOT (
                       p.annotator IS NULL
                       AND p.raw ~* '[\s,(]+(m\.\s?nt\.|met\s+(annotatie|noot)|annotatie:?|annotator:?|noot:)'
                   )
                 ORDER BY p.case_id, btrim(p.raw)
            ),
            docs_wanted AS (
                SELECT CASE WHEN note IS NOT NULL THEN 'annotation' ELSE 'literature' END AS kind,
                       'nl_journal_locator'::text AS identifier_scheme,
                       journal || ' ' || yr || '/' || num
                           || CASE WHEN note IS NOT NULL THEN ' m.nt. ' || note ELSE '' END AS identifier,
                       journal AS issuer,
                       case_id,
                       raw_ref
                  FROM parsed
                 WHERE journal <> '' AND journal NOT IN ('ECLI', 'LJN', 'NR', 'ZAAKNR')
            )
        """
        while cur < hi:
            nxt_hi = min(hi, cur + range_step)
            cte = parsed_cte_tpl.format(lo=int(cur), hi=int(nxt_hi))
            batch_sql = (
                "SET LOCAL statement_timeout = '120s';\n"
                + cte
                + """
                INSERT INTO cle_v2.authority_document (
                    kind, identifier_scheme, identifier, title, issuer
                )
                SELECT DISTINCT kind, identifier_scheme, identifier, identifier, issuer
                  FROM docs_wanted
                ON CONFLICT (kind, COALESCE(identifier_scheme, ''), identifier)
                    WHERE identifier IS NOT NULL
                DO NOTHING;
                """
                + cte
                + """
                INSERT INTO cle_v2.case_authority_reference (
                    case_id, authority_document_id, raw_reference, text_field,
                    source_dataset, extractor_version, match_method, confidence
                )
                SELECT w.case_id,
                       d.id,
                       w.raw_ref,
                       'metadata',
                       'rs_publication',
                       'rs_publication/1.0.0',
                       'publication_columns',
                       0.95::real
                  FROM docs_wanted w
                  JOIN cle_v2.authority_document d
                    ON d.kind = w.kind
                   AND COALESCE(d.identifier_scheme, '') = w.identifier_scheme
                   AND d.identifier = w.identifier
                 WHERE d.identifier IS NOT NULL
                ON CONFLICT (case_id, md5(raw_reference), COALESCE(text_field, ''), COALESCE(language, ''),
                             COALESCE(char_start, -1), source_dataset)
                DO NOTHING;
                """
            )
            res = client.execute(batch_sql, dry_run=False)
            stmt_res = res.get("statements") or []
            d_ins = int(stmt_res[1].get("row_count") or 0) if len(stmt_res) >= 3 else 0
            r_ins = int(res.get("row_count") or 0)
            sql_docs_inserted += d_ins
            sql_refs_inserted += r_ins
            batch_num += 1
            cur = nxt_hi
            if batch_num % 5 == 0 or cur >= hi:
                print(
                    f"  [r4.6-sql] batch {batch_num} up to case_id={nxt_hi:,}: "
                    f"+{d_ins:,} docs, +{r_ins:,} refs "
                    f"(totals: {sql_docs_inserted:,} docs, {sql_refs_inserted:,} refs, {time.monotonic()-t0:.1f}s)",
                    flush=True,
                )

    # Pass 2: Python pass using rs_publication.parse_publication directly for the remaining
    # non-self_published rows not handled by Pass 1 (bad locator format, missing annotator col, `other`).
    py_docs_inserted = 0
    py_refs_inserted = 0
    py_scanned = 0
    py_parsed = 0
    if not dry_run:
        print("[r4.6-py] Processing remaining edge-case publication rows with rs_publication.parse_publication...", flush=True)
        last_cid = from_id
        pending_records: list[tuple[int, str, str, str, str, str, str, float]] = []

        def _flush(records: list[tuple[int, str, str, str, str, str, str, float]]):
            nonlocal py_docs_inserted, py_refs_inserted
            if not records:
                return
            r1 = client.execute(
                """
                INSERT INTO cle_v2.authority_document (
                    kind, identifier_scheme, identifier, title, issuer
                )
                SELECT DISTINCT kind, scheme, identifier, identifier, issuer
                  FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[])
                       AS b(kind, scheme, identifier, issuer)
                ON CONFLICT (kind, COALESCE(identifier_scheme, ''), identifier)
                    WHERE identifier IS NOT NULL
                DO NOTHING
                """,
                [
                    [r[2] for r in records],
                    [r[3] for r in records],
                    [r[4] for r in records],
                    [r[5] for r in records],
                ],
                dry_run=False,
            )
            r2 = client.execute(
                """
                WITH batch AS (
                    SELECT * FROM unnest(
                        %s::bigint[], %s::text[], %s::text[], %s::text[],
                        %s::text[], %s::text[], %s::real[]
                    ) AS b(case_id, raw_ref, kind, scheme, identifier, method, confidence)
                )
                INSERT INTO cle_v2.case_authority_reference (
                    case_id, authority_document_id, raw_reference, text_field,
                    source_dataset, extractor_version, match_method, confidence
                )
                SELECT b.case_id, d.id, b.raw_ref, 'metadata',
                       'rs_publication', 'rs_publication/1.0.0', b.method, b.confidence
                  FROM batch b
                  JOIN cle_v2.authority_document d
                    ON d.kind = b.kind
                   AND COALESCE(d.identifier_scheme, '') = b.scheme
                   AND d.identifier = b.identifier
                 WHERE d.identifier IS NOT NULL
                ON CONFLICT (case_id, md5(raw_reference), COALESCE(text_field, ''), COALESCE(language, ''),
                             COALESCE(char_start, -1), source_dataset)
                DO NOTHING
                """,
                [
                    [r[0] for r in records],
                    [r[1] for r in records],
                    [r[2] for r in records],
                    [r[3] for r in records],
                    [r[4] for r in records],
                    [r[6] for r in records],
                    [r[7] for r in records],
                ],
                dry_run=False,
            )
            py_docs_inserted += int(r1.get("row_count") or 0)
            py_refs_inserted += int(r2.get("row_count") or 0)

        while True:
            rows = client.query_rows(
                r"""
                SELECT p.case_id,
                       json_agg(json_build_object(
                           'raw', p.raw, 'journal_abbr', p.journal_abbr,
                           'year', p.year, 'locator', p.locator, 'annotator', p.annotator
                       )) AS pubs
                  FROM cle_v2.rs_document_publication p
                 WHERE p.case_id > %s
                   AND p.kind <> 'self_published'
                   AND p.raw IS NOT NULL AND btrim(p.raw) <> ''
                   AND (
                       p.journal_abbr IS NULL OR p.year IS NULL OR p.locator IS NULL
                       OR upper(ltrim(regexp_replace(p.locator, '\s+', '', 'g'), '/,'))
                          !~ '^[0-9]{1,6}(\.[0-9]{1,3})?[A-Z]{0,2}$'
                       OR (
                           p.annotator IS NULL
                           AND p.raw ~* '[\s,(]+(m\.\s?nt\.|met\s+(annotatie|noot)|annotatie:?|annotator:?|noot:)'
                       )
                   )
                 GROUP BY p.case_id
                 ORDER BY p.case_id
                 LIMIT 600
                """,
                [last_cid],
            )
            if not rows:
                break
            for r in rows:
                cid = int(r["case_id"])
                last_cid = cid
                seen_raw: set[str] = set()
                for pub in r["pubs"] or []:
                    raw = (pub.get("raw") or "").strip()
                    if not raw or raw in seen_raw:
                        continue
                    seen_raw.add(raw)
                    py_scanned += 1
                    parts = pub_mod.parse_publication(
                        raw,
                        pub.get("journal_abbr"),
                        pub.get("year"),
                        pub.get("locator"),
                        pub.get("annotator"),
                    )
                    if parts is None:
                        continue
                    py_parsed += 1
                    doc = pub_mod.publication_doc(parts)
                    method = parts["method"] or "publication_raw"
                    conf = 0.95 if method == "publication_columns" else 0.90
                    pending_records.append(
                        (cid, raw, doc.kind, doc.identifier_scheme, doc.identifier, doc.issuer, method, conf)
                    )
            if len(pending_records) >= 5000:
                _flush(pending_records)
                pending_records.clear()
        if pending_records:
            _flush(pending_records)
            pending_records.clear()
        print(
            f"[r4.6-py] Scanned {py_scanned:,} edge-case rows, parsed {py_parsed:,}, "
            f"inserted +{py_docs_inserted:,} docs, +{py_refs_inserted:,} refs ({time.monotonic()-t0:.1f}s)",
            flush=True,
        )

    final_refs = int(
        client.query_rows(
            "SELECT COUNT(*) AS n FROM cle_v2.case_authority_reference WHERE source_dataset = 'rs_publication'"
        )[0]["n"]
    )
    final_docs = int(
        client.query_rows(
            "SELECT COUNT(*) AS n FROM cle_v2.authority_document WHERE identifier_scheme = 'nl_journal_locator'"
        )[0]["n"]
    )
    out = {
        "task": "rs_publication",
        "dry_run": dry_run,
        "sql_docs_inserted": sql_docs_inserted,
        "sql_refs_inserted": sql_refs_inserted,
        "py_scanned": py_scanned,
        "py_parsed": py_parsed,
        "py_docs_inserted": py_docs_inserted,
        "py_refs_inserted": py_refs_inserted,
        "total_docs_in_db": final_docs,
        "total_refs_in_db": final_refs,
        "elapsed_s": round(time.monotonic() - t0, 1),
    }
    print(f"[r4.6] DONE: {json.dumps(out)}", flush=True)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill R3.7 echr_article and R4.6 rs_publication.")
    parser.add_argument("--task", choices=("echr_article", "rs_publication", "all"), default="all")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--from-id", type=int, default=0)
    parser.add_argument("--cle-checkout", type=Path, default=DEFAULT_CLE_CHECKOUT)
    args = parser.parse_args(argv)

    dry_run = not args.write and os.environ.get("DRY_RUN", "1") != "0"
    client = SqlRunnerClient(read_only=dry_run, timeout=300)

    results = {}
    if args.task in ("echr_article", "all"):
        results["echr_article"] = backfill_echr_articles(client, args.cle_checkout, dry_run=dry_run)
    if args.task in ("rs_publication", "all"):
        results["rs_publication"] = backfill_rs_publications(
            client, args.cle_checkout, dry_run=dry_run, from_id=args.from_id
        )

    print(json.dumps(results, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
