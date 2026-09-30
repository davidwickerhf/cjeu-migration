#!/usr/bin/env python3
"""Publish a validated Vast rebuild without keeping a local agent alive.

Waits for the live Kamil verifier, enforces the persisted structural and live
acceptance gates, uploads the already-consolidated artifacts, and records the
result (including the Hugging Face revision) in a small JSON status file.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from cjeu_migration.huggingface_push import push_dataset

REQUIRED_CATALOG_IDENTITIES = {
    "ECLI:EU:C:2013:654": "62012CO0041",
    "ECLI:EU:C:2013:656": "62011CO0444",
    "ECLI:EU:C:2014:72": "62013CO0298",
    "ECLI:EU:C:2021:965": "62021CO0201(01)",
    "ECLI:EU:F:2011:62": "62011FO0005",
    "ECLI:EU:T:2003:190": "62002TJ0065",
    # CELLAR (EUR-Lex's store) binds this ECLI to (01); InfoCuria numbers
    # the procedure's orders differently and says (02).
    "ECLI:EU:T:2014:1": "62013TO0505(01)",
    "ECLI:EU:T:2014:166": "62013TO0505(03)",
}
FORBIDDEN_STALE_ECLIS = {
    "ECLI:EU:C:2012:820",
    "ECLI:EU:T:2026:68",
}
MIN_CASE_ROWS = 46_000
MIN_FULLTEXT_ROWS = 500_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_status(path: Path, status: str, **details) -> None:
    payload = {"status": status, "updated_at": utc_now(), **details}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def process_running(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return False
    try:
        state = stat_path.read_text(encoding="utf-8").split()[2]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _normalize_celex(value: object) -> str:
    raw = str(value or "").replace(" ", "")
    if ";" in raw:
        options = [part for part in raw.split(";") if part]
        base = [part for part in options if "INF" not in part]
        raw = (base or options or [""])[0]
    if "_" in raw:
        raw = raw.split("_", 1)[0]
    return re.sub(r"\.(\d{2})$", r"(\1)", raw)


def validate_catalog_identities(cases_path: Path) -> dict:
    """Validate reconciled ECLI/CELEX identities without loading the table."""
    import pyarrow.parquet as pq

    parquet = pq.ParquetFile(cases_path)
    by_ecli: dict[str, set[str]] = defaultdict(set)
    for batch in parquet.iter_batches(batch_size=2_048, columns=["ecli", "celex"]):
        eclis = batch.column("ecli").to_pylist()
        celexes = batch.column("celex").to_pylist()
        for ecli, celex in zip(eclis, celexes):
            ecli_value = str(ecli or "").strip()
            celex_value = _normalize_celex(celex)
            if ecli_value and celex_value:
                by_ecli[ecli_value].add(celex_value)

    conflicts = {
        ecli: sorted(celexes) for ecli, celexes in by_ecli.items() if len(celexes) > 1
    }
    missing = sorted(
        ecli for ecli in REQUIRED_CATALOG_IDENTITIES if ecli not in by_ecli
    )
    mismatched = {
        ecli: {
            "expected": expected,
            "actual": sorted(by_ecli.get(ecli, set())),
        }
        for ecli, expected in REQUIRED_CATALOG_IDENTITIES.items()
        if ecli in by_ecli and expected not in by_ecli[ecli]
    }
    stale_present = sorted(FORBIDDEN_STALE_ECLIS & set(by_ecli))
    summary = {
        "unique_eclis": len(by_ecli),
        "required_identities": len(REQUIRED_CATALOG_IDENTITIES),
        "missing_required": missing,
        "mismatched_required": mismatched,
        "identity_conflicts": conflicts,
        "stale_eclis_present": stale_present,
    }
    if missing or mismatched or conflicts or stale_present:
        raise RuntimeError(f"catalogue identity gate failed: {summary}")
    return summary


def validate_gates(live: dict, integrity: dict, identity: dict) -> None:
    required_live = {
        "expected_cases": 604,
        "passed": 604,
        "residuals": 0,
    }
    for key, expected in required_live.items():
        actual = live.get(key)
        if actual != expected:
            raise RuntimeError(f"live gate {key}: expected {expected}, got {actual}")
    exact = live.get("live_status_counts", {}).get("exact_match")
    if exact != 604:
        raise RuntimeError(f"live gate exact_match: expected 604, got {exact}")

    zero_gates = (
        "duplicate_case_eclis",
        "duplicate_celex_groups",
        "malformed_celex",
        "duplicate_ecli_language_pairs",
        "derived_fulltext_bodies",
    )
    for key in zero_gates:
        actual = integrity.get(key)
        if actual != 0:
            raise RuntimeError(f"integrity gate {key}: expected 0, got {actual}")
    baseline = integrity.get("baseline")
    if not isinstance(baseline, dict):
        raise RuntimeError("integrity gate baseline: compare against the published build")
    for key in ("lost_eclis", "lost_cellar_eclis"):
        actual = baseline.get(key)
        if actual != 0:
            raise RuntimeError(f"baseline gate {key}: expected 0, got {actual}")
    if int(integrity.get("cases_rows") or 0) < MIN_CASE_ROWS:
        raise RuntimeError(f"integrity gate cases_rows below {MIN_CASE_ROWS}")
    if int(integrity.get("fulltexts_rows") or 0) < MIN_FULLTEXT_ROWS:
        raise RuntimeError(f"integrity gate fulltexts_rows below {MIN_FULLTEXT_ROWS}")
    if identity.get("required_identities") != len(REQUIRED_CATALOG_IDENTITIES):
        raise RuntimeError(
            "catalogue identity gate did not check every required mapping"
        )
    for key in (
        "missing_required",
        "mismatched_required",
        "identity_conflicts",
        "stale_eclis_present",
    ):
        if identity.get(key):
            raise RuntimeError(f"catalogue identity gate {key}: {identity[key]}")


def main() -> int:
    validation_dir = Path(
        os.environ.get("VALIDATION_DIR", "/workspace/cjeu-data/validation")
    )
    dataset_dir = Path(os.environ.get("DATASET_DIR", "/workspace/cjeu-data/dataset"))
    token_file = Path(os.environ.get("HF_TOKEN_FILE", "/workspace/.hf_token"))
    repo_id = os.environ.get("HF_DATASET_REPO", "davidwickerhf/cjeu-opendata")
    status_path = validation_dir / "publish_status.json"

    try:
        pid = int((validation_dir / "kamil_live.pid").read_text().strip())
        write_status(status_path, "waiting_for_live_validation", validation_pid=pid)
        while process_running(pid):
            time.sleep(30)

        live_path = validation_dir / "kamil_live_summary.json"
        integrity_path = validation_dir / "parquet_integrity.json"
        if not live_path.exists():
            raise RuntimeError("live validator exited without writing its summary")
        live = json.loads(live_path.read_text(encoding="utf-8"))
        integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
        identity = validate_catalog_identities(dataset_dir / "cases.parquet")
        (validation_dir / "catalog_identity_summary.json").write_text(
            json.dumps(identity, indent=2) + "\n", encoding="utf-8"
        )
        validate_gates(live, integrity, identity)

        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(f"empty Hugging Face token file: {token_file}")
        write_status(
            status_path,
            "publishing",
            validation=live,
            integrity=integrity,
            catalog_identity=identity,
        )
        uploaded = push_dataset(
            dataset_dir,
            repo_id,
            token=token,
            commit_message=(
                "Full CJEU rebuild: canonical CELLAR judgments, "
                "604/604 Kamil cases verified"
            ),
        )

        from huggingface_hub import HfApi

        revision = HfApi(token=token).dataset_info(repo_id).sha
        write_status(
            status_path,
            "complete",
            repo_id=repo_id,
            revision=revision,
            uploaded=uploaded,
            validation=live,
            integrity=integrity,
            catalog_identity=identity,
        )
        return 0
    except Exception as exc:
        write_status(
            status_path,
            "failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        print(f"publish gate failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
