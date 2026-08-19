#!/usr/bin/env python3
"""Sync new CJEU fulltext rows into cle_v2 through the Coolify sql-runner.

The runner-transport successor to 57_supplement_cjeu_texts.py, needed since
the production Postgres is only reachable inside the compose network.
Append-only: diffs fulltexts.parquet against the loaded (case_id, language,
source) triples and inserts what is missing, then recomputes the is_stub
flag in id-windowed chunks (the runner caps UPDATE at 10k affected rows and
30s per statement; DROP/TRUNCATE are blocked, so no staging tables).

Measured transport: the runner takes 60 MB bodies in ~3.5s. Batches here
stay ~6 MB / 250 rows because inserts compute fulltext_tsv server-side and
must fit the 30s statement timeout.

Env:
  SQL_RUNNER_URL      e.g. https://demo-psql.caselawexplorer.tech
  SQL_RUNNER_TOKEN    HMAC token
  SQL_RUNNER_CONFIRM  default execute-cle-v2
  FULLTEXTS_PARQUET   local path; downloads from HF when unset
"""
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

import pyarrow.parquet as pq

URL = os.environ["SQL_RUNNER_URL"]
TOKEN = os.environ["SQL_RUNNER_TOKEN"]
CONFIRM = os.environ.get("SQL_RUNNER_CONFIRM", "execute-cle-v2")

COLS = ("ecli", "text", "text_source", "text_language", "text_format",
        "missing_reasons")
BATCH_ROWS = 250
BATCH_BYTES = 6_000_000


def runner(sql, params=None, execute=False):
    endpoint = "execute" if execute else "query"
    payload = {"sql": sql}
    if params is not None:
        payload["params"] = params
    if execute:
        payload["confirm"] = CONFIRM
    body = json.dumps(payload, separators=(",", ":")).encode()
    url = URL.rstrip("/") + "/" + endpoint
    last = None
    for attempt in range(5):
        ts, nonce = str(int(time.time())), secrets.token_hex(16)
        msg = "\n".join(["POST", urllib.parse.urlparse(url).path, ts, nonce,
                         hashlib.sha256(body).hexdigest()]).encode()
        sig = hmac.new(TOKEN.encode(), msg, hashlib.sha256).hexdigest()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "X-SQL-Runner-Timestamp": ts, "X-SQL-Runner-Nonce": nonce,
            "X-SQL-Runner-Signature": f"v1={sig}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                out = json.loads(r.read())
            if not out.get("ok"):
                raise RuntimeError(str(out)[:300])
            return out
        except urllib.error.HTTPError as exc:
            # surface the body — the runner's 4xx JSON (e.g.
            # write_row_limit_exceeded) is what callers dispatch on
            last = RuntimeError(f"HTTP {exc.code}: {exc.read().decode()[:300]}")
            time.sleep(3 * (attempt + 1))
        except Exception as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise last


def paginated(sql_tmpl, key="id"):
    """Keyset-paginate a read (the runner caps results at 1,000 rows)."""
    last_id = 0
    while True:
        out = runner(sql_tmpl, params=[last_id])
        rows = out["rows"]
        if not rows:
            return
        yield from rows
        last_id = rows[-1][key]


def main() -> int:
    recompute_only = os.environ.get("RECOMPUTE_ONLY") == "1"
    path = os.environ.get("FULLTEXTS_PARQUET")
    if not path and not recompute_only:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download("davidwickerhf/cjeu-opendata",
                               "fulltexts.parquet", repo_type="dataset")

    print("fetching ECLI map ...", flush=True)
    ecli_to_id = {}
    for r in paginated("""
            SELECT c.id, c.ecli FROM cases c
            JOIN cjeu_document d ON d.case_id = c.id
            WHERE c.id > %s ORDER BY c.id LIMIT 900"""):
        ecli_to_id[r["ecli"]] = r["id"]
    print(f"  {len(ecli_to_id)} CJEU-corpus ECLIs")

    print("fetching loaded text triples ...", flush=True)
    seen = set()
    for r in paginated("""
            SELECT ct.id, ct.case_id, ct.language, ct.source FROM case_text ct
            JOIN cjeu_document d ON d.case_id = ct.case_id
            WHERE ct.id > %s ORDER BY ct.id LIMIT 900"""):
        seen.add((r["case_id"], r["language"], r["source"]))
    print(f"  {len(seen)} triples already loaded")

    inserted = 0
    scanned = 0
    batch, batch_bytes = [], 0
    if recompute_only:
        print("RECOMPUTE_ONLY=1 — skipping parquet scan/insert phase")

    def flush():
        nonlocal inserted, batch, batch_bytes
        if not batch:
            return
        out = runner("""
            INSERT INTO case_text (case_id, language, fulltext, source,
                                   text_format, missing_reasons)
            SELECT v.case_id, v.language, nullif(v.fulltext, ''), v.source,
                   nullif(v.text_format, ''), nullif(v.missing_reasons, '')
            FROM unnest(%s::bigint[], %s::text[], %s::text[], %s::text[],
                        %s::text[], %s::text[])
                 AS v(case_id, language, fulltext, source, text_format,
                      missing_reasons)
            ON CONFLICT (case_id, language, source) DO NOTHING""",
            params=[[b[i] for b in batch] for i in range(6)], execute=True)
        inserted += out.get("row_count") or 0
        batch, batch_bytes = [], 0

    pf = pq.ParquetFile(path) if not recompute_only else None
    for pbatch in (pf.iter_batches(batch_size=2000, columns=list(COLS))
                   if pf else []):
        cols = {c: pbatch.column(c).to_pylist() for c in COLS}
        for e, t, s2, l, f2, m2 in zip(cols["ecli"], cols["text"],
                                       cols["text_source"], cols["text_language"],
                                       cols["text_format"], cols["missing_reasons"]):
            scanned += 1
            cid = ecli_to_id.get(str(e).strip().upper()) if e else None
            lang = (l or "").lower()
            src = s2 or "UNKNOWN"
            if not cid or not lang or (cid, lang, src) in seen:
                continue
            seen.add((cid, lang, src))
            batch.append((cid, lang, t or "", src, f2 or "", m2 or ""))
            batch_bytes += len(t or "")
            if len(batch) >= BATCH_ROWS or batch_bytes >= BATCH_BYTES:
                flush()
        if scanned % 100_000 < 2000:
            print(f"  scanned {scanned:,} — inserted {inserted:,}", flush=True)
    flush()
    print(f"insert phase done: scanned {scanned:,}, inserted {inserted:,}")

    # Stub-flag recompute, chunked by case id list (respects the 10k-row
    # UPDATE cap and the 30s statement timeout; splits a chunk on 403).
    print("recomputing is_stub in chunks ...", flush=True)
    case_ids = sorted(set(ecli_to_id.values()))
    flags_updated = 0

    def recompute(ids):
        nonlocal flags_updated
        try:
            out = runner("""
                UPDATE case_text ct
                   SET is_stub = (length(ct.fulltext) < 0.40 * m.med AND m.med > 10000)
                  FROM (SELECT t.case_id,
                               percentile_cont(0.5) WITHIN GROUP
                                   (ORDER BY length(t.fulltext)) AS med
                        FROM case_text t
                        WHERE t.case_id = ANY(%s::bigint[])
                          AND t.fulltext IS NOT NULL AND t.source <> 'RECHTSPRAAK'
                        GROUP BY t.case_id) m
                 WHERE ct.case_id = m.case_id
                   AND ct.fulltext IS NOT NULL AND ct.source <> 'RECHTSPRAAK'
                   AND ct.is_stub IS DISTINCT FROM
                       (length(ct.fulltext) < 0.40 * m.med AND m.med > 10000)""",
                params=[ids], execute=True)
            flags_updated += out.get("row_count") or 0
        except RuntimeError as exc:
            # split on the 10k-row cap AND on statement timeouts — chunks of
            # text-heavy cases detoast a lot computing lengths for medians
            splittable = ("write_row_limit_exceeded" in str(exc)
                          or "QueryCanceled" in str(exc))
            if splittable and len(ids) > 50:
                half = len(ids) // 2
                recompute(ids[:half])
                recompute(ids[half:])
            else:
                raise

    failed_chunks = 0
    CHUNK = 400
    for i in range(0, len(case_ids), CHUNK):
        try:
            recompute(case_ids[i:i + CHUNK])
        except Exception as exc:
            # a transient outage must not kill the whole pass — skip the
            # chunk, keep going, and fail the exit code at the end
            failed_chunks += 1
            print(f"  recompute chunk at {i} FAILED: {str(exc)[:200]}", flush=True)
    print(f"done: {inserted:,} rows inserted, {flags_updated:,} stub flags updated, "
          f"{failed_chunks} recompute chunks failed")
    return 1 if failed_chunks else 0


if __name__ == "__main__":
    raise SystemExit(main())
