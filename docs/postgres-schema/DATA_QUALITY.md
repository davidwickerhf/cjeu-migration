# Data quality & known limitations — table by table

> Blunt inventory of where the loaded data is thin, WHY (source policy vs
> extraction gap vs loader shortcut), and the concrete enrichment path.
> Companion to MIGRATION_MAPPING.md §7 (implementation status).
> Every limitation below is a documented CHOICE, not an accident.

Legend for the "whose limitation" column:
- **SOURCE** — the upstream system genuinely does not have this data
- **EXTRACTION** — upstream has it, our extractors never pulled it
- **LOADER** — we have the data, the migration loads it minimally/rawly

## party / case_party

**Current state:** 142 distinct parties (50 respondent states as raw HUDOC
alpha-3 codes + 92 CJEU agent name strings) backing ~297k case links.

| Limitation | Whose | Detail / enrichment path |
|---|---|---|
| RS has zero parties | **SOURCE** | Dutch case law is anonymized by policy ("[verdachte]", "[appellant]"). Will never be rich. Only path: NER over fulltext (`case_entity` pipeline), yielding anonymized tokens. |
| ECHR: only respondent states | **SOURCE** | HUDOC's only structured party field. Applicant names exist ONLY inside `docname` ("SÖREMSKI v. POLAND"). **Enrichment: parse applicant from docname** — cheap, ~80k names, planned loader follow-up. |
| ECHR states are alpha-3 codes, no `country_iso`, no readable name | **LOADER** | HUDOC uses ISO-3166 alpha-3 (NLD), our `jurisdiction` uses alpha-2 (NL); loader stored codes raw. **Fix: static 50-row mapping post-pass** (name + country_iso). Scheduled after the full run. |
| CJEU: agents only, no litigants | **EXTRACTION** | CELLAR/CDM carries agents + referring states; actual party names live in CURIA (InfoCuria), which our extraction didn't pull as structured fields. **Enrichment: CURIA parties extraction** — would also fix `cases.title` synthesis (see below). |
| Agent names are raw strings, no aliases/dedup | **LOADER** | exact-string dedup only. Alias normalization deferred until someone needs to query agents. |

## cases.title

| Limitation | Whose | Detail |
|---|---|---|
| CJEU titles are `"Case C-123/22"` without party names | **EXTRACTION** | CELLAR `work_title` is 1.3% populated; party names not extracted (see party above). The D6 target format `"C-123/22, X v Y"` needs the CURIA parties extraction. RS titles: only 25% populated in the source. ECHR: `docname` used, good coverage. |

## judge / case_judge

| Limitation | Whose | Detail |
|---|---|---|
| Only CJEU has judges (rapporteur + panel) | **SOURCE** | Legacy RS and HUDOC-structured ECHR carry no judge fields. ECHR judge names exist in fulltext headers only → future parser. RS judges are not published structurally. |
| No name normalization across decades | **LOADER** | "P.J.G. Kapteyn" vs "Kapteyn" are separate rows. `judge.aliases[]` exists for a future normalization pass. |

## instance (lookup) + cases.instance_id

| Limitation | Whose | Detail |
|---|---|---|
| Completely empty | **SOURCE** | No corpus carries instance-level data. Legacy RS `instance` turned out to hold COURT NAMES (1,230 of them → mapped to `court`). Instance semantics (first instance / appeal / cassation) are derivable for RS from court type + `rs_document_formal_relation.aanleg` — **derivation pass documented but not scheduled**. |

## court

| Limitation | Whose | Detail |
|---|---|---|
| 1,230 RS courts loaded verbatim, incl. historical duplicates | **LOADER (deliberate)** | "Rechtbank 's-Gravenhage" and "Rechtbank Den Haag" are the same court pre/post-2013 rename, currently two rows. Lossless-first choice (§5.3): merge via `parent_court_id`/rename mapping is a curated follow-up task. `level`/`parent_court_id` NULL for all RS courts until then. |

## legal_provision.text / effective_from / effective_to

| Limitation | Whose | Detail |
|---|---|---|
| Provision TEXT is empty — structure and titles only | **SOURCE** | The BWB catalog (rs_law_element) carries hierarchy + labels + deeplinks, never the provision body text. Enrichment: wetten.overheid.nl fetch pipeline (big; not planned). Validity dates same story. |

## domain / domain_label

| Limitation | Whose | Detail |
|---|---|---|
| Labels only in source language; no hierarchy for eurovoc; `domain_label` empty | **LOADER (deferred)** | EuroVoc SKOS ingest (DECISIONS.md) populates uri/parent_id/24-language labels. RS domains and CJEU schemes are flat label strings by nature. |

## cases dates

| Limitation | Whose | Detail |
|---|---|---|
| ~65% of ECHR variant rows have no judgement/reference date | **SOURCE** | Loader parses the decision date OUT OF THE ECLI (`ECLI:CE:ECHR:2020:0206JUD…` → 2020-02-06), raising case-level coverage to ~100% of ECLI-bearing cases. Dates for ECLI-less communicated cases remain NULL. |
| `cases.date_published` semantics differ per corpus | **LOADER (accepted)** | RS: real publication date. CJEU/ECHR: NULL (no equivalent field). |

## case_text — cross-corpus Dutch texts (the 175 RS∩CJEU cases)

For the 175 Dutch sector-8 decisions present in both corpora, BOTH sources
can provide a Dutch fulltext, and they are **never identical** (verified
2026-07-07: 0 of 120 overlapping pairs match on whitespace-normalized md5).
The CELLAR variant is a PDF extraction (`text_format='pdf'`): hard-wrapped
lines, document header often missing, but frequently *longer* because the
PDF bundles the judgment with the P-G conclusion — material Rechtspraak
publishes as a separate ECLI (and which we therefore already carry).

| Rule | Whose | Detail |
|---|---|---|
| RS text wins when both exist (120 cases) | **LOADER (deliberate)** | Rechtspraak is the ORIGIN for these national decisions (CELLAR's JURE collection redistributes them); its XML-sourced text is clean and exactly scoped to the ECLI. The CELLAR pdf rendition stays retrievable from the HF corpus. |
| CELLAR text fills the gap when RS has none (55 cases) | **LOADER** | `source='CELLAR_ITEM'`, `text_format='pdf'`. Without this the 55 had no fulltext at all — the original loader filtered its ECLI map on `cases.source='CJEU'` and silently dropped every cross-corpus parquet text (fixed in 50_load_cjeu.py; staging backfilled 2026-07-07). |
| One text per (case, language) | **SCHEMA (deliberate)** | `case_text` is UNIQUE on (case_id, language); the API and views join on that key. Storing both renditions would relax this to include source for the benefit of 120 lower-quality duplicates-with-extra-headers — not worth it. |

## case_citation

| Limitation | Whose | Detail |
|---|---|---|
| ~8k RS targets + external CELEX targets unresolved | **SOURCE** | Cases that don't exist in any loaded corpus (unpublished NL decisions, pre-1954 ECSC, EPO). Kept as `target_ecli_raw`/`target_celex_raw` — queryable, never silently dropped. |
| `weight` only meaningful for ECHR edges | **SOURCE** | rs_edge/CDM have no citation frequency. |
| `extractor_version` NULL | **LOADER** | trivial stamp, in §7.2 gap list. |

## cjeu_document

| Limitation | Whose | Detail |
|---|---|---|
| `cellar_uri`/`work_uri` NULL | **LOADER** | derivable from CELEX (§7.2 gap — small fix). |
| `procedure_result` NULL | **LOADER** | parse from `type_procedure` not written; raw value preserved in `proc_type` (§7.2). |
| `dossier_parent_case_id` NULL | **LOADER** | resolution pass over `dossier_uri` not written (§7.2); dossier_uri itself is loaded. |

## echr_document_article.raw

| Limitation | Whose | Detail |
|---|---|---|
| NULL for all migrated rows | **SOURCE** | legacy normalized table has no per-row source fragment; the column exists for the future HUDOC parser. |

## case_summary_version, case_entity, clusters/metrics, search_query_log

Empty by design — they belong to downstream pipelines (LLM summaries, NER,
graph analytics, app runtime), not to the migration. See §7.3.

---

## The rule these choices follow

1. **Never fake data**: a NULL with a documented reason beats a synthesized
   value that looks real.
2. **Never drop data**: everything the sources have is loaded somewhere,
   even if raw (e.g. `national_parties_raw`, `target_celex_raw`, alpha-3
   codes) — enrichment passes can always upgrade raw → structured later
   without re-migration.
3. **Every gap has an owner**: SOURCE gaps need new extraction projects;
   EXTRACTION gaps need extractor work (CURIA parties is the big one);
   LOADER gaps are small follow-up passes listed in §7.2.
