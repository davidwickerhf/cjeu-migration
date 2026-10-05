#!/usr/bin/env python3
"""Run the post-window text guard over windows that were already scraped.

Applies :func:`cjeu_migration.scraper.repair_missing_cellar_texts` to every
window in a workspace, so texts that cellar-extractor silently dropped (or
replaced with the mislabelled legacy EUR-Lex fallback) are re-fetched without
re-scraping whole windows. Run ``cjeu-migrate run --consolidate-only``
afterwards.

Usage::

    repair_windows.py WORKSPACE_DIR SUMMARY_JSON [WORKERS]
"""

from __future__ import annotations

import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cjeu_migration.scraper import CellarCoverageError, repair_missing_cellar_texts

log = logging.getLogger("repair_windows")


def repair(cases_dir: Path, fulltexts_path: Path) -> tuple[str, int, str]:
    window = fulltexts_path.stem
    try:
        repaired = repair_missing_cellar_texts(
            fulltexts_path,
            cases_path=cases_dir / f"{window}.csv",
            max_attempts=4,
        )
        return window, repaired, ""
    except CellarCoverageError as exc:
        return window, 0, str(exc)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    workspace, summary_path = Path(sys.argv[1]), Path(sys.argv[2])
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    cases_dir = workspace / "windows" / "cases"
    windows = sorted((workspace / "windows" / "fulltexts").glob("*.json"))

    results = {"repaired": {}, "unrecovered": {}}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for window, repaired, error in pool.map(lambda p: repair(cases_dir, p), windows):
            if repaired:
                results["repaired"][window] = repaired
            if error:
                results["unrecovered"][window] = error
            log.info("window %s: repaired %d%s", window, repaired, f" ({error})" if error else "")
            summary_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")

    results["windows"] = len(windows)
    results["repaired_eclis"] = sum(results["repaired"].values())
    summary_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    log.info(
        "done: %d ECLIs repaired across %d windows; %d windows with unrecovered CELLAR texts",
        results["repaired_eclis"], len(results["repaired"]), len(results["unrecovered"]),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
