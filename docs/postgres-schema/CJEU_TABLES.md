# CJEU corpus → Postgres schema proposal

> **Status:** draft for review — David & colleague, June 2026
> **Source dataset:** [davidwickerhf/cjeu-opendata](https://huggingface.co/datasets/davidwickerhf/cjeu-opendata)
> **Scope:** 46,180 cases × 107 columns (after extractor v2 + multi-language top-up) + 546,733 (post-topup ~1M) fulltext rows.

This document proposes how the CJEU corpus maps into the shared `cle_v2`
Postgres schema. The shared layer (`case`, `case_text`, `case_citation`,
`case_law_reference`, `case_judge`, etc.) stays generic across Rechtspraak /
ECHR / CJEU; CJEU-specific structure lives in `cjeu_*` tables.

The migration runs **in parallel** to the existing tables — nothing is touched
until we're confident the new shape is stable.

---

## TL;DR — what changes from the current ERD

**Existing tables we use as-is (no change needed):**
`court`, `language`, `country`, `document_type`, `procedure_type`, `instance`,
`judge`, `party`, `legislation`, `provision`, `dataset_source`.

**Existing tables we touch (minor additions):**

| Table | Change |
|---|---|
| `case` | Fix `court_id` from `int PK` (typo?) to `int FK` → `court(id)` |
| `case_text` | Add `UNIQUE(case_id, language)`, add `text_format`, `missing_reasons`, `summary_source`. Per-language row stays the model. |
| `case_citation` | Add `extractor_version text`, `confidence real NULL`. Expand allowed `relation_type` values (list below). |
| `case_law_reference` | Make `provision_id` nullable (CJEU often cites whole acts). Expand `role` enum to cover the 30+ CDM predicates (list below). |

**New CJEU-specific tables (5):**

1. `cjeu_document` — exists in current ERD, expanded
2. `cjeu_ag_opinion` — exists in current ERD, expanded
3. `cjeu_national_document` — **NEW**, for sector-8 (national court) records
4. `cjeu_classification` — **NEW**, single table covering all 4 of CJEU's parallel tagging schemes (subject_matter / eurovoc / keyword / directory_code)
5. `court_formation` — **NEW** lookup, ~20 values (Grand Chamber, 1st Chamber, etc.)

---

## ER diagram

```mermaid
erDiagram
    case ||--o{ case_text : "1-N langs"
    case ||--o{ case_judge : ""
    case ||--o{ case_party : ""
    case ||--o{ case_citation : "source"
    case ||--o{ case_law_reference : ""

    case ||--o| cjeu_document : "CJEU only"
    case ||--o| cjeu_national_document : "sector-8 only"
    case ||--o| cjeu_ag_opinion : "Opinion CELEXes only"
    case ||--o{ cjeu_classification : "1-N tags"

    cjeu_document }o--|| court_formation : ""
    cjeu_document }o--o| case : "dossier_parent_case_id"

    cjeu_ag_opinion }o--|| case : "parent_case_id"

    case {
        bigserial id PK
        text ecli UK
        text celex_id
        text source "CJEU/RS/ECHR"
        int court_id FK
        int instance_id FK
        int language_iso FK
        int document_type_id FK
        int procedure_type_id FK
        date date_decision
        date date_published
        text case_number
        bool is_landmark
    }

    case_text {
        bigserial id PK
        bigint case_id FK
        text language
        text fulltext
        text summary
        text summary_source
        text text_source
        text text_format
        text missing_reasons
    }

    case_citation {
        bigserial id PK
        bigint source_case_id FK
        bigint target_case_id FK
        text target_celex "if not yet resolved"
        text relation_type
        text source_dataset
        text extractor_version
        real confidence
    }

    case_law_reference {
        bigserial id PK
        bigint case_id FK
        bigint legislation_id FK
        bigint provision_id FK "nullable"
        text role
        text raw_reference
    }

    cjeu_document {
        bigint case_id PK_FK
        text celex_id
        text sector "6 or 8"
        int formation_id FK
        date date_lodged
        text cellar_uri
        text work_uri
        text journal_refs
        text erecueil_ref
        text local_identifier
        text procedure_result
        text dossier_uri
        bigint dossier_parent_case_id FK "nullable"
    }

    cjeu_ag_opinion {
        bigint case_id PK_FK
        bigint parent_case_id FK "the judgment this opinion is for"
        text advocate_general
        text opinion_uri
        date date_delivered
    }

    cjeu_national_document {
        bigint case_id PK_FK
        text national_court_uri
        text national_decision_internal_id
        text national_parties_raw
        text national_keywords
        text national_reference_publication
        text national_reference_publication_conclusion
        text national_follow_up
        text national_judgement_reference
        text national_act_reference_national
        text national_act_reference_international
        text national_act_reference_european
    }

    cjeu_classification {
        bigserial id PK
        bigint case_id FK
        text scheme "subject_matter | eurovoc | keyword | directory_code"
        text code
        text label
        bigint parent_id FK "nullable, for hierarchies"
    }

    court_formation {
        serial id PK
        text code UK
        text label
        int judge_count
    }
```

---

## Per-table specification

### `case` (shared, existing — minor fix)

| Column | Source HF column | Notes |
|---|---|---|
| `ecli` | `ecli` | UK |
| `celex_id` | `celex` (first `;`-token) | |
| `source` | constant `'CJEU'` | |
| `court_id` | derived from CELEX (`CJ`→CJEU, `TJ`→General Court, `CC`→CST) | FK, NOT PK as currently in the ERD |
| `instance_id` | derived from court | |
| `language_iso` | `language_procedure` | first token |
| `document_type_id` | derived from CELEX procedure code (`CJ`=judgment, `CO`=order, `CC`=opinion, `CD`=decision, `CV`=ruling) | |
| `procedure_type_id` | `judicial_procedure_type` | drop `type_procedure` (redundant human-readable mirror) |
| `date_decision` | `date_publication` (first `;`-token) | The CELLAR "date of judgment". |
| `date_published` | nullable; use `references_journals` parse if needed | |
| `case_number` | parsed from CELEX (`C-123/22`) | |
| `is_landmark` | NULL initially; future flag | |

**Fields explicitly NOT on `case`** (per "cases table stays general"): subject_matter,
eurovoc, keywords, directory_codes, advocate_general, judge_rapporteur, formation,
sector, dossier — all go to CJEU-specific tables below.

---

### `case_text` (shared, existing — add cols)

One row per **(case_id, language)** pair. Captures both fulltext and summary
per-language.

| Column | Source HF column | Notes |
|---|---|---|
| `case_id` | FK → `case.id` | |
| `language` | `fulltexts.text_language` | upper-case ISO-639-1 (EN, FR, …) |
| `fulltext` | `fulltexts.text` | nullable if upstream-missing |
| `summary` | `cases.summary` | one per language; sparse — only EN/FR for older cases |
| `summary_source` | `cases.summary_source` | provenance |
| `text_source` | `fulltexts.text_source` | `INFOCURIA_BLOB_HTML` / `CELLAR_ITEM` / `EXTRACTOR_FALLBACK_TEXT` |
| `text_format` | `fulltexts.text_format` | `xhtml` / `html` / `pdf` / `fmx4` |
| `missing_reasons` | `fulltexts.missing_reasons` | e.g. `FULLTEXT_UNAVAILABLE_UPSTREAM` |

Constraints:
```sql
UNIQUE (case_id, language)
```

---

### `case_citation` (shared, existing — add provenance, expand relation_type)

Each `;`-separated CELEX in any of the source HF citation columns becomes one row.

**`relation_type` values used for CJEU:**

| relation_type | Source HF column | Notes |
|---|---|---|
| `cites` | `citing` / `work_cites_work` | merged; CELEX `;`-list exploded |
| `cited_by` | `cited_by` | inverse of `cites` (denormalized for query convenience) |
| `joins` | `case_law_joins_case_court` | procedurally joined cases (4.9%) |
| `subject_to_appeal` | `case_law_subject_to_appeal_in_case_court` | appeal chains (8.0%) |
| `reexamined_by` | `case_law_reexamined_by_case_court` | Art. 256(2) re-exam (0.0%) |
| `referred_for_preliminary_ruling` | `case_law_referred_to_for_preliminary_ruling_case_law` | (0.1%) |
| `is_about_concept` | `case_law_is_about_concept_case_law` + `case_law_is_about_concept_new_case_law` | merged; "this case is about the doctrine established by X" (27.9% / 41.5%) |
| `interprets_judgement` | `case_law_interpretes_judgement_resource_legal` | when the target is another case-law work, not legislation |
| `logical_successor_of` | `work_is_logical_successor_of_work` | rare |

**Added columns (vs current ERD):**
```sql
extractor_version text NOT NULL DEFAULT 'cellar-extractor-1.5.0',
confidence real NULL  -- ML-extracted citations populate this (Rechtspraak); SPARQL-clean (CJEU) leaves NULL
```

`source_dataset` already exists ✓ — populated as `'cellar_sparql'` for CJEU.

---

### `case_law_reference` (shared, existing — minor changes)

CJEU often cites whole acts, not specific provisions → `provision_id` must be nullable.

**`role` values needed for CJEU** (CDM predicates → role string):

| role | Source HF column |
|---|---|
| `based_on_treaty` | `based_on_treaty` |
| `legal_basis` | `legal_resource` |
| `affects` | `affecting_string` / `affecting_ids` |
| `amends` | `case_law_amends_resource_legal` |
| `amends_by_correction` | `case_law_amends_by_correction_resource_legal` |
| `confirms` | `case_law_confirms_resource_legal` |
| `interprets` | (default for non-specific) |
| `interprets_judgement` | `case_law_interpretes_judgement_resource_legal` (when target is legislation) |
| `declares_void` | `case_law_declares_void_resource_legal` |
| `declares_void_by_preliminary_ruling` | `case_law_declares_void_by_preliminary_ruling_resource_legal` |
| `incidentally_declares_void` | `case_law_incidentally_declares_void_resource_legal` |
| `declares_valid` | `case_law_declares_valid_resource_legal` |
| `declares_incidentally_valid` | `case_law_declares_incidentally_valid_resource_legal` |
| `states_failure` | `case_law_states_failure_concerning_resource_legal` |
| `suspends_application` | `case_law_suspends_application_of_resource_legal` |
| `immediately_enforces` | `case_law_immediately_enforces_resource_legal` |
| `incorporates` | `resource_legal_incorporates_resource_legal` |
| `corrects` | `resource_legal_corrects_resource_legal` |

Stored as a postgres enum, or a text column with a `CHECK` constraint —
your colleague's call.

---

### `cjeu_document` (CJEU-specific, existing — expand)

| Column | Source HF column | Notes |
|---|---|---|
| `case_id` | FK → `case.id` (PK) | one-to-one with case |
| `celex_id` | `celex` (first token) | |
| `ecli` | `ecli` | mirrored from case for query convenience |
| `sector` | `sector` | single char: `'6'` (Court of Justice) or `'8'` (national CJEU-referred) |
| `formation_id` | `delivered_by_court_formation` → lookup | FK → `court_formation` |
| `date_lodged` | `date_of_request` | when the case was lodged at the court |
| `cellar_uri` | derived | |
| `work_uri` | derived | |
| `journal_refs` | `references_journals` | "OJ C 123/45" |
| `erecueil_ref` | `case_law_published_in_erecueil` | ECR / European Court Reports citation |
| `local_identifier` | `local_identifier` | rare, case-internal ID |
| `procedure_result` | derived from `type_procedure` | parsed out: `'successful'`/`'unfounded'`/`'inadmissible'` |
| `dossier_uri` | `work_part_of_dossier` | 18.8% populated; groups Opinion + Judgment + Order |
| `dossier_parent_case_id` | nullable; resolved post-load | FK → `case.id` of the dossier's "main" judgment, if known |

---

### `cjeu_ag_opinion` (CJEU-specific, existing — expand)

For Opinion CELEXes (`...CC...`). One row per Opinion case_id.

| Column | Source HF column | Notes |
|---|---|---|
| `case_id` | FK → `case.id` (PK) | the Opinion's own case row |
| `parent_case_id` | `opinion_advocate_general_joined_to_case_court` → resolve CELEX→case_id | FK → `case.id` of the judgment this opinion was given for |
| `advocate_general` | `advocate_general` | drop `case_law_delivered_by_advocate_general` (redundant) |
| `opinion_uri` | `conclusions` | URI of opinion document |
| `date_delivered` | `date_publication` | |

---

### `cjeu_national_document` (CJEU-specific — NEW)

Only for sector-8 cases (~1,800 of 46,180 = 3.9%). Splitting these out keeps
`cjeu_document` shaped for actual CJEU judgments.

| Column | Source HF column | Notes |
|---|---|---|
| `case_id` | FK → `case.id` (PK) | |
| `national_court_uri` | `case_law_delivered_by_court_national` | URI of the national court that ruled |
| `national_decision_internal_id` | `case_law_national_decision_internal_identifier` | internal ID at the national court |
| `national_parties_raw` | `case_law_national_parties` | original-language party names |
| `national_keywords` | `case_law_national_keywords` | national-court keywords (different from CJEU's) |
| `national_reference_publication` | `case_law_national_reference_publication` | where the national decision was published |
| `national_reference_publication_conclusion` | `case_law_national_reference_publication_conclusion` | |
| `national_follow_up` | `case_law_national_follow_up` | further national-court action |
| `national_judgement_reference` | `case_law_national_judgement_reference` | |
| `national_act_reference_national` | `case_law_national_act_reference_national` | national legislation cited |
| `national_act_reference_international` | `case_law_national_act_reference_international` | international law cited |
| `national_act_reference_european` | `case_law_national_act_reference_european` | EU law cited |

---

### `cjeu_classification` (CJEU-specific — NEW)

Single table covers CJEU's four parallel tagging schemes. Each `;`-separated
token in the source becomes one row.

| Column | Notes |
|---|---|
| `id` | bigserial PK |
| `case_id` | FK → `case.id` |
| `scheme` | enum: `'subject_matter' \| 'eurovoc' \| 'keyword' \| 'directory_code'` |
| `code` | the token (e.g. `'Competition'`, `'human-rights'`, `'62-CASE_LAW'`) |
| `label` | optional human-readable label (lookup populated separately if needed) |
| `parent_id` | nullable; for hierarchical schemes (Eurovoc, directory codes) |

Source mapping:
- `subject_matter` (CJEU's curated topic taxonomy) → scheme = `'subject_matter'`
- `eurovoc` (EU thesaurus terms) → scheme = `'eurovoc'`
- `keywords` (CJEU's free-text keywords) → scheme = `'keyword'`
- `directory_codes` (Lex-EUR hierarchy codes) → scheme = `'directory_code'`

Constraints:
```sql
UNIQUE (case_id, scheme, code)
```

**Why a single table** vs four separate tables: queries like "all cases tagged
with `eurovoc:human_rights` AND `subject_matter:Competition`" are clean
intersections on one table. Adding a new scheme later is one INSERT, not a
schema migration.

---

### `court_formation` (CJEU-specific — NEW lookup)

| Column | Notes |
|---|---|
| `id` | serial PK |
| `code` | `'GC'`, `'1C'`, `'2C'`, …, `'FC'`, `'PR'`, `'SOLE'` |
| `label` | `'Grand Chamber'`, `'First Chamber'`, …, `'Full Court'`, `'President sitting alone'`, `'Single judge'` |
| `judge_count` | 15 (GC), 5 (chamber), 3 (small chamber), 1 (sole) |

Seed data: ~15-20 rows total (CJ + GC chambers).

---

## Field-by-field disposition (all 107 cases.parquet columns)

### Mapped (kept somewhere)

| HF column | → Destination |
|---|---|
| `ecli` | `case.ecli`, `cjeu_document.ecli` |
| `celex` | `case.celex_id`, `cjeu_document.celex_id` |
| `sector` | `cjeu_document.sector` |
| `date_publication` | `case.date_decision` |
| `date_of_request` | `cjeu_document.date_lodged` |
| `judicial_procedure_type` | `case.procedure_type_id` → lookup |
| `type_procedure` | parsed into `cjeu_document.procedure_result`; raw value dropped |
| `language_procedure` | `case.language_iso` |
| `delivered_by_court_formation` | `cjeu_document.formation_id` → `court_formation` lookup |
| `judge_rapporteur` | `case_judge` row, role=`'rapporteur'` |
| `case_law_delivered_by_judge` | `case_judge` rows, role=`'judge'` |
| `advocate_general` | `cjeu_ag_opinion.advocate_general` (Opinion CELEXes only) |
| `case_law_delivered_by_advocate_general` | redundant — drop in favour of `advocate_general` |
| `case_law_defended_by_agent` | `case_party` rows, role=`'defendant_agent'` |
| `case_law_requested_by_agent` | `case_party` rows, role=`'applicant_agent'` |
| `commented_by_agent` | `case_party` rows, role=`'commenting_agent'` |
| `origin_country` | `case_party` row, role=`'referring_state'` |
| `subject_matter` | `cjeu_classification` rows, scheme=`'subject_matter'` |
| `eurovoc` | `cjeu_classification` rows, scheme=`'eurovoc'` |
| `keywords` | `cjeu_classification` rows, scheme=`'keyword'` |
| `directory_codes` | `cjeu_classification` rows, scheme=`'directory_code'` |
| `citing`, `work_cites_work` | `case_citation` rows, relation_type=`'cites'` |
| `cited_by` | `case_citation` rows, relation_type=`'cited_by'` |
| `legal_resource` | `case_law_reference` rows, role=`'legal_basis'` |
| `based_on_treaty` | `case_law_reference` rows, role=`'based_on_treaty'` |
| `affecting_string`, `affecting_ids` | `case_law_reference` rows, role=`'affects'` |
| 18 `case_law_*_resource_legal` predicates | `case_law_reference` rows with respective `role` (see table above) |
| `case_law_joins_case_court` | `case_citation`, relation_type=`'joins'` |
| `case_law_subject_to_appeal_in_case_court` | `case_citation`, relation_type=`'subject_to_appeal'` |
| `case_law_reexamined_by_case_court` | `case_citation`, relation_type=`'reexamined_by'` |
| `case_law_referred_to_for_preliminary_ruling_case_law` | `case_citation`, relation_type=`'referred_for_preliminary_ruling'` |
| `case_law_is_about_concept_case_law` + `case_law_is_about_concept_new_case_law` | merged → `case_citation`, relation_type=`'is_about_concept'` |
| `case_law_interpretes_judgement_resource_legal` | split: if target is case-law → `case_citation`; if legislation → `case_law_reference` |
| `opinion_advocate_general_joined_to_case_court` | `cjeu_ag_opinion.parent_case_id` |
| `conclusions` | `cjeu_ag_opinion.opinion_uri` |
| `case_law_published_in_erecueil` | `cjeu_document.erecueil_ref` |
| `references_journals` | `cjeu_document.journal_refs` |
| `local_identifier` | `cjeu_document.local_identifier` |
| `work_part_of_dossier` | `cjeu_document.dossier_uri` |
| `summary` | `case_text.summary` (per-language) |
| `summary_source` | `case_text.summary_source` |
| `case_law_national_*` (12 fields, sector 8 only) | `cjeu_national_document` |
| `case_law_delivered_by_court_national` | `cjeu_national_document.national_court_uri` |

### Dropped — useless / redundant

| HF column | Reason |
|---|---|
| `__source_window` | Internal scrape provenance — not user-facing |
| `fulltext_source` | Mirror of `text_source` |
| `summary_language` | Always empty in our extraction |
| `year_of_resource` | Derivable from `date_publication.year` |
| `natural_number_celex` | Internal CELLAR sorting key |
| `alternate_identifiers` | Redundant with ECLI + CELEX |
| `creation_date`, `date_of_creation`, `work_date_creation`, `work_date_creation_legacy`, `date_creation_legacy`, `datetime_negotiation`, `work_datetime_transmission` | All CMR re-indexing timestamps, not case-meaningful (verified: shows 2026 timestamps for 1960s cases — record-creation, not decision-date) |
| `internal_status_code` | InfoCuria internal, opaque, sparse |
| `resource_legal_type`, `resource_type`, `resource_legal_uses_originally_language`, `resource_legal_id_obsolete_document`, `resource_legal_information_miscellaneous`, `resource_legal_number_sequence_celex`, `work_id_obsolete_notice` | CDM plumbing, not user-facing |
| `work_version`, `work_title`, `work_embargo`, `work_created_by_agent` | Work-level metadata, mostly empty/noise |
| `case_law_affaire_jurisdiction/number/type/year` | Populated for **1** out of 46,180 rows |
| `work_is_member_of_complex_work`, `work_related_to_work` | Sparse (<28%), purpose unclear, no obvious use case |
| `case_law_uses_originally_language_resource_legal` | Redundant with `language_procedure` |
| `case_law_delivered_by_advocate_general` | Redundant with `advocate_general` |
| `work_part_of_event` | Same set as `work_part_of_dossier` (always co-populated); dossier is the more useful name |
| `type_procedure` | Raw value redundant with `judicial_procedure_type`; we extract `procedure_result` from it then drop |

---

## ETL strategy (parallel-load, switch-over)

1. **Create new tables side-by-side** with the existing ones (different schema or `_v2` suffix).
2. **Initial bulk load** from current `cjeu-opendata` HF dataset:
   - `cases.parquet` → upsert into `case`, `cjeu_document`, `cjeu_ag_opinion`, `cjeu_national_document`, `cjeu_classification`, fan out judges/parties/citations/references.
   - `fulltexts.parquet` → upsert into `case_text` keyed on `(case_id, language)`.
3. **Reconciliation queries** — counts per table vs current corpus, sample diffs vs old schema.
4. **Application read switch** — point readers at the new tables. Keep both fed from the same source pipeline for a soft cutover window.
5. **Decommission old tables** once stable.

Loader will live at `cjeu_migration/load_postgres.py` (mirrors the existing
`consolidate.py` / `hf_push.py` pattern). Idempotent on re-run via
`ON CONFLICT (ecli) DO UPDATE`.

---

## Open questions for review

1. **`case.court_id`** in the current ERD is `int PK` — is that a typo for `int FK → court(id)`? Same case for a few other `*_id` fields shown as PKs that look like FKs.

2. **`relation_type` storage** in `case_citation` — Postgres enum (strict, requires migration to add) or `text` with `CHECK` (flexible)? CJEU adds ~12 new values; Rechtspraak / ECHR may add their own.

3. **`role` storage** in `case_law_reference` — same question; CJEU adds ~18 new values.

4. **`case_text.summary` cardinality** — current ERD has `summary` and `summary_source` as columns on a `case_text` table that's already per-language. Confirm that means "0-1 summary per (case, language)" — i.e. a French summary lives in the French row. Our data populates this for ~30% of cases (mostly EN/FR for older cases).

5. **CELEX-as-natural-key on `case_citation`** — should `case_citation.target_case_id` be required (target must exist in `case`), or do we keep a `target_celex text` column so we can store citations to cases not yet in our corpus? CJEU cites pre-1954 ECSC decisions and external European Patent Office cases that we don't ingest. Recommend: nullable `target_case_id` + non-null `target_celex_raw`.

6. **`cjeu_classification.label`** — populate from lookup tables (Eurovoc thesaurus has labels in 24 languages; subject_matter is curated by CJEU) or leave null and resolve at query time? Eurovoc labels alone are ~50,000 rows × 24 languages — that's a separate ingest project.

7. **Existing tables in your shared layer that I may have missed** — your colleague's ERD shows tables like `case_event`, `case_disposition`, `case_outcome`. Should any of those be used for CJEU's procedure_result / appeal outcome data instead of putting it on `cjeu_document`?

---

## Appendix: data-grounded counts (post-topup expected)

| Item | Count |
|---|---|
| Total CJEU cases | 46,180 |
| Cases with at least 1 fulltext | 45,608 (98.8%) |
| Distinct languages (post-topup expected) | up to 24 per case |
| Sector 6 (Court of Justice direct) | ~44,300 |
| Sector 8 (national CJEU-referred) | ~1,800 |
| Cases with AG Opinion | ~14,000 |
| Cases with Grand Chamber | ~600 |
| Cases tagged with eurovoc | ~28,000 (60%) |
| Cases with at least 1 citation | ~38,000 (82%) |
| Cases with at least 1 legal reference | ~41,000 (89%) |

---

*End of draft. Annotations welcome on any line. — David*
