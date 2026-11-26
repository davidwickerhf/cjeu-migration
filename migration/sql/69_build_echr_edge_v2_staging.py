#!/usr/bin/env python3
"""Build cle_v2.echr_edge_v2_staging (roadmap R6.1) and run the 300-row drop gate.

Reads HUDOC `echr_document.scl` citations through the Coolify sql-runner and
resolves each semicolon-separated `scl` entry to at most ONE ECtHR target case
using the R6.1 disambiguation rules:

  1. Candidate lookup uses ONLY documents that own the application number
     (`echr_document_appno WHERE source = 'appno'`, plus 9-digit ECLI appno
     fallback for recent cases loaded before CASE_IDENTIFIER_ENABLED was on;
     never `extractedappno`).
  2. Hard exclusions:
       - no self-loops (`target_case_id == source_case_id`)
       - no communicated cases (`doctype ~* 'COM'` with no JUD/DEC)
       - no targets dated after the source (`target_date > source_date`)
  3. Kind selection:
       - `(dec.)` / `(déc.)` / Commission decision cue -> prefer `DEC`
       - otherwise -> prefer `JUD` (fall back to `DEC` only if no `JUD` exists)
  4. Tie-breaking among multiple candidates of the chosen kind:
       a. exact cited date (EN/FR month names or DD.MM.YYYY)
       b. cited year (`ECHR YYYY-X`, `Reports YYYY-X`, `, YYYY`, etc.)
       c. Grand Chamber formation (`[GC]` / `(GC)` / `Grand Chamber`)
       d. explicit secondary cue (`just satisfaction`, `revision`,
          `interpretation`, `striking out`, `preliminary objections`,
          `article 50`) or principal judgment over secondary follow-up
       e. earliest `(date_decision, item_id, case_id)`

Writes ONLY to `cle_v2.echr_edge_v2_staging` (never touches `case_citation` or
`citation_source_dataset`). `DRY_RUN=1` by default; pass `--write` or set
`DRY_RUN=0` to insert into `echr_edge_v2_staging`.

Env:
  SQL_RUNNER_URL      e.g. https://demo-psql.caselawexplorer.tech
  SQL_RUNNER_TOKEN    HMAC token (never printed)
  SQL_RUNNER_CONFIRM  default execute-cle-v2
  DRY_RUN             default 1; set DRY_RUN=0 (or --write) to insert
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "migration" / "sql"))

from sql_runner_client import SqlRunnerClient  # noqa: E402

DEFAULT_BUILD_VERSION = "r6.1-v3"

# Application number pattern: 1-6 digits / 2 digits (not preceded/followed by digit or slash).
APPNO_RE = re.compile(r"(?<![0-9/])(\d{1,6})/(\d{2})(?![0-9/])")

# 9-digit appno tail in HUDOC ECLIs: ECLI:CE:ECHR:2026:1119JUD003766823 -> 37668/23
ECLI_APPNO_RE = re.compile(
    r"^ECLI:CE:ECHR:\d{4}:\d{4}(?:JUD|DEC|COM)(\d{7})(\d{2})$", re.IGNORECASE
)

# Decision cues in scl entries (including Eur. Comm. H.R. / Comm. eur. D.H. entries).
DECISION_CUE_RE = re.compile(
    r"(?:\(\s*d[eé]c\.?\s*\)"
    r"|\b(?:Commission\s+decision|d[eé]cision\s+de\s+la\s+Commission)\b"
    r"|\b(?:Eur\.?\s*Comm\.?\s*H\.?R\.?|Comm\.?\s*[Ee]ur\.?\s*D\.?H\.?)\b"
    r"|\bd[eé]c\.\s*\d{1,2}\s*\.\s*\d{1,2}\s*\.\s*\d{2,4}\b)",
    re.IGNORECASE,
)

# Sub-splitting patterns for older HUDOC scl rows where semicolons were omitted.
_NL_CONTINUATION_RE = re.compile(
    r"\s*\n+\s*(?=(?:d[eé]c\.|p\.|pp\.|§|D\.R\.|DR\b))", re.IGNORECASE
)
_DR_MISSING_SEMI_RE = re.compile(
    r"(pp?\.\s*\d+(?:,\s*\d+|[-–]\d+){0,6})\s*(?=(?:Nos?\.?\s+\d|N°|n°|Eur\.?\s*Comm\.?|Comm\.?\s*[Ee]ur\.?))"
)
_COMM_HEADER_SPLIT_RE = re.compile(
    r"(?<=\S)\s+(?=(?:Eur\.?\s*Comm\.?\s*H\.?R\.?|Comm\.?\s*[Ee]ur\.?\s*D\.?H\.?)\b)"
)
_AFTER_CITATION_TAIL_SPLIT_RE = re.compile(
    r"((?:ECHR|CEDH|Reports|Recueil)\s+(?:19|20)\d{2}(?:\s*[-–]\s*[IVX]+|\s+[IVX]+|\.\.\.)?(?:\s*\(extracts\))?"
    r"|\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|janvier|f[eé]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[eé]cembre)\s+(?:19|20)\d{2}"
    r"|\b\d{1,2}\.\d{1,2}\.(?:19|20)\d{2}"
    r"|\)\s*,\s*and)"
    r"(?:\s*[:.,]\s*|\s*)(?=(?-i:[A-ZÀ-Ý])[^\n;]{1,80}\s+(?:v\.|c\.)\s+)",
    re.IGNORECASE,
)
_SUBSPLIT_RE = re.compile(
    r"(?:(?<=\S)\s{2,}(?=(?:(?:Eur\.?\s*Comm\.?|Comm\.?\s*[Ee]ur\.?|Nos?\.|N°|n°|(?:Case\s+of|Affaire|Arr[eê]t)\b)"
    r"|[A-ZÀ-Ý][^\n;]{1,90}\s+(?:v\.|c\.)\s+))"
    r"|(?<=\S)\s+[:.]\s+(?=[A-ZÀ-Ý][^\n;]{1,90}\s+(?:v\.|c\.)\s+))"
)

# Grand Chamber cues in scl entries.
GC_CUE_RE = re.compile(
    r"(?:\[\s*GC\s*\]|\(\s*GC\s*\)|\bGrand\s+Chamber\b|\bGrande\s+Chambre\b)",
    re.IGNORECASE,
)

# Secondary judgment cues in scl entries and docnames.
SECONDARY_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "just_satisfaction",
        re.compile(
            r"(?:just\s+satisfaction|satisfaction\s+[eé]quitable|article\s+50|art\.\s*50|article\s+41|art\.\s*41)",
            re.IGNORECASE,
        ),
    ),
    ("revision", re.compile(r"(?:\(\s*r[eé]vision\s*\)|\br[eé]vision\b)", re.IGNORECASE)),
    (
        "interpretation",
        re.compile(r"(?:\(\s*interpr[eé]tation\s*\)|\binterpr[eé]tation\b)", re.IGNORECASE),
    ),
    (
        "striking_out",
        re.compile(r"(?:striking\s+out|radiation\s+du\s+r[oô]le|\(\s*radiation\s*\))", re.IGNORECASE),
    ),
    (
        "preliminary_objections",
        re.compile(
            r"(?:preliminary\s+objection|exception\s+pr[eé]liminaire|exceptions\s+pr[eé]liminaires)",
            re.IGNORECASE,
        ),
    ),
)

# Conclusion strings indicating a follow-up/secondary judgment when no merits violation/non-violation exists.
MERITS_CONCLUSION_RE = re.compile(
    r"\b(?:violation|non-violation|no\s+violation|no\s+separate\s+issue|aucune\s+question\s+distincte)\b",
    re.IGNORECASE,
)
SECONDARY_CONCLUSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "just_satisfaction",
        re.compile(
            r"(?:just\s+satisfaction|satisfaction\s+[eé]quitable|pecuniary\s+damage|non-pecuniary\s+damage|"
            r"dommage\s+mat[eé]riel|pr[eé]judice\s+moral|costs\s+and\s+expenses|frais\s+et\s+d[eé]pens)",
            re.IGNORECASE,
        ),
    ),
    ("revision", re.compile(r"(?:request\s+for\s+revision|demande\s+en\s+r[eé]vision|revision)", re.IGNORECASE)),
    (
        "interpretation",
        re.compile(r"(?:request\s+for\s+interpretation|demande\s+en\s+interpr[eé]tation)", re.IGNORECASE),
    ),
    ("striking_out", re.compile(r"(?:struck\s+out|radiation\s+du\s+r[oô]le)", re.IGNORECASE)),
    (
        "preliminary_objections",
        re.compile(r"(?:preliminary\s+objection|exception\s+pr[eé]liminaire)", re.IGNORECASE),
    ),
)

# Month maps (EN + FR, with and without diacritics).
MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "janvier": 1, "janv": 1,
    "february": 2, "feb": 2, "fevrier": 2, "février": 2, "fev": 2, "fév": 2,
    "march": 3, "mar": 3, "mars": 3,
    "april": 4, "apr": 4, "avril": 4, "avr": 4,
    "may": 5, "mai": 5,
    "june": 6, "jun": 6, "juin": 6,
    "july": 7, "jul": 7, "juillet": 7, "juil": 7,
    "august": 8, "aug": 8, "aout": 8, "août": 8,
    "september": 9, "sep": 9, "sept": 9, "septembre": 9,
    "october": 10, "oct": 10, "octobre": 10,
    "november": 11, "nov": 11, "novembre": 11,
    "december": 12, "dec": 12, "decembre": 12, "décembre": 12, "déc": 12,
}

_MONTH_ALT = "|".join(sorted(MONTHS.keys(), key=len, reverse=True))
DATE_DMY_WORD_RE = re.compile(
    rf"\b(\d{{1,2}})(?:er|st|nd|rd|th)?\s+({_MONTH_ALT})\.?\s+((?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)
DATE_MDY_WORD_RE = re.compile(
    rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+((?:19|20)\d{{2}})\b",
    re.IGNORECASE,
)
DATE_NUMERIC_RE = re.compile(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*((?:19|20)\d{2}|\d{2})\b")

# Year extraction from `ECHR 2000-VI`, `CEDH 2001-XI`, `Reports 1997-VI`, `, 2005`, `(2005)`.
YEAR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:ECHR|CEDH|Reports|Recueil)\s+((?:19|20)\d{2})\b", re.IGNORECASE),
    re.compile(r"(?:,\s*|\(\s*)((?:19|20)\d{2})(?:\s*\)|\s*[-–]\s*[IVX]+|\s*$|\s*,)", re.IGNORECASE),
    re.compile(r"\b((?:19[5-9]\d|20[0-2]\d))\b"),
)

INSERT_STAGING_SQL = """
INSERT INTO echr_edge_v2_staging (
    source_ecli,
    source_item_id,
    target_ecli,
    target_item_id,
    scl_reference,
    appno,
    rule,
    build_version
)
SELECT *
  FROM unnest(
      %s::text[],
      %s::text[],
      %s::text[],
      %s::text[],
      %s::text[],
      %s::text[],
      %s::text[],
      %s::text[]
  )
ON CONFLICT (
    COALESCE(source_ecli, source_item_id),
    COALESCE(target_ecli, target_item_id, ''),
    scl_reference,
    build_version
) DO NOTHING
"""


@dataclass(frozen=True)
class TargetCandidate:
    case_id: int
    ecli: Optional[str]
    item_id: Optional[str]
    date_decision: Optional[dt.date]
    kind: str  # 'judgment' | 'decision' | 'communicated' | 'other'
    branch: Optional[str]  # 'GRANDCHAMBER', 'CHAMBER', ...
    secondary_kind: Optional[str]  # None if principal, else 'just_satisfaction' | ...


@dataclass(frozen=True)
class ParsedSclEntry:
    raw: str
    appnos: tuple[str, ...]
    decision_cue: bool
    grand_chamber: bool
    cited_date: Optional[dt.date]
    cited_year: Optional[int]
    secondary_cue: Optional[str]


@dataclass(frozen=True)
class ResolvedEdge:
    source_case_id: int
    source_ecli: Optional[str]
    source_item_id: Optional[str]
    target_case_id: int
    target_ecli: Optional[str]
    target_item_id: Optional[str]
    scl_reference: str
    appno: str
    rule: str


def normalize_appno(raw: str) -> Optional[str]:
    """Normalize an ECtHR application number ('0046221/99' -> '46221/99')."""
    if not raw:
        return None
    m = APPNO_RE.search(raw.strip())
    if not m:
        return None
    num = str(int(m.group(1)))
    yr = m.group(2)
    return f"{num}/{yr}"


def appno_from_ecli(ecli: Optional[str]) -> Optional[str]:
    """Recover the primary owner appno from a 9-digit HUDOC ECLI tail."""
    if not ecli:
        return None
    m = ECLI_APPNO_RE.match(ecli.strip())
    if not m:
        return None
    num = int(m.group(1))
    if num <= 0:
        return None
    return f"{num}/{m.group(2)}"


def classify_target_kind(has_jud: bool, has_dec: bool, has_com: bool) -> str:
    if has_jud:
        return "judgment"
    if has_dec:
        return "decision"
    if has_com:
        return "communicated"
    return "other"


def classify_secondary_judgment(docname: Optional[str], conclusion: Optional[str]) -> Optional[str]:
    """Identify whether an ECtHR judgment is a secondary follow-up judgment.

    Checks docname suffixes first (e.g. '(ARTICLE 50)', '(JUST SATISFACTION)',
    '(REVISION)', '(INTERPRETATION)'), then falls back to `conclusion` when
    no merits violation/non-violation finding is present.
    """
    dn = docname or ""
    for label, pat in SECONDARY_CUES:
        if pat.search(dn):
            return label
    conc = conclusion or ""
    if conc and not MERITS_CONCLUSION_RE.search(conc):
        for label, pat in SECONDARY_CONCLUSION_PATTERNS:
            if pat.search(conc):
                return label
    return None


def parse_date_from_text(text: str) -> Optional[dt.date]:
    """Extract an exact calendar date from an EN or FR HUDOC scl entry."""
    for pat, order in (
        (DATE_DMY_WORD_RE, "dmy"),
        (DATE_MDY_WORD_RE, "mdy"),
        (DATE_NUMERIC_RE, "num"),
    ):
        m = pat.search(text)
        if not m:
            continue
        try:
            if order == "dmy":
                day = int(m.group(1))
                month = MONTHS[m.group(2).lower()]
                year = int(m.group(3))
            elif order == "mdy":
                month = MONTHS[m.group(1).lower()]
                day = int(m.group(2))
                year = int(m.group(3))
            else:
                day = int(m.group(1))
                month = int(m.group(2))
                yr_raw = m.group(3)
                year = int(yr_raw)
                if len(yr_raw) == 2:
                    year = 1900 + year if year >= 50 else 2000 + year
            return dt.date(year, month, day)
        except (ValueError, KeyError):
            continue
    return None


def parse_year_from_text(text: str, cited_date: Optional[dt.date]) -> Optional[int]:
    if cited_date is not None:
        return cited_date.year
    # Strip application numbers and paragraph ranges before searching for a 4-digit year
    cleaned = APPNO_RE.sub(" ", text)
    cleaned = re.sub(r"§+\s*[\d\-–,\s]+", " ", cleaned)
    for pat in YEAR_PATTERNS:
        m = pat.search(cleaned)
        if m:
            yr = int(m.group(1))
            if 1959 <= yr <= 2030:
                return yr
    return None


def parse_scl_entry(entry: str) -> ParsedSclEntry:
    raw = entry.strip()
    appnos: list[str] = []
    seen: set[str] = set()
    for m in APPNO_RE.finditer(raw):
        norm = f"{int(m.group(1))}/{m.group(2)}"
        if norm not in seen:
            seen.add(norm)
            appnos.append(norm)
    decision_cue = bool(DECISION_CUE_RE.search(raw))
    grand_chamber = bool(GC_CUE_RE.search(raw))
    cited_date = parse_date_from_text(raw)
    cited_year = parse_year_from_text(raw, cited_date)
    secondary_cue: Optional[str] = None
    for label, pat in SECONDARY_CUES:
        if pat.search(raw):
            secondary_cue = label
            break
    return ParsedSclEntry(
        raw=raw,
        appnos=tuple(appnos),
        decision_cue=decision_cue,
        grand_chamber=grand_chamber,
        cited_date=cited_date,
        cited_year=cited_year,
        secondary_cue=secondary_cue,
    )


def split_scl(scl: str) -> list[str]:
    if not scl:
        return []
    cleaned = _NL_CONTINUATION_RE.sub(" ", scl)
    cleaned = _DR_MISSING_SEMI_RE.sub(r"\1; ", cleaned)
    cleaned = _COMM_HEADER_SPLIT_RE.sub("; ", cleaned)
    cleaned = _AFTER_CITATION_TAIL_SPLIT_RE.sub(r"\1; ", cleaned)
    out: list[str] = []
    for chunk in re.split(r"[;\n]+", cleaned):
        chunk = chunk.strip()
        if not chunk:
            continue
        for sub in _SUBSPLIT_RE.split(chunk):
            sub = sub.strip()
            if sub:
                out.append(sub)
    return out


def choose_target_for_entry(
    parsed: ParsedSclEntry,
    source_case_id: int,
    source_date: Optional[dt.date],
    appno_index: dict[str, list[TargetCandidate]],
) -> Optional[tuple[TargetCandidate, str, str]]:
    """Resolve one parsed scl entry to (TargetCandidate, matched_appno, rule) or None."""
    if not parsed.appnos:
        return None

    # Gather deduplicated candidate targets across all appnos in this single scl entry
    by_case: dict[int, tuple[TargetCandidate, str]] = {}
    for ap in parsed.appnos:
        for cand in appno_index.get(ap, ()):
            if cand.case_id == source_case_id:
                continue
            if cand.kind == "communicated":
                continue
            if (
                source_date is not None
                and cand.date_decision is not None
                and cand.date_decision > source_date
            ):
                continue
            if cand.case_id not in by_case:
                by_case[cand.case_id] = (cand, ap)

    if not by_case:
        return None

    candidates = [pair[0] for pair in by_case.values()]
    judgments = [c for c in candidates if c.kind == "judgment"]
    decisions = [c for c in candidates if c.kind == "decision"]

    if parsed.decision_cue and decisions:
        pool = decisions
        rule = "scl_decision_cue"
    elif judgments:
        pool = judgments
        rule = "scl_judgment"
    elif decisions:
        pool = decisions
        rule = "scl_decision_no_judgment"
    else:
        return None

    # Deterministic base sort
    pool = sorted(pool, key=lambda c: (c.date_decision or dt.date.max, c.item_id or "", c.case_id))

    if len(pool) > 1 and parsed.cited_date is not None:
        exact = [c for c in pool if c.date_decision == parsed.cited_date]
        if exact:
            pool = exact
            rule += "_exact_date"

    if len(pool) > 1 and parsed.cited_year is not None:
        same_year = [
            c for c in pool if c.date_decision is not None and c.date_decision.year == parsed.cited_year
        ]
        if same_year:
            pool = same_year
            rule += "_year"

    if len(pool) > 1 and parsed.grand_chamber:
        gc = [c for c in pool if (c.branch or "").upper() == "GRANDCHAMBER"]
        if gc:
            pool = gc
            rule += "_gc"

    if len(pool) > 1:
        if parsed.secondary_cue is not None:
            matched_sec = [c for c in pool if c.secondary_kind == parsed.secondary_cue]
            if matched_sec:
                pool = matched_sec
                rule += f"_{parsed.secondary_cue}"
        else:
            principals = [c for c in pool if c.secondary_kind is None]
            if principals:
                pool = principals
                rule += "_principal"

    if len(pool) > 1:
        # Prefer candidates whose own ECLI primary appno matches a cited appno
        # (disambiguates same-day companion cases like T. v. UK 24724/94 vs V. v. UK 24888/94).
        cited_set = set(parsed.appnos)
        ecli_match = [c for c in pool if appno_from_ecli(c.ecli) in cited_set]
        if ecli_match and len(ecli_match) < len(pool):
            pool = ecli_match
            rule += "_ecli_appno"

    if len(pool) > 1:
        rule += "_earliest"

    chosen = pool[0]
    matched_appno = by_case[chosen.case_id][1]
    return chosen, matched_appno, rule


def resolve_case_scl(
    source_case_id: int,
    source_ecli: Optional[str],
    source_item_id: Optional[str],
    source_date: Optional[dt.date],
    scl_texts: Iterable[str],
    appno_index: dict[str, list[TargetCandidate]],
) -> tuple[list[ResolvedEdge], Counter[str]]:
    """Resolve all scl entries for a single source case, deduplicating per target_case_id."""
    stats: Counter[str] = Counter()
    chosen_by_target: dict[int, ResolvedEdge] = {}
    seen_entries: set[str] = set()

    for scl in scl_texts:
        for raw_entry in split_scl(scl):
            if raw_entry in seen_entries:
                continue
            seen_entries.add(raw_entry)
            stats["scl_entries_total"] += 1
            parsed = parse_scl_entry(raw_entry)
            if not parsed.appnos:
                stats["scl_entries_no_appno"] += 1
                continue
            stats["scl_entries_with_appno"] += 1
            res = choose_target_for_entry(parsed, source_case_id, source_date, appno_index)
            if res is None:
                stats["scl_entries_unresolved"] += 1
                continue
            cand, matched_appno, rule = res
            stats["scl_entries_resolved"] += 1
            if cand.case_id in chosen_by_target:
                stats["scl_entries_dedup_same_target"] += 1
                continue
            chosen_by_target[cand.case_id] = ResolvedEdge(
                source_case_id=source_case_id,
                source_ecli=source_ecli,
                source_item_id=source_item_id,
                target_case_id=cand.case_id,
                target_ecli=cand.ecli,
                target_item_id=cand.item_id,
                scl_reference=raw_entry[:500],
                appno=matched_appno,
                rule=rule,
            )

    return list(chosen_by_target.values()), stats


def _parse_iso_date(val: Any) -> Optional[dt.date]:
    if not val:
        return None
    if isinstance(val, dt.date):
        return val
    try:
        return dt.date.fromisoformat(str(val)[:10])
    except ValueError:
        return None


def load_target_catalog(
    client: SqlRunnerClient,
) -> tuple[dict[str, list[TargetCandidate]], dict[int, TargetCandidate], dict[int, list[str]]]:
    """Load all ECHR target cases and their owner appnos via paged sql-runner queries."""
    print("[load] Fetching ECHR target catalog...", flush=True)
    t0 = time.monotonic()
    cases_by_id: dict[int, TargetCandidate] = {}
    appno_index: dict[str, list[TargetCandidate]] = defaultdict(list)
    owner_appnos_by_case: dict[int, list[str]] = defaultdict(list)

    last_id = 0
    pages = 0
    while True:
        rows = client.query_rows(
            """
            SELECT c.id,
                   c.ecli,
                   c.item_id,
                   c.date_decision::text AS date_decision,
                   bool_or(d.doctype ~* 'JUD') AS has_jud,
                   bool_or(d.doctype ~* 'DEC') AS has_dec,
                   bool_or(d.doctype ~* 'COM') AS has_com,
                   bool_or(upper(COALESCE(d.doctype_branch, '')) = 'GRANDCHAMBER') AS is_gc,
                   COALESCE(
                       MAX(CASE WHEN d.doctype = 'HEJUD' THEN d.docname END),
                       MAX(CASE WHEN d.doctype = 'HFJUD' THEN d.docname END),
                       MAX(d.docname)
                   ) AS docname,
                   COALESCE(
                       MAX(CASE WHEN d.doctype = 'HEJUD' THEN d.conclusion END),
                       MAX(CASE WHEN d.doctype = 'HFJUD' THEN d.conclusion END),
                       MAX(d.conclusion)
                   ) AS conclusion,
                   array_remove(array_agg(DISTINCT a.appno), NULL) AS owner_appnos
              FROM cases c
              JOIN echr_document d ON d.case_id = c.id
              LEFT JOIN echr_document_appno a ON a.item_id = d.item_id AND a.source = 'appno'
             WHERE c.id > %s
             GROUP BY c.id, c.ecli, c.item_id, c.date_decision
             ORDER BY c.id
             LIMIT 900
            """,
            [last_id],
        )
        if not rows:
            break
        pages += 1
        for r in rows:
            cid = int(r["id"])
            last_id = cid
            kind = classify_target_kind(bool(r["has_jud"]), bool(r["has_dec"]), bool(r["has_com"]))
            branch = "GRANDCHAMBER" if r["is_gc"] else None
            sec_kind = (
                classify_secondary_judgment(r["docname"], r["conclusion"])
                if kind == "judgment"
                else None
            )
            cand = TargetCandidate(
                case_id=cid,
                ecli=r["ecli"],
                item_id=r["item_id"],
                date_decision=_parse_iso_date(r["date_decision"]),
                kind=kind,
                branch=branch,
                secondary_kind=sec_kind,
            )
            cases_by_id[cid] = cand

            raw_appnos = r["owner_appnos"] or []
            seen_ap: set[str] = set()
            for ap in raw_appnos:
                norm = normalize_appno(ap)
                if norm and norm not in seen_ap:
                    seen_ap.add(norm)
                    appno_index[norm].append(cand)
                    owner_appnos_by_case[cid].append(norm)
            if not seen_ap:
                fallback_ap = appno_from_ecli(r["ecli"])
                if fallback_ap:
                    appno_index[fallback_ap].append(cand)
                    owner_appnos_by_case[cid].append(fallback_ap)

        if pages % 25 == 0:
            print(
                f"  [catalog] {pages} pages, {len(cases_by_id):,} cases, "
                f"{len(appno_index):,} appnos ({time.monotonic() - t0:.1f}s)",
                flush=True,
            )

    print(
        f"[load] Loaded {len(cases_by_id):,} ECHR cases, {len(appno_index):,} distinct owner appnos "
        f"in {time.monotonic() - t0:.1f}s",
        flush=True,
    )
    return dict(appno_index), cases_by_id, dict(owner_appnos_by_case)


def load_source_scls(
    client: SqlRunnerClient,
) -> list[dict[str, Any]]:
    """Load all ECHR cases that have at least one non-empty `echr_document.scl`."""
    print("[load] Fetching ECHR source scl texts...", flush=True)
    t0 = time.monotonic()
    sources: list[dict[str, Any]] = []
    last_id = 0
    while True:
        rows = client.query_rows(
            """
            SELECT c.id,
                   c.ecli,
                   c.item_id,
                   c.date_decision::text AS date_decision,
                   array_agg(d.scl ORDER BY
                       (d.doctype = 'HEJUD') DESC,
                       (d.doctype = 'HEDEC') DESC,
                       (d.doctype = 'HFJUD') DESC,
                       (d.doctype = 'HFDEC') DESC,
                       d.item_id
                   ) AS scls
              FROM cases c
              JOIN echr_document d ON d.case_id = c.id
             WHERE c.id > %s
               AND d.scl IS NOT NULL
               AND btrim(d.scl) <> ''
             GROUP BY c.id, c.ecli, c.item_id, c.date_decision
             ORDER BY c.id
             LIMIT 900
            """,
            [last_id],
        )
        if not rows:
            break
        for r in rows:
            last_id = int(r["id"])
            # Keep EN primary plus FR only when it has distinct entries; dedup identical strings
            seen_str: set[str] = set()
            uniq_scls: list[str] = []
            for s in r["scls"] or []:
                if s and s not in seen_str:
                    seen_str.add(s)
                    uniq_scls.append(s)
            # Only keep primary + up to 1 secondary language if lengths differ materially
            if len(uniq_scls) > 1:
                primary = uniq_scls[0]
                primary_appnos = {
                    f"{int(m.group(1))}/{m.group(2)}" for m in APPNO_RE.finditer(primary)
                }
                extras = []
                for alt in uniq_scls[1:]:
                    alt_appnos = {
                        f"{int(m.group(1))}/{m.group(2)}" for m in APPNO_RE.finditer(alt)
                    }
                    if alt_appnos - primary_appnos:
                        extras.append(alt)
                uniq_scls = [primary] + extras
            sources.append(
                {
                    "case_id": last_id,
                    "ecli": r["ecli"],
                    "item_id": r["item_id"],
                    "date_decision": _parse_iso_date(r["date_decision"]),
                    "scls": uniq_scls,
                }
            )
    print(
        f"[load] Loaded {len(sources):,} ECHR source cases with scl in {time.monotonic() - t0:.1f}s",
        flush=True,
    )
    return sources


def insert_staging_edges(
    client: SqlRunnerClient,
    edges: list[ResolvedEdge],
    build_version: str,
    batch_size: int = 3000,
) -> int:
    """Insert resolved edges into cle_v2.echr_edge_v2_staging in batches."""
    total_written = 0
    t0 = time.monotonic()
    for i in range(0, len(edges), batch_size):
        chunk = edges[i : i + batch_size]
        res = client.execute(
            INSERT_STAGING_SQL,
            [
                [e.source_ecli for e in chunk],
                [e.source_item_id for e in chunk],
                [e.target_ecli for e in chunk],
                [e.target_item_id for e in chunk],
                [e.scl_reference for e in chunk],
                [e.appno for e in chunk],
                [e.rule for e in chunk],
                [build_version for _ in chunk],
            ],
            dry_run=False,
        )
        total_written += int(res.get("row_count") or res.get("rowcount") or 0)
        if ((i // batch_size) + 1) % 10 == 0 or i + batch_size >= len(edges):
            print(
                f"  [insert] {min(i + batch_size, len(edges)):,}/{len(edges):,} "
                f"(inserted {total_written:,}, {time.monotonic() - t0:.1f}s)",
                flush=True,
            )
    return total_written


def load_legacy_echr_edges(client: SqlRunnerClient) -> list[tuple[int, int]]:
    """Load all resolved legacy `echr_edge` (source_case_id, target_case_id) pairs.

    Pages by `source_case_id` (only ~19.1k distinct sources -> ~22 requests).
    """
    print("[audit] Loading legacy echr_edge rows...", flush=True)
    t0 = time.monotonic()
    pairs: list[tuple[int, int]] = []
    last_src = 0
    while True:
        rows = client.query_rows(
            """
            SELECT source_case_id, array_agg(target_case_id) AS targets
              FROM case_citation
             WHERE source_dataset = 'echr_edge'
               AND target_case_id IS NOT NULL
               AND source_case_id > %s
             GROUP BY source_case_id
             ORDER BY source_case_id
             LIMIT 900
            """,
            [last_src],
        )
        if not rows:
            break
        for r in rows:
            s = int(r["source_case_id"])
            last_src = s
            for t in r["targets"] or []:
                pairs.append((s, int(t)))
    print(f"[audit] Loaded {len(pairs):,} legacy echr_edge rows in {time.monotonic() - t0:.1f}s", flush=True)
    return pairs


def classify_dropped_edge(
    src_id: int,
    tgt_id: int,
    src_meta: Optional[dict[str, Any]],
    tgt_cand: Optional[TargetCandidate],
    tgt_owner_appnos: list[str],
    v2_by_source: dict[int, list[ResolvedEdge]],
    src_scl_parsed: dict[int, list[ParsedSclEntry]],
    owner_appnos_by_case: dict[int, list[str]],
) -> tuple[str, str]:
    """Classify WHY v2 dropped a legacy (src_id, tgt_id) edge and return (reason, evidence)."""
    if src_id == tgt_id:
        return "self_loop", "source_case_id == target_case_id"
    if tgt_cand is None:
        return "missing_target_metadata", "target has no echr_document metadata"
    if tgt_cand.kind == "communicated":
        return "communicated_target", f"target is communicated case ({tgt_cand.ecli})"
    if src_meta is None:
        return "source_no_scl", "source case has NULL/empty echr_document.scl (post-Oct-27 weekly ETL)"

    src_date: Optional[dt.date] = src_meta["date_decision"]
    if src_date is not None and tgt_cand.date_decision is not None and tgt_cand.date_decision > src_date:
        return (
            "future_target_date",
            f"target {tgt_cand.date_decision} > source {src_date}",
        )

    entries = src_scl_parsed.get(src_id, [])
    tgt_ap_set = set(tgt_owner_appnos)
    matching_entries = [e for e in entries if any(ap in tgt_ap_set for ap in e.appnos)]

    if not matching_entries:
        return (
            "non_owner_appno_fanout",
            f"none of target owner appnos {tgt_owner_appnos[:3]} appear in source scl "
            f"(legacy joined extractedappno)",
        )

    # Target's owner appno DOES appear in source scl -> check what v2 picked for that appno
    # (including joined-case targets whose other owned appno matched the same scl entry).
    v2_edges_for_appno = [
        e
        for e in v2_by_source.get(src_id, [])
        if e.appno in tgt_ap_set
        or any(ap in tgt_ap_set for ap in owner_appnos_by_case.get(e.target_case_id, ()))
    ]
    matched_snippet = matching_entries[0].raw[:180]
    if not v2_edges_for_appno:
        return "unmatched_with_owner_appno_in_scl", f"scl={matched_snippet!r}"

    chosen = v2_edges_for_appno[0]
    if tgt_cand.kind == "decision" and not any(e.decision_cue for e in matching_entries):
        return (
            "wrong_doctype_decision_without_dec_cue",
            f"v2 chose {chosen.target_ecli} ({chosen.rule}); scl={matched_snippet!r}",
        )
    if tgt_cand.kind == "judgment" and all(e.decision_cue for e in matching_entries):
        return (
            "wrong_doctype_judgment_for_dec_cue",
            f"v2 chose {chosen.target_ecli} ({chosen.rule}); scl={matched_snippet!r}",
        )
    if tgt_cand.kind == "judgment":
        return (
            "extra_judgment_for_same_appno",
            f"v2 chose {chosen.target_ecli} ({chosen.rule}) over {tgt_cand.ecli} "
            f"(date={tgt_cand.date_decision}, branch={tgt_cand.branch}, sec={tgt_cand.secondary_kind}); "
            f"scl={matched_snippet!r}",
        )
    return (
        "extra_decision_for_same_appno",
        f"v2 chose {chosen.target_ecli} ({chosen.rule}) over {tgt_cand.ecli}; scl={matched_snippet!r}",
    )


def _degree_summary(pairs: Iterable[tuple[int, int]]) -> dict[str, Any]:
    deg: Counter[int] = Counter()
    targets: set[int] = set()
    edge_count = 0
    for s, t in pairs:
        deg[s] += 1
        targets.add(t)
        edge_count += 1
    vals = sorted(deg.values())
    if not vals:
        return {"sources": 0, "targets": 0, "edges": 0}
    n = len(vals)

    def pct(p: float) -> float:
        idx = min(n - 1, int(round(p * (n - 1))))
        return float(vals[idx])

    return {
        "sources": n,
        "targets": len(targets),
        "edges": edge_count,
        "mean_out_deg": round(edge_count / n, 2),
        "p50_out_deg": pct(0.50),
        "p90_out_deg": pct(0.90),
        "p99_out_deg": pct(0.99),
        "max_out_deg": vals[-1],
        "gt_100_cases": sum(1 for v in vals if v > 100),
        "gt_300_cases": sum(1 for v in vals if v > 300),
        "gt_300_edges": sum(v for v in vals if v > 300),
    }


def run_gate_audit(
    client: SqlRunnerClient,
    build_version: str,
    sources: list[dict[str, Any]],
    v2_edges: list[ResolvedEdge],
    cases_by_id: dict[int, TargetCandidate],
    owner_appnos_by_case: dict[int, list[str]],
    report_path: Path,
    sample_seed: int = 20261126,
    drop_sample_size: int = 320,
    keep_sample_size: int = 60,
) -> dict[str, Any]:
    """Run the R6.1 gate comparison, drop classification, and stratified sampling."""
    legacy_pairs = load_legacy_echr_edges(client)
    legacy_set = set(legacy_pairs)
    v2_set = {(e.source_case_id, e.target_case_id) for e in v2_edges}

    kept_set = legacy_set & v2_set
    dropped_set = legacy_set - v2_set
    added_set = v2_set - legacy_set

    src_by_id = {s["case_id"]: s for s in sources}
    v2_by_source: dict[int, list[ResolvedEdge]] = defaultdict(list)
    v2_by_pair: dict[tuple[int, int], ResolvedEdge] = {}
    for e in v2_edges:
        v2_by_source[e.source_case_id].append(e)
        v2_by_pair[(e.source_case_id, e.target_case_id)] = e

    src_scl_parsed: dict[int, list[ParsedSclEntry]] = {}
    for s in sources:
        parsed_list: list[ParsedSclEntry] = []
        for txt in s["scls"]:
            for entry in split_scl(txt):
                parsed_list.append(parse_scl_entry(entry))
        src_scl_parsed[s["case_id"]] = parsed_list

    drop_reasons: Counter[str] = Counter()
    drops_by_reason: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for s_id, t_id in sorted(dropped_set):
        reason, ev = classify_dropped_edge(
            s_id,
            t_id,
            src_by_id.get(s_id),
            cases_by_id.get(t_id),
            owner_appnos_by_case.get(t_id, []),
            v2_by_source,
            src_scl_parsed,
            owner_appnos_by_case,
        )
        drop_reasons[reason] += 1
        drops_by_reason[reason].append((s_id, t_id, ev))

    # Stratified random sample of >= 300 dropped rows across all scl-present drop categories
    # (and separately include a slice of source_no_scl so nothing is hidden).
    rng = random.Random(sample_seed)
    scl_drop_reasons = [r for r in sorted(drops_by_reason) if r != "source_no_scl"]
    scl_drop_total = sum(len(drops_by_reason[r]) for r in scl_drop_reasons)

    sampled_drops: list[dict[str, Any]] = []
    # Allocate at least min(25, len) per reason, then proportional up to drop_sample_size
    alloc: dict[str, int] = {}
    remaining = drop_sample_size
    for r in scl_drop_reasons:
        n_r = len(drops_by_reason[r])
        base = min(n_r, 25)
        alloc[r] = base
        remaining -= base
    if remaining > 0 and scl_drop_total > 0:
        for r in scl_drop_reasons:
            n_r = len(drops_by_reason[r])
            room = n_r - alloc[r]
            if room <= 0:
                continue
            extra = min(room, int(round(remaining * (n_r / scl_drop_total))))
            alloc[r] += extra
    # Top up if rounding left us short of drop_sample_size
    while sum(alloc.values()) < min(drop_sample_size, scl_drop_total):
        for r in scl_drop_reasons:
            if alloc[r] < len(drops_by_reason[r]):
                alloc[r] += 1
                if sum(alloc.values()) >= min(drop_sample_size, scl_drop_total):
                    break

    for r in scl_drop_reasons:
        k = alloc[r]
        chosen = rng.sample(drops_by_reason[r], k)
        for s_id, t_id, ev in chosen:
            sc = cases_by_id.get(s_id)
            tc = cases_by_id.get(t_id)
            sampled_drops.append(
                {
                    "reason": r,
                    "source_ecli": sc.ecli if sc else str(s_id),
                    "source_date": str(sc.date_decision) if sc and sc.date_decision else None,
                    "target_ecli": tc.ecli if tc else str(t_id),
                    "target_date": str(tc.date_decision) if tc and tc.date_decision else None,
                    "target_kind": tc.kind if tc else None,
                    "target_owner_appnos": owner_appnos_by_case.get(t_id, [])[:3],
                    "evidence": ev,
                }
            )

    # Random sample of kept edges for precision check
    kept_sorted = sorted(kept_set)
    kept_sample_pairs = rng.sample(kept_sorted, min(keep_sample_size, len(kept_sorted)))
    sampled_kept: list[dict[str, Any]] = []
    for s_id, t_id in kept_sample_pairs:
        edge = v2_by_pair[(s_id, t_id)]
        tc = cases_by_id.get(t_id)
        sampled_kept.append(
            {
                "source_ecli": edge.source_ecli or str(s_id),
                "target_ecli": edge.target_ecli or str(t_id),
                "target_date": str(tc.date_decision) if tc and tc.date_decision else None,
                "target_kind": tc.kind if tc else None,
                "appno": edge.appno,
                "rule": edge.rule,
                "scl_reference": edge.scl_reference,
            }
        )

    # Random sample of newly added edges
    added_sorted = sorted(added_set)
    added_sample_pairs = rng.sample(added_sorted, min(30, len(added_sorted)))
    sampled_added: list[dict[str, Any]] = []
    for s_id, t_id in added_sample_pairs:
        edge = v2_by_pair[(s_id, t_id)]
        tc = cases_by_id.get(t_id)
        sampled_added.append(
            {
                "source_ecli": edge.source_ecli or str(s_id),
                "target_ecli": edge.target_ecli or str(t_id),
                "target_date": str(tc.date_decision) if tc and tc.date_decision else None,
                "target_kind": tc.kind if tc else None,
                "appno": edge.appno,
                "rule": edge.rule,
                "scl_reference": edge.scl_reference,
            }
        )

    rule_counts = Counter(e.rule for e in v2_edges)
    before_deg = _degree_summary(legacy_set)
    after_deg = _degree_summary(v2_set)

    # Top 10 worst out-degree sources before vs after
    legacy_deg = Counter(s for s, _ in legacy_set)
    v2_deg = Counter(s for s, _ in v2_set)
    top_explosions = []
    for s_id, old_n in legacy_deg.most_common(15):
        sc = cases_by_id.get(s_id)
        top_explosions.append(
            {
                "source_ecli": sc.ecli if sc else str(s_id),
                "source_date": str(sc.date_decision) if sc and sc.date_decision else None,
                "legacy_out_deg": old_n,
                "v2_out_deg": v2_deg.get(s_id, 0),
                "dropped": old_n - v2_deg.get(s_id, 0),
            }
        )

    summary = {
        "build_version": build_version,
        "legacy_total_resolved": len(legacy_set),
        "v2_total_staged": len(v2_set),
        "kept": len(kept_set),
        "dropped": len(dropped_set),
        "dropped_with_scl": scl_drop_total,
        "dropped_source_no_scl": drop_reasons.get("source_no_scl", 0),
        "added": len(added_set),
        "before_network": before_deg,
        "after_network": after_deg,
        "drop_reasons": dict(drop_reasons.most_common()),
        "v2_rule_counts": dict(rule_counts.most_common()),
        "top_explosions_before_after": top_explosions,
        "drop_sample_count": len(sampled_drops),
        "keep_sample_count": len(sampled_kept),
        "added_sample_count": len(sampled_added),
        "sampled_drops": sampled_drops,
        "sampled_kept": sampled_kept,
        "sampled_added": sampled_added,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = report_path.with_suffix(".json")
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Write concise, human-verifiable Markdown gate report
    md_lines = [
        f"# R6.1 ECtHR Edge v2 Staging Gate Report (`{build_version}`)",
        "",
        "## 1. Global Parity & Network Metrics (Before vs After)",
        "",
        "| Metric | Legacy `echr_edge` | Staged `echr_edge_v2` | Delta |",
        "|---|---:|---:|---:|",
        f"| Resolved edges | {before_deg['edges']:,} | {after_deg['edges']:,} | {after_deg['edges'] - before_deg['edges']:+,} |",
        f"| Distinct source cases | {before_deg['sources']:,} | {after_deg['sources']:,} | {after_deg['sources'] - before_deg['sources']:+,} |",
        f"| Distinct target cases | {before_deg['targets']:,} | {after_deg['targets']:,} | {after_deg['targets'] - before_deg['targets']:+,} |",
        f"| Mean out-degree | {before_deg['mean_out_deg']:.2f} | {after_deg['mean_out_deg']:.2f} | {after_deg['mean_out_deg'] - before_deg['mean_out_deg']:+.2f} |",
        f"| p50 / p90 / p99 out-degree | {before_deg['p50_out_deg']:.0f} / {before_deg['p90_out_deg']:.0f} / {before_deg['p99_out_deg']:.0f} | {after_deg['p50_out_deg']:.0f} / {after_deg['p90_out_deg']:.0f} / {after_deg['p99_out_deg']:.0f} | — |",
        f"| Max out-degree | {before_deg['max_out_deg']:,} | {after_deg['max_out_deg']:,} | {after_deg['max_out_deg'] - before_deg['max_out_deg']:+,} |",
        f"| Cases with >100 outgoing edges | {before_deg['gt_100_cases']:,} | {after_deg['gt_100_cases']:,} | {after_deg['gt_100_cases'] - before_deg['gt_100_cases']:+,} |",
        f"| Cases with >300 outgoing edges (`case_cited_by` cutoff) | {before_deg['gt_300_cases']:,} | {after_deg['gt_300_cases']:,} | {after_deg['gt_300_cases'] - before_deg['gt_300_cases']:+,} |",
        f"| Edges trapped in >300 out-degree cases | {before_deg['gt_300_edges']:,} | {after_deg['gt_300_edges']:,} | {after_deg['gt_300_edges'] - before_deg['gt_300_edges']:+,} |",
        "",
        f"- **Kept:** `{len(kept_set):,}`",
        f"- **Dropped (total):** `{len(dropped_set):,}` (`{scl_drop_total:,}` on sources with `scl`; `{drop_reasons.get('source_no_scl', 0):,}` on 148 post-Oct-27 weekly-ETL sources whose `echr_document.scl` is NULL because `PostgresECHRProcessor._detail_row` did not persist `scl`)",
        f"- **Newly added (recovered from `scl`):** `{len(added_set):,}`",
        "",
        "## 2. Classification of ALL Dropped Legacy Edges",
        "",
        "| Drop Reason | Rows | % of Drops | Sampled in Gate |",
        "|---|---:|---:|---:|",
    ]
    for r, n in drop_reasons.most_common():
        pct = 100.0 * n / max(1, len(dropped_set))
        md_lines.append(f"| `{r}` | {n:,} | {pct:.2f}% | {alloc.get(r, 0)} |")

    md_lines += [
        "",
        "## 3. Top 15 Legacy Out-Degree Explosions (Before vs After)",
        "",
        "| Source ECLI | Date | Legacy Out-Deg | v2 Out-Deg | Dropped |",
        "|---|---|---:|---:|---:|",
    ]
    for row in top_explosions:
        md_lines.append(
            f"| `{row['source_ecli']}` | {row['source_date'] or ''} | "
            f"{row['legacy_out_deg']:,} | {row['v2_out_deg']:,} | {row['dropped']:,} |"
        )

    md_lines += [
        "",
        "## 4. Disambiguation Rule Breakdown (`echr_edge_v2_staging`)",
        "",
        "| Rule | Rows |",
        "|---|---:|",
    ]
    for r, n in rule_counts.most_common():
        md_lines.append(f"| `{r}` | {n:,} |")

    md_lines += [
        "",
        f"## 5. Stratified Drop Gate Sample ({len(sampled_drops)} rows, seed={sample_seed})",
        "",
        f"Full JSON artifact with all {len(sampled_drops)} dropped rows, {len(sampled_kept)} kept rows, "
        f"and {len(sampled_added)} added rows saved to `{json_path.name}`.",
        "",
        "| # | Reason | Source ECLI | Target ECLI (Date, Kind) | Evidence / `scl` Excerpt |",
        "|---:|---|---|---|---|",
    ]
    for idx, row in enumerate(sampled_drops[:60], 1):
        ev = (row["evidence"] or "").replace("|", "\\|").replace("\n", " ")[:160]
        md_lines.append(
            f"| {idx} | `{row['reason']}` | `{row['source_ecli']}` | "
            f"`{row['target_ecli']}` ({row['target_date']}, {row['target_kind']}) | {ev} |"
        )
    if len(sampled_drops) > 60:
        md_lines.append(
            f"\n*(Showing first 60 of {len(sampled_drops)} stratified dropped rows; all {len(sampled_drops)} rows are in `{json_path.name}`.)*"
        )

    md_lines += [
        "",
        f"## 6. Kept Edge Precision Sample ({len(sampled_kept)} rows, showing first 30)",
        "",
        "| # | Source ECLI | Target ECLI (Date, Kind) | Rule | `scl` Reference |",
        "|---:|---|---|---|---|",
    ]
    for idx, row in enumerate(sampled_kept[:30], 1):
        ref = (row["scl_reference"] or "").replace("|", "\\|").replace("\n", " ")[:140]
        md_lines.append(
            f"| {idx} | `{row['source_ecli']}` | `{row['target_ecli']}` ({row['target_date']}, {row['target_kind']}) | "
            f"`{row['rule']}` | {ref} |"
        )

    report_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"[audit] Wrote gate report to {report_path} and {json_path}", flush=True)
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build cle_v2.echr_edge_v2_staging and run R6.1 gate audit.")
    parser.add_argument("--build-version", default=DEFAULT_BUILD_VERSION)
    parser.add_argument("--write", action="store_true", help="Write staged rows to cle_v2.echr_edge_v2_staging")
    parser.add_argument("--batch-size", type=int, default=3000)
    parser.add_argument(
        "--audit-report",
        type=Path,
        default=ROOT / "migration" / "verify" / "r6_1_echr_edge_v2_gate.md",
        help="Path for the R6.1 Markdown/JSON gate report",
    )
    parser.add_argument("--skip-audit", action="store_true")
    parser.add_argument("--drop-sample-size", type=int, default=320)
    parser.add_argument("--keep-sample-size", type=int, default=60)
    args = parser.parse_args(argv)

    dry_run = not args.write and os.environ.get("DRY_RUN", "1") != "0"
    client = SqlRunnerClient(read_only=dry_run)

    appno_index, cases_by_id, owner_appnos_by_case = load_target_catalog(client)
    sources = load_source_scls(client)

    print("[build] Resolving scl entries locally...", flush=True)
    t0 = time.monotonic()
    v2_edges: list[ResolvedEdge] = []
    agg_stats: Counter[str] = Counter()
    for src in sources:
        edges, st = resolve_case_scl(
            source_case_id=src["case_id"],
            source_ecli=src["ecli"],
            source_item_id=src["item_id"],
            source_date=src["date_decision"],
            scl_texts=src["scls"],
            appno_index=appno_index,
        )
        v2_edges.extend(edges)
        agg_stats.update(st)
    print(
        f"[build] Resolved {len(v2_edges):,} edges across {len(sources):,} sources "
        f"in {time.monotonic() - t0:.1f}s: {dict(agg_stats)}",
        flush=True,
    )

    if dry_run:
        print("[build] DRY_RUN=1: skipping INSERT into cle_v2.echr_edge_v2_staging.", flush=True)
    else:
        written = insert_staging_edges(
            client, v2_edges, build_version=args.build_version, batch_size=args.batch_size
        )
        print(
            f"[build] Inserted {written:,} new rows into cle_v2.echr_edge_v2_staging "
            f"(build_version={args.build_version!r}).",
            flush=True,
        )

    if not args.skip_audit:
        summary = run_gate_audit(
            client=client,
            build_version=args.build_version,
            sources=sources,
            v2_edges=v2_edges,
            cases_by_id=cases_by_id,
            owner_appnos_by_case=owner_appnos_by_case,
            report_path=args.audit_report,
            drop_sample_size=args.drop_sample_size,
            keep_sample_size=args.keep_sample_size,
        )
        print(
            json.dumps(
                {
                    k: summary[k]
                    for k in (
                        "build_version",
                        "legacy_total_resolved",
                        "v2_total_staged",
                        "kept",
                        "dropped",
                        "dropped_with_scl",
                        "dropped_source_no_scl",
                        "added",
                        "before_network",
                        "after_network",
                        "drop_reasons",
                    )
                },
                indent=2,
            ),
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
