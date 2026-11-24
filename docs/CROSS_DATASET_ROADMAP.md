# Cross-dataset linking roadmap

Last updated 2026-11-24.

The Rechtspraak (RS), CJEU and ECtHR collections are each well linked
internally, but the links between them, the links to legislation outside
Dutch law, and the links to everything else courts cite (parliamentary
papers, AG opinions, literature) are mostly missing or wrong. This roadmap
collects the measured gaps and the planned work across the four repositories
involved, in the order we intend to do it.

Tracking issues: case-law-explorer
[#85](https://github.com/maastrichtlawtech/case-law-explorer/issues/85)
(cross-court links), [#84](https://github.com/maastrichtlawtech/case-law-explorer/issues/84)
(CJEU law references), [#83](https://github.com/maastrichtlawtech/case-law-explorer/issues/83)
(CJEU outcome), [#69](https://github.com/maastrichtlawtech/case-law-explorer/issues/69)
(ECtHR edge fan-out); citations-api
[#55](https://github.com/MaastrichtU-BISS/citations-api/issues/55) and
[#24](https://github.com/MaastrichtU-BISS/citations-api/issues/24).

## Ground rules

1. **Schema changes are additive.** New tables, new nullable columns, new
   indexes, new rows in lookup tables. No column is dropped, renamed or
   retyped, and no existing constraint is tightened. Migrations live in
   caselaw-coolify `db/migrations/` (next number: `0008`).
2. **API changes are backwards compatible.** New fields are optional
   additions to existing responses; new behaviour goes behind new endpoints
   or new query parameters. Existing clients (caselaw-vue, node-legal-docs-client)
   keep working unchanged.
3. **Data changes are insert-only by default.** Every new kind of link gets its
   own `source_dataset` and an `extractor_version`, and is written with
   `ON CONFLICT ... DO NOTHING`, so a run can be repeated safely and removed
   with one `DELETE ... WHERE source_dataset = ...`. Updates or deletes of
   existing rows are listed separately below, each with its own safety gate,
   and need the owner's go-ahead.
4. **One code path for new data and backfill.** Extractors live in
   case-law-explorer and read from the database (`case_text`, `cjeu_document`,
   ...), so the weekly DAGs and the historical backfill run the same code.
   Weekly runs write to db-dev and reach production through the promoter.
   The one-off historical backfill runs from cjeu-migration
   (`migration/sql/65_...` onwards) against db-dev first, is verified there,
   then is promoted or replayed into production.
5. **Every link carries provenance**: source dataset, extractor version and
   date, match method, confidence, and where possible the paragraph it was
   found in (see R1.3).
6. **Measured before claimed.** Each extractor ships with a precision sample
   (at least 20 links checked by hand against the text) and is scored on the
   gold set (R0.3).

## Repositories

| Repository | Role in this roadmap |
|---|---|
| [case-law-explorer](https://github.com/maastrichtlawtech/case-law-explorer) | ETL: extractors, loaders, weekly DAGs, promotion client |
| [caselaw-coolify](https://github.com/MaastrichtU-BISS/caselaw-coolify) | Additive schema migrations, count triggers, deploy |
| cjeu-migration (this repo) | One-off historical backfills and verification scripts, run against db-dev, then production |
| [citations-api](https://github.com/MaastrichtU-BISS/citations-api) | Serving the new links, relation types, provenance and identifiers, backwards compatibly |

## Measured state (2026-11-24)

Numbers come from read-only queries against production `cle_v2`; "est." means
extrapolated from a sample.

**Hand-checked gold set** (the E-01 answer key: Tjebbes C-221/17 and
X, Y and Z C-199/12 to C-201/12, 20 links): 4 exist as edges. Of the 16
missing, 11 would be added by R2, 1 needs R2.6 plus R2.4 (an AG opinion citing
F. v. UK by application number), and 4 are not citations in either direction
(Ramadan, K2 and Usmanov for Tjebbes; HR:2022:637, a second Hoge Raad ruling
that only refers back to HR:2020:593). See open decision D1.

**Cross-court citations**

| Gap | Cause | Est. new links | Precision (hand-checked) |
|---|---|---|---|
| RS text → CJEU by case number ("C-199/12", "zaak C-221/17") | No text extractor since the July 2026 migration; the legacy one stopped in 2021 | 13,000–19,000 | 42/42 |
| RS text → CJEU by short ECLI ("EU:C:2023:236") and all ECLIs in cases loaded after July 2026 | `rs_body_cite` exists only from the migration | ~3,500 plus every new case | ~100% |
| RS text → ECtHR by application number | Same | ~16,000 | 24/24 |
| ECtHR text → CJEU | ECtHR edges come only from HUDOC's `scl` field, which lists Strasbourg cases only | ~1,950 | 19/22 (the 3 misses are fixed by preferring an ECLI in the same citation) |
| ECtHR text → RS | Same | ~300 | 5/5 |
| CJEU text → ECtHR | Not extracted | ~2,900 by ECLI, plus application-number and name citations | 20/20 |
| Referring and follow-up Dutch decisions (Cellar `national_judgement_xml`, lines `*A9*`/`*P1*`) | Field stored, never parsed | ~630 | 20/20 |
| AG opinion ↔ judgment | `cjeu_ag_opinion.parent_case_id` filled for 51 of 11,467 | ~11,000 | 20/20 |
| Cellar `subject_to_appeal` / `referred_for_preliminary_ruling` / `reexamined_by` | Raw URIs and dossier UUIDs never resolved | ~3,370 | 20/20 |
| Cellar `joins` | Members of joined cases have no document of their own | ~6,700 aliases (better as identifiers, R1.2) | — |
| `rs_lido_sqlite` links to CJEU/ECtHR flagged internal | `is_cross_jurisdiction` never set for that dataset | 7,541 rows mislabelled | — |

**Legislation and treaties.** The `legislation` table holds only the 265k
Dutch BWB acts, so every reference to EU or Council of Europe law is
unresolved, whichever dataset it comes from.

| Dataset | Law references | Resolved | Unresolved, by kind (est.) |
|---|---|---|---|
| RS | ≈ 8.0M | ≈ 7.25M | ~770k Dutch free-text references not parsed to an act; ~41k ECHR Convention; ~20k other treaties; ~2.5k EU law (a large undercount, LiDO links mostly Dutch law); 85k `rs_lido_sqlite` rows with a valid BWB id that still don't match |
| CJEU | ≈ 308k (74k law references + 234k stored as unresolved citations) | 0 | EU legislation ~128k, treaties ~128k, preparatory acts ~5k, international agreements ~3k |
| ECtHR | 929k Convention articles in `echr_document_article` | not linked to `legislation` | EU Charter, EU law and national law in the text are not extracted |

**Other cited authorities** (not cases, not legislation): AG opinions,
courts of other EU countries (3,912 unresolved Cellar rows), EFTA Court (697),
EU preparatory acts and Commission decisions (~19k), Dutch parliamentary papers
and policy rules (not extracted), UN treaty bodies and Council of Europe
reports (not extracted), legal literature and case annotations (stored in
`rs_document_publication` and Cellar's referral data, not shown).

**Existing data that is wrong**

- ECtHR edges (#69): about 245,000 of 428,581 `echr_edge` rows (57%) are
  confidently wrong, from application numbers matched to every case that
  shares them and from decisions picked where judgments were cited.
- `case_citation_counts`: 43,141 of 44,313 CJEU cases have `cites_count`
  exactly double the real number; the trigger counts inverse `cited_by` rows
  and formal relations as citations.
- 24,358 `rs_formal_relation / replaced_by` rows marked cross-court are
  identity aliases (`ECLI:NL:XX` copies of foreign judgments), not citations;
  414 self-loops.

**API** (#55): every collection endpoint filters edges to its own
`source_dataset`, and all cross-court edges come from RS loaders, so CJEU and
ECtHR pages never show them. The RS endpoint omits `rs_lido_sqlite`
(68k rows, 7.5k cross-court). `/api/combined/expand` drops `relation_type`.

## Phase 0: in flight

| ID | Item | Repo | Status |
|---|---|---|---|
| R0.1 | Keep Rechtspraak requests under 10/s (limiter, sequential metadata, no fake headers) | case-law-explorer [#86](https://github.com/maastrichtlawtech/case-law-explorer/pull/86) | PR open |
| R0.2 | Get the Airflow IP unblocked at data.rechtspraak.nl, then re-run the 2, 9 and 16 Nov weeks one at a time | ops | Email sent to kennissystemen@rechtspraak.nl on 2026-11-24 |
| R0.3 | Gold set as a regression fixture: the 20 E-01 links with the expected source, target and relation, scored after every extractor change | case-law-explorer tests + cjeu-migration `migration/verify/` | To do |
| R0.4 | Find out why lido.db answers 0 of ~840 ECLIs a day in the weekly RS run (every metadata row falls back to the live API) | case-law-explorer, rechtspraak-lido-sqlite [#1](https://github.com/maastrichtlawtech/rechtspraak-lido-sqlite/pull/1) | To investigate |
| R0.5 | Merge #80, #81, #82 (LiDO download restart, base-file fix, promotion unblock) | case-law-explorer | Waiting for review |

## Phase 1: foundations (schema, identity, provenance)

These are prerequisites for most of what follows.

**R1.1 Additive schema for links and references** (caselaw-coolify `0008`).

- `case_citation`: new nullable columns `match_method` (`ecli`, `case_number`,
  `appno`, `celex`, `metadata`, `formal_relation`, ...), `confidence` (real),
  `context_char_start`/`context_char_end` (offsets in the source text),
  `target_ref_raw` (the reference as written, for case numbers and application
  numbers that are not ECLIs). `context_segment_id` already exists.
- New relation types, recorded in a lookup or documented enum:
  `preliminary_reference` (national referral → CJEU ruling),
  `national_follow_up` (national decision after the ruling → ruling),
  `opinion_of` (AG opinion → judgment), `appeal_of` (CJEU appeal → General
  Court decision), `earlier_instance`.

**R1.2 One identity per case** (caselaw-coolify `0008`, filled by R2/R3).

- New table `case_identifier (case_id, scheme, value, value_normalized,
  source, is_primary)` for ECLI, LJN, CELEX, CJEU case numbers (every member
  of joined cases, from Cellar `joins`), ECtHR application numbers, national
  case numbers (zaaknummers), and duplicate ECLIs (e.g. `RVS:2012:BW3076` =
  `RVS:2012:4057`, English versions).
- Normalisation rules shared by all extractors: hyphen variants and spaces
  (`C‑199/12`, `C 199/12`), leading zeros (`C-04/19`), suffixes (`P`, `PPU`,
  `R`), Dutch zaaknummer variants (`201012342/1/T1/V2` vs `201012342/T1/V2`).
- Feeds both link resolution and search by any identifier (R4.1).

**R1.3 Paragraph provenance.** Extractors record the match offsets and, where
`case_segment` covers the text, the `context_segment_id`. Backfills fill it
for the rows they create.

**R1.4 Correct citation counts** (caselaw-coolify). Change the count trigger
to skip inverse `cited_by` rows, `is_about_concept` and identity relations,
count distinct citing cases, and rebuild `case_citation_counts`; fix the
direction of `case_cited_by`. Recomputing derived counts is not a change to
source data, but it changes numbers users see, so it ships with a before/after
report.

## Phase 2: cross-court citations

Each item is a case-law-explorer module run in the weekly DAG for new cases
and from cjeu-migration for history. Insert-only, own `source_dataset`.

| ID | Item | `source_dataset` / relation | Depends on |
|---|---|---|---|
| R2.1 | RS text extractor: full and short ECLIs (`ECLI:` optional), CJEU case numbers (judgments only, dated before the RS decision, skip a number with an EU ECLI within 80 characters), ECtHR application numbers (owner `appno` only, prefer judgments, "(dec.)" selects decisions, choose by cited date/year/[GC]) | `rs_text_ecli`, `rs_text_cjeu_casenum`, `rs_text_echr_appno` / `cites` | R1.1, R1.2 |
| R2.2 | ECtHR text extractor: CJEU and General Court case numbers (incl. "C 199/12", joined cases and ranges → lead case, ECLI in the same citation wins, AG opinion only when "Opinion"/"conclusions" is written), Dutch ECLIs and LJNs | `echr_text_cjeu`, `echr_text_rs` / `cites` | R1.1, R1.2 |
| R2.3 | Cellar referral data: parse `national_judgement_xml` (`A9` referring, `A7`/`A8` earlier instances, `P1`... follow-ups, `I` interim) and match on court, date and zaaknummer, with the HR/PHR and duplicate-publication rules | `cellar_national_judgement` / `preliminary_reference`, `national_follow_up`, `earlier_instance` | R1.2 |
| R2.4 | AG opinion → judgment: CELEX `CC`→`CJ`/`CO`, fallback to the first judgment with the same case number dated on or after the opinion; optionally fill `cjeu_ag_opinion.parent_case_id` where null | `cjeu_ag_link` / `opinion_of` | — |
| R2.5 | Resolve Cellar URI/UUID relations: decode, strip suffixes, map dossier UUIDs via SPARQL `cdm:dossier_identifier` (cached), match case numbers | `cellar_relation_resolved` / `appeal_of`, `preliminary_reference`, `reexamined_by` | R1.2 |
| R2.6 | CJEU text extractor (judgments and AG opinions, all languages): ECtHR ECLIs, application numbers, and "F. v. the United Kingdom"-style name + date citations where unambiguous | `cjeu_text_echr` / `cites` | R1.2 |
| R2.7 | Set `is_cross_jurisdiction` correctly on every new row from the target's `sources`; fix `row_processors/postgres.py` for `rs_lido_sqlite` going forward | — | — |
| R2.8 | Restart RS citation extraction in the weekly pipeline (formal relations with their `psi:type`, and the text extractor from R2.1), fixing the `"\n"` vs `"; "` split that stores labels such as "Eerste aanleg: ..." in `target_ecli_raw` | `rs_formal_relation` (new rows) | R2.1 |

Expected total: roughly 40,000–55,000 new cross-court links plus ~11,000
opinion links and ~3,400 resolved Cellar relations. Gold-set target: 16 of 16
citation links found (the 4 non-citations depend on D1).

## Phase 3: legislation and treaties

| ID | Item | Repo |
|---|---|---|
| R3.1 | Add EU and Council of Europe acts to `legislation` as new rows: EU legislation by CELEX and ELI, the treaties (TEU, TFEU, EC/EEC, Merger Treaty, accession acts), the Charter, the ECHR and its Protocols, the main UN conventions; `jurisdiction` already has `EU` and `CE` | case-law-explorer loader, cjeu-migration backfill |
| R3.2 | Resolve the 234k Cellar legislation/treaty targets now stored as unresolved `case_citation` rows into `case_law_reference` rows (new rows; the existing citation rows stay) and the 74k Cellar law references (#84) | case-law-explorer, cjeu-migration |
| R3.3 | Extract EU-law references from RS texts ourselves ("Richtlijn 2011/95/EU", "artikel 267 VWEU", "Verordening (EU) 2016/679", "Handvest"), with article and paragraph where given | case-law-explorer |
| R3.4 | Extract EU-law, Charter and national-law references from ECtHR texts | case-law-explorer |
| R3.5 | Fix the 85k `rs_lido_sqlite` references whose valid BWB id doesn't match an act (format/version mismatch) | case-law-explorer |
| R3.6 | Resolve the ~770k Dutch free-text references ("artikel 8:54 Awb") through `legislation_alias` (302k aliases) and a provision parser | case-law-explorer |
| R3.7 | Article level everywhere: map `echr_document_article` onto `legal_provision` rows of the ECHR act (new `case_law_reference` rows with `source_dataset = echr_article`), so one provision shows cases from all three courts | case-law-explorer, cjeu-migration |

## Phase 4: other cited authorities

New additive tables in caselaw-coolify: `authority_document (id, kind,
identifier, title, issuer, date, url, ...)` and `case_authority_reference
(case_id, authority_document_id, raw_reference, pinpoint, source_dataset,
extractor_version, context_segment_id, ...)`. Kinds and sources:

| ID | Kind | Cited by | Source |
|---|---|---|---|
| R4.1 | Dutch parliamentary papers (Kamerstukken, Handelingen, explanatory memoranda) | RS | RS texts ("Kamerstukken II 2015/16, 34 356, nr. 3, p. 12"); link to officielebekendmakingen.nl |
| R4.2 | Dutch policy rules and circulars (Vreemdelingencirculaire, tax decrees, beleidsregels) | RS | RS texts and the unparsed LiDO references |
| R4.3 | EU preparatory acts and Commission decisions (COM documents, state aid) | CJEU | Cellar sector 5 and decision targets |
| R4.4 | Courts of other EU countries and the EFTA Court | CJEU | Cellar sector 8 and `E` targets (store as authority documents until those courts are collections) |
| R4.5 | UN treaty bodies, Council of Europe bodies (Venice Commission, CPT, GRECO, ECRI) | ECtHR, RS | Texts |
| R4.6 | Legal literature and case annotations (who annotated or published a judgment) | RS, CJEU | `rs_document_publication` (1.86M rows), Cellar referral data literature lines |

## Phase 5: case context

| ID | Item | Repo |
|---|---|---|
| R5.1 | Outcomes: CJEU (#83, parse `type_procedure` into `procedure_result`), ECtHR violation/no violation per article from HUDOC's conclusion field, RS dictum (cassation dismissed/quashed, appeal upheld/dismissed); new table `case_outcome` | case-law-explorer |
| R5.2 | Status: pending, struck out, referred to the Grand Chamber, judgment final, overturned on appeal (e.g. M.E. v. Sweden was struck out by the Grand Chamber in 2015); new nullable `status` fields or a `case_status` table | case-law-explorer |
| R5.3 | Procedural timeline from typed relations: referral → ruling → follow-up, first instance → appeal → cassation, opinion → judgment, General Court → appeal | citations-api (data from R2) |
| R5.4 | Treatment of cited cases (follows, applies, distinguishes, departs from) classified per citation paragraph with a model, stored with model version and confidence | case-law-explorer (later) |
| R5.5 | Entities: country of origin in asylum cases, parties' roles, named doctrines | case-law-explorer (later) |
| R5.6 | Citation trends per year and authority scores, after R1.4 | citations-api (later) |

## Phase 6: repair existing wrong data

These are the only steps that change or remove existing rows. Each needs the
owner's go-ahead after its gate has passed.

**R6.1 Rebuild the ECtHR edges (#69).** Agreed in principle (2026-11-24) on
the condition that it is completely safe and correct.

1. Build the corrected edges as a new dataset `echr_edge_v2` alongside the old
   one, with one target per `scl` reference: only documents that own the
   application number; "(dec.)"/"(déc.)" selects a decision, otherwise a
   judgment (decision only if no judgment exists); several judgments are
   resolved by exact date, then year, then [GC], then principal judgment over
   just-satisfaction/revision/interpretation; no targets dated after the
   source; no communicated cases.
2. Gate: on a stratified random sample of at least 300 rows that v2 drops,
   every dropped row is checked against the source's `scl` text, and the
   sample must show no row that v2 drops wrongly; a sample of rows v2 keeps
   must show the same precision as the hand-checked rule (≥ 98%). Report
   per-source out-degree before and after, and the effect on network metrics.
3. Switch the API to read `echr_edge_v2` (configuration only, R7.1) and watch
   for a cycle, with the old rows still in place.
4. Copy the old rows to `cle_v2.case_citation_archive` (new table), then
   delete them from `case_citation`. Restoring is one `INSERT ... SELECT`.

**R6.2** Mark the 24,358 identity `replaced_by` rows and 414 self-loops as
non-citations: exclude them in counts and graphs (R1.4, R7) rather than
deleting them.

**R6.3** Correct `is_cross_jurisdiction` on the 7,541 existing `rs_lido_sqlite`
rows (an update; alternatively derive it from `sources` in the API and leave
the rows unchanged).

## Phase 7: citations-api (backwards compatible)

| ID | Item |
|---|---|
| R7.1 | Incoming edges across collections (#55): on in-steps, accept rows from any dataset whose target is in the collection; stop walks at the collection boundary; hydrate foreign nodes with their `dataset`. Dataset lists become configuration (needed for R6.1 step 3). |
| R7.2 | Include `rs_lido_sqlite` in the RS edge datasets; exclude identity relations from graphs and counts |
| R7.3 | Return `relation_type`, `source_dataset`, `match_method`, `confidence` and the context paragraph as new optional fields on edges (`/api/combined/expand` first) |
| R7.4 | Search by any identifier via `case_identifier` (R1.2), including name + case number queries that return nothing today |
| R7.5 | New endpoints for law references across collections (by act and article), other authorities (R4), outcomes and status (R5), and the procedural timeline (R5.3) |
| R7.6 | Tests: cross-collection fixtures in `tests/test_citation_graph.py`, and an integration check that EU:C:2019:189 lists RVS:2020:423 and HR:2020:593 as citing cases and that the gold set scores as expected |
| R7.7 | Re-test #24 on the deployed version and close it if the Postgres expand fixed it |

## Backfill procedure

1. Implement and test the extractor in case-law-explorer (unit tests,
   precision sample, gold set).
2. Add the matching script in cjeu-migration `migration/sql/` that pages
   through the source rows by id range and calls the same extraction code,
   inserting with `ON CONFLICT DO NOTHING`. Batch inserts so the count trigger
   (or the R1.4 rebuild) keeps up.
3. Run it against db-dev; compare counts per `source_dataset`, run the
   precision and gold-set checks on db-dev.
4. Promote or replay into production with the owner's go-ahead, showing the
   expected row counts first.
5. Record each run (dataset, extractor version, rows inserted) in
   `migration/verify/`.

The text scans need no external compute: the texts are already in the database
(99% of ECtHR judgments, all CJEU documents, every RS judgment that has
published text), and regular-expression scans over ~1.1M texts are light. We
do not fetch from data.rechtspraak.nl from other IPs to get around its rate
limit or block.

## Open decisions

- **D1.** The 4 gold-set links that are not citations (Ramadan, K2 and Usmanov
  for Tjebbes; HR:2022:637): add an editorial "related case" layer, keyed by
  hand with its own provenance, or treat them as errors in the answer key?
- **D2.** For decisions published twice under different ECLIs, link to all
  of them or pick a canonical ECLI (R1.2 makes both findable either way)?
- **D3.** Should citations of an AG opinion also count toward its judgment
  (derived rows), or only be reachable through `opinion_of`?
- **D4.** Should references inside a quoted national judgment (in ECtHR texts)
  count as `cites` or get their own relation type?
- **D5.** Name-only citations (no number or ECLI): include with lower
  confidence, or leave out?
