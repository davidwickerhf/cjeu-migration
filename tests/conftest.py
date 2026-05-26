"""Shared test fixtures."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Clear project env vars so per-test Config.from_env behaviour is deterministic."""
    for var in (
        "HUGGINGFACE_TOKEN",
        "HF_DATASET_REPO",
        "WORKSPACE_DIR",
        "START_DATE",
        "END_DATE",
        "WINDOW",
        "MAX_ECLI_PER_WINDOW",
        "EXTRACTOR_THREADS",
        "MAX_WINDOW_RETRIES",
        "SKIP_UPLOAD",
        "NTFY_TOPIC_URL",
        "NTFY_INTERVAL_SECONDS",
        "NTFY_AUTH_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
