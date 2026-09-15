# CJEU pipeline handoff

State of the CJEU data-quality campaign as of 2026-09-15: what the pipeline
is, what broke and why, what has been fixed, and exactly where work stopped.
Written so someone (or a future session) can pick this up cold. Companion
docs: [DB_ACCESS.md](DB_ACCESS.md) for the database transport,
[postgres-schema/KNOWN_ISSUES.md](postgres-schema/KNOWN_ISSUES.md) for the
issue ledger, [postgres-schema/DECISIONS.md](postgres-schema/DECISIONS.md)
for schema decisions.

## TL;DR — where things stand right now

A corpus-wide "source upgrade" sweep (replacing wrong documents that
InfoCuria served into judgment slots with the correct CELLAR texts) ran on
a rented Vast.ai box on 2026-09-11 and got through **~15,000 of 30,083
cases** before the box died at 11:22 UTC. Everything completed is safely
on HuggingFace (15 checkpoint commits). The remaining ~15k cases need a
new worker box and roughly 3–3.5 hours; the sweep is idempotent, so
relaunching is safe and skips finished work automatically. The database
sync never ran (it was chained behind the sweep on the dead box). The
user-facing promise: Kamil Szostak (external data-quality reporter) was
told the rebuild takes 12–15h and updates HF automatically.

**Immediate next steps, in order:**

1. Get a new Vast.ai box (any cheap 6+ core machine; GPU irrelevant).
2. Commit the pending sidecar-checkpoint patch in
   `scripts/topup_multilang_fulltexts.py` (uncommitted in the working
   tree; 32 tests pass), copy the script to the box, relaunch with
   `MODE=source_upgrade` (see runbook below).
3. When the sweep finishes: run the DB sync bracketed by snapshots
   (60 → 61 before/after → diff). Can run from the Mac; does not need the
   box.
4. Verify all 604 cases in `migration/verify/kamil_2026-09_en_gaps.tsv`
   now carry an English judgment (corpus and DB); produce a residual
   report for rows CELLAR could not replace (expected: a small tail,
   Kamil estimated ~33 genuinely unavailable).
5. Update KNOWN_ISSUES #5 to resolved with final numbers; email Kamil the
   confirmation.
6. Turn the sql-runner off (`SQL_RUNNER_ENABLED=false` in Coolify env,
   redeploy) — the maintenance window has been open the whole time.

## What this project is

Three assets, kept in sync:

| Asset | Where | Role |
|---|---|---|
| CJEU corpus | HF dataset `davidwickerhf/cjeu-opendata` (`cases.parquet`, `fulltexts.parquet`, `citations.parquet`) | source of truth for scraped data |
| cellar-extractor | `maastrichtlawtech/cellar-extractor` (upstream), fork `davidwickerhf/…` | the scraping library (CELLAR SPARQL + InfoCuria) |
| production DB | Postgres `cle_v2` schema, self-hosted on Coolify | serves the Case Law Explorer app/API |

The corpus is authoritative: every fix lands there first, then syncs into
the DB. The DB has been recovered from the corpus before (19,803 summaries
after a botched sequence); never treat the DB as the only copy of
anything.

Production endpoints: `demo-api.caselawexplorer.tech`,
`demo-app.caselawexplorer.tech`, sql-runner at
`https://demo-psql.caselawexplorer.tech`. Postgres itself is only
reachable inside the compose network (`db:5432`). Note that
`app.`/`api.caselawexplorer.tech` (no `demo-`) are a separate legacy
Vercel deployment; do not confuse them. Neon is fully deprecated.

## The campaign: three external reports, five root causes

Kamil Szostak (external user extracting CJEU judgments from our dataset)
sent three successive data-quality reports over the summer. Each one
exposed a real bug. Full ledger with verification details in
KNOWN_ISSUES.md; the short version:

**Report 1 (July).** A recent case with almost no language versions.
Root causes found while digging: (#1) CELLAR curates `work_cites_work`
citation edges weeks-to-months after publication, so recent judgments
legitimately have no citation rows yet — mitigated with
`59_supplement_cjeu_citations.py`, to be re-run monthly; (#2) the loader
stored full-word language codes ("english") instead of ISO codes from
`language_procedure` — fixed and normalized DB-wide; (#3, operational)
fixes applied to the old Neon staging DB do not propagate to Coolify by
themselves.

**Report 2 (July 30).** Nine preliminary-ruling judgments missing their
English text that EUR-Lex clearly has. Root cause (#4a): a CELEX can map
to several CELLAR work records with different language coverage, and the
extractor picked one arbitrarily (`order by asc(str(?doc)) limit 1`).
Fixed upstream (extractor PR #10): `_fetch_sector8_items_for_celex` now
unions manifestations across all works sharing a CELEX. The corpus-wide
re-run surfaced (#4b): 3,642 cases whose first CELEX token is a suffixed
variant (`_SUM`/`_INF`) got probed under the wrong identifier — fixed by
normalizing the token (`_first_celex`). Campaign totals: +60,660 language
rows in the corpus, +60,422 in the DB, cases with 20+ fulltext languages
went 17,326 → 20,637. Issue #4 closed, all nine cases verified.

**Report 3 (Sept 9, the one in flight).** Of ~9k preliminary-ruling
judgments on the merits, 657 lack a usable English judgment; 604 of those
have it on EUR-Lex, concentrated in 2012+ cases. Root cause (#5, verified
on samples): InfoCuria's per-procedure lookup sometimes returned the
WRONG document for a language slot — the Advocate General's opinion, a
procedural order, a referred-questions notice, or a headnote summary —
and the extractor stored it as the judgment (`INFOCURIA_BLOB_HTML`
source). That wrong row then blocked CELLAR supplementation ("language
already covered"), and a long wrong document (a 55k-char AG opinion)
defeats the stub-length detector by design. This is not an ECHR/HUDOC
issue and not limited to English: the Trojan control case had the Italian
AG opinion sitting in its Italian judgment slot.

Two findings that shaped the fix:

- **AG opinions are already first-class corpus documents** under their
  own ECLIs/CELEXes (11,270 of them). The bug is misfiling, not missing
  scope — a misfiled opinion is a duplicate of a text we already store at
  its proper home (verified byte-identical for C-307/10).
- **CELLAR under the document's own CELEX is correct by construction** —
  whatever the celex-keyed work carries IS that document. So the fix
  replaces every InfoCuria row with the CELLAR manifestation when one
  exists, with no length-ratio guard (the wrong text is often longer than
  the right one).

## The fix in flight: MODE=source_upgrade sweep

`scripts/topup_multilang_fulltexts.py` is the one sweep tool, with three
modes selected by the `MODE` env var:

- `topup` — add missing language versions (`MIN_LANGS` gate)
- `upgrade` — replace stub texts with longer CELLAR texts (min_ratio=2.0)
- `source_upgrade` — replace ALL `INFOCURIA_BLOB_HTML` rows with CELLAR
  texts under the document's own CELEX (min_ratio=0). This is the mode
  the current campaign runs.

Safety properties (these were deliberate, keep them):

- **No data loss.** Replaced originals are archived to a "superseded"
  sidecar parquet uploaded to `superseded/` in the dataset repo. After
  the 9/11 box death (sidecar only uploaded at run end, so the completed
  half's archive died with the box RAM), the script was patched to
  re-upload the accumulated sidecar **with every checkpoint**. That patch
  is currently uncommitted in the working tree. Independently, the HF
  dataset repo is git: every checkpoint is a commit, so any historical
  text is recoverable by revision.
- **Idempotent / resumable.** Replaced rows carry `CELLAR_ITEM` as their
  source in the uploaded parquet, so a relaunch's index pass
  (`stream_infocuria_index`) simply does not flag them again. Relaunching
  after a crash re-does nothing and costs only the index scan.
- **Checkpointed.** `CHECKPOINT_EVERY=1000` uploads the full corpus
  parquet every 1,000 processed cases. A dead box loses minutes.
- CELLAR rows already in the corpus are passed through byte-identical
  (verified in dry-run).

Dry-run validation (7-case mini corpus): 22/23 InfoCuria rows replaced,
all five known wrong-document English slots became judgments, 0 failures,
idempotent on second pass. ~4.4s/case at 3 workers.

**Run status:** launched 2026-09-11 08:05 UTC on a Vast box with
WORKERS=6. Index pass flagged exactly 69,073 InfoCuria rows across 30,083
cases. 15 checkpoints uploaded (08:27–11:22 UTC), i.e. ~15,000 cases
done at ~1.3 cases/s average. Box then died (connection refused;
Vast instance destroyed or outbid — third box lost this way). Remaining:
~15,000 cases, roughly 3–3.5h on a similar machine.

## Runbook: resuming the sweep on a new box

What lived on the dead box (all reproducible): `launch_sweep.sh`,
`chain_sync.sh`, `topup_multilang_fulltexts.py`,
`60_sync_cjeu_texts_via_runner.py`, `61_snapshot_cjeu_text_stats.py`,
`diff_snapshots.py`, `.runner_env` (runner URL + token), `.hf_token`, and
a venv with pandas/pyarrow/huggingface_hub/requests plus
`cellar-extractor` installed from the upstream `dev` branch.

Setup on a fresh box:

```bash
apt-get update && apt-get install -y python3-venv git
python3 -m venv /root/venv && . /root/venv/bin/activate
pip install pandas pyarrow huggingface_hub requests xmltodict sparqlwrapper beautifulsoup4
pip install "git+https://github.com/maastrichtlawtech/cellar-extractor@dev"
mkdir -p /root/work
```

Copy up the scripts (from this repo: `scripts/topup_multilang_fulltexts.py`,
`migration/sql/60…`, `migration/sql/61…`, `migration/verify/diff_snapshots.py`)
and the secrets (HF write token; `SQL_RUNNER_URL` + `SQL_RUNNER_TOKEN`
from `caselaw-coolify/.env.coolify` — never commit either). Launch:

```bash
cd /root && . venv/bin/activate && . .runner_env
HF_TOKEN=$(cat .hf_token) MODE=source_upgrade WORKERS=6 CHECKPOINT_EVERY=1000 \
TMPDIR=/root/work nohup python topup_multilang_fulltexts.py > sweep.log 2>&1 &
```

Then arm the chain (waits for "done. stats:" in sweep.log, then runs
before-snapshot → 60 sync → after-snapshot → diff): `chain_sync.sh`
pattern, writing status to `/root/chain_status.txt`. The chain can just
as well run from the Mac afterwards — the sync only needs the runner URL,
the token, and the HF parquet.

Gotchas that have actually bitten:

- **pkill self-match**: a remote `pkill -f topup_multilang` where the ssh
  command text itself contains the plain script name kills your own
  session. Use a bracketed pattern (`pkill -f 'topup[_]multilang'`) or
  separate kill and relaunch into different ssh invocations.
- **HTTP 400 from the runner is usually a Postgres statement timeout**
  (`QueryCanceled`), not a transport problem. Shrink the chunk and
  bisect; 60 already does this for the is_stub recompute (400-case
  chunks, split on failure).
- Runner limits that shape code: 30s statement timeout, 1,000-row result
  cap (keyset-paginate), 10k-row UPDATE/DELETE cap (403), 1MB SQL text
  but `params` exempt (60MB bodies fine), DROP/TRUNCATE blocked, one
  transaction per request. Full table in DB_ACCESS.md.
- Take the before-snapshot before the first write. Always. Skipping it
  once cost 19,803 summaries.

## After the sweep: DB sync and verification

`migration/sql/60_sync_cjeu_texts_via_runner.py` diffs the corpus parquet
against the DB through the runner and does two things:

- **Inserts** missing (case_id, language, source) triples
  (`INSERT … ON CONFLICT DO NOTHING`, ~250 rows / 6MB batches).
- **Upgrades in place**: when the parquet row for a (case, language) pair
  supersedes a stale DB row of a different CJEU source (the InfoCuria
  wrong-documents), it UPDATEs that row rather than inserting a sibling —
  keeping the row id and summary, and keeping the canonical view from
  serving the stale text (the D12 source-preference order ranks INFOCURIA
  above CELLAR_ITEM, so a stale InfoCuria row would otherwise win).
  RECHTSPRAAK rows are never touched.

Then it recomputes `is_stub` (fulltext shorter than 40% of the case's
median CJEU-rendition length, median ≥ 10k chars, RECHTSPRAAK excluded)
in chunked windowed UPDATEs. `RECOMPUTE_ONLY=1` skips the parquet phase.

Bracket the sync with `61_snapshot_cjeu_text_stats.py` (before + after)
and diff. The snapshot separates **control metrics that must not move**
(total cases, CJEU cases, citation rows, RECHTSPRAAK text rows, HUDOC
text rows) from expected movement (text rows by source, language-coverage
histogram). If a control moves, stop and investigate before anything
else.

Verification targets for this campaign:

- `migration/verify/kamil_2026-09_en_gaps.tsv` — the 604 reported cases
  (case number, ECLI, CELEX). Every one should now have an English
  fulltext row whose text looks like a judgment (starts with
  JUDGMENT/Judgment, not "OPINION OF ADVOCATE GENERAL"). Check corpus and
  DB.
- Residual list: InfoCuria rows the sweep could not replace (CELLAR has
  no manifestation in that language). These keep their InfoCuria text by
  design. Count them, sample a few, and report — this is Kamil's "~33
  genuinely unavailable" tail.

## cellar-extractor repo state

- Upstream PR #14 is open:
  <https://github.com/maastrichtlawtech/cellar-extractor/pull/14>.
- The immutable fixed revision is
  `2898f3123305d29069654a418f6b6691a4bfbf97` on the fork. It adds the
  public manifestation API, canonicalizes CELEX at that boundary, and
  makes canonical CELLAR text replace overlapping InfoCuria text.
- `cjeu-migration` and the Airflow handoff pin this exact revision until
  an upstream release containing PR #14 is available. Do not use PyPI
  `2.0.2` or the old moving `dev` branch for a rerun.
- Package verification: 140 tests passed, 53 opt-in integration tests
  skipped.

## 2026-09-15 correction: the first sweep was not sufficient

The first acceptance check incorrectly treated any English `CELLAR_ITEM`
row as success. The exact 604-case audit showed:

- 294 base-CELEX English judgments fixed;
- 310 English rows still backed by derived works: 282 `_SUM`, 27 `_RES`,
  and 1 `_INF`;
- every residual came from `__source_window=topup_v2_multilang`.

Root cause: the earlier top-up populated the language slot from a derived
work. Later top-ups skipped it because English appeared covered, and the
first `source_upgrade` indexed only `INFOCURIA_BLOB_HTML` rows. The generic
SQL sync also keyed equality on `(case, language, source)`, so it could not
notice corrected content within an existing `CELLAR_ITEM` row.

The replacement must therefore be corpus-wide, not a Kamil-only patch:

1. Install the fixed extractor revision above on Vast.ai.
2. Run `MODE=source_upgrade` without `TARGET_ECLIS_TSV` or
   `TARGET_LANGUAGES`. The mode now indexes both InfoCuria rows and all
   CELLAR rows whose CELEX ends in `_SUM`, `_RES`, or `_INF`, then refetches
   the canonical base work for every affected language.
3. Archive replaced rows and checkpoint to HF as usual.
4. Verify the full corpus contains no derived-work fulltext rows, then run
   the exact Kamil 604-case verifier. Required result: 604 pass, 0 residual.
5. Run a content-aware production sync so corrected same-source
   `CELLAR_ITEM` bodies are updated, recompute all CJEU `is_stub` flags, and
   bracket with control snapshots.

The original DB sync was stopped after it had applied 37,404 source
replacements and while it was recomputing `is_stub`. Production is an
intermediate state until the corrected full rerun and final snapshot pass.

Airflow has a separate implementation/deployment handoff at
`case-law-explorer/docs/etl/CELLAR_JUDGMENT_FIX_HANDOFF.md`. Its historical
rerun must use `force_refresh: true`; otherwise month-scoped artifacts from
the old package are silently reused.

## Standing/recurring work

- **Monthly**: re-run `migration/sql/59_supplement_cjeu_citations.py`
  against the Coolify DB. CELLAR curates citation edges with a lag;
  recent judgments fill in over time. (KNOWN_ISSUES #1.)
- **Corpus refresh**: the corpus covers decisions up to the 2026-05-28
  extraction. Cases decided since need a scrape pass at some point.
- **Runner hygiene**: `SQL_RUNNER_ENABLED=false` (and ideally rotate the
  token) outside maintenance windows; writes additionally gated by
  `SQL_RUNNER_ALLOW_WRITES`. Tokens live only in the Coolify env editor
  and local `.env.coolify` copies.
- **Kamil correspondence**: he has been told (Sept 11) that the rebuild
  runs on a dedicated server for 12–15h and updates HF automatically.
  Owe him a short confirmation once the sweep + sync are verified, with
  the residual-tail explanation.
