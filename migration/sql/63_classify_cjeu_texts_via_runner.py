#!/usr/bin/env python3
"""Classify what document each CJEU case_text row holds (document_kind).

Fills case_text.document_kind / document_celex (caselaw-coolify migration
0008) for rows of CJEU cases, through the sql-runner. Rules, first match wins:

1. Rechtspraak / HUDOC rows, and rows without fulltext: left unclassified.
2. Text identical to the published corpus text of the same (ECLI, language):
   the case's own decision (kind = the case's document type), or its OJ
   notice when the corpus labels it INFOCURIA_OJ_NOTICE.
3. Text identical to another ECLI's corpus text: misfiled.
4. Text starting with a CELEX label (CELLAR manifestations):
   ``_SUM``/``_RES`` summary, ``_INF`` information, ``_EXT`` extract; a bare
   CELEX is the case's decision when it is the case's CELEX, else misfiled.
   CELLAR's Official Journal notices start with a ``…-ARRET_DR-…`` label.
5. InfoCuria texts without a label: InfoCuria fans out language variants of
   every document in a procedure. A text whose opening does not carry the
   case's decision date (day and year) is another document: misfiled. One
   that does is the decision when its heading is in capitals ("JUDGMENT OF
   THE COURT"), else the decision's OJ notice ("Judgment of the Court ... of
   10 September 2019 – X v Y (Case C-…)").
6. Anything else stays unclassified.

Env:
  SQL_RUNNER_URL, SQL_RUNNER_TOKEN, SQL_RUNNER_CONFIRM (default execute-cle-v2)
  CORPUS_FULLTEXTS   fulltexts.parquet path or hf:// URL of the published corpus
  CORPUS_CASES       cases.parquet path or hf:// URL of the same revision
  DRY_RUN            default 1: classify and report, write nothing
  REPORT_TSV         optional path: one line per classified row
  OVERWRITE          default 0: only fill rows whose document_kind is NULL
  CORPUS_INDEX_CACHE optional pickle path: reuse the corpus index across runs
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

import pyarrow.parquet as pq

DECISION_KINDS = {"judgment", "order", "opinion", "ruling", "decision", "other"}
CELEX_LABEL = re.compile(
    r"^\s*(\d{5}[A-Z]{1,2}\d{4}(?:\(\d{2}\))?)(?:_(SUM|RES|INF|EXT))?(?:_[A-Z]{2})?\b"
)
OJ_NOTICE_LABEL = re.compile(r"^\s*\S*-TRA-DOC-[A-Z]{2}-[A-Z]+_DR-")
SKIP_SOURCES = {"RECHTSPRAAK", "HUDOC"}
PAGE = 500
UPDATE_CHUNK = 2_000


def runner(sql, params=None, execute=False):
    url = os.environ["SQL_RUNNER_URL"].rstrip("/") + ("/execute" if execute else "/query")
    payload = {"sql": sql}
    if params is not None:
        payload["params"] = params
    if execute:
        payload["confirm"] = os.environ.get("SQL_RUNNER_CONFIRM", "execute-cle-v2")
    body = json.dumps(payload, separators=(",", ":")).encode()
    last = None
    for attempt in range(5):
        ts, nonce = str(int(time.time())), secrets.token_hex(16)
        msg = "\n".join(
            ["POST", urllib.parse.urlparse(url).path, ts, nonce, hashlib.sha256(body).hexdigest()]
        ).encode()
        sig = hmac.new(os.environ["SQL_RUNNER_TOKEN"].encode(), msg, hashlib.sha256).hexdigest()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "X-SQL-Runner-Timestamp": ts,
            "X-SQL-Runner-Nonce": nonce, "X-SQL-Runner-Signature": f"v1={sig}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                out = json.loads(response.read())
            if not out.get("ok"):
                raise RuntimeError(str(out)[:300])
            return out
        except urllib.error.HTTPError as exc:
            last = RuntimeError(f"HTTP {exc.code}: {exc.read().decode()[:300]}")
            if "QueryCanceled" in str(last):
                raise last  # a statement timeout repeats; callers shrink the batch
        except Exception as exc:  # noqa: BLE001 - transport errors are retried
            last = exc
        time.sleep(3 * (attempt + 1))
    raise last


def open_parquet(path):
    if str(path).startswith("hf://"):
        from huggingface_hub import HfFileSystem

        return pq.ParquetFile(HfFileSystem().open(path, "rb"))
    return pq.ParquetFile(path)


def corpus_index(fulltexts_path, cases_path):
    """(ECLI, language) -> (source, md5); md5 -> owners; ECLI -> CELEX."""
    texts, owners = {}, defaultdict(set)
    columns = ["ecli", "text_language", "text_source", "text"]
    for batch in open_parquet(fulltexts_path).iter_batches(batch_size=1_000, columns=columns):
        rows = batch.to_pydict()
        for ecli, language, source, text in zip(*(rows[c] for c in columns)):
            if not (text or "").strip():
                continue
            key = ((ecli or "").strip().upper(), (language or "").strip().lower())
            digest = hashlib.md5(text.encode("utf-8")).hexdigest()
            texts[key] = (source or "", digest)
            owners[(key[1], digest)].add(key[0])
    celex = {}
    for batch in open_parquet(cases_path).iter_batches(batch_size=4_096, columns=["ecli", "celex"]):
        for ecli, value in zip(batch.column("ecli").to_pylist(), batch.column("celex").to_pylist()):
            if ecli and value:
                celex[ecli.strip().upper()] = value.split(";", 1)[0].split("_", 1)[0]
    return texts, owners, celex


def capitalised_heading(head: str) -> bool:
    """Decisions open with a heading in capitals ("ARRÊT DE LA COUR"), possibly
    after a "provisional text" marker ("Edizione provvisoria SENTENZA DEL
    TRIBUNALE"); OJ notices and information sheets open with a sentence
    ("Judgment of the Court ... of ... – X v Y (Case C-…)")."""
    for match in re.finditer(r"[^\W\d_]{2,}(?:\s+[^\W\d_]+){1,2}", head[:60]):
        words = match.group(0).split()
        if match.start() < 40 and all(word.isupper() for word in words):
            return True
    return False


def carries_date(head: str, decision_date: str | None) -> bool:
    if not decision_date:
        return False
    year, _, day = decision_date.split("-")
    opening = head[:300]
    return year in opening and re.search(rf"(?<!\d){int(day)}(?!\d)", opening) is not None


def classify(row, texts, owners, celex_of):
    """Return (document_kind, document_celex, rule) or None to leave NULL."""
    if row["source"] in SKIP_SOURCES or not row["md5"]:
        return None
    ecli, language = row["ecli"], row["language"]
    case_kind = row["doctype"] if row["doctype"] in DECISION_KINDS else "other"
    case_celex = row["celex_id"]
    own = texts.get((ecli, language))
    if own and own[1] == row["md5"]:
        if own[0] == "INFOCURIA_OJ_NOTICE":
            return "oj_notice", case_celex, "corpus"
        return case_kind, case_celex, "corpus"
    others = owners.get((language, row["md5"]), set()) - {ecli}
    if others:
        owner = sorted(others)[0]
        return "misfiled", celex_of.get(owner), "corpus-other-ecli"
    head = row["head"] or ""
    if OJ_NOTICE_LABEL.match(head):
        return "oj_notice", case_celex, "label-dr"
    label = CELEX_LABEL.match(head)
    if label:
        base, suffix = label.group(1), label.group(2)
        if suffix in ("SUM", "RES"):
            return "summary", base, "label"
        if suffix == "INF":
            return "information", base, "label"
        if suffix == "EXT":
            return "extract", base, "label"
        if base == case_celex:
            return case_kind, base, "label"
        return "misfiled", base, "label-other-celex"
    if row["source"] == "INFOCURIA_BLOB_HTML":
        if not row["decision_date"]:
            return None
        if not carries_date(head, row["decision_date"]):
            return "misfiled", None, "infocuria-other-date"
        if capitalised_heading(head):
            return case_kind, case_celex, "infocuria-dated-decision"
        return "oj_notice", case_celex, "infocuria-dated-notice"
    if row["source"] == "CELLAR_ITEM" and row["decision_date"]:
        # CELLAR manifestations without a CELEX label line: provisional
        # texts of the decision, or information sheets.
        if not carries_date(head, row["decision_date"]):
            return None
        if capitalised_heading(head):
            return case_kind, case_celex, "cellar-dated-decision"
        return "information", case_celex, "cellar-dated-information"
    return None


def fetch_rows(overwrite):
    # md5() detoasts every text, so a page of very long texts can exceed the
    # runner's 30s statement timeout: halve the page on a timeout and grow it
    # back afterwards.
    last, page = 0, PAGE
    while True:
        try:
            rows = fetch_page(last, page, overwrite)
        except RuntimeError as exc:
            if "QueryCanceled" not in str(exc) or page <= 10:
                raise
            page = max(10, page // 2)
            continue
        if not rows:
            return
        yield from rows
        last = rows[-1]["id"]
        page = min(PAGE, page * 2)


def fetch_page(last, page, overwrite):
    return runner(f"""
            SELECT ct.id, upper(c.ecli) AS ecli, c.celex_id, dt.code AS doctype,
                   c.date_decision::text AS decision_date, lower(ct.language) AS language,
                   ct.source, md5(ct.fulltext) AS md5,
                   left(regexp_replace(left(ct.fulltext, 800), '\\s+', ' ', 'g'), 400) AS head
              FROM case_text ct
              JOIN cjeu_document d ON d.case_id = ct.case_id
              JOIN cases c ON c.id = ct.case_id
              LEFT JOIN document_type dt ON dt.id = c.document_type_id
             WHERE ct.id > %s {'' if overwrite else 'AND ct.document_kind IS NULL'}
             ORDER BY ct.id LIMIT {page}""", [last])["rows"]


def write(assignments):
    written = 0
    for i in range(0, len(assignments), UPDATE_CHUNK):
        chunk = assignments[i : i + UPDATE_CHUNK]
        out = runner(
            """
            UPDATE case_text ct
               SET document_kind = v.kind, document_celex = v.celex
              FROM unnest(%s::bigint[], %s::text[], %s::text[]) AS v(id, kind, celex)
             WHERE ct.id = v.id""",
            [[a[0] for a in chunk], [a[1] for a in chunk], [a[2] for a in chunk]],
            execute=True,
        )
        written += out.get("row_count") or 0
        print(f"  updated {written:,}/{len(assignments):,}", flush=True)
    return written


def main() -> int:
    dry_run = os.environ.get("DRY_RUN", "1") != "0"
    overwrite = os.environ.get("OVERWRITE") == "1"
    cache = os.environ.get("CORPUS_INDEX_CACHE")
    if cache and os.path.exists(cache):
        import pickle

        with open(cache, "rb") as handle:
            texts, owners, celex_of = pickle.load(handle)
    else:
        print("indexing corpus ...", flush=True)
        texts, owners, celex_of = corpus_index(
            os.environ["CORPUS_FULLTEXTS"], os.environ["CORPUS_CASES"]
        )
        if cache:
            import pickle

            with open(cache, "wb") as handle:
                pickle.dump((texts, owners, celex_of), handle)
    print(f"  {len(texts):,} corpus texts", flush=True)

    report_path = os.environ.get("REPORT_TSV")
    report = csv.writer(open(report_path, "w", newline="")) if report_path else None
    if report:
        report.writerow(["id", "ecli", "language", "source", "kind", "celex", "rule", "head"])
    counts, assignments, scanned = Counter(), [], 0
    for row in fetch_rows(overwrite):
        scanned += 1
        result = classify(row, texts, owners, celex_of)
        if result is None:
            counts[("unclassified", row["source"])] += 1
            continue
        kind, celex, rule = result
        counts[(kind, rule)] += 1
        assignments.append((row["id"], kind, celex))
        if report:
            report.writerow([row["id"], row["ecli"], row["language"], row["source"],
                             kind, celex or "", rule, (row["head"] or "")[:160]])
        if scanned % 50_000 == 0:
            print(f"  scanned {scanned:,}", flush=True)

    print(f"scanned {scanned:,} rows; classified {len(assignments):,}")
    for (kind, rule), n in sorted(counts.items(), key=lambda item: -item[1]):
        print(f"  {n:>8,}  {kind:<12} {rule}")
    if dry_run:
        print("DRY RUN: nothing written")
        return 0
    print(f"wrote {write(assignments):,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
