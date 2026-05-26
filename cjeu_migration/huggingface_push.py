"""Push the consolidated dataset to the HuggingFace Hub.

Wraps ``huggingface_hub.HfApi`` so the rest of the package never imports HF
directly — keeps testing simple (mock this module's interface, not HF).

The upload is idempotent: re-running on the same repo overwrites the four
files we publish. Large parquet files automatically go through LFS via the
Hub's normal pre-commit hooks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Protocol


log = logging.getLogger(__name__)


class HfApiLike(Protocol):
    """Subset of huggingface_hub.HfApi the pusher uses (for typing + mocking)."""

    def create_repo(
        self, repo_id: str, *, repo_type: str, exist_ok: bool, private: bool, token: Optional[str]
    ) -> object:
        ...

    def upload_file(
        self,
        *,
        path_or_fileobj: str,
        path_in_repo: str,
        repo_id: str,
        repo_type: str,
        token: Optional[str],
        commit_message: str,
    ) -> str:
        ...


def _default_api() -> HfApiLike:
    """Lazy import so the rest of the package doesn't need HF installed."""
    from huggingface_hub import HfApi  # type: ignore

    return HfApi()  # type: ignore[return-value]


def _files_to_publish(dataset_dir: Path) -> List[Path]:
    """Return the canonical set of files we publish to a HF dataset repo, in upload order.

    Order matters: README + FIELDS get pushed first so the repo is browsable
    even if the parquet uploads stall on a slow connection.
    """
    preferred = ["README.md", "FIELDS.md", "cases.parquet", "fulltexts.parquet"]
    out: List[Path] = []
    for name in preferred:
        p = dataset_dir / name
        if p.exists():
            out.append(p)
        else:
            log.warning("skipping missing artifact: %s", p)
    return out


def push_dataset(
    dataset_dir: Path,
    repo_id: str,
    *,
    token: Optional[str],
    private: bool = False,
    commit_message: Optional[str] = None,
    api: Optional[HfApiLike] = None,
) -> List[str]:
    """Create the dataset repo (idempotent) and upload every file in ``dataset_dir``.

    Returns the list of file paths (within the repo) that were uploaded.
    Raises :class:`RuntimeError` when ``dataset_dir`` contains no expected
    artifacts.
    """
    if not dataset_dir.exists():
        raise FileNotFoundError(f"dataset_dir does not exist: {dataset_dir}")

    files = _files_to_publish(dataset_dir)
    if not files:
        raise RuntimeError(
            f"no dataset artifacts to push under {dataset_dir} — "
            "did consolidation step run?"
        )

    client = api or _default_api()

    log.info("ensuring HF dataset repo exists: %s", repo_id)
    client.create_repo(
        repo_id=repo_id,
        repo_type="dataset",
        exist_ok=True,
        private=private,
        token=token,
    )

    message = commit_message or "Refresh CJEU corpus from cellar-extractor"
    uploaded: List[str] = []
    for path in files:
        path_in_repo = path.name
        log.info("uploading %s -> %s:%s", path, repo_id, path_in_repo)
        client.upload_file(
            path_or_fileobj=str(path),
            path_in_repo=path_in_repo,
            repo_id=repo_id,
            repo_type="dataset",
            token=token,
            commit_message=message,
        )
        uploaded.append(path_in_repo)
    return uploaded
