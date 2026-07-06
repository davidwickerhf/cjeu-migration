# CLE migration mapping — legacy → unified schema

> Verified against the **live legacy database** (read-only introspection,
> 2026-07-06) and the CJEU HF corpus. Every claim below about value sets,
> null rates, and row counts comes from actual queries, not the dump file.
>
> Targets refer to [schema_full.sql](schema_full.sql) (deployed into the
> `cle_v2` namespace). Legacy tables live in `public` on the same server.

## Legacy inventory (live, 2026-07-06)

| Legacy table | Rows | Size | Disposition |
|---|---:|---:|---|
| `rs_document` | 3,679,855 | 4.7 GB | → `cases` + `rs_document` + `case_text` |
| `rs_document_text` | 933,326 | 24 GB | → `case_text` (language `nl`) |
| `rs_document_law_reference` | 6,665,106 | 3.5 GB | → `case_law_reference` (raw_scheme `bwb`) |
| `rs_document_publication` | 1,860,533 | 513 MB | → `rs_document_publication` |
| `rs_document_formal_relation` | 330,099 | 215 MB | → `rs_document_formal_relation` + fan-out to `case_citation` |
| `rs_document_external_authority` | 6,150 | 5 MB | → `rs_document_external_authority` |
| `rs_edge` | 1,681,390 | 515 MB | → `case_citation` |
| `rs_citation_counts` | 3,677,887 | 638 MB | recomputed by trigger — not copied |
| `rs_law_element` | 887,208 | 326 MB | → `legislation` + `legal_provision` |
| `rs_law_alias` | 302,611 | 202 MB | → `legislation_alias` |
| `echr_document` | 197,011 | 284 MB | → `cases` + `echr_document` (per language) |
| `echr_document_text` | 155,474 | 5.9 GB | → `case_text` |
| `echr_document_appno` | 1,857,206 | 358 MB | → `echr_document_appno` |
| `echr_document_article` | 932,132 | 143 MB | → `echr_document_article` |
| `echr_extractor_segments` | 108,488 | 915 MB | → `echr_extractor_segments` |
| `echr_edge` | 424,721 | 188 MB | → `case_citation` |
| `echr_citation_counts` | 48,413 | 7 MB | recomputed by trigger — not copied |
| `ecli_segments` | 3,650,962 | **32 GB** | → `case_segment` (see §5.4) |
| `ecli_texts` | 137,788 | 1.2 GB | not ported (older subset of rs_document_text) |
| `ecli_keywords` | 48,089 | 25 MB | not ported (KeyBERT output — see §5.5) |
| `ecli_bwb_opschrift` | 649,419 | 235 MB | not ported (superseded by legislation catalog) |
| `case_law` | 10,177,299 | 2.6 GB | → `case_law_reference` where resolvable (see §5.6) |
| `legal_case` | 3,619,979 | 1.2 GB | not ported directly — cross-corpus LIDO ECLI registry (see §5.6) |
| `law_element` / `law_alias` | 887k / 303k | — | staging twins of rs_law_* — not ported |

## 0. Conventions (apply to every corpus)

**IDs.** All surrogate keys are `bigint GENERATED ALWAYS AS IDENTITY` — the
loader must NEVER supply ids (Postgres will reject them). Parent rows are
resolved by natural key: `ecli`, `item_id`, `celex_id`, `(scheme, identifier)`
for legislation.

**Language codes** — lowercase ISO 639-1. Observed HUDOC codes and their
mappings (ISO 639-2/B → 639-1):

| HUDOC | → | HUDOC | → | HUDOC | → |
|---|---|---|---|---|---|
| ENG | en | FRE | fr | TUR | tr |
| RUS | ru | UKR | uk | RUM | ro |
| GER | de | CZE | cs | (30+ more) | 639-2/B table |

The `language` seed covers only the EU 24 — **the loader upserts unseen
languages** (`INSERT … ON CONFLICT DO NOTHING`) before dependent rows.

**Load order.** lookups (`language`, `jurisdiction`, `court`,
`court_formation`, `document_type`, `procedure_type`, `domain`) →
`legislation` / `legal_provision` / `legislation_alias` → `cases` →
corpus extension tables → `case_text` → fan-outs (`case_domain`,
`case_judge`, `case_party`, `case_law_reference`) → `case_citation`
(after ALL corpora are in `cases`, so cross-corpus targets resolve) →
analytics (`case_segment`, …).

---

## 1. Rechtspraak (3.68M cases, monolingual `nl`)

### 1.1 `rs_document` → `cases`

| Legacy column | → New column | Notes |
|---|---|---|
| `ecli` (PK) | `cases.ecli` | natural key |
| — | `cases.item_id` | NULL for RS |
| `'RS'` | `cases.source` | constant |
| `title` | `cases.title` | populated for only 922,844 / 3.68M (25%) — NULL otherwise |
| `date_decision` | `cases.date_decision` | 100% populated |
| `date_published` | `cases.date_published` | |
| `instance` | `cases.court_id` → `court` | ⚠ **legacy `instance` holds 1,230 distinct court NAMES** ("Rechtbank Den Haag", "Raad van State"), not instance levels — it maps to the `court` lookup. Name variants exist ("Rechtbank 's-Gravenhage" = pre-2013 "Rechtbank Den Haag") — see §5.3 |
| `document_type` | `cases.document_type_id` → lookup | only 2 values: `Uitspraak` (3.60M), `Conclusie` (75k) |
| `procedure_type` | `cases.procedure_type_id` → lookup | 43 distinct values, 26% populated |
| `language` (`nl`) | `cases.language_iso` | uniform |
| `zaaknummer` | `cases.case_number` | 98% populated |
| — | `cases.importance` | RS native importance was not found in the live schema — leave NULL (see DECISIONS D7 note) |
| `created_at` / `updated_at` | same | |

### 1.2 `rs_document` → `rs_document` (corpus satellite, keyed by `case_id`)

Direct carry-over: `zaaknummer`, `creator_uri`, `replaces_identifier`,
`vindplaatsen[]`, `zittingsplaats`, `access_rights`, `opendata_status`,
`snapshot_date`, `url_publication`, `predecessor_successor_cases`,
`date_issued`, `date_modified`, `domains[]` (also exploded, §1.4),
`legal_provisions[]` (display cache), `subject_uris[]`.

Legacy `summary` does NOT get a column here — it moves to `case_text`
(§1.3). The `rs_v_document_with_text` view re-exposes it.

### 1.3 `rs_document_text` + `rs_document.summary` → `case_text`

One row per case, `language='nl'`:

| Legacy | → |
|---|---|
| `rs_document_text.fulltext` | `case_text.fulltext` (933,326 rows — 25% of cases have text) |
| `rs_document.summary` | `case_text.summary` (922,668 rows) |
| — | `case_text.source` = `'RECHTSPRAAK'` |
| `fulltext_tsv` | regenerated (GENERATED column) — loader guards >1M-char inputs |

Cases with a summary but no fulltext still get a `case_text` row
(fulltext NULL).

### 1.4 `rs_document.domains[]` → `domain` + `case_domain`

Each array element → `domain` row (scheme=`'rs_domain'`, dedup on name)
+ `case_domain` join row.

### 1.5 `rs_edge` + `rs_document_formal_relation` → `case_citation`

Observed `rs_edge.source` values → `source_dataset`:

| Legacy source | rows | → `source_dataset` | `relation_type` |
|---|---:|---|---|
| `body-cite` | 898,475 | `rs_body_cite` | `'cites'` |
| `legacy-ddb` | 490,521 | `rs_legacy_ddb` | `'cites'` |
| `formal-relation` | ~292k | `rs_formal_relation` | legacy `relation_type`: `conclusie`, `hogerberoep`, `cassatie`, `replaced_by`, `tussenuitspraak`, … |

Column mapping: `source_ecli`/`target_ecli` resolve → `source_case_id` /
`target_case_id`; unresolved targets keep `target_ecli_raw`.
`rs_document_formal_relation` is ALSO ported as its own table (keyed by
`case_id`) because `aanleg` (`eerdereaanleg`/`latereaanleg`) and `gevolg`
(`bekrachtiging/bevestiging` 56k, `(gedeeltelijke) vernietiging en zelf
afgedaan` 28k, `gevolgd` 25k, …) don't fit the shared edge model.

### 1.6 `rs_document_law_reference` → `case_law_reference`

| Legacy | → New |
|---|---|
| `ecli` | `case_id` (resolved via `cases.ecli`) |
| `bwb_resource` | `raw_resource` (+ `raw_scheme='bwb'`) |
| `article` | `raw_subdivision` |
| `bwb_label_id` | `raw_label_id` |
| `opschrift` | `raw_reference` |
| `version_date` | `version_date` |
| `source` (`lido-ref`/`lido-linkt`/`custom`) | `source_dataset` (`rs_lido_ref` / `rs_lido_linkt` / `rs_custom`) |
| — | `role` = `'cited'` |
| generated URL columns | dropped — reconstructed by `rs_v_document_law_reference` view |
| — | `legislation_id`/`provision_id` resolved post-load by joining `legislation(scheme='bwb', identifier=bwb_resource)` and `legal_provision.bwb_label_id` |

### 1.7 `rs_law_element` / `rs_law_alias` → Legislation catalog

| Legacy | → New |
|---|---|
| `rs_law_element` WHERE type=`'wet'` | `legislation` (scheme=`'bwb'`, identifier=`bwb_id`, `title`, `lido_id`, `jc_id`, `snapshot_date`) |
| `rs_law_element` other types (`boek`/`hoofdstuk`/`afdeling`/`artikel`/…) | `legal_provision` (`element_type`=type, `article_label`=number, `title`, `bwb_label_id`, `lido_id`, `jc_id`; `parent_id` reconstructed from the BWB hierarchy) |
| `rs_law_alias` | `legislation_alias` (alias; legislation resolved via bwb_id; source=`'bwbidlist'`) |

### 1.8 Direct carry-overs (re-keyed `ecli` → `case_id`)

`rs_document_publication` (1.86M), `rs_document_external_authority` (6k),
`lido_link` (source/target ECLIs additionally resolved to case ids).

---

## 2. ECHR (197k document-language rows → ~150k cases)

### 2.1 Case identity — the critical subtlety

**Verified: every language variant has its own HUDOC `itemid`**
(197,011 distinct itemids across 197,011 rows). The case identity is the
**ECLI** (150,737 rows have one) — variants sharing an ECLI are the same
case. Live case counts (grouped by ECLI): **81,717 cases** — 62,087 have
an ENG variant (76%), 19,620 are FRE-only (24%, the French-only
Commission era), 10 have neither (8 GER, 1 GEO, 1 TUR).

Loader groups `echr_document` by `ecli`:

- one `cases` row per distinct ECLI (`source='ECHR'`)
- `cases.item_id` = the canonical variant's itemid, selected
  **deterministically**: prefer **ENG**, else **FRE**, else any other
  language; within a language, doctype rank **JUD > DEC > COM > other**,
  tie-broken by lowest itemid. (Rule covers all 81,717 cases.)
- each language variant → one `echr_document` row carrying its own
  `item_id` — which is the table's **primary key**.

⚠ **(case, language) is NOT unique**: 3,261 (ecli, language) pairs carry
multiple variants (6,901 rows — e.g. two admissibility decisions sharing
one ECLI+ENG). That is why `echr_document`'s PK is `item_id`, with
`(case_id, language)` as a plain index — no variant is dropped. The ECHR
satellites (`appno`, `article`, `extractor_segments`) anchor on `item_id`
too, mirroring the legacy key structure.

Rows without ECLI: `PR` (13,979), `CLIN`/`CLINF` (12,964) have **zero**
ECLIs — they are press releases and info notes, not decisions. See §5.1.

### 2.2 `echr_document` → `cases`

| Legacy column | → New | Notes |
|---|---|---|
| `ecli` | `cases.ecli` | grouping key |
| itemid (ENG variant) | `cases.item_id` | canonical |
| `'ECHR'` | `cases.source` | constant |
| `docname` | `cases.title` | from the ENG variant |
| `judgementdate` | `cases.date_decision` | date part |
| `importance` | `cases.importance` | native HUDOC 1–4 (observed: 1→14,191; 2→8,522; 3→29,823; 4→130,496; NULL→13,979 = the PR rows) |
| `doctype` | `cases.document_type_id` | `HEJUD`/`HFJUD`→judgment, `HEDEC`/`HFDEC`→decision, `HECOM`/`HFCOM`→communicated, `HJUDTUR` etc.→judgment (translation) |
| — | `cases.court_id` | constant: ECtHR (+ `originatingbody` detail stays on `echr_document`) |
| — | `cases.language_iso` | procedure language not in HUDOC — NULL or `en` |

### 2.3 `echr_document` → `echr_document` (per case × language)

| Legacy | → New |
|---|---|
| `itemid` | `item_id` (per-variant, UNIQUE) |
| `languageisocode` | `language` (normalized, §0) |
| `extractedappno` | `extractedappno` |
| `docname` | `docname` |
| `doctype` | `doctype` |
| `doctypebranch` | `doctype_branch` |
| `judgementdate` / `referencedate` | `judgement_date` / `reference_date` |
| `article`, `conclusion`, `violation`, `nonviolation` | same names (raw fields kept; normalized form in `echr_document_article`) |
| `respondent` | `respondent` |
| `originatingbody` (int) | `originating_body` |
| `representedby` / `publishedby` / `rulesofcourt` | `represented_by` / `published_by` / `rules_of_court` |
| `applicability`, `separateopinion`, `issue` | `applicability`, `separate_opinion`, `issue` |
| `importance`, `rank`, `scl`, `externalsources` | `importance`, `rank`, `scl`, `external_sources` |
| `judgement_year` | regenerated (GENERATED column) |
| `appno` | not carried — normalized rows live in `echr_document_appno` |

### 2.4 `echr_document_text` → `case_text`

(155,474 rows) keyed (itemid, languageisocode) → (case_id, language).
`fulltext` carries over; `source='HUDOC'`; tsv regenerated.
⚠ If two variants of the same case share a language (3,261 such pairs,
see §2.1), `case_text UNIQUE(case_id, language)` forces a pick — doctype
rank JUD > DEC > COM > other, then lowest itemid. The losing variant's
metadata is still fully present in `echr_document`; only its fulltext is
not the case_text pick (flag count in the load report).

### 2.5 `echr_document_appno` → `echr_document_appno`

(1.86M rows) `(itemid, languageisocode, appno, source)` →
`(item_id, appno, source)` — direct carry-over, language now implied by
the variant.

### 2.6 `echr_document_article` → `echr_document_article`

(932k rows: `applied` 771,748; `violation` 185,208; `nonviolation`
40,740.) Re-keyed to `(item_id, kind, article_code)`; the new `protocol` column is parsed from
`article_code` (`P1-1` → protocol `P1`), `raw` gets the source fragment
when the parser has it (NULL for migrated rows).

### 2.7 `echr_extractor_segments` → `echr_extractor_segments`

(108k rows; parser modes: `commission_decision` 47k, `standard` 29k,
`communicated_case` 15k, `info_note` 11k, `press_release` 7k.) Direct
carry-over, PK `item_id`.

### 2.8 `echr_edge` → `case_citation`

(424,721 edges.) `source_itemid`/`target_itemid` resolve via
`echr_document.item_id` → `case_id` (verified: ~95% of edge itemids are
ENG variants). Mapping: `relation_type='cites'`,
`source_dataset='echr_edge'`, `weight` carries over,
unresolved targets → `target_ecli_raw` (from the edge's `target_ecli`
when present).

---

## 3. CJEU (46,180 cases from the HF corpus — no legacy DB)

Source: [davidwickerhf/cjeu-opendata](https://huggingface.co/datasets/davidwickerhf/cjeu-opendata)
(`cases.parquet` 107 cols; `fulltexts.parquet` 591k rows, up to 24
languages/case). Field-level detail in [CJEU_TABLES.md](CJEU_TABLES.md);
this is the operative summary.

### 3.1 → `cases`

| HF field | → New | Notes |
|---|---|---|
| `ecli` | `ecli` | |
| `celex` (first `;`-token) | `celex_id` | ⚠ verify first-token uniqueness at load; collisions → keep first, log |
| — | `item_id` | CELLAR work id if we want it; else NULL |
| `'CJEU'` | `source` | |
| synthesized | `title` | `"C-123/22, X v Y"` (D6) |
| `date_publication` (first token) | `date_decision` | |
| CELEX court code (`CJ`/`TJ`/`CC`) | `court_id` | CJEU / General Court / CST |
| `language_procedure` | `language_iso` | lowercase |
| CELEX doc code (`CJ`=judgment, `CO`=order, `CC`=opinion…) | `document_type_id` | |
| `judicial_procedure_type` | `procedure_type_id` | |
| formation proxy | `importance` | GC/FC→1, 5-judge→2, 3-judge→3, sole/order→4 (D7) |
| parsed from CELEX | `case_number` | `C-123/22` |

### 3.2 → `cjeu_document`

`sector` ('6'/'8'), `formation_id` (← `delivered_by_court_formation` via
`court_formation` lookup), `proc_type` (← `type_procedure`),
`procedure_result` (parsed), `date_lodged` (← `date_of_request`),
`cellar_uri`, `work_uri`, `journal_refs` (← `references_journals`),
`erecueil_ref` (← `case_law_published_in_erecueil`), `local_identifier`,
`dossier_uri` (← `work_part_of_dossier`), `dossier_parent_case_id`
(resolved post-load), plus denormalized `celex_id`/`ecli`/`case_number`.

### 3.3 → `cjeu_ag_opinion` (Opinion CELEXes, `…CC…`)

`advocate_general`, `opinion_uri` (← `conclusions`), `delivered_date`,
`parent_case_id` (← `opinion_advocate_general_joined_to_case_court`,
CELEX resolved to case).

### 3.4 → `cjeu_national_document` (sector-8 rows only, ~1,800)

The 12 `case_law_national_*` fields, 1:1 (see CJEU_TABLES.md).

### 3.5 → `case_text` (from `fulltexts.parquet`)

One row per (ECLI, language): `text`→`fulltext`, `text_source`→`source`,
`text_format`→`text_format`, `missing_reasons`→`missing_reasons`;
`cases.summary`+`summary_source` land on the procedure-language row.

### 3.6 → fan-outs

| HF field(s) | → | Detail |
|---|---|---|
| `subject_matter` / `eurovoc` / `keywords` / `directory_codes` | `domain` + `case_domain` | schemes `cjeu_subject_matter`, `eurovoc`, `cjeu_keyword`, `cjeu_directory_code` |
| `judge_rapporteur`; `case_law_delivered_by_judge` | `case_judge` | roles `rapporteur` / `judge` |
| `origin_country`, `case_law_national_parties`, agent fields | `party` + `case_party` | roles `referring_state`, `national_party`, `defendant_agent`, `applicant_agent`, `commenting_agent` |
| `citing` + `work_cites_work`, `cited_by` | `case_citation` | `cites`/`cited_by`, `source_dataset='cellar_sparql'`, `target_celex_raw` always set |
| `case_law_joins_case_court` etc. (7 relations) | `case_citation` | `joins`, `subject_to_appeal`, `reexamined_by`, `referred_for_preliminary_ruling`, `is_about_concept`, `interprets_judgement`, `logical_successor_of` |
| 18 CDM legal predicates + `legal_resource`, `based_on_treaty`, `affecting_*` | `case_law_reference` | `raw_scheme='celex'`, roles per predicate (see schema comments) |

---

## 4. What is intentionally NOT ported

| Legacy | Reason |
|---|---|
| `rs_citation_counts`, `echr_citation_counts` | recomputed by the `case_citation_counts` trigger during citation load |
| `ecli_texts` (138k) | older, smaller subset of `rs_document_text` (all `ECLI:NL:`) |
| `ecli_bwb_opschrift` | superseded by the legislation catalog |
| `law_element`, `law_alias` | staging twins of `rs_law_*` |
| `legal_case`, `case_law` | see §5.6 |
| `ecli_keywords` | see §5.5 |

---

## 5. Open loader decisions

**5.1 ECHR non-decision rows.** `PR` / `CLIN` / `CLINF` (26,943 rows, no
ECLI, no importance) are press releases and info notes, not case law.
*Recommendation: skip them* — they'd create ECLI-less `cases` rows with no
citation value. Revisit if the product wants info-note search.

**5.2 ECHR ECLI-less decisions.** After removing PR/CLIN, some COM
(communicated) rows may still lack ECLI. Case identity falls back to
appno+date grouping, or skip until HUDOC assigns an ECLI.
*Recommendation: load them keyed on itemid (cases.item_id) with ecli NULL.*

**5.3 Dutch court-name normalization.** 1,230 distinct names in
`rs_document.instance`, including historical renames ("'s-Gravenhage" →
"Den Haag" 2013). *Recommendation: load verbatim as `court` rows first
(fast, lossless), merge via `parent_court_id`/rename mapping later.*
A curated mapping table is a follow-up task.

**5.4 `ecli_segments` (32 GB, 3.65M rows, vector(768)).** Maps cleanly to
`case_segment` (`ecli`→case_id, `segment`→segment_text, `segment_hash`,
`embedding`; `language='nl'`, `segment_type='legacy'`, index unknown).
*Recommendation: port it* — regenerating 3.6M embeddings costs real money;
porting is a bulk copy.

**5.5 `ecli_keywords` (48k, method=`keybert` only).** Generated analytics,
regenerable. Could map to `domain(scheme='keybert')`+`case_domain` but the
`score` would be lost. *Recommendation: don't port; regenerate when the
keyword pipeline is rebuilt.*

**5.6 `legal_case` + `case_law` (LIDO registry, 3.62M + 10.2M).**
`legal_case` is a cross-corpus ECLI registry (3.49M `ECLI:NL`, 83k
`ECLI:CE` = ECHR, 42.6k `ECLI:EU` = CJEU, plus AT/DE/…; `celex_id` is
100% NULL). `case_law` links it to `law_element` — i.e. Dutch-law
references made by *any* court known to LIDO. Rows whose ECLI exists in
`cases` fold into `case_law_reference` (source_dataset=`'lido_registry'`);
the rest (foreign courts we don't ingest) are dropped or parked.
*Recommendation: fold resolvable rows in a second pass after all three
corpora are loaded; report the unresolvable remainder.*

---

## 6. Edge-case findings from the deep probe (2026-07-06, live DB)

All verified with read-only queries; each has a loader rule.

**6.1 ECHR compound article codes.** `article_code` includes conjunction
forms: `13+3` (5,423), `14+8` (3,421), `13+6-1` (3,241), `14+P1-1` (2,259),
`6+6-3-c` (1,889), plus deep paths (`P1-1-1`, 18,685). Loader rule for the
`protocol` column: set only when the code is a single `P{n}` reference
(`P1-1` → `P1`); compound codes keep `protocol NULL` and stay verbatim in
`article_code` (the API's per-article filters match on the verbatim code).

**6.2 ECHR dates are sparse — parse the ECLI.** Only 91,274 / 197,011
variant rows have `judgementdate`; case-level coverage is 28,978 / 81,717
(35%) even with `referencedate` fallback. But the ECLI encodes the decision
date: `ECLI:CE:ECHR:2020:0206JUD…` → 2020-02-06. Loader rule for
`cases.date_decision`: `judgementdate` → `referencedate` → parse from ECLI
segment. Raises date coverage to ~100% of ECLI-bearing cases.

**6.3 ECHR multi-respondent strings.** `respondent` holds `;`-joined lists
(`MDA;RUS` ×219, `BIH;HRV;MKD;SRB;SVN` ×46, …). The legacy API's
`respondent = ANY(…)` equality silently misses these. Loader rule: explode
respondent into `party` (country-typed) + `case_party`
(role=`'respondent_state'`); keep the raw string on
`echr_document.respondent` for display. Fixes the API bug.

**6.4 ECLI hygiene.** 8 RS ECLIs are not uppercase-normalized. Loader rule:
`trim()` + canonical ECLI casing before any resolution or insert; log
collisions post-normalization.

**6.5 BWB references to unknown laws.** 5,734 distinct `bwb_resource`
values in `rs_document_law_reference` have no `wet` row in
`rs_law_element`. Loader rule: create stub `legislation` rows
(scheme=`'bwb'`, identifier, title NULL) so `/api/links/laws`-style counts
still group correctly; stubs get titles if/when the BWB catalog refreshes.

**6.6 Unresolvable RS edge targets.** 169,610 `rs_edge` targets are not in
`rs_document` — but 161,922 of them are cross-corpus (`ECLI:CE:` 50,920 →
ECHR, `ECLI:EU:` 111,002 → CJEU) and RESOLVE once all three corpora are
loaded (`is_cross_jurisdiction=true`). Only ~7,700 NL targets remain
genuinely unresolved → `target_ecli_raw`. Load citations LAST (§0).

**6.7 Depublicated cases.** 46 rows with `opendata_status='depublicated'`.
Loader rule: load them (flag intact) — the API filters on the column.

**6.8 Sanity results that need no action.** No orphan ECHR edges (0 of
424,721), no orphan text rows, no ECLI spanning both JUD and DEC doctypes,
no future dates; 30 pre-1900 RS dates are plausible historical records.
