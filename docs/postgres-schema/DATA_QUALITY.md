# Data quality and known limitations, table by table

An inventory of where the loaded data is thin, why, and what the concrete
enrichment path would be. Companion to MIGRATION_MAPPING.md §7
(implementation status).

The "whose" column attributes each limitation:

- **source** — the upstream system does not have this data
- **extraction** — upstream has it, our extractors never pulled it
- **loader** — we have the data, the migration loads it minimally or raw

## party / case_party

Current state: 142 distinct parties (50 respondent states as raw HUDOC
alpha-3 codes plus 92 CJEU agent name strings) backing about 297k case
links.

| Limitation | Whose | Detail / enrichment path |
|---|---|---|
| RS has zero parties | source | Dutch case law is anonymized by policy ("[verdachte]", "[appellant]"). Will never be rich. The only path is NER over fulltext (the `case_entity` pipeline), which yields anonymized tokens. |
| ECHR: only respondent states | source | HUDOC's only structured party field. Applicant names exist only inside `docname` ("SÖREMSKI v. POLAND"). Enrichment: parse the applicant from docname — cheap, about 80k names, planned as a loader follow-up. |
| ECHR states are alpha-3 codes, no `country_iso`, no readable name | loader | HUDOC uses ISO-3166 alpha-3 (NLD), our `jurisdiction` uses alpha-2 (NL); the loader stored the codes raw. Fix: a static 50-row mapping post-pass (name plus country_iso). Scheduled after the full run. |
| CJEU: agents only, no litigants | extraction | CELLAR/CDM carries agents and referring states; the actual party names live in CURIA (InfoCuria), which our extraction did not pull as structured fields. Enrichment: a CURIA parties extraction — which would also fix `cases.title` synthesis (below). |
| Agent names are raw strings, no aliases or dedup | loader | exact-string dedup only. Alias normalization is deferred until someone needs to query agents. |

## cases.title

| Limitation | Whose | Detail |
|---|---|---|
| CJEU titles are "Case C-123/22" without party names | extraction | CELLAR `work_title` is 1.3% populated and party names were not extracted (see party above). The D6 target format "C-123/22, X v Y" needs the CURIA parties extraction. RS titles are only 25% populated in the source. ECHR uses `docname`, which has good coverage. |

## judge / case_judge

| Limitation | Whose | Detail |
|---|---|---|
| Only CJEU has judges (rapporteur plus panel) | source | Legacy RS and HUDOC-structured ECHR carry no judge fields. ECHR judge names exist in fulltext headers only, which would need a parser. RS judges are not published structurally. |
| No name normalization across decades | loader | "P.J.G. Kapteyn" and "Kapteyn" are separate rows. `judge.aliases[]` exists for a future normalization pass. |

## instance (lookup) and cases.instance_id

| Limitation | Whose | Detail |
|---|---|---|
| Completely empty | source | No corpus carries instance-level data. Legacy RS `instance` turned out to hold court names (1,230 of them, mapped to `court`). Instance semantics (first instance / appeal / cassation) are derivable for RS from court type plus `rs_document_formal_relation.aanleg` — documented but not scheduled. |

## court

| Limitation | Whose | Detail |
|---|---|---|
| 1,230 RS courts loaded verbatim, including historical duplicates | loader (deliberate) | "Rechtbank 's-Gravenhage" and "Rechtbank Den Haag" are the same court before and after a 2013 rename, currently two rows. Lossless-first choice (§5.3): merging via `parent_court_id` plus a rename mapping is a curated follow-up task. `level` and `parent_court_id` stay NULL for RS courts until then. |

## case_text — cross-corpus Dutch texts (the 175 RS∩CJEU cases)

For the 175 Dutch sector-8 decisions present in both corpora, both sources
provide a Dutch fulltext, and the renditions are never identical (verified
2026-07-07: 0 of 120 overlapping pairs match on whitespace-normalized md5).
The CELLAR variant is a PDF extraction (`text_format='pdf'`): hard-wrapped
lines, document header often missing, but frequently longer because the PDF
bundles the judgment with the P-G conclusion — material Rechtspraak
publishes as a separate ECLI, which we already carry.

| Rule | Whose | Detail |
|---|---|---|
| Both renditions stored (D12) | schema (deliberate) | `case_text` is unique on (case_id, language, source); the 120 overlap cases carry an RS row and a CELLAR row for `nl` (294 rows across the 175 cases). The frontend lists renditions with `source` as the label. |
| Single-text reads go through `case_text_canonical` | schema (deliberate) | one row per case and language, origin preferred: RECHTSPRAAK, HUDOC, INFOCURIA_BLOB_HTML, CELLAR_ITEM. The `rs_v_`/`echr_v_` views join it, so they never fan out. |
| CELLAR text is the only text for 55 of the cases | source | Rechtspraak has no text for them; canonical resolves to the CELLAR pdf rendition there. (These 55, and the other 119 renditions, were originally dropped by a loader ECLI-map filter on the old single-valued source column; fixed in 50_load_cjeu.py, staging backfilled 2026-07-07.) |
| CELLAR pdf renditions are lower typographic quality | source | pdf-extracted: hard line wraps, missing headers. Stored verbatim; the clean RS text stays canonical where it exists. |

## case_text — stub texts and the `is_stub` flag (CJEU)

Some CJEU rows were captured as headnotes / OJ notices (mostly from
InfoCuria) before the full CELLAR manifestation existed. The 2026-07-20/21
upgrade passes refetched every row below 40% of its case's median rendition
length and replaced 20,521 of them (19,585 + 936) with the full CELLAR
judgment, in the HF corpus and in `case_text` alike.

| Fact | Whose | Detail |
|---|---|---|
| 9,124 stub rows remain (1.6% of CJEU texts) | source | CELLAR has nothing longer for these language/case pairs: genuine OJ notices (removed or withdrawn cases) or languages where InfoCuria is the only source. A flagged short text beats a missing row. |
| `case_text.is_stub` marks them | schema (deliberate) | boolean, default false; rule: length < 40% of the case's median CJEU rendition, median >= 10k chars. RS-origin rows are never judged and never enter the median. Recomputed by migration/sql/57 after every corpus sync. Quality-sensitive consumers filter `NOT is_stub`. |

## legislation (catalog breadth)

| Limitation | Whose | Detail |
|---|---|---|
| 256,252 of 265,108 BWB acts are alias-only stubs | loader (deliberate) | `rs_law_alias` carries the full Dutch BWB register (bwbidlist: official and citation titles for every regulation). Only about 8.8k acts appear in the LIDO structural catalog or are cited by case law; the rest were loaded as stub rows (title = official bwbidlist title) so all 302,611 aliases port 1:1. Stubs have no provisions and no case links until cited. |

## echr_document_secondary_text

| Limitation | Whose | Detail |
|---|---|---|
| Holds only non-canonical variant texts (3,222 rows) | schema (deliberate) | When several text-bearing variants of one case share a language (a judgment and an admissibility decision, both English), the canonical variant's text (doctype rank JUD > DEC > other, lowest item_id) lives in `case_text`; the remainder lives here. Together: 135,258 rows, matching legacy `echr_document_text` exactly. Not tsvector-indexed; search runs on the canonical text. |

## legal_provision.text / effective_from / effective_to

| Limitation | Whose | Detail |
|---|---|---|
| Provision text is empty — structure and titles only | source | The BWB catalog (rs_law_element) carries hierarchy, labels, and deeplinks, never the provision body text. Enrichment would be a wetten.overheid.nl fetch pipeline (large; not planned). Validity dates are the same story. |

## domain / domain_label

| Limitation | Whose | Detail |
|---|---|---|
| Labels only in source language; no hierarchy for eurovoc; `domain_label` empty | loader (deferred) | The EuroVoc SKOS ingest (DECISIONS.md) populates uri, parent_id, and the 24-language labels. RS domains and CJEU schemes are flat label strings by nature. |

## cases dates

| Limitation | Whose | Detail |
|---|---|---|
| About 65% of ECHR variant rows have no judgement or reference date | source | The loader parses the decision date out of the ECLI (`ECLI:CE:ECHR:2020:0206JUD…` → 2020-02-06), raising case-level coverage to about 100% of ECLI-bearing cases. Dates for ECLI-less communicated cases remain NULL. |
| `cases.date_published` semantics differ per corpus | loader (accepted) | RS: real publication date. CJEU/ECHR: NULL (no equivalent field). |

## case_citation

| Limitation | Whose | Detail |
|---|---|---|
| About 8k RS targets plus external CELEX targets unresolved | source | Cases that don't exist in any loaded corpus (unpublished NL decisions, pre-1954 ECSC, EPO). Kept as `target_ecli_raw`/`target_celex_raw` — queryable, never silently dropped. |
| `weight` only meaningful for ECHR edges | source | rs_edge and CDM have no citation frequency. |
| `extractor_version` NULL | loader | trivial stamp, listed in the §7.2 gap list. |

## cjeu_document

| Limitation | Whose | Detail |
|---|---|---|
| `cellar_uri`/`work_uri` NULL | loader | derivable from CELEX (§7.2 gap, small fix). |
| `procedure_result` NULL | loader | the parse from `type_procedure` is not written; the raw value is preserved in `proc_type` (§7.2). |
| `dossier_parent_case_id` NULL | loader | the resolution pass over `dossier_uri` is not written (§7.2); dossier_uri itself is loaded. |

## echr_document_article.raw

| Limitation | Whose | Detail |
|---|---|---|
| NULL for all migrated rows | source | the legacy normalized table has no per-row source fragment; the column exists for the future HUDOC parser. |

## case_summary_version, case_entity, clusters/metrics, search_query_log

Empty by design — they belong to downstream pipelines (LLM summaries, NER,
graph analytics, app runtime), not to the migration. See §7.3.

---

## Principles behind these choices

1. Don't fake data: a NULL with a documented reason beats a synthesized
   value that looks real.
2. Don't drop data: everything the sources have is loaded somewhere, even
   if raw (`national_parties_raw`, `target_celex_raw`, alpha-3 codes).
   Enrichment passes can upgrade raw to structured later without
   re-migration.
3. Every gap has an owner: source gaps need new extraction projects,
   extraction gaps need extractor work (CURIA parties is the big one),
   loader gaps are small follow-up passes listed in §7.2.
