#!/usr/bin/env python3
"""Sync new CJEU fulltext rows into cle_v2 through the Coolify sql-runner.

The runner-transport successor to 57_supplement_cjeu_texts.py, needed since
the production Postgres is only reachable inside the compose network.
Diffs fulltexts.parquet against loaded (case_id, language, source, content-md5)
state, inserts missing rows, replaces stale-source or changed same-source rows,
then recomputes the is_stub flag in id-windowed chunks (the runner caps UPDATE
at 10k affected rows and 30s per statement; DROP/TRUNCATE are blocked, so no
staging tables).

Measured transport: the runner takes 60 MB bodies in ~3.5s. Batches here
stay ~6 MB / 250 rows because inserts compute fulltext_tsv server-side and
must fit the 30s statement timeout.

Env:
  SQL_RUNNER_URL      e.g. https://demo-psql.caselawexplorer.tech
  SQL_RUNNER_TOKEN    HMAC token
  SQL_RUNNER_CONFIRM  default execute-cle-v2
  FULLTEXTS_PARQUET   local path; downloads from HF when unset
  TARGET_ECLIS_TSV    optional ecli/celex TSV; force-replaces only those ECLIs
  TARGET_LANGUAGE     language used with TARGET_ECLIS_TSV (default: en)
"""

import hashlib
import hmac
import csv
import json
import os
import re
import secrets
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq

URL = os.environ["SQL_RUNNER_URL"]
TOKEN = os.environ["SQL_RUNNER_TOKEN"]
CONFIRM = os.environ.get("SQL_RUNNER_CONFIRM", "execute-cle-v2")

COLS = (
    "ecli",
    "celex",
    "text",
    "text_source",
    "text_language",
    "text_format",
    "missing_reasons",
)
BATCH_ROWS = 250
BATCH_BYTES = 6_000_000


def load_targets(path):
    """Return normalized ``{ECLI: CELEX}`` targets from a TSV file."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows or not {"ecli", "celex"}.issubset(rows[0]):
        raise SystemExit(f"{path} must contain ecli and celex columns")
    return {row["ecli"].strip().upper(): row["celex"].strip().upper() for row in rows}


def looks_like_judgment(text):
    opening = " ".join((text or "").split())[:5_000]
    opening = re.sub(r"\bENJUDGMENT\b", "JUDGMENT", opening, flags=re.IGNORECASE)
    return any(
        re.search(marker, opening, re.IGNORECASE)
        for marker in (
            r"\bjudgment of the court\b",
            r"\bparties\s+grounds\s+operative part\b",
            r"\bthe court(?:\s*\([^)]*\))? hereby\b",
        )
    )


def select_target_rows(rows, targets, language="en"):
    """Select and validate one full judgment row per targeted ECLI.

    This is a mutation preflight: it rejects the whole sync before any SQL
    write when a target is absent, points at a suffixed manifestation, or
    does not look like a judgment.
    """
    candidates = {}
    wanted_language = language.lower()
    for row in rows:
        ecli = str(row.get("ecli") or "").strip().upper()
        lang = str(row.get("text_language") or "").strip().lower()
        if ecli not in targets or lang != wanted_language:
            continue
        expected_celex = targets[ecli]
        celexes = {
            token.strip().upper()
            for token in str(row.get("celex") or "").split(";")
            if token.strip()
        }
        text = row.get("text") or ""
        valid = expected_celex in celexes and looks_like_judgment(text)
        rank = (valid, expected_celex in celexes, len(text))
        if ecli not in candidates or rank > candidates[ecli][0]:
            candidates[ecli] = (rank, row)

    errors = []
    selected = []
    for ecli, expected_celex in targets.items():
        item = candidates.get(ecli)
        if item is None:
            errors.append(f"{ecli}: missing {wanted_language} row")
            continue
        row = item[1]
        celexes = {
            token.strip().upper()
            for token in str(row.get("celex") or "").split(";")
            if token.strip()
        }
        if expected_celex not in celexes:
            errors.append(
                f"{ecli}: expected CELEX {expected_celex}, got {sorted(celexes)}"
            )
        elif not looks_like_judgment(row.get("text") or ""):
            errors.append(f"{ecli}: selected text is not a full judgment")
        else:
            selected.append(row)
    if errors:
        preview = "\n".join(errors[:20])
        raise SystemExit(
            f"target preflight failed for {len(errors)}/{len(targets)} rows:\n{preview}"
        )
    return selected


def iter_parquet_rows(parquet):
    # Match the published parquet's 500-row groups. Larger batches cause
    # Arrow's allocator to retain multiple text-heavy groups and can grow to
    # several gigabytes over a full-corpus scan on a 32 GB worker.
    for pbatch in parquet.iter_batches(batch_size=500, columns=list(COLS)):
        columns = {column: pbatch.column(column).to_pylist() for column in COLS}
        for values in zip(*(columns[column] for column in COLS)):
            yield dict(zip(COLS, values))


def content_md5(text):
    return hashlib.md5((text or "").encode()).hexdigest()


def content_needs_update(database_md5, text, *, force=False):
    return force or database_md5 != content_md5(text)


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
        msg = "\n".join(
            [
                "POST",
                urllib.parse.urlparse(url).path,
                ts,
                nonce,
                hashlib.sha256(body).hexdigest(),
            ]
        ).encode()
        sig = hmac.new(TOKEN.encode(), msg, hashlib.sha256).hexdigest()
        req = urllib.request.Request(
            url,
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
    target_tsv = os.environ.get("TARGET_ECLIS_TSV")
    targets = load_targets(target_tsv) if target_tsv else None
    target_language = os.environ.get("TARGET_LANGUAGE", "en").lower()
    path = os.environ.get("FULLTEXTS_PARQUET")
    if not path and not recompute_only:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(
            "davidwickerhf/cjeu-opendata", "fulltexts.parquet", repo_type="dataset"
        )

    print("fetching ECLI map ...", flush=True)
    ecli_to_id = {}
    for r in paginated("""
            SELECT c.id, c.ecli FROM cases c
            JOIN cjeu_document d ON d.case_id = c.id
            WHERE c.id > %s ORDER BY c.id LIMIT 900"""):
        ecli_to_id[r["ecli"]] = r["id"]
    print(f"  {len(ecli_to_id)} CJEU-corpus ECLIs")
    if targets:
        missing_db_cases = sorted(set(targets).difference(ecli_to_id))
        if missing_db_cases:
            raise SystemExit(
                f"target preflight failed: {len(missing_db_cases)} ECLIs absent "
                f"from production, first={missing_db_cases[:10]}"
            )

    print("fetching loaded text triples ...", flush=True)
    seen = set()
    db_pair_rows = {}  # (case_id, language) -> [(row_id, source, md5), ...]
    for r in paginated("""
            SELECT ct.id, ct.case_id, ct.language, ct.source,
                   md5(coalesce(ct.fulltext, '')) AS fulltext_md5
            FROM case_text ct
            JOIN cjeu_document d ON d.case_id = ct.case_id
            WHERE ct.id > %s ORDER BY ct.id LIMIT 900"""):
        seen.add((r["case_id"], r["language"], r["source"]))
        db_pair_rows.setdefault((r["case_id"], r["language"]), []).append(
            (r["id"], r["source"], r["fulltext_md5"])
        )
    print(f"  {len(seen)} triples already loaded")

    inserted = 0
    upgraded = 0
    scanned = 0
    batch, batch_bytes = [], 0
    upd_batch, upd_bytes = [], 0
    if recompute_only:
        print("RECOMPUTE_ONLY=1 — skipping parquet scan/insert phase")

    def flush():
        nonlocal inserted, batch, batch_bytes
        if not batch:
            return
        out = runner(
            """
            INSERT INTO case_text (case_id, language, fulltext, source,
                                   text_format, missing_reasons)
            SELECT v.case_id, v.language, nullif(v.fulltext, ''), v.source,
                   nullif(v.text_format, ''), nullif(v.missing_reasons, '')
            FROM unnest(%s::bigint[], %s::text[], %s::text[], %s::text[],
                        %s::text[], %s::text[])
                 AS v(case_id, language, fulltext, source, text_format,
                      missing_reasons)
            ON CONFLICT (case_id, language, source) DO NOTHING""",
            params=[[b[i] for b in batch] for i in range(6)],
            execute=True,
        )
        inserted += out.get("row_count") or 0
        batch, batch_bytes = [], 0

    def flush_upgrades():
        # in-place source replacement: the parquet's row for this (case,
        # language) pair supersedes a stale CJEU-source DB row (e.g. an
        # InfoCuria wrong-document replaced by the CELLAR judgment).
        # Updating the existing row keeps its id and summary, and keeps the
        # canonical view from serving the stale text (D12 order prefers
        # INFOCURIA over CELLAR_ITEM).
        nonlocal upgraded, upd_batch, upd_bytes
        if not upd_batch:
            return
        out = runner(
            """
            UPDATE case_text ct
               SET source = v.source, fulltext = nullif(v.fulltext, ''),
                   text_format = nullif(v.text_format, ''),
                   missing_reasons = nullif(v.missing_reasons, '')
              FROM unnest(%s::bigint[], %s::text[], %s::text[],
                          %s::text[], %s::text[])
                   AS v(id, fulltext, source, text_format, missing_reasons)
             WHERE ct.id = v.id""",
            params=[[b[i] for b in upd_batch] for i in range(5)],
            execute=True,
        )
        upgraded += out.get("row_count") or 0
        upd_batch, upd_bytes = [], 0

    pf = pq.ParquetFile(path) if not recompute_only else None
    parquet_rows = iter_parquet_rows(pf) if pf else []
    if targets and pf:
        print(
            f"preflighting {len(targets)} targeted {target_language} judgments ...",
            flush=True,
        )
        parquet_rows = select_target_rows(parquet_rows, targets, target_language)
        print("  target preflight passed", flush=True)
    for row in parquet_rows:
        e = row["ecli"]
        t = row["text"]
        s2 = row["text_source"]
        l = row["text_language"]
        f2 = row["text_format"]
        m2 = row["missing_reasons"]
        scanned += 1
        normalized_ecli = str(e).strip().upper() if e else ""
        cid = ecli_to_id.get(normalized_ecli) if normalized_ecli else None
        lang = (l or "").lower()
        src = s2 or "UNKNOWN"
        text_md5 = content_md5(t)
        if not cid or not lang:
            continue

        existing = db_pair_rows.get((cid, lang), [])
        same_source = [item for item in existing if item[1] == src]
        force_target = bool(targets)
        content_changed = same_source and content_needs_update(
            same_source[0][2], t, force=force_target
        )
        if same_source and (force_target or content_changed):
            rid, _, _ = same_source[0]
            db_pair_rows[(cid, lang)] = [
                (row_id, row_source, text_md5 if row_id == rid else old_md5)
                for row_id, row_source, old_md5 in existing
            ]
            upd_batch.append((rid, t or "", src, f2 or "", m2 or ""))
            upd_bytes += len(t or "")
            if len(upd_batch) >= BATCH_ROWS or upd_bytes >= BATCH_BYTES:
                flush_upgrades()
            continue
        if same_source:
            continue

        seen.add((cid, lang, src))
        stale = [item for item in existing if item[1] not in ("RECHTSPRAAK", src)]
        if stale:
            rid, old_src, _ = stale[0]
            seen.discard((cid, lang, old_src))
            db_pair_rows[(cid, lang)] = [
                (
                    row_id,
                    src if row_id == rid else row_source,
                    text_md5 if row_id == rid else old_md5,
                )
                for row_id, row_source, old_md5 in existing
            ]
            upd_batch.append((rid, t or "", src, f2 or "", m2 or ""))
            upd_bytes += len(t or "")
            if len(upd_batch) >= BATCH_ROWS or upd_bytes >= BATCH_BYTES:
                flush_upgrades()
            continue
        db_pair_rows.setdefault((cid, lang), []).append((None, src, text_md5))
        batch.append((cid, lang, t or "", src, f2 or "", m2 or ""))
        batch_bytes += len(t or "")
        if len(batch) >= BATCH_ROWS or batch_bytes >= BATCH_BYTES:
            flush()
        if scanned % 100_000 < 2000:
            print(f"  scanned {scanned:,} — inserted {inserted:,}", flush=True)
    flush()
    flush_upgrades()
    print(
        f"insert phase done: scanned {scanned:,}, inserted {inserted:,}, "
        f"upgraded in place {upgraded:,}"
    )

    # Stub-flag recompute, chunked by case id list (respects the 10k-row
    # UPDATE cap and the 30s statement timeout; splits a chunk on 403).
    print("recomputing is_stub in chunks ...", flush=True)
    case_ids = sorted(
        {ecli_to_id[ecli] for ecli in targets} if targets else set(ecli_to_id.values())
    )
    flags_updated = 0

    def recompute(ids):
        nonlocal flags_updated
        try:
            out = runner(
                """
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
                params=[ids],
                execute=True,
            )
            flags_updated += out.get("row_count") or 0
        except RuntimeError as exc:
            # split on the 10k-row cap AND on statement timeouts — chunks of
            # text-heavy cases detoast a lot computing lengths for medians
            splittable = "write_row_limit_exceeded" in str(
                exc
            ) or "QueryCanceled" in str(exc)
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
            recompute(case_ids[i : i + CHUNK])
        except Exception as exc:
            # a transient outage must not kill the whole pass — skip the
            # chunk, keep going, and fail the exit code at the end
            failed_chunks += 1
            print(f"  recompute chunk at {i} FAILED: {str(exc)[:200]}", flush=True)
    print(
        f"done: {inserted:,} rows inserted, {flags_updated:,} stub flags updated, "
        f"{failed_chunks} recompute chunks failed"
    )
    return 1 if failed_chunks else 0


if __name__ == "__main__":
    raise SystemExit(main())
