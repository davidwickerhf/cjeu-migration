#!/usr/bin/env python3
"""Verify Kamil's 604 expected-English cases against a corpus parquet.

The scan is streaming: only matching English rows are converted to Python
objects, so the multi-gigabyte fulltext parquet stays memory-bounded.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

GOOD_MARKERS = (
    re.compile(r"^\S*\s*judgment\b", re.IGNORECASE),
    re.compile(r"\bjudgment of the court\b", re.IGNORECASE),
    re.compile(r"\bthe court(?:\s*\([^)]*\))? hereby\b", re.IGNORECASE),
    re.compile(r"\bparties\s+grounds\s+operative part\b", re.IGNORECASE),
)


def normalized_excerpt(text: str, limit: int = 240) -> str:
    return " ".join((text or "").split())[:limit]


def looks_like_judgment(text: str) -> bool:
    normalized = " ".join((text or "").split())
    opening = normalized[:5_000]
    # Some EUR-Lex HTML joins its language badge to the heading, producing
    # "ENJUDGMENT OF THE COURT" after text extraction.
    opening = re.sub(r"\bENJUDGMENT\b", "JUDGMENT", opening, flags=re.IGNORECASE)
    return any(pattern.search(opening) for pattern in GOOD_MARKERS)


def normalized_fulltext(text: str) -> str:
    """Normalize layout-only whitespace before comparing full bodies."""
    return " ".join((text or "").split())


def fulltext_sha256(text: str) -> str:
    return hashlib.sha256(normalized_fulltext(text).encode("utf-8")).hexdigest()


def load_expected(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    required = {"case", "ecli", "celex"}
    if not rows or not required.issubset(rows[0]):
        raise SystemExit(
            f"{path} must contain tab-separated columns {sorted(required)}"
        )
    return rows


def scan_english_rows(parquet_path: Path, eclis: set[str]) -> dict[str, list[dict]]:
    parquet = pq.ParquetFile(parquet_path)
    required = {"ecli", "celex", "text", "text_source", "text_language"}
    missing = required.difference(parquet.schema_arrow.names)
    if missing:
        raise SystemExit(f"{parquet_path} is missing columns: {sorted(missing)}")

    target_values = pa.array(sorted(eclis))
    english_values = pa.array(["EN", "ENG", "ENGLISH"])
    found: dict[str, list[dict]] = defaultdict(list)
    columns = ["ecli", "celex", "text", "text_source", "text_language"]
    for optional_column in ("text_format", "__source_window"):
        if optional_column in parquet.schema_arrow.names:
            columns.append(optional_column)

    # Two-stage row-group scan: project only the two small filter columns for
    # every group, then read the large text column only when that group contains
    # a target English row. Reading text for the entire 8+ GB corpus makes
    # Arrow's allocator retain several gigabytes even though almost every row
    # is filtered out afterwards.
    for row_group in range(parquet.num_row_groups):
        index = parquet.read_row_group(
            row_group, columns=["ecli", "text_language"]
        )
        ecli_column = pc.utf8_upper(index.column("ecli"))
        language_column = pc.utf8_upper(index.column("text_language"))
        mask = pc.and_(
            pc.is_in(ecli_column, value_set=target_values),
            pc.is_in(language_column, value_set=english_values),
        )
        if not pc.any(mask).as_py():
            continue
        selected = parquet.read_row_group(row_group, columns=columns).filter(mask)
        if not selected.num_rows:
            continue
        for row in selected.to_pylist():
            found[str(row["ecli"]).strip().upper()].append(row)
    return found


def rank_rows(target: dict[str, str], rows: list[dict]) -> list[dict]:
    celex = target["celex"].strip().upper()
    return sorted(
        rows,
        key=lambda row: (
            celex
            in {
                token.strip().upper()
                for token in str(row.get("celex") or "").split(";")
                if token.strip()
            },
            looks_like_judgment(row.get("text") or ""),
            (row.get("text_source") or "") == "CELLAR_ITEM",
            len(row.get("text") or ""),
        ),
        reverse=True,
    )


def classify(
    expected: list[dict[str, str]], found: dict[str, list[dict]]
) -> list[dict]:
    results = []
    for target in expected:
        ecli = target["ecli"].strip().upper()
        celex = target["celex"].strip().upper()
        rows = found.get(ecli, [])
        ranked = rank_rows(target, rows)
        best = ranked[0] if ranked else {}
        text = best.get("text") or ""
        row_celexes = {
            token.strip().upper()
            for row in rows
            for token in str(row.get("celex") or "").split(";")
            if token.strip()
        }
        if not rows:
            status = "missing_english"
        elif not text.strip():
            status = "empty_english"
        elif celex not in row_celexes:
            status = "non_judgment_manifestation"
        elif not looks_like_judgment(text):
            status = "suspect_not_judgment"
        else:
            status = "pass"
        results.append(
            {
                **target,
                "status": status,
                "english_rows": len(rows),
                "best_celex": best.get("celex") or "",
                "best_source": best.get("text_source") or "",
                "best_source_window": best.get("__source_window") or "",
                "best_format": best.get("text_format") or "",
                "best_length": len(text),
                "excerpt": normalized_excerpt(text),
            }
        )
    return results


def fetch_live_english(celex: str) -> str:
    """Fetch the current canonical English judgment from CELLAR."""
    from cellar_extractor import (  # imported lazily for offline verification
        extract_cellar_fulltexts,
        get_cellar_manifestations_by_celex,
    )

    _, manifestations = get_cellar_manifestations_by_celex(celex, sector="6")
    english = [
        item
        for item in manifestations
        if str(item.get("language") or "").strip().upper() == "EN"
    ]
    fulltexts = extract_cellar_fulltexts(english, source_label="CELLAR_ITEM")
    candidates = [
        row.get("text") or ""
        for row in fulltexts
        if str(row.get("text_language") or "").strip().upper() == "EN"
        and (row.get("text") or "").strip()
    ]
    return max(candidates, key=len) if candidates else ""


def compare_live_cellar(
    expected: list[dict[str, str]],
    found: dict[str, list[dict]],
    results: list[dict],
    *,
    max_workers: int = 2,
    fetch_fn: Callable[[str], str] = fetch_live_english,
) -> None:
    """Add exact fresh-CELLAR comparison fields to structural results.

    Only structurally passing rows are fetched.  Comparison covers the entire
    normalized body, so a summary, different judgment, or truncated rendition
    cannot pass merely because its metadata and opening words look plausible.
    Results are mutated in place to keep the TSV output a single audit trail.
    """
    targets_by_ecli = {target["ecli"].strip().upper(): target for target in expected}
    results_by_ecli = {result["ecli"].strip().upper(): result for result in results}
    pending = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for ecli, result in results_by_ecli.items():
            result.update(
                {
                    "live_status": "not_compared",
                    "stored_sha256": "",
                    "live_sha256": "",
                    "live_length": 0,
                    "live_error": "",
                }
            )
            if result["status"] != "pass":
                continue
            target = targets_by_ecli[ecli]
            pending[pool.submit(fetch_fn, target["celex"])] = ecli

        for future in as_completed(pending):
            ecli = pending[future]
            result = results_by_ecli[ecli]
            target = targets_by_ecli[ecli]
            ranked = rank_rows(target, found.get(ecli, []))
            stored_text = (ranked[0].get("text") or "") if ranked else ""
            result["stored_sha256"] = fulltext_sha256(stored_text)
            try:
                live_text = future.result()
            except Exception as exc:  # network/API failure is an audit failure
                result["live_status"] = "fetch_error"
                result["live_error"] = f"{type(exc).__name__}: {exc}"
                continue
            result["live_length"] = len(live_text)
            if not live_text.strip():
                result["live_status"] = "missing_live_english"
                continue
            result["live_sha256"] = fulltext_sha256(live_text)
            result["live_status"] = (
                "exact_match"
                if result["stored_sha256"] == result["live_sha256"]
                else "content_mismatch"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("parquet", type=Path)
    parser.add_argument("expected_tsv", type=Path)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--residuals", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument(
        "--live-cellar",
        action="store_true",
        help="compare each structurally passing English body with a fresh CELLAR fetch",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="concurrent CELLAR requests used by --live-cellar (default: 2)",
    )
    args = parser.parse_args()

    expected = load_expected(args.expected_tsv)
    found = scan_english_rows(args.parquet, {row["ecli"].upper() for row in expected})
    results = classify(expected, found)
    if args.live_cellar:
        compare_live_cellar(
            expected,
            found,
            results,
            max_workers=max(1, args.workers),
        )
    fieldnames = list(results[0])
    residual_rows = [
        row
        for row in results
        if row["status"] != "pass"
        or (args.live_cellar and row["live_status"] != "exact_match")
    ]
    for path, rows in (
        (args.results, results),
        (args.residuals, residual_rows),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
            writer.writeheader()
            writer.writerows(rows)

    counts: dict[str, int] = defaultdict(int)
    for row in results:
        counts[row["status"]] += 1
    summary = {
        "expected_cases": len(expected),
        "status_counts": dict(sorted(counts.items())),
        "passed": counts["pass"],
        "residuals": len(residual_rows),
    }
    if args.live_cellar:
        live_counts: dict[str, int] = defaultdict(int)
        for row in results:
            live_counts[row["live_status"]] += 1
        summary["live_status_counts"] = dict(sorted(live_counts.items()))
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))
    return 1 if summary["residuals"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
