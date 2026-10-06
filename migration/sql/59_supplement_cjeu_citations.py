#!/usr/bin/env python3
"""Top up case_citation with CELLAR relations published after extraction.

The Publications Office curates cdm:work_cites_work for judgments with a
lag of weeks to months (KNOWN_ISSUES #1), so recent cases loaded from the
corpus have metadata and texts but no `cites` edges. This script re-probes
CELLAR for CJEU cases decided in the last MONTHS_BACK months and appends
whatever exists now:

- outgoing: our case cites X  -> resolved target or target_celex_raw
- incoming, citing work in the corpus  -> forward `cites` edge from it
- incoming, citing work unknown        -> `cited_by` row on our case with
  the citing CELEX in target_celex_raw

All inserts go through ON CONFLICT DO NOTHING against the existing partial
unique indexes, so re-running is a no-op; the case_citation_counts trigger
maintains counts. Talks to the database through the Coolify sql-runner
(HMAC-signed) — Postgres itself is not reachable from outside.

Env: SQL_RUNNER_URL, SQL_RUNNER_TOKEN, MONTHS_BACK (default 18),
     SQL_RUNNER_CONFIRM (default execute-cle-v2)

Run monthly until an ETL service inside the compose network replaces it.
"""
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
CDM = "http://publications.europa.eu/ontology/cdm#"
XSD_STRING = "http://www.w3.org/2001/XMLSchema#string"

URL = os.environ["SQL_RUNNER_URL"]
TOKEN = os.environ["SQL_RUNNER_TOKEN"]
CONFIRM = os.environ.get("SQL_RUNNER_CONFIRM", "execute-cle-v2")
MONTHS_BACK = int(os.environ.get("MONTHS_BACK", "18"))


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
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.loads(r.read())
            if not out.get("ok"):
                raise RuntimeError(str(out))
            return out
        except Exception as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise last


def sparql(query):
    body = urllib.parse.urlencode({"query": query}).encode()
    req = urllib.request.Request(SPARQL, data=body, method="POST", headers={
        "Accept": "application/sparql-results+json",
        "Content-Type": "application/x-www-form-urlencoded"})
    last = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                d = json.loads(r.read())
            return [(b["src"]["value"], b["dst"]["value"])
                    for b in d["results"]["bindings"]]
        except Exception as exc:
            last = exc
            time.sleep(5 * (attempt + 1))
    raise last


def sparql_pairs(celexes, direction):
    values = " ".join(f'"{c}"^^<{XSD_STRING}>' for c in celexes)
    if direction == "out":
        pattern = f"""VALUES ?src {{ {values} }}
          ?w <{CDM}resource_legal_id_celex> ?src .
          ?w <{CDM}work_cites_work> ?c .
          ?c <{CDM}resource_legal_id_celex> ?dst ."""
    else:
        pattern = f"""VALUES ?dst {{ {values} }}
          ?w <{CDM}resource_legal_id_celex> ?dst .
          ?c <{CDM}work_cites_work> ?w .
          ?c <{CDM}resource_legal_id_celex> ?src ."""
    return sparql(f"SELECT DISTINCT ?src ?dst WHERE {{ {pattern} }}")


def main() -> int:
    # 1. candidates: recent CJEU cases (paginated — the runner caps rows)
    candidates = []
    last_id = 0
    while True:
        out = runner("""
            SELECT c.id, c.celex_id FROM cases c
            JOIN cjeu_document d ON d.case_id = c.id
            WHERE c.celex_id IS NOT NULL
              AND c.date_decision >= (current_date - (%s || ' months')::interval)
              AND c.id > %s
            ORDER BY c.id LIMIT 900""", params=[str(MONTHS_BACK), last_id])
        rows = out["rows"]
        if not rows:
            break
        candidates.extend((r["id"], r["celex_id"]) for r in rows)
        last_id = rows[-1]["id"]
    celexes = sorted({c for _, c in candidates})
    print(f"{len(celexes)} candidate CELEXes (decided in last {MONTHS_BACK} months)")

    # 2. probe CELLAR in batches, both directions
    outgoing, incoming = [], []
    for i in range(0, len(celexes), 100):
        chunk = celexes[i:i + 100]
        outgoing.extend(sparql_pairs(chunk, "out"))
        incoming.extend(sparql_pairs(chunk, "in"))
        print(f"  probed {min(i + 100, len(celexes))}/{len(celexes)} "
              f"(+{len(outgoing)} out, +{len(incoming)} in)", flush=True)
    outgoing = sorted(set(outgoing))
    incoming = sorted(set(incoming))

    # 3. insert via the runner, resolution done server-side
    inserted = 0
    for i in range(0, len(outgoing), 500):
        chunk = outgoing[i:i + 500]
        out = runner("""
            INSERT INTO case_citation (source_case_id, target_case_id,
                target_celex_raw, relation_type, source_dataset, is_cross_jurisdiction)
            SELECT s.id, t.id, v.dst, 'cites', 'cellar_sparql', false
            FROM unnest(%s::text[], %s::text[]) AS v(src, dst)
            JOIN cases s ON s.celex_id = v.src
            LEFT JOIN cases t ON t.celex_id = v.dst
            ON CONFLICT DO NOTHING""",
            params=[[p[0] for p in chunk], [p[1] for p in chunk]], execute=True)
        inserted += out.get("row_count") or 0
    for i in range(0, len(incoming), 500):
        chunk = incoming[i:i + 500]
        out = runner("""
            INSERT INTO case_citation (source_case_id, target_case_id,
                target_celex_raw, relation_type, source_dataset, is_cross_jurisdiction)
            SELECT s.id, t.id, v.dst, 'cites', 'cellar_sparql', false
            FROM unnest(%s::text[], %s::text[]) AS v(src, dst)
            JOIN cases s ON s.celex_id = v.src
            JOIN cases t ON t.celex_id = v.dst
            ON CONFLICT DO NOTHING""",
            params=[[p[0] for p in chunk], [p[1] for p in chunk]], execute=True)
        inserted += out.get("row_count") or 0
        out = runner("""
            INSERT INTO case_citation (source_case_id, target_celex_raw,
                relation_type, source_dataset, is_cross_jurisdiction)
            SELECT t.id, v.src, 'cited_by', 'cellar_sparql', false
            FROM unnest(%s::text[], %s::text[]) AS v(src, dst)
            JOIN cases t ON t.celex_id = v.dst
            WHERE NOT EXISTS (SELECT 1 FROM cases s WHERE s.celex_id = v.src)
            ON CONFLICT DO NOTHING""",
            params=[[p[0] for p in chunk], [p[1] for p in chunk]], execute=True)
        inserted += out.get("row_count") or 0

    print(f"done: {len(outgoing)} outgoing + {len(incoming)} incoming pairs "
          f"from CELLAR, {inserted} new case_citation rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
