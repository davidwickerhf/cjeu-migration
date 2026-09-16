#!/usr/bin/env python3
"""Publish a validated Vast rebuild without keeping a local agent alive.

Waits for the live Kamil verifier, enforces the persisted structural and live
acceptance gates, uploads the already-consolidated artifacts, and records the
result (including the Hugging Face revision) in a small JSON status file.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cjeu_migration.huggingface_push import push_dataset


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


def validate_gates(live: dict, integrity: dict) -> None:
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
        "duplicate_ecli_language_pairs",
        "derived_fulltext_bodies",
    )
    for key in zero_gates:
        actual = integrity.get(key)
        if actual != 0:
            raise RuntimeError(f"integrity gate {key}: expected 0, got {actual}")
    if integrity.get("cases_rows") != 46_638:
        raise RuntimeError("integrity gate cases_rows changed")
    if integrity.get("fulltexts_rows") != 608_668:
        raise RuntimeError("integrity gate fulltexts_rows changed")


def main() -> int:
    validation_dir = Path(
        os.environ.get("VALIDATION_DIR", "/workspace/cjeu-data/validation")
    )
    dataset_dir = Path(
        os.environ.get("DATASET_DIR", "/workspace/cjeu-data/dataset")
    )
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
        validate_gates(live, integrity)

        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise RuntimeError(f"empty Hugging Face token file: {token_file}")
        write_status(status_path, "publishing", validation=live, integrity=integrity)
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
