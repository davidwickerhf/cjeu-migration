#!/usr/bin/env python3
"""One-shot GDPR Art. 5(1)(d) data-accuracy cleanup for people records in cle_v2.

Fixes four empirically audited accuracy defects across cle_v2.cjeu_ag_opinion,
cle_v2.judge, and cle_v2.case_judge:

  1. Concatenated & diacritic-variant ``cle_v2.cjeu_ag_opinion.advocate_general`` values:
       - Self-concatenations (e.g. ``SzpunarSzpunar`` -> ``Szpunar``,
         ``La PergolaLa Pergola`` -> ``La Pergola``).
       - 6 two-person fusions (``BotLéger``, ``SharpstonJacobs``, ``TrstenjakGeelhoed``,
         ``Stix-HacklTizzano``, ``Stix-HacklJacobs``, ``TizzanoMengozzi``) resolved per
         opinion row via verified ECLI/CELEX mapping and opinion header text.
       - Diacritic variant ``Capeta`` -> ``Ćapeta``.
  2. 6 OCR-spaced Dutch Parket bij de Hoge Raad (PHR) AG duplicates in
     ``cle_v2.judge`` + ``cle_v2.case_judge`` merged onto canonical judge rows
     (with old variant appended to ``judge.aliases``).
  3. 5 diacritic / capitalization duplicates in ``cle_v2.judge`` + ``cle_v2.case_judge``
     merged onto canonical judge rows (with old variant appended to ``judge.aliases``).
  4. Living + deceased CJEU surname homonyms in ``cle_v2.judge`` + ``cle_v2.case_judge``:
       - ``Rossi`` -> ``Rino Rossi`` (CJEU 1959-1964) vs ``Lucia Serena Rossi`` (CJEU 2018-2024).
       - ``O'Higgins`` / ``O’Higgins`` -> ``Thomas Francis O'Higgins`` (CJEU 1985-1991) vs
         ``Kevin O'Higgins`` (EGC 2006-2014).

Default mode is strictly read-only (DRY-RUN); pass ``--apply`` only after owner go-ahead.
Every ``--apply`` operation is idempotent and respects sql-runner's 10,000-row DELETE cap.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migration.backfill_cjeu_text_treaty import SqlRunnerClient  # noqa: E402

DEFAULT_URL = "https://demo-psql.caselawexplorer.tech"
_PROVENANCE_COLS = ("source_dataset", "extractor_version", "confidence")
_UPDATE_BATCH_SIZE = 500
_INSERT_BATCH_SIZE = 500
_DELETE_CHUNK_SIZE = 2000


# ---------------------------------------------------------------------------
# 1. cle_v2.cjeu_ag_opinion.advocate_general normalization rules
# ---------------------------------------------------------------------------

AG_DIACRITIC_MAP: Dict[str, str] = {
    "Capeta": "Ćapeta",
}

TWO_PERSON_FUSION_CANDIDATES: Dict[str, Tuple[str, str]] = {
    "BotLéger": ("Bot", "Léger"),
    "SharpstonJacobs": ("Sharpston", "Jacobs"),
    "Stix-HacklJacobs": ("Stix-Hackl", "Jacobs"),
    "Stix-HacklTizzano": ("Stix-Hackl", "Tizzano"),
    "TizzanoMengozzi": ("Tizzano", "Mengozzi"),
    "TrstenjakGeelhoed": ("Trstenjak", "Geelhoed"),
}

# Verified against live cle_v2.cases (ECLI / CELEX) and multilingual cle_v2.case_text
# opinion headers. Each fused pair comes from a CJEU case where the oral procedure was
# reopened and a second Advocate General delivered a second Opinion:
#   - base CELEX (e.g. 62005CC0110)     = first Opinion
#   - CELEX(01)  (e.g. 62005CC0110(01)) = second Opinion
VERIFIED_FUSION_BY_ECLI: Dict[Tuple[str, str], Tuple[str, str]] = {
    (
        "BotLéger",
        "ECLI:EU:C:2006:646",
    ): (
        "Léger",
        "CELEX 62005CC0110 first Opinion (Philippe Léger, delivered 2006-10-05)",
    ),
    (
        "BotLéger",
        "ECLI:EU:C:2008:386",
    ): (
        "Bot",
        "CELEX 62005CC0110(01) second Opinion referencing AG Léger's first Opinion (Yves Bot, delivered 2008-07-08)",
    ),
    (
        "SharpstonJacobs",
        "ECLI:EU:C:2005:656",
    ): (
        "Jacobs",
        "CELEX 62004CC0227 first Opinion (F.G. Jacobs, delivered 2005-10-27)",
    ),
    (
        "SharpstonJacobs",
        "ECLI:EU:C:2006:748",
    ): (
        "Sharpston",
        "CELEX 62004CC0227(01) second Opinion (Eleanor Sharpston, delivered 2006-11-30)",
    ),
    (
        "Stix-HacklJacobs",
        "ECLI:EU:C:2005:183",
    ): (
        "Jacobs",
        "CELEX 62003CC0475 first Opinion (F.G. Jacobs, delivered 2005-03-17)",
    ),
    (
        "Stix-HacklJacobs",
        "ECLI:EU:C:2006:172",
    ): (
        "Stix-Hackl",
        "CELEX 62003CC0475(01) second Opinion (Christine Stix-Hackl, delivered 2006-03-14)",
    ),
    (
        "Stix-HacklTizzano",
        "ECLI:EU:C:2005:676",
    ): (
        "Tizzano",
        "CELEX 62004CC0292 first Opinion (Antonio Tizzano, delivered 2005-11-10)",
    ),
    (
        "Stix-HacklTizzano",
        "ECLI:EU:C:2006:642",
    ): (
        "Stix-Hackl",
        "CELEX 62004CC0292(01) second Opinion (Christine Stix-Hackl, delivered 2006-10-05)",
    ),
    (
        "TizzanoMengozzi",
        "ECLI:EU:C:2006:213",
    ): (
        "Tizzano",
        "CELEX 62004CC0170 first Opinion (Antonio Tizzano, delivered 2006-03-30)",
    ),
    (
        "TizzanoMengozzi",
        "ECLI:EU:C:2006:747",
    ): (
        "Mengozzi",
        "CELEX 62004CC0170(01) second Opinion (Paolo Mengozzi, delivered 2006-11-30)",
    ),
    (
        "TrstenjakGeelhoed",
        "ECLI:EU:C:2005:390",
    ): (
        "Geelhoed",
        "CELEX 62003CC0466 first Opinion (L.A. Geelhoed, delivered 2005-06-16)",
    ),
    (
        "TrstenjakGeelhoed",
        "ECLI:EU:C:2007:145",
    ): (
        "Trstenjak",
        "CELEX 62003CC0466(01) second Opinion (Verica Trstenjak, delivered 2007-03-08)",
    ),
}


def _fold_ascii(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    ).lower()


def strip_self_concatenation(name: str) -> Optional[str]:
    """Detect exact ``X + X`` self-concatenation (e.g. ``SzpunarSzpunar`` -> ``Szpunar``)."""
    cleaned = (name or "").strip()
    if len(cleaned) < 6 or len(cleaned) % 2 != 0:
        return None
    half = cleaned[: len(cleaned) // 2]
    if half == cleaned[len(cleaned) // 2 :] and half == half.strip() and len(half) >= 3:
        return half
    return None


def resolve_two_person_fusion(
    fused: str,
    ecli: Optional[str] = None,
    text_headers: Sequence[str] = (),
) -> Optional[Tuple[str, str]]:
    """Resolve a fused two-person AG string to ``(canonical_ag, basis)`` for one opinion row."""
    norm_ecli = (ecli or "").strip().upper()
    verified = VERIFIED_FUSION_BY_ECLI.get((fused, norm_ecli))
    if verified is not None:
        return verified

    candidates = TWO_PERSON_FUSION_CANDIDATES.get(fused)
    if not candidates:
        return None

    # Fallback: check whether multilingual opinion header lines unambiguously name one candidate.
    matched: set[str] = set()
    for header in text_headers:
        snippet = _fold_ascii((header or "")[:260])
        for cand in candidates:
            folded_cand = _fold_ascii(cand)
            if re.search(rf"\b{re.escape(folded_cand)}", snippet):
                matched.add(cand)
    if len(matched) == 1:
        winner = next(iter(matched))
        return winner, "unambiguous_opinion_header_match"
    return None


def normalize_ag_opinion_value(
    name: Optional[str],
    *,
    ecli: Optional[str] = None,
    text_headers: Sequence[str] = (),
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Classify one ``cjeu_ag_opinion.advocate_general`` value.

    Returns ``(target_name, category, basis)``, or ``(None, None, None)`` if already clean
    (or ``(None, 'unresolved_fusion', reason)`` if a known two-person fusion cannot be resolved).
    """
    if not name:
        return None, None, None

    if name in AG_DIACRITIC_MAP:
        return AG_DIACRITIC_MAP[name], "diacritic", "verified_diacritic_canonicalization"

    unconcat = strip_self_concatenation(name)
    if unconcat is not None:
        canonical = AG_DIACRITIC_MAP.get(unconcat, unconcat)
        return canonical, "self_concatenation", "exact_half_deduplication"

    if name in TWO_PERSON_FUSION_CANDIDATES:
        resolved = resolve_two_person_fusion(name, ecli=ecli, text_headers=text_headers)
        if resolved is not None:
            target, basis = resolved
            return target, "two_person_fusion", basis
        return None, "unresolved_fusion", f"no verified or unambiguous header resolution for {name!r} ({ecli!r})"

    return None, None, None


# ---------------------------------------------------------------------------
# 2 & 3. cle_v2.judge duplicate merges (OCR PHR AG + diacritic/case variants)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JudgeMergeSpec:
    category: str
    old_name: str
    canonical_name: str


JUDGE_MERGE_SPECS: Tuple[JudgeMergeSpec, ...] = (
    # (b) 6 OCR-spaced Dutch PHR AG duplicates
    JudgeMergeSpec("ocr_phr_ag", "D.J. M .W. Paridaens", "D.J.M.W. Paridaens"),
    JudgeMergeSpec("ocr_phr_ag", "T .N.B. M . Spronken", "T.N.B.M. Spronken"),
    JudgeMergeSpec("ocr_phr_ag", "B .F. Keulen", "B.F. Keulen"),
    JudgeMergeSpec("ocr_phr_ag", "D. J. C. Aben", "D.J.C. Aben"),
    JudgeMergeSpec("ocr_phr_ag", "W. L. Valk", "W.L. Valk"),
    JudgeMergeSpec("ocr_phr_ag", "P.H.P.H.M.C van Kempen", "P.H.P.H.M.C. van Kempen"),
    # (c) 5 diacritic / capitalization duplicates
    JudgeMergeSpec("diacritic_or_case", "Canolic Mingorance Cairat", "Canòlic Mingorance Cairat"),
    JudgeMergeSpec("diacritic_or_case", "Sebastian Raduletu", "Sebastian Răduleţu"),
    JudgeMergeSpec("diacritic_or_case", "Diez de Velasco", "Díez de Velasco"),
    JudgeMergeSpec("diacritic_or_case", "Cruz Vilaça", "da Cruz Vilaça"),
    JudgeMergeSpec("diacritic_or_case", "M. Van der Woude", "van der Woude"),
)


# ---------------------------------------------------------------------------
# 4. Living + deceased CJEU surname homonym splits
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HomonymTargetRule:
    target_name: str
    court_code: str
    ecli_prefix: str
    min_year: int
    max_year: int
    aliases: Tuple[str, ...]
    era_note: str


@dataclass(frozen=True)
class HomonymSplitSpec:
    group_key: str
    source_names: Tuple[str, ...]
    rules: Tuple[HomonymTargetRule, ...]


HOMONYM_SPLIT_SPECS: Tuple[HomonymSplitSpec, ...] = (
    HomonymSplitSpec(
        group_key="rossi",
        source_names=("Rossi",),
        rules=(
            HomonymTargetRule(
                target_name="Rino Rossi",
                court_code="CJEU",
                ecli_prefix="ECLI:EU:C:",
                min_year=1958,
                max_year=1966,
                aliases=("Rossi",),
                era_note="ECJ/CJEU Judge 1958-1964 (deceased 1974)",
            ),
            HomonymTargetRule(
                target_name="Lucia Serena Rossi",
                court_code="CJEU",
                ecli_prefix="ECLI:EU:C:",
                min_year=2018,
                max_year=2026,
                aliases=("Rossi",),
                era_note="CJEU Judge 2018-2024 (living)",
            ),
        ),
    ),
    HomonymSplitSpec(
        group_key="ohiggins",
        source_names=("O'Higgins", "O\u2019Higgins"),
        rules=(
            HomonymTargetRule(
                target_name="Thomas Francis O'Higgins",
                court_code="CJEU",
                ecli_prefix="ECLI:EU:C:",
                min_year=1984,
                max_year=1993,
                aliases=("O'Higgins",),
                era_note="ECJ/CJEU Judge 1985-1991 (deceased 2003)",
            ),
            HomonymTargetRule(
                target_name="Kevin O'Higgins",
                court_code="EGC",
                ecli_prefix="ECLI:EU:T:",
                min_year=2006,
                max_year=2015,
                aliases=("O'Higgins", "O\u2019Higgins"),
                era_note="General Court (EGC) Judge 2006-2013 (living; includes 2014 order)",
            ),
        ),
    ),
)


def _extract_year(date_decision: Any, ecli: Optional[str] = None) -> Optional[int]:
    if isinstance(date_decision, dt.datetime):
        return date_decision.year
    if isinstance(date_decision, dt.date):
        return date_decision.year
    if isinstance(date_decision, str) and len(date_decision) >= 4 and date_decision[:4].isdigit():
        return int(date_decision[:4])
    if ecli:
        parts = ecli.strip().split(":")
        if len(parts) >= 4 and parts[3].isdigit():
            return int(parts[3])
    return None


def classify_homonym_case(
    source_name: str,
    court_code: Optional[str],
    date_decision: Any,
    ecli: Optional[str] = None,
) -> Optional[str]:
    """Map one homonym ``case_judge`` row to its disambiguated judge full_name."""
    year = _extract_year(date_decision, ecli=ecli)
    if year is None:
        return None
    norm_court = (court_code or "").strip().upper()
    norm_ecli = (ecli or "").strip().upper()

    for spec in HOMONYM_SPLIT_SPECS:
        if source_name not in spec.source_names:
            continue
        matches = []
        for rule in spec.rules:
            court_ok = (norm_court == rule.court_code) or (
                not norm_court and norm_ecli.startswith(rule.ecli_prefix)
            )
            if court_ok and rule.min_year <= year <= rule.max_year:
                matches.append(rule.target_name)
        if len(matches) == 1:
            return matches[0]
        return None
    return None


def merge_alias_list(
    canonical_name: str,
    existing_aliases: Optional[Sequence[str]],
    add_aliases: Iterable[str],
) -> List[str]:
    """Return sorted, deduplicated aliases excluding empty strings and ``canonical_name``."""
    combined = set()
    for val in existing_aliases or ():
        s = (val or "").strip()
        if s and s != canonical_name:
            combined.add(s)
    for val in add_aliases:
        s = (val or "").strip()
        if s and s != canonical_name:
            combined.add(s)
    return sorted(combined)


# ---------------------------------------------------------------------------
# Database helpers (read-only planning + safe idempotent --apply)
# ---------------------------------------------------------------------------

def _detect_case_judge_extra_cols(client: SqlRunnerClient) -> List[str]:
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


def _delete_case_judge_by_judge_chunked(client: SqlRunnerClient, judge_id: int) -> int:
    """Delete case_judge rows for ``judge_id`` in chunks below sql-runner's 10k row cap."""
    total = 0
    while True:
        res = client.execute(
            "DELETE FROM cle_v2.case_judge "
            " WHERE ctid IN ("
            "     SELECT ctid FROM cle_v2.case_judge "
            "      WHERE judge_id = %s "
            "      LIMIT %s"
            " )",
            [judge_id, _DELETE_CHUNK_SIZE],
        )
        deleted = int(res.get("row_count") or 0)
        total += deleted
        if deleted < _DELETE_CHUNK_SIZE:
            break
    return total


def _delete_case_judge_by_ids_chunked(client: SqlRunnerClient, row_ids: Sequence[int]) -> int:
    """Delete exact case_judge.id rows in chunks below sql-runner's 10k row cap."""
    total = 0
    for offset in range(0, len(row_ids), _DELETE_CHUNK_SIZE):
        chunk = list(row_ids[offset : offset + _DELETE_CHUNK_SIZE])
        res = client.execute(
            "DELETE FROM cle_v2.case_judge WHERE id = ANY(%s)",
            [chunk],
        )
        total += int(res.get("row_count") or 0)
    return total


def _append_judge_aliases(client: SqlRunnerClient, judge_id: int, new_aliases: Sequence[str]) -> int:
    clean = [a.strip() for a in new_aliases if a and a.strip()]
    if not clean:
        return 0
    res = client.execute(
        "UPDATE cle_v2.judge "
        "   SET aliases = ("
        "       SELECT ARRAY("
        "           SELECT DISTINCT a "
        "             FROM unnest(COALESCE(aliases, ARRAY[]::text[]) || %s::text[]) AS a "
        "            WHERE a IS NOT NULL AND btrim(a) <> '' AND a <> full_name "
        "            ORDER BY a"
        "       )"
        "   ) "
        " WHERE id = %s",
        [clean, judge_id],
    )
    return int(res.get("row_count") or 0)


def run_ag_opinion_cleanup(client: SqlRunnerClient, *, apply: bool) -> Dict[str, Any]:
    """Audit and optionally normalize ``cle_v2.cjeu_ag_opinion.advocate_general``."""
    distinct_rows = client.query(
        "SELECT advocate_general, COUNT(*) AS cnt "
        "  FROM cle_v2.cjeu_ag_opinion "
        " WHERE advocate_general IS NOT NULL "
        " GROUP BY advocate_general "
        " ORDER BY advocate_general"
    )

    defective_values: List[str] = []
    for r in distinct_rows:
        ag = r["advocate_general"]
        if (
            ag in AG_DIACRITIC_MAP
            or strip_self_concatenation(ag) is not None
            or ag in TWO_PERSON_FUSION_CANDIDATES
        ):
            defective_values.append(ag)

    if not defective_values:
        return {
            "distinct_non_null_values_scanned": len(distinct_rows),
            "defective_distinct_values": 0,
            "planned_updates": 0,
            "applied_updates": 0,
            "by_category": {},
            "self_concatenations": [],
            "diacritic_fixes": [],
            "two_person_fusions": [],
            "unresolved_fusions": [],
        }

    target_rows = client.query(
        "SELECT o.id, o.case_id, o.advocate_general, c.ecli, c.celex_id "
        "  FROM cle_v2.cjeu_ag_opinion o "
        "  LEFT JOIN cle_v2.cases c ON c.id = o.case_id "
        " WHERE o.advocate_general = ANY(%s) "
        " ORDER BY o.advocate_general, o.id",
        [defective_values],
    )

    # Fetch short opinion headers only if a two-person fusion isn't in VERIFIED_FUSION_BY_ECLI.
    unverified_fusion_case_ids = sorted(
        {
            int(r["case_id"])
            for r in target_rows
            if r["advocate_general"] in TWO_PERSON_FUSION_CANDIDATES
            and (r["advocate_general"], (r["ecli"] or "").strip().upper())
            not in VERIFIED_FUSION_BY_ECLI
        }
    )
    headers_by_case: Dict[int, List[str]] = defaultdict(list)
    if unverified_fusion_case_ids:
        hrows = client.query(
            "SELECT case_id, LEFT(fulltext, 300) AS head "
            "  FROM cle_v2.case_text "
            " WHERE case_id = ANY(%s) AND fulltext IS NOT NULL",
            [unverified_fusion_case_ids],
        )
        for hr in hrows:
            headers_by_case[int(hr["case_id"])].append(hr["head"] or "")

    updates: List[Dict[str, Any]] = []
    by_category: Counter = Counter()
    self_concat_counts: Counter = Counter()
    diacritic_counts: Counter = Counter()
    fusion_resolutions: List[Dict[str, Any]] = []
    unresolved_fusions: List[Dict[str, Any]] = []

    for r in target_rows:
        oid = int(r["id"])
        cid = int(r["case_id"])
        old_ag = r["advocate_general"]
        ecli = r["ecli"] or ""
        celex_id = r["celex_id"] or ""
        target, category, basis = normalize_ag_opinion_value(
            old_ag,
            ecli=ecli,
            text_headers=headers_by_case.get(cid, ()),
        )
        if target is None:
            if category == "unresolved_fusion":
                unresolved_fusions.append({
                    "opinion_id": oid,
                    "case_id": cid,
                    "ecli": ecli,
                    "celex_id": celex_id,
                    "from_ag": old_ag,
                    "reason": basis,
                })
            continue

        by_category[category] += 1
        updates.append({"id": oid, "old_ag": old_ag, "new_ag": target})
        if category == "self_concatenation":
            self_concat_counts[(old_ag, target)] += 1
        elif category == "diacritic":
            diacritic_counts[(old_ag, target)] += 1
        elif category == "two_person_fusion":
            fusion_resolutions.append({
                "opinion_id": oid,
                "case_id": cid,
                "ecli": ecli,
                "celex_id": celex_id,
                "from_ag": old_ag,
                "to_ag": target,
                "basis": basis,
            })

    applied_updates = 0
    if apply and updates:
        for offset in range(0, len(updates), _UPDATE_BATCH_SIZE):
            batch = updates[offset : offset + _UPDATE_BATCH_SIZE]
            res = client.execute(
                "UPDATE cle_v2.cjeu_ag_opinion o "
                "   SET advocate_general = x->>'new_ag' "
                "  FROM jsonb_array_elements(%s::jsonb) AS x "
                " WHERE o.id = (x->>'id')::bigint "
                "   AND o.advocate_general = x->>'old_ag'",
                [json.dumps(batch, ensure_ascii=False)],
            )
            applied_updates += int(res.get("row_count") or 0)

    return {
        "distinct_non_null_values_scanned": len(distinct_rows),
        "defective_distinct_values": len(defective_values),
        "planned_updates": len(updates),
        "applied_updates": applied_updates,
        "by_category": dict(by_category),
        "self_concatenations": [
            {"from_ag": old, "to_ag": new, "rows": cnt}
            for (old, new), cnt in sorted(self_concat_counts.items())
        ],
        "diacritic_fixes": [
            {"from_ag": old, "to_ag": new, "rows": cnt}
            for (old, new), cnt in sorted(diacritic_counts.items())
        ],
        "two_person_fusions": fusion_resolutions,
        "unresolved_fusions": unresolved_fusions,
    }


def run_judge_duplicate_merges(
    client: SqlRunnerClient,
    *,
    extra_cols: Sequence[str],
    apply: bool,
) -> Dict[str, Any]:
    """Audit and optionally merge OCR-spaced PHR AG and diacritic/case judge duplicates."""
    all_names = sorted(
        {spec.old_name for spec in JUDGE_MERGE_SPECS}
        | {spec.canonical_name for spec in JUDGE_MERGE_SPECS}
    )
    jrows = client.query(
        "SELECT j.id, j.full_name, j.aliases, COUNT(cj.id) AS link_cnt "
        "  FROM cle_v2.judge j "
        "  LEFT JOIN cle_v2.case_judge cj ON cj.judge_id = j.id "
        " WHERE j.full_name = ANY(%s) "
        " GROUP BY j.id, j.full_name, j.aliases "
        " ORDER BY j.id",
        [all_names],
    )
    by_exact_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in jrows:
        by_exact_name[r["full_name"]].append(r)

    insert_cols_sql = ", ".join(["case_id", "judge_id", "role", *extra_cols])
    select_extra_sql = "".join(f", cj.{c}" for c in extra_cols)

    plans: List[Dict[str, Any]] = []
    totals = Counter()

    for spec in JUDGE_MERGE_SPECS:
        old_matches = by_exact_name.get(spec.old_name, [])
        canon_matches = by_exact_name.get(spec.canonical_name, [])
        if not old_matches:
            plans.append({
                "category": spec.category,
                "old_name": spec.old_name,
                "canonical_name": spec.canonical_name,
                "action": "already_clean",
            })
            totals["already_clean"] += 1
            continue

        # Prefer lowest id if multiple canonical rows somehow existed.
        canon_row = canon_matches[0] if canon_matches else None

        for old_row in old_matches:
            old_id = int(old_row["id"])
            old_aliases = list(old_row.get("aliases") or [])
            link_cnt = int(old_row["link_cnt"])

            if canon_row is not None and int(canon_row["id"]) != old_id:
                canon_id = int(canon_row["id"])
                canon_aliases = list(canon_row.get("aliases") or [])
                ov_rows = client.query(
                    "SELECT COUNT(*) AS cnt "
                    "  FROM cle_v2.case_judge a "
                    "  JOIN cle_v2.case_judge b "
                    "    ON a.case_id = b.case_id AND a.role = b.role "
                    " WHERE a.judge_id = %s AND b.judge_id = %s",
                    [old_id, canon_id],
                )
                conflict_cnt = int(ov_rows[0]["cnt"]) if ov_rows else 0
                net_insert = max(0, link_cnt - conflict_cnt)
                aliases_to_add = [spec.old_name, *old_aliases]
                resulting_aliases = merge_alias_list(
                    spec.canonical_name, canon_aliases, aliases_to_add
                )

                inserted = 0
                deleted_links = 0
                deleted_judge = 0
                if apply:
                    res_ins = client.execute(
                        f"INSERT INTO cle_v2.case_judge ({insert_cols_sql}) "
                        f"SELECT cj.case_id, %s, cj.role{select_extra_sql} "
                        "  FROM cle_v2.case_judge cj "
                        " WHERE cj.judge_id = %s "
                        "ON CONFLICT (case_id, judge_id, role) DO NOTHING",
                        [canon_id, old_id],
                    )
                    inserted = int(res_ins.get("row_count") or 0)
                    _append_judge_aliases(client, canon_id, aliases_to_add)
                    deleted_links = _delete_case_judge_by_judge_chunked(client, old_id)
                    res_del = client.execute(
                        "DELETE FROM cle_v2.judge "
                        " WHERE id = %s "
                        "   AND NOT EXISTS (SELECT 1 FROM cle_v2.case_judge WHERE judge_id = %s)",
                        [old_id, old_id],
                    )
                    deleted_judge = int(res_del.get("row_count") or 0)

                plans.append({
                    "category": spec.category,
                    "action": "merge",
                    "old_id": old_id,
                    "old_name": spec.old_name,
                    "canonical_id": canon_id,
                    "canonical_name": spec.canonical_name,
                    "source_case_judge_rows": link_cnt,
                    "conflicting_rows_deduped": conflict_cnt,
                    "planned_net_case_judge_inserts": net_insert,
                    "resulting_aliases": resulting_aliases,
                    "applied_case_judge_inserts": inserted,
                    "applied_case_judge_deletes": deleted_links,
                    "applied_judge_deletes": deleted_judge,
                })
                totals["planned_merges"] += 1
                totals[f"planned_{spec.category}"] += 1
                totals["planned_source_links"] += link_cnt
                totals["planned_net_inserts"] += net_insert
                totals["planned_conflicts_deduped"] += conflict_cnt
                totals["applied_case_judge_inserts"] += inserted
                totals["applied_case_judge_deletes"] += deleted_links
                totals["applied_judge_deletes"] += deleted_judge
            else:
                # Canonical row did not yet exist: rename in place and preserve old variant as alias.
                resulting_aliases = merge_alias_list(
                    spec.canonical_name, old_aliases, [spec.old_name]
                )
                renamed = 0
                if apply:
                    res_ren = client.execute(
                        "UPDATE cle_v2.judge SET full_name = %s WHERE id = %s",
                        [spec.canonical_name, old_id],
                    )
                    renamed = int(res_ren.get("row_count") or 0)
                    _append_judge_aliases(client, old_id, [spec.old_name])
                plans.append({
                    "category": spec.category,
                    "action": "rename",
                    "old_id": old_id,
                    "old_name": spec.old_name,
                    "canonical_id": old_id,
                    "canonical_name": spec.canonical_name,
                    "source_case_judge_rows": link_cnt,
                    "resulting_aliases": resulting_aliases,
                    "applied_renames": renamed,
                })
                totals["planned_renames"] += 1
                totals[f"planned_{spec.category}"] += 1
                totals["applied_renames"] += renamed

    return {
        "summary": dict(totals),
        "merges": plans,
    }


def run_homonym_splits(
    client: SqlRunnerClient,
    *,
    extra_cols: Sequence[str],
    apply: bool,
) -> Dict[str, Any]:
    """Audit and optionally split living + deceased CJEU surname homonyms."""
    source_names = sorted({n for spec in HOMONYM_SPLIT_SPECS for n in spec.source_names})
    target_names = sorted({r.target_name for spec in HOMONYM_SPLIT_SPECS for r in spec.rules})

    existing_rows = client.query(
        "SELECT id, full_name, aliases FROM cle_v2.judge WHERE full_name = ANY(%s) ORDER BY id",
        [sorted(set(source_names) | set(target_names))],
    )
    existing_by_name: Dict[str, Dict[str, Any]] = {}
    for r in existing_rows:
        existing_by_name.setdefault(r["full_name"], r)

    insert_cols_sql = ", ".join(["case_id", "judge_id", "role", *extra_cols])
    select_extra_sql = "".join(f", x->>'{c}'" if c != "confidence" else f", (x->>'{c}')::real" for c in extra_cols)
    extra_select_from_cj = "".join(f", cj.{c}" for c in extra_cols)

    split_reports: List[Dict[str, Any]] = []
    totals = Counter()

    for spec in HOMONYM_SPLIT_SPECS:
        rule_by_target = {r.target_name: r for r in spec.rules}
        for src_name in spec.source_names:
            src_row = existing_by_name.get(src_name)
            if src_row is None:
                split_reports.append({
                    "group": spec.group_key,
                    "source_name": src_name,
                    "action": "already_clean",
                })
                totals["sources_already_clean"] += 1
                continue

            src_id = int(src_row["id"])
            link_rows = client.query(
                f"SELECT cj.id AS cj_id, cj.case_id, cj.role{extra_select_from_cj}, "
                "       c.ecli, c.date_decision, co.code AS court_code "
                "  FROM cle_v2.case_judge cj "
                "  JOIN cle_v2.cases c ON c.id = cj.case_id "
                "  LEFT JOIN cle_v2.court co ON co.id = c.court_id "
                " WHERE cj.judge_id = %s "
                " ORDER BY c.date_decision NULLS LAST, cj.id",
                [src_id],
            )

            classified: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            unclassified: List[Dict[str, Any]] = []
            for lr in link_rows:
                target = classify_homonym_case(
                    src_name,
                    lr.get("court_code"),
                    lr.get("date_decision"),
                    lr.get("ecli"),
                )
                if target is None:
                    unclassified.append({
                        "cj_id": int(lr["cj_id"]),
                        "case_id": int(lr["case_id"]),
                        "ecli": lr.get("ecli"),
                        "court_code": lr.get("court_code"),
                        "date_decision": str(lr.get("date_decision")),
                        "role": lr.get("role"),
                    })
                else:
                    classified[target].append(lr)

            by_target_summary: Dict[str, Dict[str, Any]] = {}
            for target_name, items in sorted(classified.items()):
                dates = [str(it["date_decision"]) for it in items if it.get("date_decision")]
                courts = sorted({it["court_code"] for it in items if it.get("court_code")})
                roles = dict(Counter(it["role"] for it in items))
                rule = rule_by_target[target_name]
                by_target_summary[target_name] = {
                    "rows": len(items),
                    "min_date": min(dates) if dates else None,
                    "max_date": max(dates) if dates else None,
                    "courts": courts,
                    "roles": roles,
                    "era_note": rule.era_note,
                    "existing_target_id": (
                        int(existing_by_name[target_name]["id"])
                        if target_name in existing_by_name
                        else None
                    ),
                }

            applied_judges_created = 0
            applied_links_inserted = 0
            applied_links_deleted = 0
            applied_source_deleted = 0

            if apply and classified:
                # 1. Ensure each needed target judge exists & has the surname alias recorded.
                target_ids: Dict[str, int] = {}
                for target_name in sorted(classified.keys()):
                    existing_t = existing_by_name.get(target_name)
                    if existing_t is None:
                        res_new = client.execute(
                            "INSERT INTO cle_v2.judge (full_name) "
                            "SELECT %s "
                            " WHERE NOT EXISTS ("
                            "     SELECT 1 FROM cle_v2.judge WHERE lower(full_name) = lower(%s)"
                            " )",
                            [target_name, target_name],
                        )
                        applied_judges_created += int(res_new.get("row_count") or 0)
                        fetched = client.query(
                            "SELECT id, full_name, aliases FROM cle_v2.judge "
                            " WHERE lower(full_name) = lower(%s) ORDER BY id LIMIT 1",
                            [target_name],
                        )
                        if not fetched:
                            raise RuntimeError(f"Failed to resolve created target judge {target_name!r}")
                        existing_t = fetched[0]
                        existing_by_name[target_name] = existing_t
                    tid = int(existing_t["id"])
                    target_ids[target_name] = tid
                    _append_judge_aliases(client, tid, rule_by_target[target_name].aliases)

                # 2. Insert reassigned case_judge rows in bounded batches.
                insert_payload: List[Dict[str, Any]] = []
                reassigned_cj_ids: List[int] = []
                for target_name, items in classified.items():
                    tid = target_ids[target_name]
                    for it in items:
                        entry: Dict[str, Any] = {
                            "case_id": int(it["case_id"]),
                            "judge_id": tid,
                            "role": it["role"],
                        }
                        for c in extra_cols:
                            entry[c] = it.get(c)
                        insert_payload.append(entry)
                        reassigned_cj_ids.append(int(it["cj_id"]))

                for offset in range(0, len(insert_payload), _INSERT_BATCH_SIZE):
                    batch = insert_payload[offset : offset + _INSERT_BATCH_SIZE]
                    res_ins = client.execute(
                        f"INSERT INTO cle_v2.case_judge ({insert_cols_sql}) "
                        f"SELECT (x->>'case_id')::bigint, (x->>'judge_id')::bigint, x->>'role'{select_extra_sql} "
                        "  FROM jsonb_array_elements(%s::jsonb) AS x "
                        "ON CONFLICT (case_id, judge_id, role) DO NOTHING",
                        [json.dumps(batch, ensure_ascii=False)],
                    )
                    applied_links_inserted += int(res_ins.get("row_count") or 0)

                # 3. Delete only the classified source case_judge rows in chunks <= 2000.
                applied_links_deleted = _delete_case_judge_by_ids_chunked(client, reassigned_cj_ids)

                # 4. Delete conflated source judge ONLY if 0 unclassified rows and 0 links remain.
                if not unclassified:
                    res_del = client.execute(
                        "DELETE FROM cle_v2.judge "
                        " WHERE id = %s "
                        "   AND NOT EXISTS (SELECT 1 FROM cle_v2.case_judge WHERE judge_id = %s)",
                        [src_id, src_id],
                    )
                    applied_source_deleted = int(res_del.get("row_count") or 0)

            split_reports.append({
                "group": spec.group_key,
                "action": "split" if not unclassified else "partial_split_blocked_delete",
                "source_id": src_id,
                "source_name": src_name,
                "total_case_judge_rows": len(link_rows),
                "classified_rows": sum(len(v) for v in classified.values()),
                "unclassified_rows": len(unclassified),
                "unclassified_sample": unclassified[:10],
                "by_target": by_target_summary,
                "will_delete_source_judge": len(unclassified) == 0,
                "applied_judges_created": applied_judges_created,
                "applied_case_judge_inserts": applied_links_inserted,
                "applied_case_judge_deletes": applied_links_deleted,
                "applied_source_judge_deletes": applied_source_deleted,
            })
            totals["planned_sources_to_split"] += 1
            totals["planned_total_rows"] += len(link_rows)
            totals["planned_classified_rows"] += sum(len(v) for v in classified.values())
            totals["planned_unclassified_rows"] += len(unclassified)
            totals["applied_judges_created"] += applied_judges_created
            totals["applied_case_judge_inserts"] += applied_links_inserted
            totals["applied_case_judge_deletes"] += applied_links_deleted
            totals["applied_source_judge_deletes"] += applied_source_deleted

    return {
        "summary": dict(totals),
        "splits": split_reports,
    }


def run_cleanup(client: SqlRunnerClient, *, apply: bool) -> Dict[str, Any]:
    """Execute full Art. 5(1)(d) people accuracy audit (and optional --apply writes)."""
    extra_cols = _detect_case_judge_extra_cols(client)
    ag_report = run_ag_opinion_cleanup(client, apply=apply)
    merge_report = run_judge_duplicate_merges(client, extra_cols=extra_cols, apply=apply)
    split_report = run_homonym_splits(client, extra_cols=extra_cols, apply=apply)
    return {
        "mode": "APPLIED" if apply else "DRY-RUN",
        "case_judge_provenance_columns": extra_cols,
        "cjeu_ag_opinion_cleanup": ag_report,
        "judge_duplicate_merges": merge_report,
        "judge_homonym_splits": split_report,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=(
            "One-shot Art. 5(1)(d) data-accuracy cleanup for cle_v2 people tables "
            "(dry-run by default; pass --apply for writes)."
        )
    )
    ap.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"sql-runner base URL (default: {DEFAULT_URL})",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Apply idempotent writes to cle_v2 (default is read-only DRY-RUN)",
    )
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    token = os.getenv("SQL_RUNNER_TOKEN")
    if not token:
        print("SQL_RUNNER_TOKEN is required (run via anvil-secrets run)", file=sys.stderr)
        return 2
    client = SqlRunnerClient(args.url, token)
    report = run_cleanup(client, apply=args.apply)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
