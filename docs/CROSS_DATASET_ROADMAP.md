# Cross-dataset linking roadmap

Last updated 2026-11-24 (after independent review; status below).

## Status (2026-11-24, evening)

- Phase 1 design: [LINKING_DESIGN.md](LINKING_DESIGN.md) v2, after two independent reviews (design, then the SQL). Migrations are `0009`/`0010` (two `0008` files already exist), on caselaw-coolify `feature/cross-dataset-link-schema`, tested on a fresh build; not deployed.
- Shared linking code (identifiers, JSONL link artifact, insert-only link loader, resolver): case-law-explorer `feature/cross-dataset-linking`.
- In progress on branches stacked on it: R1.2a (`case_identifier`), R2.1, R2.2 + R2.6, R2.3–R2.5.
- Phase 2 data stays in db-dev until a count/API guard and R7.1 are deployed with the owner's go-ahead (design, "Order of work").
- No new pull requests while the open-PR cap is reached.

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
X, Y and Z C-199/12 to C-201/12, 20 links): 4 exist as edges. 17 of the 20
are citations. Of the 13 missing citations, 11 would be added by R2, 1 needs
R2.6 plus R2.4 (an AG opinion citing F. v. UK by application number), and 1
(HR:2022:637, an art. 81 RO ruling) cites C-221/17 and EU:C:2019:189 only in
its `case_text.summary`, so the extractors must scan summaries too. The other
3 (Ramadan, K2 and Usmanov for Tjebbes) are not citations in either direction.
See decision D1.

**Cross-court citations**

| Gap | Cause | Est. new links | Precision (hand-checked) |
|---|---|---|---|
| RS text → CJEU by case number ("C-199/12", "zaak C-221/17") | No text extractor since the July 2026 migration; the legacy one stopped in 2021 | 13,000–19,000 (independent sample: up to ~19,900) | 42/42; independent sample 24/25 (one court typo, C-178/98 for C-178/94) |
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
Dutch BWB acts (treaties in the BWB, such as the EVRM, included), so references
to EU law and to Council of Europe instruments outside the BWB are
unresolved, whichever dataset they come from.

| Dataset | Law references | Resolved | Unresolved |
|---|---|---|---|
| RS | ≈ 8.12M | ≈ 8.04M (786,746 of them only through `provision_id → legal_provision.legislation_id`; `legislation_id` on the row is empty) | 81,796 `rs_lido_sqlite` rows: 81,776 have a BWB id that matches an act exactly, but the loader never sets `legislation_id` (`row_processors/postgres.py`); ~100 others. EU law: LiDO links mostly Dutch law, and EU references in the judgment text ("Richtlijn 2011/95/EU", "artikel 267 VWEU") are not extracted at all |
| CJEU | ≈ 318k: 74k law references + 243,706 stored as unresolved citations | 0 | Unresolved citations by CELEX sector: legislation 127,681; treaties 106,308; preparatory acts 5,423; international agreements 2,828; other 1,466 |
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
- `case_citation_counts`: 43,148 of 44,414 CJEU cases have `cites_count`
  exactly double their outgoing rows, and CJEU `cited_by_count` is about
  double too; RS and ECtHR counts are close to exact. Cause: the counts were
  computed (`41_counts`, 06:02) before `90_post_load` deduplicated
  `case_citation` and installed the trigger (08:59), and the trigger then
  counts every row, including inverse `cited_by` rows and formal relations.
- 26,462 `rs_formal_relation / replaced_by` rows marked cross-court are
  identity aliases (`ECLI:NL:XX` copies of foreign judgments), not citations;
  419 self-loops.

**API** (#55): every collection endpoint filters edges to its own
`source_dataset`, and all cross-court edges come from RS loaders, so CJEU and
ECtHR pages never show them. The RS endpoint omits `rs_lido_sqlite`
(68k rows, 7.5k cross-court). `/api/combined/expand` drops `relation_type`.

## Phase 0: in flight

| ID | Item | Repo | Status |
|---|---|---|---|
| R0.1 | Keep Rechtspraak requests under 10/s (limiter, sequential metadata, no fake headers, circuit breaker after 25 consecutive failures, no repeated live calls for rows already fetched from the API) | case-law-explorer [#86](https://github.com/maastrichtlawtech/case-law-explorer/pull/86) | PR open |
| R0.2 | Get the Airflow IP unblocked at data.rechtspraak.nl, then re-run the 2, 9 and 16 Nov weeks one at a time | ops | Email sent to kennissystemen@rechtspraak.nl on 2026-11-24 |
| R0.3 | Evaluation set as a regression fixture: the 17 gold citation links, the 3 non-citations and 63 adversarial/negative entries grounded in the research files (83 entries, 54 with exact text excerpts), plus a scorer reporting TP/FP/FN per category. Categories the research files could not ground (court typos without a named source, non-CJEU "C-" codes, shared application numbers, Caribbean court) are listed in its README | case-law-explorer `feature/linking-evaluation-set` (05fcbca) | Built; no PR yet (PR cap) |
| R0.4 | Why lido.db answered 0 of ~840 ECLIs a day: not a lookup bug. Until case-law-explorer 578684b (2 Nov) the build downloaded rls 0.2.0's old export address, which returned an HTML error page that parsed into an empty `metadata` table; the extractor then falls back silently. rls #1 (CELEX-first identifiers) is unrelated. Correction: a working lido.db saves about a third of live requests on main, and less with #86, because judgment text is still fetched live; real savings need deferred or bulk text fetching. Before the catch-up: confirm a `lido_sqlite_build` run has finished since 2 Nov (`COUNT(*)`, `MAX(date_decision)` on the server's lido.db) | case-law-explorer `fix/lido-sqlite-lookup` (3cd2e87, logs lido.db status on every run) | Diagnosed; needs an ops check of the server's lido.db |
| R0.5 | Merge #80, #81, #82 (LiDO download restart, base-file fix, promotion unblock) | case-law-explorer | Waiting for review |
| R0.6 | `get_rechtspraak(eclis=...)` is unsupported in 1.6.0, but the DAG never reaches it (dead `process_failed_eclis` branch); the live task uses the LiDO links service, not data.rechtspraak.nl. Now fails fast before any request; opt-in shared pool `rechtspraak_api` (size 1) for API-calling tasks, enabled by creating the pool and setting `RECHTSPRAAK_API_POOL` | case-law-explorer `fix/update-citation-details-call` (38f1ee0) | Built; no PR yet (PR cap); pool needs an ops action |

## Phase 1: foundations (schema, identity, provenance)

These are prerequisites for most of what follows.

**R1.1 Additive schema for links and references** (caselaw-coolify `0008`).

- `case_citation`: new nullable columns `match_method` (`ecli`, `case_number`,
  `appno`, `celex`, `metadata`, `formal_relation`, `name_date`, ...),
  `confidence` (real), and `target_ref_raw` (the reference as written, for case
  numbers and application numbers that are not ECLIs), with a partial unique
  index on `(source_case_id, target_ref_raw, relation_type, source_dataset)
  WHERE target_case_id IS NULL`, so unresolved rows are idempotent under
  `ON CONFLICT DO NOTHING` too. `context_segment_id` already exists and keeps
  pointing at the first occurrence.
- New child table `case_citation_mention (citation_id, segment_id,
  char_start, char_end, raw_text, section, treatment_label, ...)`: one row per
  occurrence, because `case_citation_uk_resolved` allows one row per
  (source, target, relation, dataset) and a judgment often cites the same case
  in several paragraphs. Paragraph provenance (R1.3) and treatment
  classification (R5.4) live here.
- New relation types, recorded in a lookup or documented enum:
  `preliminary_reference` (national referral → CJEU ruling),
  `national_follow_up` (national decision after the ruling → ruling),
  `opinion_of` (AG opinion → judgment), `appeal_of` (CJEU appeal → General
  Court decision), `earlier_instance`.

**R1.2 One identity per case** (caselaw-coolify `0008`). R1.2a fills it from
data already in the database (ECLIs, CELEX, `cjeu_document.case_number`,
Cellar `joins` members, `echr_document_appno` owner rows, `rs_document`
zaaknummers and `replaces_identifier` LJNs) *before* Phase 2 starts, so the
extractors resolve against it rather than depending on each other.

- New table `case_identifier (case_id, scheme, value, value_normalized,
  source, is_primary)` for ECLI, LJN, CELEX, CJEU case numbers (every member
  of joined cases, from Cellar `joins`), ECtHR application numbers, national
  case numbers (zaaknummers), and duplicate ECLIs (e.g. `RVS:2012:BW3076` =
  `RVS:2012:4057`, English versions).
- Normalisation rules shared by all extractors: hyphen variants and spaces
  (`C‑199/12`, `C 199/12`), leading zeros (`C-04/19`), suffixes (`P`, `PPU`,
  `R`), Dutch zaaknummer variants (`201012342/1/T1/V2` vs `201012342/T1/V2`).
- Feeds both link resolution and search by any identifier (R4.1).

**R1.3 Paragraph provenance.** Extractors write a `case_citation_mention` row
per occurrence with its offsets and, where `case_segment` covers the text, the
segment. Backfills fill it for the rows they create.

**R1.4 Correct citation counts** (caselaw-coolify). A row-level trigger cannot
keep "distinct citing cases" correct once the same pair exists under several
`source_dataset`s: it races under concurrent writes and contends on landmark
targets during backfills. Instead, refresh `case_citation_counts` set-based
for the touched `case_id`s after each load or backfill batch (and once in
full), excluding inverse `cited_by` rows, `is_about_concept` and identity
relations, and counting distinct citing cases. Replace the per-row increments
of `trg_case_citation_counts` accordingly, and check the direction of the
`case_cited_by` view the API reads (it is not in `0001`, so find where it was
created). Recomputing derived counts is not a change to source data, but it
changes numbers users see, so it ships with a before/after report.

**R1.5 Natural keys across environments.** db-dev and production have
different surrogate ids. Backfill replay and promotion join on natural keys
(`ecli`, `celex_id`, `item_id`, application number) and never copy `case_id`
values between databases.

## Phase 2: cross-court citations

Each item is a case-law-explorer module run in the weekly DAG for new cases
and from cjeu-migration for history. Insert-only, own `source_dataset`.

| ID | Item | `source_dataset` / relation | Depends on |
|---|---|---|---|
| R2.1 | RS text extractor over `fulltext` **and `summary`** (art. 81 RO rulings often cite only in the summary): full and short ECLIs (`ECLI:` optional); CJEU case numbers, dated on or before the RS decision, preferring judgments (CJ/TJ/FJ) and falling back to orders (CO/TO/FO; 4,864 case numbers have only orders), AG opinions only with "conclusie"/"Opinion" cues, several candidates ranked by type and date rather than taken arbitrarily; a case number is skipped only when an EU ECLI within 80 characters resolves to the *same* case number (a blind distance rule drops real citations, e.g. RBGEL:2026:1195, where C-684/16 sits 65 characters from the ECLI of C-619/16); ECtHR application numbers (owner `appno` only, prefer judgments, "(dec.)" selects decisions, choose by cited date/year/[GC]) | `rs_text_ecli`, `rs_text_cjeu_casenum`, `rs_text_echr_appno` / `cites` | R1.1, R1.2a |
| R2.2 | ECtHR text extractor: CJEU and General Court case numbers (incl. "C 199/12", joined cases and ranges → lead case, ECLI in the same citation wins, AG opinion only when "Opinion"/"conclusions" is written), Dutch ECLIs and LJNs | `echr_text_cjeu`, `echr_text_rs` / `cites` | R1.1, R1.2 |
| R2.3 | Cellar referral data: parse `cjeu_document.national_judgement_xml` (`A9` referring, `A7`/`A8` earlier instances, `P1`... follow-ups, `I` interim) and match on court, date and zaaknummer, with the HR/PHR and duplicate-publication rules; filter foreign (e.g. Belgian) courts on the court prefix of each line only, not on the whole line | `cellar_national_judgement` / `preliminary_reference`, `national_follow_up`, `earlier_instance` | R1.2 |
| R2.4 | AG opinion → judgment: CELEX `CC`→`CJ`/`CO`, fallback to the first judgment with the same case number dated on or after the opinion; optionally fill `cjeu_ag_opinion.parent_case_id` where null | `cjeu_ag_link` / `opinion_of` | — |
| R2.5 | Resolve Cellar URI/UUID relations: decode, strip suffixes, map dossier UUIDs via SPARQL `cdm:dossier_identifier` (cached), match case numbers | `cellar_relation_resolved` / `appeal_of`, `preliminary_reference`, `reexamined_by` | R1.2 |
| R2.6 | CJEU text extractor (judgments and AG opinions, all languages): ECtHR ECLIs, application numbers, and "F. v. the United Kingdom"-style name + date citations where unambiguous | `cjeu_text_echr` / `cites` | R1.2 |
| R2.7 | Set `is_cross_jurisdiction` correctly on every new row from the target's `sources`; fix `row_processors/postgres.py` for `rs_lido_sqlite` going forward | — | — |
| R2.8 | Restart RS citation extraction in the weekly pipeline (formal relations with their `psi:type`, and the text extractor from R2.1), fixing the `"\n"` vs `"; "` split that stores labels such as "Eerste aanleg: ..." in `target_ecli_raw` | `rs_formal_relation` (new rows) | R2.1 |
| R2.9 | Late-binding resolution: when new cases are loaded, resolve existing rows with `target_case_id IS NULL` whose `target_ecli_raw`, `target_celex_raw` or `target_ref_raw` now matches `case_identifier` (as new resolved rows; the unresolved row stays). Without it, a reference to a case loaded later (a pending preliminary ruling, a new ECtHR judgment) stays unresolved forever | per source dataset | R1.1, R1.2a |

Expected total: roughly 40,000–55,000 new cross-court links plus ~11,000
opinion links and ~3,400 resolved Cellar relations. Gold-set target: 17 of 17
citation links found (the 3 thematic links depend on D1), and no false
positives on the adversarial pairs of R0.3.

## Phase 3: legislation and treaties

| ID | Item | Repo |
|---|---|---|
| R3.1 | Add EU and Council of Europe acts to `legislation` as new rows: EU legislation by CELEX and ELI, the treaties (TEU, TFEU, EC/EEC, Merger Treaty, accession acts), the Charter, the ECHR and its Protocols, the main UN conventions; `jurisdiction` already has `EU` and `CE` | case-law-explorer loader, cjeu-migration backfill |
| R3.2 | Resolve the ~244k Cellar legislation/treaty targets now stored as unresolved `case_citation` rows into `case_law_reference` rows (new rows), and the 74k Cellar law references (#84). The old citation rows stay, so graphs and counts must exclude `case_citation` rows whose target CELEX is not case law (sector 6) | case-law-explorer, cjeu-migration |
| R3.3 | Extract EU-law references from RS texts ourselves ("Richtlijn 2011/95/EU", "artikel 267 VWEU", "Verordening (EU) 2016/679", "Handvest"), with article and paragraph where given | case-law-explorer |
| R3.4 | Extract EU-law, Charter and national-law references from ECtHR texts | case-law-explorer |
| R3.5 | `rs_lido_sqlite` law references: 81,776 of 81,796 have a BWB id that exactly matches `legislation.identifier`, but `PostgresRSProcessor._law_reference_rows` never sets `legislation_id`. Fix the loader for new rows (low effort); resolving the existing rows is an update and sits in Phase 6 (R6.4) | case-law-explorer |
| R3.6 | The 786,846 `rs_lido-both` references with an empty `legislation_id` are already resolved at article level (786,746 through `provision_id → legal_provision.legislation_id`). Check every consumer (API, counts, search) joins through the provision; only ~100 references are truly unresolved | case-law-explorer, citations-api |
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
| R5.1 | Outcomes: CJEU (#83, parse `type_procedure` into `procedure_result`, keeping the raw value untouched), ECtHR violation/no violation per article from HUDOC's conclusion field, RS dictum (cassation dismissed/quashed, appeal upheld/dismissed); new table `case_outcome` | case-law-explorer |
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

0. Prerequisite: R7.1's active-dataset configuration is deployed for *every*
   endpoint, including `/api/combined/expand`, which today has no
   `source_dataset` filter. Otherwise the API would serve old and new edges
   together.
1. Build the corrected edges in a separate staging table
   (`cle_v2.echr_edge_v2_staging`), not in `case_citation`, so the API and the
   count trigger never see old and new edges side by side; one target per `scl`
   reference: only documents that own the
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
3. Review the staged edges through a read-only preview (the gate report and
   API responses computed against the staging table) with the old rows still
   live.
4. Swap in one transaction: copy the old `echr_edge` rows to
   `cle_v2.case_citation_archive` (new table), insert the staged rows into
   `case_citation` as `echr_edge_v2`, delete the old rows, switch the active
   dataset, and refresh the counts for the touched cases. Restoring is the
   reverse `INSERT ... SELECT` from the archive.

**R6.2** Mark the 24,358 identity `replaced_by` rows and 414 self-loops as
non-citations: exclude them in counts and graphs (R1.4, R7) rather than
deleting them.

**R6.3** Correct `is_cross_jurisdiction` on the 7,541 existing `rs_lido_sqlite`
rows (an update; alternatively derive it from `sources` in the API and leave
the rows unchanged).

**R6.4** Set `legislation_id` on the 81,776 existing `rs_lido_sqlite` law
references whose BWB id exactly matches an act (one `UPDATE` of a null
column; gate: every updated row's `raw_resource` equals the act's identifier,
counts before and after).

## Phase 7: citations-api (backwards compatible)

| ID | Item |
|---|---|
| R7.1 | One shared active-dataset configuration (allowlist of `source_dataset`s and relation types) used by *every* endpoint, `/api/combined/expand` included (today it has no dataset filter). Incoming edges across collections (#55): on in-steps, accept rows from active datasets whose target is in the collection; stop walks at the collection boundary; hydrate foreign nodes with their `dataset`. Prerequisite for R6.1. |
| R7.2 | Include `rs_lido_sqlite` in the RS edge datasets; exclude identity relations (`replaced_by` aliases), self-loops and non-case-law Cellar targets from graphs and counts; fix `detect_dataset` in `/api/combined/expand`, which labels any non-ECHR/non-CJEU ECLI (e.g. `ECLI:BE:...`) as RS |
| R7.3 | Return `relation_type`, `source_dataset`, `match_method`, `confidence` and the context paragraphs (`case_citation_mention`) as new optional fields on edges (`/api/combined/expand` first); an opt-in parameter (e.g. `includeOpinionCitations=true`) follows one `opinion_of` hop and labels those edges `via_opinion` (D3) |
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

Each has a recommendation from the review (2026-11-24); none is final until
the owner confirms it.

- **D1.** The 3 gold-set links that are not citations (Ramadan, K2 and Usmanov
  for Tjebbes). HR:2022:637 turned out to be a citation (in its summary) and
  moves into the citation set. *Recommended:* keep the 3 out of citation
  scoring and out of `case_citation`; if wanted, add a separate hand-curated
  "related case" layer with its own provenance.
- **D2.** Decisions published twice under different ECLIs. *Recommended:*
  resolve every edge to one canonical case, with the other ECLIs as
  identifiers (R1.2) so both stay findable; linking to every duplicate inflates
  counts and repeats the fan-out problem of #69.
- **D3.** Citations of an AG opinion and its judgment. *Recommended:* no
  derived `cites` rows (courts often depart from their AG); keep `opinion_of`
  as a typed edge and offer the opt-in traversal in R7.3.
- **D4.** References inside a quoted national judgment in ECtHR texts.
  *Recommended:* their own relation type (e.g. `cites_in_domestic_law`) or a
  section label from `echr_extractor_segments`, excluded from the default
  ECtHR citation counts.
- **D5.** Name-only citations. *Recommended:* leave out bare names; allow
  name plus exact decision date (`match_method = 'name_date'`) only after it
  reaches at least 95% precision on a 100-row stratified sample, excluded from
  strict counts until then.

## Review log

- **2026-11-24, independent review** (separate model, read-only access to
  production and code). Corrections applied above: HR:2022:637 is a citation
  (in its summary); the RS legislation gap is 81,796 rows the loader never
  resolves, not ~770k unparsed references (those resolve through their
  provision); the doubled CJEU counts come from computing counts before
  deduplication; corrected counts for `replaced_by` aliases, self-loops and
  Cellar sectors. Design changes: separate staging table and a single
  transaction for R6.1, behind an active-dataset configuration that covers
  `/api/combined/expand`; set-based count refresh instead of a distinct-checking
  trigger; a per-occurrence mention table; a partial unique index for
  unresolved rows; filling `case_identifier` before Phase 2; natural keys
  across environments; summary scanning, the same-case-number rule for nearby
  ECLIs and fallback to orders in R2.1; late-binding resolution (R2.9); an
  adversarial evaluation set (R0.3); lido.db before the catch-up (R0.4).
  PR #86 gained a circuit breaker and stops repeating live calls.
