"""HF push tests — uses an in-memory fake HfApi, never touches the network."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pytest

from cjeu_migration.huggingface_push import push_dataset


class _FakeHfApi:
    """In-memory stand-in for huggingface_hub.HfApi."""

    def __init__(self):
        self.repos_created: List[tuple] = []
        self.files_uploaded: List[dict] = []

    def create_repo(
        self,
        repo_id: str,
        *,
        repo_type: str,
        exist_ok: bool,
        private: bool,
        token: Optional[str],
    ) -> object:
        self.repos_created.append((repo_id, repo_type, exist_ok, private, token))
        return object()

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
        self.files_uploaded.append(
            {
                "path_or_fileobj": path_or_fileobj,
                "path_in_repo": path_in_repo,
                "repo_id": repo_id,
                "repo_type": repo_type,
                "token": token,
                "commit_message": commit_message,
            }
        )
        return f"hf://{repo_id}/{path_in_repo}"


def _populate_dataset_dir(tmp_path: Path) -> Path:
    d = tmp_path / "dataset"
    d.mkdir()
    (d / "README.md").write_text("# CJEU", encoding="utf-8")
    (d / "FIELDS.md").write_text("# fields", encoding="utf-8")
    (d / "cases.parquet").write_bytes(b"PAR1fake")
    (d / "fulltexts.parquet").write_bytes(b"PAR1fake")
    return d


def test_push_dataset_creates_repo_and_uploads_canonical_files(tmp_path):
    dataset_dir = _populate_dataset_dir(tmp_path)
    api = _FakeHfApi()

    uploaded = push_dataset(
        dataset_dir,
        "example-org/cjeu-cases",
        token="secret-token",
        api=api,
    )

    assert uploaded == ["README.md", "FIELDS.md", "cases.parquet", "fulltexts.parquet"]
    # Repo created with the right kind + idempotent flag.
    assert len(api.repos_created) == 1
    repo_id, repo_type, exist_ok, private, token = api.repos_created[0]
    assert repo_id == "example-org/cjeu-cases"
    assert repo_type == "dataset"
    assert exist_ok is True
    assert private is False
    assert token == "secret-token"
    # Files uploaded in the documented order.
    assert [u["path_in_repo"] for u in api.files_uploaded] == uploaded
    # Every upload carries the same repo_id + token.
    for record in api.files_uploaded:
        assert record["repo_id"] == "example-org/cjeu-cases"
        assert record["repo_type"] == "dataset"
        assert record["token"] == "secret-token"


def test_push_dataset_skips_missing_files_but_keeps_going(tmp_path):
    d = tmp_path / "dataset"
    d.mkdir()
    # Only README + cases, no FIELDS.md, no fulltexts.parquet.
    (d / "README.md").write_text("# CJEU", encoding="utf-8")
    (d / "cases.parquet").write_bytes(b"PAR1fake")
    api = _FakeHfApi()

    uploaded = push_dataset(d, "example/x", token=None, api=api)
    assert uploaded == ["README.md", "cases.parquet"]


def test_push_dataset_raises_when_dataset_dir_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        push_dataset(tmp_path / "nope", "example/x", token=None, api=_FakeHfApi())


def test_push_dataset_raises_when_no_artifacts(tmp_path):
    d = tmp_path / "dataset"
    d.mkdir()
    # No expected files.
    (d / "junk.txt").write_text("ignore me")
    with pytest.raises(RuntimeError, match="no dataset artifacts"):
        push_dataset(d, "example/x", token=None, api=_FakeHfApi())


def test_push_dataset_uses_custom_commit_message(tmp_path):
    dataset_dir = _populate_dataset_dir(tmp_path)
    api = _FakeHfApi()
    push_dataset(
        dataset_dir,
        "example/x",
        token=None,
        commit_message="Refresh covering 1954-01-01..2024-12-31",
        api=api,
    )
    assert all(
        rec["commit_message"] == "Refresh covering 1954-01-01..2024-12-31"
        for rec in api.files_uploaded
    )


def test_push_dataset_private_flag_forwards(tmp_path):
    dataset_dir = _populate_dataset_dir(tmp_path)
    api = _FakeHfApi()
    push_dataset(dataset_dir, "example/x", token=None, private=True, api=api)
    assert api.repos_created[0][3] is True  # private flag
