#!/usr/bin/env python3
"""Streaming integrity scan of a rebuilt dataset, optionally against a baseline.

Writes the ``parquet_integrity.json`` consumed by ``publish_after_validation``:
row and identity counts for the new build and, when a baseline dataset (the
currently published build) is given, what the new build lost relative to it.

Usage::

    validate_rebuild.py DATASET_DIR OUTPUT_JSON [BASELINE_DATASET_DIR]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

DERIVED_CELEX = re.compile(r"_(SUM|RES|INF)\b")
SECTOR6_CELEX = re.compile(r"^6\d{4}[A-Z]{1,2}\d{4}(\(\d{2}\))?$")


def primary_celex(value) -> str:
    return str(value or "").split(";", 1)[0].split("_", 1)[0].strip()


def scan(dataset: Path) -> tuple[dict, set[str], set[tuple[str, str]], set[str]]:
    cases = pq.ParquetFile(dataset / "cases.parquet")
    eclis: Counter[str] = Counter()
    celex_users: dict[str, set[str]] = {}
    malformed: list[str] = []
    for batch in cases.iter_batches(batch_size=4096, columns=["ecli", "celex"]):
        for ecli, celex in zip(batch.column("ecli").to_pylist(), batch.column("celex").to_pylist()):
            ecli = (ecli or "").strip()
            eclis[ecli] += 1
            primary = primary_celex(celex)
            if not ecli or not primary:
                continue
            celex_users.setdefault(primary, set()).add(ecli)
            if primary.startswith("6") and not SECTOR6_CELEX.match(primary):
                malformed.append(ecli)

    fulltexts = pq.ParquetFile(dataset / "fulltexts.parquet")
    pairs: Counter[tuple[str, str]] = Counter()
    nonempty_pairs: set[tuple[str, str]] = set()
    cellar_eclis: set[str] = set()
    fulltext_eclis: set[str] = set()
    sources: Counter[str] = Counter()
    empty = derived_bodies = derived_empty = 0
    columns = ["ecli", "celex", "text", "text_language", "text_source"]
    for batch in fulltexts.iter_batches(batch_size=2048, columns=columns):
        rows = batch.to_pydict()
        for ecli, celex, text, language, source in zip(*(rows[c] for c in columns)):
            ecli = (ecli or "").strip()
            key = (ecli, (language or "").strip().lower())
            pairs[key] += 1
            fulltext_eclis.add(ecli)
            derived = bool(DERIVED_CELEX.search(celex or ""))
            if not (text or "").strip():
                empty += 1
                derived_empty += derived
                continue
            nonempty_pairs.add(key)
            derived_bodies += derived
            sources[source or ""] += 1
            if source == "CELLAR_ITEM":
                cellar_eclis.add(ecli)

    case_eclis = {ecli for ecli in eclis if ecli}
    report = {
        "cases_rows": cases.metadata.num_rows,
        "unique_case_eclis": len(case_eclis),
        "blank_case_eclis": eclis.get("", 0),
        "duplicate_case_eclis": sum(1 for e, n in eclis.items() if e and n > 1),
        "duplicate_celex_groups": sum(1 for users in celex_users.values() if len(users) > 1),
        "duplicate_celex_sample": sorted(
            [celex, sorted(users)] for celex, users in celex_users.items() if len(users) > 1
        )[:20],
        "malformed_celex": len(malformed),
        "malformed_celex_sample": sorted(malformed)[:20],
        "fulltexts_rows": fulltexts.metadata.num_rows,
        "fulltexts_row_groups": fulltexts.metadata.num_row_groups,
        "duplicate_ecli_language_pairs": sum(1 for n in pairs.values() if n > 1),
        "empty_language_placeholders": empty,
        "derived_fulltext_bodies": derived_bodies,
        "derived_empty_placeholders": derived_empty,
        "fulltext_eclis_not_in_cases": len(fulltext_eclis - case_eclis - {""}),
        "cases_without_any_fulltext": len(case_eclis - {e for e, _ in nonempty_pairs}),
        "text_source_counts": dict(sources),
    }
    return report, case_eclis, nonempty_pairs, cellar_eclis


def main() -> int:
    dataset, output = Path(sys.argv[1]), Path(sys.argv[2])
    report, cases, pairs, cellar = scan(dataset)
    if len(sys.argv) > 3:
        baseline, old_cases, old_pairs, old_cellar = scan(Path(sys.argv[3]))
        lost_eclis = sorted(old_cases - cases)
        lost_cellar = sorted(old_cellar - cellar)
        lost_pairs = sorted(old_pairs - pairs)
        report["baseline"] = {
            "cases_rows": baseline["cases_rows"],
            "fulltexts_rows": baseline["fulltexts_rows"],
            "text_source_counts": baseline["text_source_counts"],
            "new_eclis": len(cases - old_cases),
            "lost_eclis": len(lost_eclis),
            "lost_eclis_sample": lost_eclis[:50],
            # Baseline documents whose CELLAR text is gone. CELLAR text is
            # correct by construction, so any loss is a fetch failure.
            "lost_cellar_eclis": len(lost_cellar),
            "lost_cellar_eclis_sample": lost_cellar[:50],
            "lost_nonempty_language_pairs": len(lost_pairs),
            "lost_pairs_by_language": dict(Counter(language for _, language in lost_pairs)),
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if not k.endswith("_sample")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
