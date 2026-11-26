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
  echr_text     R2.2: HUDOC fulltext + summary -> CJEU case numbers/ECLIs & NL ECLIs/LJNs
  cjeu_text     R2.6: CJEU fulltext -> ECtHR ECLIs & appnos (name_date off per D5)

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
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "sql"))

from sql_runner_client import SqlRunnerClient  # noqa: E402

MODE_DATASETS = {
    "ag_opinions": ("cjeu_ag_link",),
    "rs_text": ("rs_text_ecli", "rs_text_cjeu_casenum", "rs_text_echr_appno"),
    "echr_text": ("echr_text_cjeu", "echr_text_rs"),
    "cjeu_text": ("cjeu_text_echr",),
    "cellar_referrals": ("cellar_national_judgement",),
    "cellar_relations": ("cellar_relation_resolved",),
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

_ECLI_CORPUS = (("ECLI:EU:", "CJEU"), ("ECLI:CE:ECHR:", "ECHR"), ("ECLI:NL:", "RS"))


def _load_cle(cle_checkout: Path):
    dags_dir = str(cle_checkout / "airflow" / "dags")
    if dags_dir not in sys.path:
        sys.path.insert(0, dags_dir)
    from data_extraction.linking import (
        ag_opinions,
        artifact,
        cellar_referrals,
        cellar_relations,
        cjeu_text,
        echr_text,
        identifiers,
        resolver,
        rs_text,
        text_common,
    )
    from data_loading import identifier_loader

    return {
        "ag_opinions": ag_opinions,
        "artifact": artifact,
        "cellar_referrals": cellar_referrals,
        "cellar_relations": cellar_relations,
        "identifiers": identifiers,
        "identifier_loader": identifier_loader,
        "rs_text": rs_text,
        "echr_text": echr_text,
        "cjeu_text": cjeu_text,
        "resolver": resolver,
        "text_common": text_common,
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


def _corpus_of_ecli(ecli: str | None) -> str | None:
    if not ecli:
        return None
    u = ecli.upper()
    for prefix, corpus in _ECLI_CORPUS:
        if u.startswith(prefix):
            return corpus
    return "FOREIGN" if u.startswith("ECLI:") else None


def _is_cross_jurisdiction(
    src_sources: Iterable[str],
    tgt_sources: Iterable[str] | None,
    *,
    target_ecli: str | None = None,
    dataset: str = "",
) -> bool:
    src_set = set(src_sources or ())
    if tgt_sources is not None:
        tgt_set = set(tgt_sources)
        if src_set and tgt_set:
            return not bool(src_set & tgt_set)
    if target_ecli:
        corpus = _corpus_of_ecli(target_ecli)
        if corpus and src_set:
            return corpus not in src_set
    return dataset in (
        "rs_text_cjeu_casenum",
        "rs_text_echr_appno",
        "echr_text_cjeu",
        "echr_text_rs",
        "cjeu_text_echr",
    )


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


class RunnerCandidateLookup:
    """On-demand cached candidate & CaseKey resolver over sql-runner /query.

    Implements both:
    - echr_text / cjeu_text interface: prefetch(scheme, values), get(scheme, values)
    - rs_text interface: prefetch_hits(hits), ecli(value), candidates(scheme, value)
    """

    def __init__(self, client: SqlRunnerClient, resolver_mod) -> None:
        self.client = client
        self.resolver = resolver_mod
        self._ecli_cache: dict[str, Any | None] = {}
        self._scheme_cache: dict[str, dict[str, list[Any]]] = {}
        self._case_key_cache: dict[tuple[str, str], tuple[int, list[str]] | None] = {}

    # -- Shared batched /query helpers --

    def prefetch_eclis(self, values: Iterable[str]) -> None:
        missing = sorted({v for v in values if v and v not in self._ecli_cache})
        for i in range(0, len(missing), 200):
            batch = missing[i : i + 200]
            rows = self.client.query_rows(
                """
                SELECT c.ecli AS value, 0 AS via_identifier, c.id, c.ecli, c.celex_id,
                       c.item_id, c.date_decision, c.sources
                  FROM cases c
                 WHERE c.ecli = ANY(%s::text[])
                UNION ALL
                SELECT ci.value_normalized, 1, c.id, c.ecli, c.celex_id,
                       c.item_id, c.date_decision, c.sources
                  FROM case_identifier ci
                  JOIN cases c ON c.id = ci.case_id
                 WHERE ci.scheme = 'ecli' AND ci.value_normalized = ANY(%s::text[])
                 ORDER BY 1, 2, 3
                """,
                params=[batch, batch],
            )
            found: dict[str, Any] = {}
            for r in rows:
                val = r["value"]
                cid = int(r["id"])
                srcs = list(r.get("sources") or [])
                if val not in found:
                    found[val] = self.resolver.Candidate(
                        case_id=cid,
                        ecli=r.get("ecli"),
                        celex=r.get("celex_id"),
                        item_id=r.get("item_id"),
                        date=_parse_date(r.get("date_decision")),
                        kind="case",
                        value=val,
                    )
                    self._case_key_cache[("ecli", val)] = (cid, srcs)
                    if r.get("ecli"):
                        self._case_key_cache[("ecli", r["ecli"])] = (cid, srcs)
            for v in batch:
                self._ecli_cache[v] = found.get(v)
                self._case_key_cache.setdefault(("ecli", v), None)

    def prefetch_scheme(self, scheme: str, values: Iterable[str]) -> None:
        cache = self._scheme_cache.setdefault(scheme, {})
        missing = sorted({v for v in values if v and v not in cache})
        # Chunk to 80 values so expanded multi-document appnos stay well below 1000-row cap.
        for i in range(0, len(missing), 80):
            batch = missing[i : i + 80]
            rows = self.client.query_rows(
                """
                SELECT ci.value_normalized, ci.value, c.id, c.ecli, c.celex_id,
                       c.item_id, c.date_decision, c.sources, d.doctype, d.doctype_branch
                  FROM case_identifier ci
                  JOIN cases c ON c.id = ci.case_id
                  LEFT JOIN LATERAL (
                        SELECT e.doctype, e.doctype_branch
                          FROM echr_document e
                         WHERE e.case_id = c.id
                         ORDER BY e.item_id
                         LIMIT 1
                  ) d ON true
                 WHERE ci.scheme = %s
                   AND ci.value_normalized = ANY(%s::text[])
                """,
                params=[scheme, batch],
            )
            grouped: dict[str, list[Any]] = {}
            for r in rows:
                norm = r["value_normalized"]
                cid = int(r["id"])
                srcs = list(r.get("sources") or [])
                if scheme == "echr_appno":
                    kind = self.resolver.echr_kind(r.get("doctype"))
                elif scheme == "cjeu_case_number":
                    kind = self.resolver.cjeu_kind(r.get("celex_id"))
                else:
                    kind = "case"
                cand = self.resolver.Candidate(
                    case_id=cid,
                    ecli=r.get("ecli"),
                    celex=r.get("celex_id"),
                    item_id=r.get("item_id"),
                    date=_parse_date(r.get("date_decision")),
                    kind=kind,
                    value=r["value"],
                    branch=r.get("doctype_branch"),
                )
                grouped.setdefault(norm, []).append(cand)
                if r.get("ecli"):
                    self._case_key_cache[("ecli", r["ecli"])] = (cid, srcs)
                if r.get("celex_id"):
                    self._case_key_cache[("celex", r["celex_id"])] = (cid, srcs)
                if r.get("item_id"):
                    self._case_key_cache[("item_id", r["item_id"])] = (cid, srcs)
            for v in batch:
                cache[v] = grouped.get(v, [])

    def prefetch_case_keys(self, keys: Iterable[Any]) -> None:
        eclis: set[str] = set()
        celexes: set[str] = set()
        items: set[str] = set()
        for k in keys:
            if k is None:
                continue
            if k.ecli and ("ecli", k.ecli) not in self._case_key_cache:
                eclis.add(k.ecli)
            if k.celex and ("celex", k.celex) not in self._case_key_cache:
                celexes.add(k.celex)
            if k.item_id and ("item_id", k.item_id) not in self._case_key_cache:
                items.add(k.item_id)
        if eclis:
            self.prefetch_eclis(eclis)
        if celexes:
            c_list = sorted(celexes)
            for i in range(0, len(c_list), 200):
                batch = c_list[i : i + 200]
                rows = self.client.query_rows(
                    "SELECT id, celex_id, sources FROM cases WHERE celex_id = ANY(%s::text[])",
                    params=[batch],
                )
                for r in rows:
                    self._case_key_cache[("celex", r["celex_id"])] = (
                        int(r["id"]),
                        list(r.get("sources") or []),
                    )
                for v in batch:
                    self._case_key_cache.setdefault(("celex", v), None)
        if items:
            i_list = sorted(items)
            for i in range(0, len(i_list), 200):
                batch = i_list[i : i + 200]
                rows = self.client.query_rows(
                    """
                    SELECT id, item_id, sources, false AS via_doc
                      FROM cases WHERE item_id = ANY(%s::text[])
                    UNION ALL
                    SELECT c.id, d.item_id, c.sources, true AS via_doc
                      FROM echr_document d
                      JOIN cases c ON c.id = d.case_id
                     WHERE d.item_id = ANY(%s::text[])
                    """,
                    params=[batch, batch],
                )
                for r in sorted(rows, key=lambda x: bool(x["via_doc"])):
                    self._case_key_cache.setdefault(
                        ("item_id", r["item_id"]),
                        (int(r["id"]), list(r.get("sources") or [])),
                    )
                for v in batch:
                    self._case_key_cache.setdefault(("item_id", v), None)

    def resolve_case_key(self, key: Any) -> tuple[int, list[str]] | None:
        if key is None:
            return None
        for attr in ("ecli", "celex", "item_id"):
            val = getattr(key, attr, None)
            if val:
                hit = self._case_key_cache.get((attr, val))
                if hit is not None:
                    return hit
        return None

    # -- echr_text / cjeu_text interface --

    def prefetch(self, scheme_or_hits, values=None):
        if values is not None:
            # echr_text / cjeu_text: prefetch(scheme, values)
            self.prefetch_scheme(scheme_or_hits, values)
            return
        # rs_text: prefetch(hits)
        hits = scheme_or_hits
        self.prefetch_eclis(h.value for h in hits if h.scheme == "ecli")
        for scheme in ("cjeu_case_number", "echr_appno"):
            vals = [h.value for h in hits if h.scheme == scheme]
            if vals:
                self.prefetch_scheme(scheme, vals)

    def get(self, scheme: str, values: Iterable[str]) -> dict[str, list[Any]]:
        vals = [v for v in values if v]
        self.prefetch_scheme(scheme, vals)
        cache = self._scheme_cache.get(scheme, {})
        return {v: cache.get(v, []) for v in vals}

    # -- rs_text interface --

    def ecli(self, value: str):
        if value not in self._ecli_cache:
            self.prefetch_eclis([value])
        return self._ecli_cache.get(value)

    def candidates(self, scheme: str, value: str) -> list[Any]:
        cache = self._scheme_cache.setdefault(scheme, {})
        if value not in cache:
            self.prefetch_scheme(scheme, [value])
        return list(cache.get(value, ()))


def _flush_link_records(
    client: SqlRunnerClient,
    lookup: RunnerCandidateLookup,
    records_with_src: list[tuple[int, list[str], Any]],
    *,
    resolved_only: bool,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
    explained_flag: list[bool],
    stats: Counter[str],
) -> tuple[int, int, int]:
    """Convert LinkRecords -> batched unnest inserts into case_citation."""
    if not records_with_src:
        return 0, 0, 0

    lookup.prefetch_case_keys(rec.target for _, _, rec in records_with_src if rec.target is not None)

    res_batch: dict[tuple, tuple] = {}
    ue_batch: dict[tuple, tuple] = {}
    ur_batch: dict[tuple, tuple] = {}

    for src_id, src_sources, rec in records_with_src:
        rel = rec.relation_type
        ds = rec.source_dataset
        ver = rec.extractor_version
        method = rec.match_method
        conf = rec.confidence

        resolved = lookup.resolve_case_key(rec.target) if rec.target is not None else None
        if resolved is not None:
            tgt_id, tgt_sources = resolved
            if tgt_id == src_id:
                stats[f"{ds}:self_skipped"] += 1
                continue
            cross = _is_cross_jurisdiction(src_sources, tgt_sources, dataset=ds)
            key = (src_id, tgt_id, rel, ds)
            res_batch[key] = (src_id, tgt_id, rel, ds, cross, ver, method, conf)
            stats[f"{ds}:resolved"] += 1
        elif rec.target is not None and rec.target.ecli:
            if resolved_only:
                stats[f"{ds}:unresolved_ecli_skipped"] += 1
                continue
            cross = _is_cross_jurisdiction(
                src_sources, None, target_ecli=rec.target.ecli, dataset=ds
            )
            key = (src_id, rec.target.ecli, rel, ds)
            ue_batch[key] = (src_id, rec.target.ecli, rel, ds, cross, ver, method, conf)
            stats[f"{ds}:unresolved_ecli"] += 1
        elif rec.target_ref_raw and rec.target_ref_scheme:
            if resolved_only:
                stats[f"{ds}:unresolved_ref_skipped"] += 1
                continue
            cross = _is_cross_jurisdiction(src_sources, None, dataset=ds)
            key = (src_id, rec.target_ref_scheme, rec.target_ref_raw, rel or "", ds)
            ur_batch[key] = (
                src_id, rec.target_ref_scheme, rec.target_ref_raw, rel, ds, cross, ver, method, conf
            )
            stats[f"{ds}:unresolved_ref"] += 1
        else:
            stats[f"{ds}:dropped_unkeyed"] += 1

    ins_res = 0
    ins_ue = 0
    ins_ur = 0
    if res_batch:
        ins_res = client.execute_unnest_chunks(
            INSERT_RESOLVED_SQL,
            list(zip(*res_batch.values())),
            chunk_size=chunk_size,
            dry_run=dry_run,
            explain_first=(dry_run and explain_sql and not explained_flag[0]),
        )
        explained_flag[0] = True
    if ue_batch:
        ins_ue = client.execute_unnest_chunks(
            INSERT_UNRESOLVED_ECLI_SQL,
            list(zip(*ue_batch.values())),
            chunk_size=chunk_size,
            dry_run=dry_run,
        )
    if ur_batch:
        ins_ur = client.execute_unnest_chunks(
            INSERT_UNRESOLVED_REF_SQL,
            list(zip(*ur_batch.values())),
            chunk_size=chunk_size,
            dry_run=dry_run,
        )
    return ins_res, ins_ue, ins_ur


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
    celex_map: dict[str, int] = {}
    last_id = 0
    while True:
        rows = client.query_rows(
            """
            SELECT case_id AS id, celex_id
              FROM cjeu_document
             WHERE celex_id IS NOT NULL AND case_id > %s
             ORDER BY case_id LIMIT 900
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
            SELECT case_id AS id, celex_id
              FROM cjeu_document
             WHERE celex_id ~ '^6[0-9]{4}CC'
               AND case_id > %s
             ORDER BY case_id LIMIT %s
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
        inserted = client.execute_unnest_chunks(
            INSERT_RESOLVED_SQL,
            list(zip(*resolved_rows)),
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
    resolved_only: bool,
) -> dict[str, Any]:
    rs_mod = cle["rs_text"]
    lookup = RunnerCandidateLookup(client, cle["resolver"])

    bounds = client.query_rows(
        "SELECT min(id) AS lo, max(id) AS hi FROM cases WHERE 'RS' = ANY(sources)"
    )[0]
    if bounds["lo"] is None:
        return {"mode": "rs_text", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(0, from_id - 1) if from_id > 0 else 0

    stats: Counter[str] = Counter()
    scanned = 0
    ins_res = ins_ue = ins_ur = 0
    explained = [False]

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        rows = client.query_rows(
            """
            SELECT c.id, c.ecli, c.celex_id, c.item_id, c.date_decision, c.sources,
                   ct.language, ct.fulltext, ct.summary
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

        # Group by case_id and prefetch all hits for the whole page in one batch!
        sources_by_case: dict[int, tuple[list[str], Any]] = {}
        for r in rows:
            cid = int(r["id"])
            entry = sources_by_case.get(cid)
            if entry is None:
                src = rs_mod.SourceCase(
                    ecli=r.get("ecli"),
                    decision_date=_parse_date(r.get("date_decision")),
                    texts=[],
                    celex=r.get("celex_id"),
                    item_id=r.get("item_id"),
                )
                entry = (list(r.get("sources") or ["RS"]), src)
                sources_by_case[cid] = entry
            _, src = entry
            lang = r.get("language") or "nl"
            if r.get("fulltext"):
                src.texts.append(("fulltext", lang, r["fulltext"]))
            if r.get("summary"):
                src.texts.append(("summary", lang, r["summary"]))

        hits_by_case = {cid: rs_mod.scan_case(src) for cid, (_, src) in sources_by_case.items()}
        all_hits = [h for hs in hits_by_case.values() for h in hs]
        if all_hits:
            lookup.prefetch(all_hits)

        records_with_src: list[tuple[int, list[str], Any]] = []
        for cid, (src_sources, src) in sources_by_case.items():
            hits = hits_by_case[cid]
            if not hits:
                continue
            recs = rs_mod.resolve_case(
                src, hits, lookup, stats=stats, ecli_scope=rs_mod.ECLI_SCOPE_CROSS
            )
            for rec in recs:
                records_with_src.append((cid, src_sources, rec))

        r1, r2, r3 = _flush_link_records(
            client, lookup, records_with_src,
            resolved_only=resolved_only, chunk_size=chunk_size,
            dry_run=dry_run, explain_sql=explain_sql,
            explained_flag=explained, stats=stats,
        )
        ins_res += r1
        ins_ue += r2
        ins_ur += r3

        if scanned % 1000 < len(rows):
            print(
                f"[rs_text] scanned={scanned} last_id={cursor_id} res={ins_res} ue={ins_ue} ur={ins_ur}",
                file=sys.stderr,
                flush=True,
            )
        if len(rows) < lim:
            break

    return {
        "mode": "rs_text",
        "resolved_only": resolved_only,
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "inserted_resolved": ins_res,
        "inserted_unresolved_ecli": ins_ue,
        "inserted_unresolved_ref": ins_ur,
        "stats": dict(sorted(stats.items())),
    }


def run_echr_text(
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
    resolved_only: bool,
) -> dict[str, Any]:
    echr_mod = cle["echr_text"]
    tc = cle["text_common"]
    art = cle["artifact"]
    lookup = RunnerCandidateLookup(client, cle["resolver"])

    bounds = client.query_rows(
        "SELECT min(case_id) AS lo, max(case_id) AS hi FROM echr_document"
    )[0]
    if bounds["lo"] is None:
        return {"mode": "echr_text", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(int(bounds["lo"]) - 1, from_id - 1) if from_id > 0 else int(bounds["lo"]) - 1

    stats: Counter[str] = Counter()
    scanned = 0
    ins_res = ins_ue = ins_ur = 0
    explained = [False]

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        id_rows = client.query_rows(
            """
            SELECT DISTINCT case_id
              FROM echr_document
             WHERE case_id > %s AND case_id <= %s
             ORDER BY case_id
             LIMIT %s
            """,
            params=[cursor_id, hi, lim],
        )
        if not id_rows:
            break
        page_ids = [int(r["case_id"]) for r in id_rows]
        scanned += len(page_ids)
        cursor_id = page_ids[-1]
        rows = client.query_rows(
            """
            SELECT c.id, c.ecli, c.celex_id, c.item_id, c.date_decision, c.sources,
                   ct.language, ct.fulltext, ct.summary
              FROM cases c
              JOIN case_text ct ON ct.case_id = c.id AND ct.source = 'HUDOC'
             WHERE c.id = ANY(%s::bigint[])
               AND (ct.fulltext IS NOT NULL OR ct.summary IS NOT NULL)
             ORDER BY c.id
            """,
            params=[page_ids],
        )

        scans = []
        cjeu_vals: set[str] = set()
        ljn_vals: set[str] = set()
        for r in rows:
            cid = int(r["id"])
            src_sources = list(r.get("sources") or ["ECHR"])
            src = tc.Source(
                art.CaseKey(
                    ecli=r.get("ecli") or None,
                    celex=r.get("celex_id") or None,
                    item_id=r.get("item_id") or None,
                ),
                _parse_date(r.get("date_decision")),
            )
            for field_name, text in (("fulltext", r.get("fulltext")), ("summary", r.get("summary"))):
                if not text:
                    continue
                unit = tc.TextUnit(text, field_name, r.get("language"), echr_mod.HUDOC)
                found = echr_mod.scan(text)
                cjeu_vals.update(found.case_numbers())
                ljn_vals.update(found.ljn_values())
                scans.append((cid, src_sources, src, unit, found))

        if cjeu_vals:
            lookup.prefetch_scheme("cjeu_case_number", cjeu_vals)
        if ljn_vals:
            lookup.prefetch_scheme("ljn", ljn_vals)

        records_with_src: list[tuple[int, list[str], Any]] = []
        pending_by_case: dict[int, tuple[list[str], Any, list[Any]]] = {}
        for cid, src_sources, src, unit, found in scans:
            sections = echr_mod.Sections(unit.text, None)
            pend = echr_mod.resolve(found, unit, src, lookup, sections, domestic_flag=True)
            if pend:
                entry = pending_by_case.setdefault(cid, (src_sources, src, []))
                entry[2].extend(pend)
        for cid, (src_sources, src, pend) in pending_by_case.items():
            for rec in tc.build_records(src, pend, echr_mod.EXTRACTOR_VERSION):
                records_with_src.append((cid, src_sources, rec))

        r1, r2, r3 = _flush_link_records(
            client, lookup, records_with_src,
            resolved_only=resolved_only, chunk_size=chunk_size,
            dry_run=dry_run, explain_sql=explain_sql,
            explained_flag=explained, stats=stats,
        )
        ins_res += r1
        ins_ue += r2
        ins_ur += r3

        if scanned % 1000 < len(page_ids):
            print(
                f"[echr_text] scanned={scanned} last_id={cursor_id} res={ins_res} ue={ins_ue} ur={ins_ur}",
                file=sys.stderr,
                flush=True,
            )
        if len(page_ids) < lim:
            break

    return {
        "mode": "echr_text",
        "resolved_only": resolved_only,
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "inserted_resolved": ins_res,
        "inserted_unresolved_ecli": ins_ue,
        "inserted_unresolved_ref": ins_ur,
        "stats": dict(sorted(stats.items())),
    }


def run_cjeu_text(
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
    resolved_only: bool,
) -> dict[str, Any]:
    cjeu_mod = cle["cjeu_text"]
    tc = cle["text_common"]
    art = cle["artifact"]
    lookup = RunnerCandidateLookup(client, cle["resolver"])

    bounds = client.query_rows(
        "SELECT min(case_id) AS lo, max(case_id) AS hi FROM cjeu_document"
    )[0]
    if bounds["lo"] is None:
        return {"mode": "cjeu_text", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(int(bounds["lo"]) - 1, from_id - 1) if from_id > 0 else int(bounds["lo"]) - 1

    stats: Counter[str] = Counter()
    scanned = 0
    ins_res = ins_ue = ins_ur = 0
    explained = [False]

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        case_rows = client.query_rows(
            """
            SELECT c.id, c.ecli, c.celex_id, c.item_id, c.date_decision, c.sources
              FROM cjeu_document d
              JOIN cases c ON c.id = d.case_id
             WHERE d.case_id > %s AND d.case_id <= %s
             ORDER BY d.case_id
             LIMIT %s
            """,
            params=[cursor_id, hi, lim],
        )
        if not case_rows:
            break
        scanned += len(case_rows)
        cursor_id = int(case_rows[-1]["id"])
        page_ids = [int(r["id"]) for r in case_rows]
        meta_by_id = {int(r["id"]): r for r in case_rows}

        text_rows = client.query_rows(
            """
            SELECT DISTINCT ON (t.case_id, t.language)
                   t.case_id, t.language, t.source, t.fulltext
              FROM case_text t
             WHERE t.case_id = ANY(%s::bigint[])
               AND t.source IN ('CELLAR_ITEM', 'INFOCURIA_BLOB_HTML')
               AND t.document_kind IS DISTINCT FROM 'misfiled'
               AND t.fulltext IS NOT NULL AND btrim(t.fulltext) <> ''
             ORDER BY t.case_id, t.language,
                      CASE WHEN t.document_kind IN ('summary', 'information', 'oj_notice') THEN 2
                           WHEN t.document_kind = 'extract' THEN 1 ELSE 0 END,
                      CASE t.source WHEN 'CELLAR_ITEM' THEN 1 ELSE 2 END,
                      t.id
            """,
            params=[page_ids],
        )

        scans = []
        appno_vals: set[str] = set()
        for tr in text_rows:
            cid = int(tr["case_id"])
            m = meta_by_id[cid]
            src_sources = list(m.get("sources") or ["CJEU"])
            src = tc.Source(
                art.CaseKey(
                    ecli=m.get("ecli") or None,
                    celex=m.get("celex_id") or None,
                    item_id=m.get("item_id") or None,
                ),
                _parse_date(m.get("date_decision")),
            )
            unit = tc.TextUnit(tr["fulltext"], "fulltext", tr.get("language"), tr.get("source"))
            # name_dates=False per confirmed D5
            found = cjeu_mod.scan(tr["fulltext"], name_dates=False)
            appno_vals.update(found.appno_values())
            scans.append((cid, src_sources, src, unit, found))

        if appno_vals:
            lookup.prefetch_scheme("echr_appno", appno_vals)

        records_with_src: list[tuple[int, list[str], Any]] = []
        pending_by_case: dict[int, tuple[list[str], Any, list[Any]]] = {}
        for cid, src_sources, src, unit, found in scans:
            pend = cjeu_mod.resolve(found, unit, src, lookup, name_index=None)
            if pend:
                entry = pending_by_case.setdefault(cid, (src_sources, src, []))
                entry[2].extend(pend)
        for cid, (src_sources, src, pend) in pending_by_case.items():
            for rec in tc.build_records(src, pend, cjeu_mod.EXTRACTOR_VERSION):
                records_with_src.append((cid, src_sources, rec))

        r1, r2, r3 = _flush_link_records(
            client, lookup, records_with_src,
            resolved_only=resolved_only, chunk_size=chunk_size,
            dry_run=dry_run, explain_sql=explain_sql,
            explained_flag=explained, stats=stats,
        )
        ins_res += r1
        ins_ue += r2
        ins_ur += r3

        if scanned % 1000 < len(case_rows):
            print(
                f"[cjeu_text] scanned={scanned} last_id={cursor_id} res={ins_res} ue={ins_ue} ur={ins_ur}",
                file=sys.stderr,
                flush=True,
            )
        if len(case_rows) < lim:
            break

    return {
        "mode": "cjeu_text",
        "resolved_only": resolved_only,
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "inserted_resolved": ins_res,
        "inserted_unresolved_ecli": ins_ue,
        "inserted_unresolved_ref": ins_ur,
        "stats": dict(sorted(stats.items())),
    }


def run_cellar_referrals(
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
    ref_mod = cle["cellar_referrals"]
    id_mod = cle["identifiers"]
    loader_mod = cle["identifier_loader"]
    lookup = RunnerCandidateLookup(client, cle["resolver"])

    bounds = client.query_rows(
        "SELECT min(case_id) AS lo, max(case_id) AS hi FROM cjeu_document WHERE national_judgement_xml IS NOT NULL"
    )[0]
    if bounds["lo"] is None:
        return {"mode": "cellar_referrals", "scanned": 0}
    hi = to_id if to_id is not None else int(bounds["hi"])
    cursor_id = max(int(bounds["lo"]) - 1, from_id - 1) if from_id > 0 else int(bounds["lo"]) - 1

    stats: Counter[str] = Counter()
    date_cache: dict[dt.date, list[Any]] = {}
    scanned = 0
    ins_res = 0
    explained = [False]

    def fetch_dates(dates: set[dt.date]) -> None:
        missing = sorted(d for d in dates if d not in date_cache)
        # Fetch 2 dates per query so busy court days never exceed sql-runner's 1000-row cap.
        for i in range(0, len(missing), 2):
            batch_dates = missing[i : i + 2]
            iso_dates = [d.isoformat() for d in batch_dates]
            rows = client.query_rows(
                """
                SELECT c.id, c.ecli, r.date_decision, r.instance, r.zaaknummer,
                       r.language, r.date_published, r.replaces_identifier
                  FROM rs_document r
                  JOIN cases c ON c.id = r.case_id
                 WHERE r.date_decision = ANY(%s::date[])
                   AND c.ecli IS NOT NULL
                   AND (r.instance = ANY(%s::text[]) OR r.instance LIKE 'Gerechtshof%%'
                        OR r.instance LIKE 'Rechtbank%%' OR r.instance LIKE 'Kantongerecht%%')
                """,
                params=[iso_dates, list(ref_mod.NATIONAL_INSTANCES)],
            )
            for d in batch_dates:
                date_cache[d] = []
            for r in rows:
                dec = _parse_date(r.get("date_decision"))
                if dec is None:
                    continue
                zk_vars: set[str] = set()
                for z in loader_mod.split_zaaknummers(r.get("zaaknummer")):
                    zk_vars.update(id_mod.zaaknummer_variants(z))
                ljn_vals: set[str] = set()
                for ljn in loader_mod.split_ljns(r.get("replaces_identifier")):
                    norm = id_mod.normalize_ljn(ljn)
                    if norm:
                        ljn_vals.add(norm)
                date_cache.setdefault(dec, []).append(
                    ref_mod.RsCandidate(
                        ecli=r["ecli"],
                        decided=dec,
                        instance=r.get("instance"),
                        zaaknummer=r.get("zaaknummer"),
                        language=r.get("language"),
                        published=_parse_date(r.get("date_published")),
                        identifiers=tuple(sorted(zk_vars)),
                        ljns=tuple(sorted(ljn_vals)),
                    )
                )
                lookup._case_key_cache[("ecli", r["ecli"])] = (int(r["id"]), ["RS"])

    while True:
        lim = page_size if max_cases is None else min(page_size, max_cases - scanned)
        if lim <= 0:
            break
        rows = client.query_rows(
            """
            SELECT c.id, c.ecli, COALESCE(d.celex_id, c.celex_id) AS celex_id,
                   c.date_decision, d.national_judgement_xml
              FROM cjeu_document d
              JOIN cases c ON c.id = d.case_id
             WHERE d.national_judgement_xml IS NOT NULL
               AND d.case_id > %s AND d.case_id <= %s
             ORDER BY d.case_id
             LIMIT %s
            """,
            params=[cursor_id, hi, lim],
        )
        if not rows:
            break
        scanned += len(rows)
        cursor_id = int(rows[-1]["id"])

        docs = []
        needed_dates: set[dt.date] = set()
        for r in rows:
            doc = ref_mod.CjeuDocument(
                ecli=r.get("ecli"),
                celex=r.get("celex_id"),
                decided=_parse_date(r.get("date_decision")),
                xml=r["national_judgement_xml"],
            )
            lines = ref_mod.parse_notice(doc.xml)
            needed_dates.update(ref_mod._needed_dates(lines))
            docs.append((int(r["id"]), doc))

        if needed_dates:
            fetch_dates(needed_dates)

        raw_recs: list[tuple[int, Any]] = []
        for tgt_id, doc in docs:
            lines = ref_mod.parse_notice(doc.xml)
            cands = []
            for d in ref_mod._needed_dates(lines):
                cands.extend(date_cache.get(d, ()))
            for rec in ref_mod.links_for_document(doc, cands, stats=stats, date_court_fallback=False):
                raw_recs.append((tgt_id, rec))

        if raw_recs:
            lookup.prefetch_case_keys(rec.source for _, rec in raw_recs)
            res_batch: dict[tuple, tuple] = {}
            for tgt_id, rec in raw_recs:
                src_hit = lookup.resolve_case_key(rec.source)
                if src_hit is None:
                    stats["source_ecli_not_in_db"] += 1
                    continue
                src_id, _ = src_hit
                if src_id == tgt_id:
                    continue
                key = (src_id, tgt_id, rec.relation_type, rec.source_dataset)
                res_batch[key] = (
                    src_id, tgt_id, rec.relation_type, rec.source_dataset, True,
                    rec.extractor_version, rec.match_method, rec.confidence,
                )
            if res_batch:
                ins_res += client.execute_unnest_chunks(
                    INSERT_RESOLVED_SQL,
                    list(zip(*res_batch.values())),
                    chunk_size=chunk_size,
                    dry_run=dry_run,
                    explain_first=(dry_run and explain_sql and not explained[0]),
                )
                explained[0] = True

        if scanned % 1000 < len(rows):
            print(
                f"[cellar_referrals] scanned={scanned} last_id={cursor_id} res={ins_res}",
                file=sys.stderr,
                flush=True,
            )
        if len(rows) < lim:
            break

    return {
        "mode": "cellar_referrals",
        "last_id_scanned": cursor_id,
        "scanned": scanned,
        "inserted_resolved": ins_res,
        "stats": dict(sorted(stats.items())),
    }


def run_cellar_relations(
    client: SqlRunnerClient,
    cle: dict[str, Any],
    *,
    uuid_dossier_path: Path,
    uuid_celex_path: Path,
    chunk_size: int,
    dry_run: bool,
    explain_sql: bool,
) -> dict[str, Any]:
    rel_mod = cle["cellar_relations"]
    id_mod = cle["identifiers"]
    lookup = RunnerCandidateLookup(client, cle["resolver"])

    uuid_dossier = rel_mod.load_uuid_map(str(uuid_dossier_path)) if uuid_dossier_path.exists() else {}
    uuid_celex = rel_mod.load_uuid_map(str(uuid_celex_path)) if uuid_celex_path.exists() else {}

    stats: Counter[str] = Counter()
    last_cid = 0
    scanned = 0
    ins_res = ins_ue = ins_ur = 0
    explained = [False]
    rel_types = sorted(rel_mod.RELATIONS)

    while True:
        rows = client.query_rows(
            """
            SELECT cc.id, cc.relation_type, COALESCE(cc.target_celex_raw, cc.target_ecli_raw) AS raw_target,
                   s.id AS s_id, s.ecli AS s_ecli, COALESCE(sd.celex_id, s.celex_id) AS s_celex,
                   s.item_id AS s_item, s.date_decision AS s_date,
                   t.id AS t_id, t.ecli AS t_ecli, COALESCE(td.celex_id, t.celex_id) AS t_celex,
                   t.item_id AS t_item, t.date_decision AS t_date
              FROM case_citation cc
              JOIN cases s ON s.id = cc.source_case_id
              LEFT JOIN cjeu_document sd ON sd.case_id = s.id
              LEFT JOIN cases t ON t.id = cc.target_case_id
              LEFT JOIN cjeu_document td ON td.case_id = t.id
             WHERE cc.source_dataset = 'cellar_sparql'
               AND cc.relation_type = ANY(%s::text[])
               AND cc.id > %s
             ORDER BY cc.id
             LIMIT 500
            """,
            params=[rel_types, last_cid],
        )
        if not rows:
            break
        scanned += len(rows)
        last_cid = int(rows[-1]["id"])

        batch = []
        numbers: set[str] = set()
        celexes: set[str] = set()
        eclis: set[str] = set()
        for r in rows:
            sdoc = rel_mod.Document(
                int(r["s_id"]), r.get("s_ecli"), r.get("s_celex"), r.get("s_item"), _parse_date(r.get("s_date"))
            )
            tdoc = (
                rel_mod.Document(
                    int(r["t_id"]), r.get("t_ecli"), r.get("t_celex"), r.get("t_item"), _parse_date(r.get("t_date"))
                )
                if r.get("t_id")
                else None
            )
            crow = rel_mod.CellarRow(int(r["id"]), r["relation_type"], r.get("raw_target"), sdoc, tdoc)
            batch.append(crow)
            if tdoc is None and crow.raw_target:
                ref = rel_mod.parse_reference(crow.raw_target)
                if ref.kind == "uuid":
                    for v in uuid_dossier.get(ref.value, ()):
                        parsed = rel_mod.parse_dossier(v)
                        if parsed:
                            numbers.add(parsed.value)
                    for v in uuid_celex.get(ref.value, ()):
                        cx = id_mod.normalize_celex(v)
                        if cx:
                            celexes.add(cx)
                elif ref.kind == "case_number":
                    numbers.add(ref.value)
                elif ref.kind == "celex":
                    celexes.add(ref.value)
                elif ref.kind == "ecli":
                    eclis.add(ref.value)

        by_celex: dict[str, Any] = {}
        by_ecli: dict[str, Any] = {}
        if celexes or eclis:
            drows = client.query_rows(
                """
                SELECT c.id, c.ecli, COALESCE(d.celex_id, c.celex_id) AS celex_id, c.item_id, c.date_decision
                  FROM cases c
                  LEFT JOIN cjeu_document d ON d.case_id = c.id
                 WHERE c.celex_id = ANY(%s::text[]) OR d.celex_id = ANY(%s::text[]) OR c.ecli = ANY(%s::text[])
                """,
                params=[sorted(celexes), sorted(celexes), sorted(eclis)],
            )
            for dr in drows:
                doc = rel_mod.Document(
                    int(dr["id"]), dr.get("ecli"), dr.get("celex_id"), dr.get("item_id"), _parse_date(dr.get("date_decision"))
                )
                if dr.get("celex_id") in celexes:
                    by_celex[dr["celex_id"]] = doc
                if dr.get("ecli") in eclis:
                    by_ecli[dr["ecli"]] = doc
        by_number = lookup.get("cjeu_case_number", sorted(numbers)) if numbers else {}
        lookups = rel_mod.Lookups(uuid_dossier, uuid_celex, by_celex, by_ecli, by_number)
        recs, _idents = rel_mod.links_for_rows(batch, lookups, stats)

        if recs:
            lookup.prefetch_case_keys([r.source for r in recs] + [r.target for r in recs if r.target is not None])
            records_with_src = []
            for rec in recs:
                src_hit = lookup.resolve_case_key(rec.source)
                if src_hit is None:
                    stats["source_missing"] += 1
                    continue
                records_with_src.append((src_hit[0], src_hit[1], rec))
            r1, r2, r3 = _flush_link_records(
                client, lookup, records_with_src,
                resolved_only=False, chunk_size=chunk_size,
                dry_run=dry_run, explain_sql=explain_sql,
                explained_flag=explained, stats=stats,
            )
            ins_res += r1
            ins_ue += r2
            ins_ur += r3

        if len(rows) < 500:
            break

    return {
        "mode": "cellar_relations",
        "scanned": scanned,
        "inserted_resolved": ins_res,
        "inserted_unresolved_ecli": ins_ue,
        "inserted_unresolved_ref": ins_ur,
        "stats": dict(sorted(stats.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("ag_opinions", "rs_text", "echr_text", "cjeu_text", "cellar_referrals", "cellar_relations"),
        required=True,
    )
    parser.add_argument(
        "--uuid-dossier",
        type=Path,
        default=Path("/home/agent/projects/cjeu-migration-roadmap/migration/verify/research-2026-11-24/cjeu/uuid_dossier.json"),
    )
    parser.add_argument(
        "--uuid-celex",
        type=Path,
        default=Path("/home/agent/projects/cjeu-migration-roadmap/migration/verify/research-2026-11-24/cjeu/uuid_map.json"),
    )
    parser.add_argument("--cle-checkout", type=Path, default=Path("/home/agent/projects/cle-law-and-context"))
    parser.add_argument("--from-id", type=int, default=0)
    parser.add_argument("--to-id", type=int, default=None)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--resolved-only", action="store_true")
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
    elif args.mode == "rs_text":
        summary = run_rs_text(
            client, cle, from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain,
            max_cases=args.max_cases, resolved_only=args.resolved_only,
        )
    elif args.mode == "echr_text":
        summary = run_echr_text(
            client, cle, from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain,
            max_cases=args.max_cases, resolved_only=args.resolved_only,
        )
    elif args.mode == "cjeu_text":
        summary = run_cjeu_text(
            client, cle, from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain,
            max_cases=args.max_cases, resolved_only=args.resolved_only,
        )
    elif args.mode == "cellar_referrals":
        summary = run_cellar_referrals(
            client, cle, from_id=args.from_id, to_id=args.to_id,
            page_size=args.page_size, chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain,
            max_cases=args.max_cases,
        )
    else:
        summary = run_cellar_relations(
            client, cle,
            uuid_dossier_path=args.uuid_dossier,
            uuid_celex_path=args.uuid_celex,
            chunk_size=args.chunk_size,
            dry_run=dry_run, explain_sql=args.explain,
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
