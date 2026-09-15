#!/usr/bin/env python3
"""Verify Kamil's 604 expected-English cases against a corpus parquet.

The scan is streaming: only matching English rows are converted to Python
objects, so the multi-gigabyte fulltext parquet stays memory-bounded.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

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
    return any(pattern.search(opening) for pattern in GOOD_MARKERS)


def load_expected(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    required = {"case", "ecli", "celex"}
    if not rows or not required.issubset(rows[0]):
        raise SystemExit(f"{path} must contain tab-separated columns {sorted(required)}")
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

    for batch in parquet.iter_batches(batch_size=2_000, columns=columns):
        ecli_column = pc.utf8_upper(batch.column("ecli"))
        language_column = pc.utf8_upper(batch.column("text_language"))
        mask = pc.and_(
            pc.is_in(ecli_column, value_set=target_values),
            pc.is_in(language_column, value_set=english_values),
        )
        selected = batch.filter(mask)
        if not selected.num_rows:
            continue
        for row in selected.to_pylist():
            found[str(row["ecli"]).strip().upper()].append(row)
    return found


def classify(expected: list[dict[str, str]], found: dict[str, list[dict]]) -> list[dict]:
    results = []
    for target in expected:
        ecli = target["ecli"].strip().upper()
        celex = target["celex"].strip().upper()
        rows = found.get(ecli, [])
        ranked = sorted(
            rows,
            key=lambda row: (
                celex in {
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("parquet", type=Path)
    parser.add_argument("expected_tsv", type=Path)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--residuals", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()

    expected = load_expected(args.expected_tsv)
    found = scan_english_rows(args.parquet, {row["ecli"].upper() for row in expected})
    results = classify(expected, found)
    fieldnames = list(results[0])
    for path, rows in (
        (args.results, results),
        (args.residuals, [row for row in results if row["status"] != "pass"]),
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
        "residuals": len(expected) - counts["pass"],
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, sort_keys=True))
    return 1 if summary["residuals"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
