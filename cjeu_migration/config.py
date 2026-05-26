"""Configuration loading. Reads .env then env vars, applies defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv


WindowKind = Literal["month", "quarter", "year"]


def _str_to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass(frozen=True)
class Config:
    """Runtime configuration. Construct via :meth:`from_env`."""

    hf_token: Optional[str]
    hf_dataset_repo: str
    workspace_dir: Path
    start_date: date
    end_date: date
    window: WindowKind
    max_ecli_per_window: int
    extractor_threads: int
    max_window_retries: int
    skip_upload: bool

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "Config":
        if env_file is None:
            env_file = Path(".env")
        if env_file.exists():
            load_dotenv(env_file, override=False)

        end_raw = os.environ.get("END_DATE", "").strip()
        end_date = date.fromisoformat(end_raw) if end_raw else date.today()

        return cls(
            hf_token=os.environ.get("HUGGINGFACE_TOKEN") or None,
            hf_dataset_repo=os.environ.get("HF_DATASET_REPO", "").strip()
                or "maastrichtlawtech/cjeu-cases",
            workspace_dir=Path(os.environ.get("WORKSPACE_DIR", "./workspace")).resolve(),
            start_date=date.fromisoformat(
                os.environ.get("START_DATE", "1954-01-01").strip()
            ),
            end_date=end_date,
            window=os.environ.get("WINDOW", "month").strip().lower(),  # type: ignore[arg-type]
            max_ecli_per_window=int(os.environ.get("MAX_ECLI_PER_WINDOW", "10000")),
            extractor_threads=int(os.environ.get("EXTRACTOR_THREADS", "10")),
            max_window_retries=int(os.environ.get("MAX_WINDOW_RETRIES", "3")),
            skip_upload=_str_to_bool(os.environ.get("SKIP_UPLOAD", "0")),
        )

    @property
    def cases_dir(self) -> Path:
        """Per-window case CSV files land here."""
        return self.workspace_dir / "windows" / "cases"

    @property
    def fulltexts_dir(self) -> Path:
        """Per-window fulltext JSON files land here."""
        return self.workspace_dir / "windows" / "fulltexts"

    @property
    def manifest_path(self) -> Path:
        return self.workspace_dir / "manifest.json"

    @property
    def consolidated_dir(self) -> Path:
        """Final parquet outputs land here, ready to push to HF."""
        return self.workspace_dir / "dataset"
