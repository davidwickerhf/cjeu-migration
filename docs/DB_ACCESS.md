# Connecting to the CLE Postgres and uploading data

How the scraped-data pipeline reaches the production database, and how to
replicate the connection. Last verified 2026-08-19.

## Architecture: there is no direct Postgres connection

The production database (`cle_v2` schema, database `caselaw`) is the
bundled Postgres of the caselaw-coolify stack. It listens only inside the
Docker compose network (`db:5432`) and is not exposed to the internet.
Three access paths exist:

| Path | Use for | Where it runs |
|---|---|---|
| **sql-runner** (HTTPS + HMAC) | day-to-day queries, incremental data uploads, maintenance passes | anywhere |
| one-shot container in the compose network | very large restores or ETL that outgrows HTTP | the Coolify host |
| db-importer service | full `pg_dump`/`pg_restore` of the schema | Coolify deploy (env-gated) |

Everything below is about the sql-runner, which is how all incremental
uploads (text top-ups, citation supplements, normalization passes) have
been done since the Coolify cutover.

Historical note: the original bulk load ran against Neon staging with
direct `psycopg` connections (`TARGET_DB_URL`, scripts 00–57 in
`migration/`). Neon is deprecated; those scripts' *logic* lives on in the
runner-based clients, but their direct-connection transport does not work
against production.

## The sql-runner

- URL: `https://demo-psql.caselawexplorer.tech` (the Coolify-generated
  domain of the `sql-runner` service — check the service card in Coolify
  if it changes)
- Source: `sql-runner/app.py` in the caselaw-coolify repo
- Config: the `SQL_RUNNER_*` variables in the Coolify environment
  (`.env.coolify` documents them)
- Health check (no auth): `curl https://demo-psql.caselawexplorer.tech/healthz`

### Authentication: HMAC request signing

The token (`SQL_RUNNER_TOKEN`, from the Coolify env editor) is never sent
over the wire. Every request carries three headers computed from it:

```
X-SQL-Runner-Timestamp: <unix seconds>
X-SQL-Runner-Nonce:     <random hex, 12-128 chars, single-use>
X-SQL-Runner-Signature: v1=<hex hmac>
```

The signature is HMAC-SHA256 over the canonical string:

```
METHOD \n PATH \n timestamp \n nonce \n sha256_hex(body)
```

signed with the token. The runner rejects timestamps older than 300s and
reused nonces — so plain `curl` cannot talk to it; use the helper or the
snippet below.

Easiest client: `scripts/sql_runner_request.py` in caselaw-coolify (reads
`SQL_RUNNER_TOKEN` from the environment or `.env.coolify`):

```bash
export SQL_RUNNER_URL=https://demo-psql.caselawexplorer.tech
python3 scripts/sql_runner_request.py --url "$SQL_RUNNER_URL" \
  --sql "SELECT count(*) FROM cases"

# writes need --execute and the confirmation value
python3 scripts/sql_runner_request.py --url "$SQL_RUNNER_URL" \
  --execute --confirm execute-cle-v2 \
  --sql "INSERT INTO ... ON CONFLICT DO NOTHING"
```

Minimal standalone signing (Python stdlib only):

```python
import hashlib, hmac, json, secrets, time, urllib.request
from urllib.parse import urlparse

def send(url_base, token, endpoint, payload):
    body = json.dumps(payload, separators=(",", ":")).encode()
    url = url_base.rstrip("/") + "/" + endpoint       # "query" or "execute"
    ts, nonce = str(int(time.time())), secrets.token_hex(16)
    msg = "\n".join(["POST", urlparse(url).path, ts, nonce,
                     hashlib.sha256(body).hexdigest()]).encode()
    sig = hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "X-SQL-Runner-Timestamp": ts,
        "X-SQL-Runner-Nonce": nonce,
        "X-SQL-Runner-Signature": f"v1={sig}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())
```

### Endpoints and request shape

- `POST /query` — runs inside a read-only transaction.
- `POST /execute` — writes; requires `SQL_RUNNER_ALLOW_WRITES=true` in the
  deployment AND `"confirm": "execute-cle-v2"` in the payload.

Payload: `{"sql": "...", "params": [...], "confirm": "..."}`. Placeholders
are psycopg-style `%s`; JSON arrays in `params` arrive as Postgres arrays,
which is the key to bulk uploads. The session `search_path` is set to
`cle_v2, public` server-side.

### Limits and guardrails (the part that shapes your code)

| Limit | Value | Consequence |
|---|---|---|
| statement timeout | 30s | long statements are killed; **surfaces as HTTP 400 with `QueryCanceled`**, not as a timeout error |
| result rows | 1,000 | paginate reads (keyset, `WHERE id > %s ORDER BY id LIMIT 900`) |
| UPDATE/DELETE affected rows | 10,000 | HTTP 403 `write_row_limit_exceeded`, statement rolled back — chunk your updates and bisect on 403 |
| SQL text size | 1 MB | **`params` are exempt** — measured: 60 MB request bodies round-trip in ~3.5s |
| blocked commands | DROP, TRUNCATE, ALTER SYSTEM, COPY PROGRAM | no staging tables; design around INSERT … ON CONFLICT and windowed UPDATEs |
| DELETE | requires a WHERE clause | — |
| transactions | one per request | TEMP tables do not survive between requests |

## The upload pipeline (scraped data → database)

The pattern every supplement pass follows, end to end:

1. **Extraction / top-up runs on a rented worker box** (Vast.ai — any
   cheap 6+ core machine; the GPU is irrelevant). It fetches from
   CELLAR/InfoCuria with `cellar-extractor@dev` and appends rows to the
   HF corpus (`davidwickerhf/cjeu-opendata`), uploading a checkpoint every
   2,000 processed cases so a dead box loses minutes, not hours
   (`scripts/topup_multilang_fulltexts.py`).
2. **The sync diffs the corpus against the DB and uploads the delta**
   (`migration/sql/60_sync_cjeu_texts_via_runner.py`): it reads the
   current `(case_id, language, source)` triples through paginated
   `/query` calls, streams the parquet, and pushes only missing rows via
   `INSERT … SELECT FROM unnest(%s::bigint[], %s::text[], …) ON CONFLICT
   DO NOTHING` — batches of ~250 rows / 6 MB, sized so server-side
   `fulltext_tsv` generation fits the 30s cap. Everything is idempotent:
   re-running after any failure is safe.
3. **Snapshots bracket every write** (`migration/sql/61_snapshot_cjeu_text_stats.py`):
   the same stats JSON is captured before and after, and diffed into a
   sanity report that separates control metrics (case/citation counts —
   must not move) from expected movement (text rows, language coverage).
   Never skip the before-snapshot.
4. Post-pass maintenance (stub-flag recompute etc.) runs as windowed
   UPDATEs over explicit case-id lists, ~400 cases per statement, with
   bisection on timeout/row-cap errors.

Reference clients, in increasing complexity:
`61_snapshot_cjeu_text_stats.py` (reads only) →
`59_supplement_cjeu_citations.py` (small writes) →
`60_sync_cjeu_texts_via_runner.py` (bulk upload + maintenance).

## Operational discipline

- The runner is a maintenance tool: set `SQL_RUNNER_ENABLED=false` (and
  ideally rotate the token) outside work windows, redeploy after changing
  either. Writes additionally require `SQL_RUNNER_ALLOW_WRITES=true`.
- Tokens live in the Coolify environment editor and local `.env.coolify`
  copies only — never in git, never in URLs.
- Take the before-snapshot before the first write, always. A mid-sequence
  failure without one cost us 19,803 summaries once (recovered from the
  corpus, but only because the corpus is the source of truth).
- Treat HTTP 400 `QueryCanceled` as "statement too big", not as a
  transport problem: shrink the chunk and retry.
