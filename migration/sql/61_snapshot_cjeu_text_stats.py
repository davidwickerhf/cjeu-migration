#!/usr/bin/env python3
"""Snapshot CJEU text/coverage statistics from cle_v2 via the sql-runner.

Prints one JSON object to stdout. Run before and after any data-altering
pass and diff the two files — the sanity check that shows exactly what a
sync changed (and that control metrics it must not touch stayed put).

Each metric is its own request so no statement risks the runner's 30s cap.

Env: SQL_RUNNER_URL, SQL_RUNNER_TOKEN
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.parse
import urllib.request

URL = os.environ["SQL_RUNNER_URL"]
TOKEN = os.environ["SQL_RUNNER_TOKEN"]


def runner(sql, params=None):
    payload = {"sql": sql}
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload, separators=(",", ":")).encode()
    url = URL.rstrip("/") + "/query"
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
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(r.read())
            if not out.get("ok"):
                raise RuntimeError(str(out)[:200])
            return out["rows"]
        except Exception as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise last


SCALARS = {
    # control metrics — a text sync must NOT change these
    "cases_total": "SELECT count(*) AS v FROM cases",
    "cjeu_cases": "SELECT count(*) AS v FROM cjeu_document",
    "citations_total": "SELECT count(*) AS v FROM case_citation",
    "rs_text_rows": "SELECT count(*) AS v FROM case_text WHERE source = 'RECHTSPRAAK'",
    "hudoc_text_rows": "SELECT count(*) AS v FROM case_text WHERE source = 'HUDOC'",
    # metrics the sync is expected to move
    "cjeu_text_rows": """SELECT count(*) AS v FROM case_text ct
        JOIN cjeu_document d ON d.case_id = ct.case_id
        WHERE ct.source <> 'RECHTSPRAAK'""",
    "cjeu_with_fulltext": """SELECT count(*) AS v FROM case_text ct
        JOIN cjeu_document d ON d.case_id = ct.case_id
        WHERE ct.source <> 'RECHTSPRAAK' AND ct.fulltext IS NOT NULL""",
    "cjeu_stub_rows": """SELECT count(*) AS v FROM case_text ct
        JOIN cjeu_document d ON d.case_id = ct.case_id
        WHERE ct.source <> 'RECHTSPRAAK' AND ct.is_stub""",
    "cjeu_cases_with_summary": """SELECT count(DISTINCT ct.case_id) AS v
        FROM case_text ct JOIN cjeu_document d ON d.case_id = ct.case_id
        WHERE ct.summary IS NOT NULL""",
    "distinct_languages": """SELECT count(DISTINCT ct.language) AS v
        FROM case_text ct JOIN cjeu_document d ON d.case_id = ct.case_id""",
}

COVERAGE = """
    SELECT CASE WHEN n >= 20 THEN '20+' WHEN n >= 10 THEN '10-19'
                WHEN n >= 5 THEN '5-9' WHEN n >= 2 THEN '2-4'
                ELSE '1' END AS bucket,
           count(*) AS cases
    FROM (SELECT ct.case_id, count(DISTINCT ct.language) AS n
          FROM case_text ct
          JOIN cjeu_document d ON d.case_id = ct.case_id
          WHERE ct.fulltext IS NOT NULL AND ct.source <> 'RECHTSPRAAK'
          GROUP BY ct.case_id) s
    GROUP BY 1 ORDER BY 1
"""


def main() -> int:
    snap = {"taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    for name, sql in SCALARS.items():
        snap[name] = runner(sql)[0]["v"]
    snap["fulltext_lang_coverage"] = {
        r["bucket"]: r["cases"] for r in runner(COVERAGE)}
    json.dump(snap, sys.stdout, indent=2, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
