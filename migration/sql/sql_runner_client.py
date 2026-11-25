#!/usr/bin/env python3
"""Shared HMAC-signed client for the caselaw-coolify sql-runner service.

Follows the proven transport protocol of 59..64 (DB_ACCESS.md):
- Never prints or logs SQL_RUNNER_TOKEN.
- Enforces read_only=True by refusing /execute before any network call.
- Supports keyset pagination past the 1,000-row /query cap.
- Supports batched unnest(...) inserts with automatic chunk bisection on
  QueryCanceled (statement timeout) or write_row_limit_exceeded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterator, Sequence


class SqlRunnerError(RuntimeError):
    """Raised on sql-runner HTTP or SQL errors."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body

    @property
    def is_splittable(self) -> bool:
        return (
            "QueryCanceled" in self.body
            or "write_row_limit_exceeded" in self.body
            or self.status == 413
        )


class SqlRunnerClient:
    """Minimal stdlib-only HMAC client for caselaw-coolify sql-runner."""

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        confirm: str | None = None,
        *,
        read_only: bool = True,
        timeout: int = 180,
        max_retries: int = 5,
    ) -> None:
        self.url = (url or os.environ.get("SQL_RUNNER_URL", "")).rstrip("/")
        self._token = token if token is not None else os.environ.get("SQL_RUNNER_TOKEN", "")
        self.confirm = confirm or os.environ.get("SQL_RUNNER_CONFIRM", "execute-cle-v2")
        self.read_only = read_only
        self.timeout = timeout
        self.max_retries = max_retries

        if not self.url:
            raise ValueError("SQL_RUNNER_URL is required")
        if not self._token:
            raise ValueError("SQL_RUNNER_TOKEN is required")

    def healthz(self) -> dict[str, Any]:
        req = urllib.request.Request(f"{self.url}/healthz", method="GET")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _send(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if endpoint != "query" and self.read_only:
            raise PermissionError(
                f"SqlRunnerClient is in read_only mode; refusing /{endpoint}"
            )
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        full_url = f"{self.url}/{endpoint}"
        path = urllib.parse.urlparse(full_url).path

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            ts = str(int(time.time()))
            nonce = secrets.token_hex(16)
            canonical = "\n".join(
                ["POST", path, ts, nonce, hashlib.sha256(body).hexdigest()]
            ).encode("utf-8")
            sig = hmac.new(
                self._token.encode("utf-8"), canonical, hashlib.sha256
            ).hexdigest()
            req = urllib.request.Request(
                full_url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-SQL-Runner-Timestamp": ts,
                    "X-SQL-Runner-Nonce": nonce,
                    "X-SQL-Runner-Signature": f"v1={sig}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                if not out.get("ok"):
                    raise SqlRunnerError(200, json.dumps(out)[:400])
                return out
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")
                err = SqlRunnerError(exc.code, err_body)
                # Client/SQL errors that won't fix themselves on retry: raise fast.
                if exc.code in (400, 401, 403, 413):
                    raise err from exc
                last_err = err
            except Exception as exc:  # noqa: BLE001
                last_err = exc
            if attempt + 1 < self.max_retries:
                time.sleep(min(15, 2 * (attempt + 1)))
        assert last_err is not None
        raise last_err

    def query(self, sql: str, params: list[Any] | dict[str, Any] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"sql": sql}
        if params is not None:
            payload["params"] = params
        return self._send("query", payload)

    def query_rows(self, sql: str, params: list[Any] | dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.query(sql, params=params).get("rows", [])

    def execute(
        self,
        sql: str,
        params: list[Any] | dict[str, Any] | None = None,
        *,
        dry_run: bool = True,
        explain_in_dry_run: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            if explain_in_dry_run:
                return self.query(f"EXPLAIN {sql}", params=params)
            return {"ok": True, "dry_run": True, "row_count": 0, "rows": []}
        payload: dict[str, Any] = {"sql": sql, "confirm": self.confirm}
        if params is not None:
            payload["params"] = params
        return self._send("execute", payload)

    def iter_keyset(
        self,
        sql: str,
        *,
        id_column: str = "id",
        start_id: int = 0,
        limit: int = 500,
        extra_params: Sequence[Any] = (),
        max_rows_total: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Page through a query whose final two %s params are (last_id, limit)."""
        last_id = start_id
        yielded = 0
        while True:
            page_limit = limit
            if max_rows_total is not None:
                remaining = max_rows_total - yielded
                if remaining <= 0:
                    return
                page_limit = min(page_limit, remaining)
            rows = self.query_rows(sql, params=[*extra_params, last_id, page_limit])
            if not rows:
                return
            for row in rows:
                yield row
                yielded += 1
            last_id = int(rows[-1][id_column])
            if len(rows) < page_limit:
                return

    def execute_unnest_chunks(
        self,
        sql: str,
        columns: Sequence[Sequence[Any]],
        *,
        chunk_size: int = 500,
        dry_run: bool = True,
        explain_first: bool = False,
        on_chunk: Callable[[int, int], None] | None = None,
    ) -> int:
        """Execute an INSERT/UPDATE taking parallel array params, bisecting on timeout."""
        if not columns:
            return 0
        total = len(columns[0])
        if total == 0:
            return 0
        if any(len(col) != total for col in columns):
            raise ValueError("All column lists must have equal length")

        def send_slice(start: int, end: int, do_explain: bool) -> int:
            params = [list(col[start:end]) for col in columns]
            try:
                res = self.execute(
                    sql,
                    params=params,
                    dry_run=dry_run,
                    explain_in_dry_run=do_explain,
                )
                affected = max(0, int(res.get("row_count") or 0))
                if on_chunk:
                    on_chunk(end - start, affected)
                return affected
            except SqlRunnerError as exc:
                span = end - start
                if exc.is_splittable and span > 1:
                    mid = start + span // 2
                    return send_slice(start, mid, False) + send_slice(mid, end, False)
                raise

        written = 0
        explained = False
        for offset in range(0, total, chunk_size):
            end = min(offset + chunk_size, total)
            written += send_slice(offset, end, explain_first and not explained)
            explained = True
        return written
