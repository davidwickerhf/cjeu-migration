# CLE unified schema — decision log

Last updated 2026-07-07. The schema itself is [schema_full.sql](schema_full.sql);
the legacy production reference is [legacy.sql](legacy.sql); the per-column
source mapping is generated into [COVERAGE_PROOF.md](COVERAGE_PROOF.md).

## Architecture decisions

### D1 — Deployment namespace: dedicated schema, not `public`

New table names collide with legacy ones (`echr_document`, `rs_document`
exist in both, with different shapes). The new schema deploys into its own
Postgres schema, `cle_v2`, on the same database as legacy, so reconciliation
queries can join across both during the parallel-run phase.

`schema_full.sql` says `"public"` only so visualization tools (chartdb,
drawdb) can import it. At apply time:

```bash
sed 's/"public"/"cle_v2"/g' schema_full.sql | psql "$CLE_DATABASE_URL"
```

Cutover means flipping the application `search_path`; rollback is flipping
it back.

### D2 — Language codes: lowercase ISO 639-1

One convention everywhere: `en`, `fr`, `nl`, `bg`, and so on. Normalized at
load time: HUDOC `ENG` becomes `en`, `FRE` becomes `fr`; CJEU `EN` becomes
`en`; RS is already `nl`. The `language` table is seeded with the 24
official EU languages, and the loaders upsert any code they meet beyond
those (HUDOC carries translations in 30+ languages).

### D3 — Full-text search vector: generated column plus loader guard

`case_text.fulltext_tsv` is a generated column over `fulltext` only.

- The 1 MB tsvector limit is handled in the loader: the text column itself
  is stored as-is, but a document over roughly 1M characters gets its
  tsvector computed from a truncated prefix. Word positions clamp at 16,383
  anyway, so the search-quality impact is negligible.
- The legacy RS trigger folded `summary` and `legal_provisions` into the
  vector; we do not replicate that. Summary search is the API's job, with a
  separate index (`summary_tsv`, see A3). Search behavior should be visible
  in the query, not hidden in a trigger.

### D4 — `relation_type` / `role`: text now, CHECK later

Both stay plain `text` for the first full load. Once the value sets across
the three corpora have proven stable, CHECK constraints can be added with
one ALTER each. Enums were rejected because every new corpus would require
a type migration. The expected values are documented inline in
`schema_full.sql`.

### D5 — Embeddings: `vector(768)`

Pinned to the legacy model dimension (`ecli_segments.embedding` is
vector(768)). Changing the embedding model later means altering the column
and rebuilding HNSW indexes over millions of rows, so if a model switch is
on the roadmap it should be decided before a bulk load, not after.

## Cross-corpus data conventions

### D6 — `cases.title`

RS and ECHR use the native title. For CJEU the loader synthesizes
`"C-123/22, X v Y"` (case number from CELEX plus party names), because
CELLAR's `work_title` is populated for only 1.3% of cases. Party names are
still an open extraction task, so current CJEU titles are the case number
only (see MIGRATION_MAPPING §7.2).

### D7 — `cases.importance`: shared 1–4 scale

1 is most important, following the HUDOC convention. Per corpus:

| Corpus | Source |
|---|---|
| ECHR | HUDOC importance, unchanged |
| RS | no native importance in legacy; NULL |
| CJEU | formation-based proxy set by the loader: Full Court / Grand Chamber = 1, five-judge chamber = 2, three-judge chamber = 3, sole judge or simple order = 4 |

These are proxies of varying quality. The column is meant for coarse
ranking, not scholarship.

### D8 — CJEU classifications and parties: no denormalized copies

The draft columns `cjeu_document.subject_matter` and `parties_text` were
dropped. No CJEU data existed in Postgres before this migration, so
classifications go straight to `domain`/`case_domain` (schemes
`cjeu_subject_matter`, `eurovoc`, `cjeu_directory_code`) and parties to
`party`/`case_party`.

### D9 — `rs_law_element` / `rs_law_alias` are legislation, not RS metadata

These tables catalog Dutch legislation (the BWB register plus
LIDO/JuriConnect ids), so they fold into the generic tables: `wet` rows go
to `legislation` (scheme `bwb`), deeper elements to `legal_provision`
(with `element_type` and a `parent_id` hierarchy that also serves EU acts),
aliases to `legislation_alias`.

### D10 — One law-reference table for all corpora

`case_law_reference` follows the same design as `case_citation`: resolved
FK targets (`legislation_id`, `provision_id`) and raw source-shaped targets
(`raw_scheme`, `raw_resource`, `raw_subdivision`, `raw_label_id`,
`raw_reference`) sit side by side in one shared table, with partial unique
indexes per resolution state. What used to justify a separate RS table is
absorbed:

- `version_date` (the temporal pin of the cited law version) became a
  shared column; EU consolidated versions are dated too.
- The BWB resolution keys map onto the generic raw columns
  (`bwb_resource` to `raw_resource`, `article` to `raw_subdivision`,
  `bwb_label_id` to `raw_label_id`, `opschrift` to `raw_reference`).
- The wetten.overheid.nl / LIDO deeplink URLs (generated columns in
  legacy) are presentation logic and moved to the
  `rs_v_document_law_reference` view, which reconstructs the legacy table
  shape for the API.

Everything reads `case_law_reference`; the Dutch-specific API endpoints
read the compat view.

### D11 — Deletion behavior: every FK has an explicit ON DELETE rule

Case deletion is a real scenario: RS depublication exists in the source
feed (`opendata_status = 'depublicated'`), and GDPR or correction requests
can follow. `DELETE FROM cases WHERE ecli = …` therefore has to work in one
statement, must not be blocked by another case's data, and must not
silently lose link information. Three tiers, audited and smoke-tested on
the fully loaded staging database (2026-07-07):

| Tier | Rule | FKs |
|---|---|---|
| Ownership — CASCADE | satellite rows are meaningless without their anchor | all `case_id` FKs (case_text, case_judge, case_party, case_domain, case_law_reference, citation source, counts, cjeu/echr/rs documents, segments, entities, summaries, memberships, metrics); chained satellites (`item_id` to echr_document, `case_id` to rs_document, label to domain, alias to legislation); analytics ownership (`snapshot_id` to network_snapshot, `cluster_id` to case_cluster) |
| Cross-case link — SET NULL | a link about another case degrades instead of blocking that case's deletion | `case_citation.target_case_id` (R5), `case_citation.context_segment_id` (also unblocks re-segmentation), `rs_document_formal_relation.target_ecli` (raw stays in `target_identifier`), `cjeu_ag_opinion.parent_case_id` (re-resolvable via `case_number`), `cjeu_document.dossier_parent_case_id` (raw stays in `dossier_uri`), `lido_link` case FKs (raw ECLIs/URIs kept), `case_summary_version.parent_version_id` |
| Lookup — NO ACTION | deleting a language, court, domain, judge, party, or legislation row that is still in use should fail; that is a curation act, not a data operation | all remaining FKs |

Resolved citations carry no raw target (R5 keeps raw only while
unresolved), so SET NULL alone would erase a resolved citation's target
when the cited case is deleted. `trg_cases_preserve_citation_raw` (BEFORE
DELETE on `cases`) writes the dying case's `ecli`/`celex_id` back onto
incoming citations first; the row then degrades to the ordinary unresolved
state and can be re-resolved if the case is ever re-ingested. If an
identical unresolved row already exists, the `case_citation_uk_unresolved_*`
indexes fail the delete instead of merging silently.

Verified in a rolled-back transaction: deleting a CJEU case cascaded all
satellites and degraded its two incoming citations with raw identifiers
preserved; deleting an RS case removed its 17 segments and nulled the
incoming formal relation while keeping `target_identifier`.

### D12 — Dual-source fulltexts: relax `case_text`, don't split `cases`

175 cases (Dutch sector-8 decisions) exist in both RS and CELLAR, and both
sources provide a Dutch fulltext. The renditions are never identical (0 of
120 overlapping pairs match on whitespace-normalized md5): CELLAR ships a
PDF extraction that bundles the P-G conclusion, Rechtspraak ships the clean
per-ECLI XML text. Both are stored and shown per source in the frontend.

Decision: the `case_text` unique key becomes `(case_id, language, source)`,
with `source` NOT NULL (verified against 1.68M loaded rows). All renditions
of a case's text in one language coexist as sibling rows, with `source` as
the display label. Single-text consumers (search, the `rs_v_`/`echr_v_`
views, API defaults) read the `case_text_canonical` view, which picks one
row per case and language by origin preference: RECHTSPRAAK, then HUDOC,
then INFOCURIA_BLOB_HTML, then CELLAR_ITEM. A loader guard keeps CJEU
summaries off the RS-origin row.

The alternative — splitting `cases` into one row per source sharing an
ECLI — was rejected. One row per ECLI is the FK anchor (R3): citations,
formal relations, segments, and LIDO links all resolve targets by ECLI, and
two rows per ECLI would make those joins ambiguous, split citation counts,
and double-count in graph queries. Per-source metadata is already separated
on the single row by the satellites (`rs_document` for RS, `cjeu_document`
for CELLAR). Only the text needed multiplicity, so only `case_text` was
relaxed.

### D13 — Corpus membership: `cases.sources text[]`, trigger-maintained

A case covered by several corpora is one `cases` row with one satellite per
corpus. Membership is stored on the row:

- `cases.sources text[] NOT NULL`, for example `{RS}` or `{RS,CJEU}`, with
  a GIN index (`case_idx_sources`). Coverage filter:
  `sources @> '{CJEU}'` (46,169 — the full parquet corpus). Origin filter:
  `sources[1] = 'CJEU'` (45,994).
- `sources[1]` is the origin corpus — the loader that created the row and
  populated the shared columns (title, dates, court). Later corpora append
  when their satellite attaches, so array order is load order.
- Drift protection follows the `case_citation_counts` pattern: loaders set
  the array during bulk load (triggers are only installed in 90_post_load,
  after the data), and afterwards the
  `trg_{rs,cjeu,echr}_document_sources_attach/detach` triggers keep it in
  sync with satellite existence. ECHR detaches only when the last language
  variant is deleted. Verified live: deleting a `cjeu_document` row removes
  `CJEU` from the array.

This replaced two earlier iterations: a single-valued `cases.source` column
(which under-reported the 175 dual-corpus cases and caused a dropped-texts
bug in the loader) and a derived `case_source` view (dropped — we want
membership stored and directly queryable on the row, not reconstructed
through a view).

## Adoptions from the interim schema draft (reviewed 2026-07-06)

The interim schema draft (predating the ECHR/RS/CJEU extension work) was
compared against `schema_full.sql`. Five things were adopted:

| # | Adoption | Why |
|---|---|---|
| A1 | Table renamed `case` → `cases` | `CASE` is a reserved SQL keyword; the old name forced `"case"` quoting in every query |
| A2 | New `case_summary_version` table | versioned LLM-generated summaries with a human-review workflow (`is_current`, `rejected_at`, `parent_version_id`, partial unique on current per case/scope/model). Distinct from `case_text.summary`, which holds upstream source summaries |
| A3 | `case_text.summary_tsv` generated column + GIN index | implements D3's "summary is searched separately" |
| A4 | Trigram index on `cases.title` | title is populated wherever the source has one (D6); search-by-name needs it |
| A5 | `echr_document_article` gains `protocol` + `raw` | absorbed from the draft's `echr_violation` table: protocol filtering (`P1` vs Convention articles) plus verbatim provenance. The three-state `kind` (including `applied`) and the per-variant anchoring are kept |

The rest of the interim draft was already superseded by the current schema
(flattened ECHR, dual-FK cjeu_document, no BWB handling, no citation
counts).

## Structural choices

| # | Choice | Rationale |
|---|---|---|
| R1 | One shared `case_text` for all corpora | replaces two per-corpus text tables; multi-language support is built in |
| R2 | One shared `case_citation` plus trigger-maintained `case_citation_counts` | replaces `echr_edge`/`rs_edge` and two count tables; enables cross-corpus graph queries |
| R3 | `cases.id` (bigint) is the FK anchor; ECLI is a unique business key | fast joins; ECLI stays the external identifier |
| R4 | `echr_document` is per language variant (PK `item_id`) | legacy production granularity; HUDOC publishes per-language variants |
| R5 | Unresolved citations keep `target_ecli_raw` / `target_celex_raw`; dedup via partial unique indexes; target FK ON DELETE SET NULL | external-target citations (about 3% for CJEU) are neither lost nor duplicated |
| R6 | `case_party` key includes `ordinal` | multiple parties can hold the same role on one case |
| R7 | Legacy staging tables (`case_law`, `legal_case`, `ecli_*`, `law_*`) not ported as tables | their content maps into the new tables; see COVERAGE_PROOF.md |

## Future ingest project: EuroVoc labels

CJEU cases carry EuroVoc concept tags (about 60% of the corpus). EuroVoc is
the EU's multilingual thesaurus, maintained by the Publications Office:
roughly 7,000 concepts, labels in 24 languages, a broader/narrower
hierarchy, and stable concept URIs (`http://eurovoc.europa.eu/<id>`).

Today we store only the tag string as it appears on the case. With the
thesaurus loaded, tags become navigable (hierarchy roll-ups, "all
competition-law cases including narrower concepts") and localizable
(showing the Dutch label to a Dutch user).

The schema already accommodates this; no migration is needed:

- `domain.uri` holds the EuroVoc concept URI (stable join key)
- `domain.parent_id` holds the broader-term hierarchy
- `domain.name` holds the canonical English prefLabel
- `domain_label (domain_id, language, label)` holds the other 23 languages,
  about 170k rows. Created empty; filled by this ingest.

Ingest steps (not scheduled yet):

1. Download the EuroVoc SKOS distribution from the Publications Office
   (op.europa.eu → EU Vocabularies → EuroVoc, RDF/SKOS format).
2. Upsert `domain` rows for scheme `eurovoc` keyed on concept URI; set
   `name` to the English prefLabel and `parent_id` via `skos:broader`.
3. Insert `domain_label` rows for every concept/language prefLabel.
4. Re-point `case_domain` rows by matching the raw tag strings loaded from
   the corpora against prefLabels in any language. Unmatched strings stay
   as label-only domain rows without a URI.

Size: about 7k `domain` rows plus 170k `domain_label` rows.

## Open at loader phase (not blocking the schema)

- CJEU party-name parsing quality (splitting "X v Y" reliably across 24
  procedural languages; loader heuristics with EN/FR fallback).
- Judge-name normalization across decades (aliases like "P.J.G. Kapteyn"
  vs "Kapteyn").
- Whether the `case_citation` fan-out of `rs_document_formal_relation`
  should carry `aanleg`/`gevolg` in `relation_type` or stay lossy (the
  detail remains in the RS table either way).
