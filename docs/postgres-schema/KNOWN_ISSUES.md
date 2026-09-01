# Known issues — CJEU corpus and cle_v2

Open issues with confirmed root causes and planned fixes. Accepted
limitations (things we chose not to change) live in
[DATA_QUALITY.md](DATA_QUALITY.md); this file tracks work that is still
owed. Last updated 2026-08-19.

## 1. Missing citation edges for recent CJEU judgments

**Status: mitigated 2026-07-23 — `59_supplement_cjeu_citations.py` built and
run against the Coolify DB (2,109 recent CELEXes probed, 3,637 new rows;
2026 coverage went from 274 to 314 of 601 cases). The lag itself is
upstream and permanent: re-run the script monthly.**

Reported 2026-07-23 against the deployed Coolify stack:
`ECLI:EU:C:2026:297` (CELEX `62024CJ0519`, decided 2026-04-16) has full
metadata and 24-language texts but zero rows in `case_citation` — no
outgoing, no incoming, no raw mentions. Across CJEU cases decided in 2026,
274 of 601 have at least one citation row.

Diagnosis (verified live against the CELLAR SPARQL endpoint, 2026-07-23):
this is not an extraction or loading bug. CELLAR itself has no
`cdm:work_cites_work` relations for this CELEX — the only relation that
exists today is one incoming reference from `52026SC0917`, a Commission
staff working document published after our extraction. The Publications
Office curates citation relations for judgments with a lag of weeks to
months. The partial 2026 coverage decomposes cleanly by relation type:

| 2026 CJEU edges by type | rows | available |
|---|---:|---|
| `cites` (work_cites_work) | 1,053 | curated with a lag; present only for early-2026 cases at the 2026-05-28 extraction |
| `is_about_concept` | 758 | at publication |
| `joins`, `subject_to_appeal`, other procedural | 130 | at publication |

The 2026 cases that "have edges" mostly have procedural relations only
(for example `ECLI:EU:C:2026:431`: 7 edges, all `joins`); their
`work_cites_work` count in CELLAR is zero, the same as the reported case.

Fix: `migration/sql/59_supplement_cjeu_citations.py` — re-probe
CELLAR (`work_cites_work`, both directions) for CJEU cases lacking `cites`
edges, or simply everything decided in the last 18 months, and append new
`case_citation` rows; update the HF corpus columns alongside. Run monthly:
recent judgments fill in as the Publications Office catches up. Until it
exists, an empty citation list for a judgment younger than ~6 months
usually means "not yet curated upstream", not "cites nothing".

## 2. Full-word language codes from `language_procedure`

**Status: fixed 2026-07-23 — `58_normalize_language_codes.sql` applied to
the Coolify DB (37,952 cases re-coded, 19,803 duplicate rows merged, 11,682
re-keyed, 24 lookup rows pruned; verified zero full-word codes remain).
The loader now maps names to ISO (`norm_lang` in 50_load_cjeu.py) and no
longer truncates summaries at semicolons (`whole`). Incident note: the
first application ran the delete before the summary carry-over (a timeout
mid-sequence was not treated as fatal), losing 19,803 stranded summaries;
they were rebuilt from cases.parquet the same day — final state 35,520
CJEU cases with a summary, more than before the incident because the
rebuild also undid the historic semicolon truncation.**

Surfaced by the same report (a case listing both `hu` and `hungarian`).
The CJEU loader lowercased `language_procedure` verbatim — but that field
holds words ("English", "Hungarian"), not ISO codes. Verified damage in
`cle_v2` (2026-07-23):

- `cases.language_iso`: 37,952 CJEU cases carry a full-word value
  (`english` 8,104, `french` 7,813, `german` 7,571, …).
- `case_text`: 31,485 summary-only rows keyed on a full-word language
  (created by the loader's summary pass, which targets "the
  procedure-language row"). 19,803 of them sit next to an ISO-coded
  sibling that should have received the summary instead.
- `language` lookup: 24 bogus full-word rows (`english` … `irish`)
  upserted alongside the real ISO codes.

Fix, in order:

1. Map full word to ISO in `cases.language_iso` (static 24-entry mapping).
2. `case_text` full-word rows: where an ISO sibling exists for the same
   (case, source), move the summary onto the sibling if it has none, then
   drop the full-word row; where no sibling exists, re-key the row's
   `language` to the ISO code.
3. Delete the 24 full-word `language` rows once nothing references them.
4. Fix `50_load_cjeu.py` to translate `language_procedure` through the
   mapping at load time so the pattern cannot recur.
5. Replay the same statements on the Coolify database (see issue 3).

## 4. Missing language versions from multi-work CELEXes

**Status: resolved. Final state verified 2026-08-24: all nine reported
cases carry their full language sets in the production DB (the ninth,
ECLI:EU:C:2021:4, at 22 languages with English after the suffixed-CELEX
sweep). Totals across the campaign: +60,660 language rows in the corpus
(652,160 fulltext rows) and +60,422 in the database; cases at 20+
fulltext languages 17,326 → 20,637; controls unchanged throughout.
History of the two root causes below.**

Original interim status 2026-08-19: The corpus-wide re-run with the
multi-work union fix added 55,302 language rows to the HF corpus and
55,064 to the database (controls verified unchanged; cases at 20+
fulltext languages: 17,326 → 20,339). Eight of the nine reported cases
verified fixed. The ninth exposed a second, related bug: the top-up
probed the raw first CELEX token, which for 3,642 corpus cases is a
suffixed variant (`_SUM`/`_INF`) whose work family carries a partial
language set — ECLI:EU:C:2021:4 was looked up as `62020CJ0414_SUM`.
Token normalization fixed (matches the extractor's `_normalize_celex`);
a follow-up sweep over those cases is running, with the DB sync and
before/after sanity report chained behind it.**

Reported 2026-07-30: in a random sample of 100 preliminary-ruling
judgments, nine lacked their English text in the database while EUR-Lex
has it (e.g. ECLI:EU:C:2012:265 with 4 of 24 languages,
ECLI:EU:C:1997:369 with 1 of 11).

Root cause (verified live): a CELEX can map to several CELLAR work
records — partial editions and re-publications share the identifier — and
language coverage differs per work. Verified for three of the nine: each
has exactly two works, one sparse and one full (2 vs 22 languages for
62006CJ0005). The extractor's work-URI lookup used
`order by asc(str(?doc)) limit 1`, an arbitrary alphabetical pick over
UUIDs, so whenever the sparse work sorted first, every downstream fetch
(v2 extraction, the July top-up passes) honestly reported "at CELLAR max"
for the wrong work. Fetch errors were also logged at debug level, so the
top-up's "0 failures" was not evidence of completeness.

Fix shipped: `_fetch_sector8_items_for_celex` unions manifestation
candidates across all works per CELEX (extractor PR #10, verified live:
2→22, 1→11, 1→22 languages on reported cases, the 11-language set matching
EUR-Lex exactly); the top-up script now uses it and logs per-ECLI fetch
failures as warnings.

Remaining: a corpus-wide `MIN_LANGS=24 YEAR_THRESHOLD=0` re-run with the
fixed extractor (per-case cost roughly doubles — expect ~12-15 h on a
6-worker box), then propagating the new rows into the Coolify database
(the sql-runner transport or a one-shot ETL container in the compose
network — 57's direct-psycopg path only worked while Neon was reachable).

## 3. Fix propagation: Coolify runs a restored copy

**Status: standing operational note.**

The production Coolify database was restored from Neon `cle_v2` at cutover.
Fixes and top-ups applied to Neon after that point (issues 1 and 2 above,
future text or citation supplements) do not reach production by themselves.
Policy: land every fix in Neon first, verify there, then replay the same
script against the Coolify DB — all supplement/normalization scripts are
idempotent, so replaying is safe. A full re-dump/re-restore also works but
is heavier than replaying the deltas.

## Non-issues investigated alongside

- `cjeu_document.cellar_uri` is NULL for the reported case — and for every
  case: it is a documented loader gap (MIGRATION_MAPPING §7.2, derivable
  from CELEX), not a per-case indicator of missing URI discovery.
- The `/api/cjeu` edge-walking path, auth plumbing, and `cited_by`
  complement flipping all behave as designed; verified against
  citation-heavy cases (C-131/12: 96 nodes / 102 edges).
