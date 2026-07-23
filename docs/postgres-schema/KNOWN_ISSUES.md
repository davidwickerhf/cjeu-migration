# Known issues — CJEU corpus and cle_v2

Open issues with confirmed root causes and planned fixes. Accepted
limitations (things we chose not to change) live in
[DATA_QUALITY.md](DATA_QUALITY.md); this file tracks work that is still
owed. Last updated 2026-07-23.

## 1. Missing citation edges for recent CJEU judgments

**Status: open — source-side lag, top-up pass planned.**

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

Planned fix: `migration/sql/58_supplement_cjeu_citations` — re-probe
CELLAR (`work_cites_work`, both directions) for CJEU cases lacking `cites`
edges, or simply everything decided in the last 18 months, and append new
`case_citation` rows; update the HF corpus columns alongside. Run monthly:
recent judgments fill in as the Publications Office catches up. Until it
exists, an empty citation list for a judgment younger than ~6 months
usually means "not yet curated upstream", not "cites nothing".

## 2. Full-word language codes from `language_procedure`

**Status: open — normalization pass planned, loader fix required.**

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
