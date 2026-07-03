# CLE unified schema — decision log

> Finalized 2026-07-03 (David; pending colleague sign-off on the PR).
> The schema itself is [schema_full.sql](schema_full.sql); legacy production
> reference is [legacy.sql](legacy.sql); CJEU field mapping detail is
> [CJEU_TABLES.md](CJEU_TABLES.md).

## Architecture decisions

### D1 — Deployment namespace: dedicated schema, not `public`

New table names collide with legacy (`echr_document`, `rs_document`, … same
names, different shapes). The new schema deploys into a dedicated Postgres
schema — suggested name `cle_v2` — on the SAME database as legacy, so
reconciliation queries can join across during the parallel-run phase.

`schema_full.sql` says `"public"` only so visualization tools (chartdb,
drawdb) import it without ceremony. At apply time:

```bash
sed 's/"public"/"cle_v2"/g' schema_full.sql | psql "$CLE_DATABASE_URL"
```

Cutover = flip the application `search_path`. Rollback = flip it back.

### D2 — Language codes: lowercase ISO 639-1

One canonical convention everywhere: `en`, `fr`, `nl`, `bg`, …
Normalized at load: HUDOC `ENG`→`en`, `FRE`→`fr`; CJEU `EN`→`en`; RS is
already `nl`. The `language` table is seeded with the 24 official EU
languages, which covers all three corpora.

### D3 — Full-text search vector: generated column + loader guard

`case_text.fulltext_tsv` is a `GENERATED` column over `fulltext` only.

- The 1 MB tsvector limit is handled in the **loader**: input to the text
  column is used as-is, but any document whose fulltext exceeds ~1M chars
  would have its tsvector computed from a truncated prefix (word positions
  clamp at 16,383 anyway — negligible search-quality impact).
- The legacy RS trigger folded `summary` + `legal_provisions` into the
  vector; we do NOT replicate that. Summary search is the API's job
  (separate expression index if needed). Search behavior is explicit, not
  hidden in a trigger.

### D4 — `relation_type` / `role`: text now, CHECK later

Both stay plain `text` for the first full load. Once the value sets across
all three corpora are empirically stable, add `CHECK` constraints (one
`ALTER` each). Enums rejected: every new corpus would mean a type
migration. The expected values are documented inline in `schema_full.sql`.

### D5 — Embeddings: `vector(768)`

Pinned to the legacy model dimension (`ecli_segments.embedding vector(768)`).
Changing the embedding model later means altering the column and rebuilding
HNSW indexes over millions of rows — if a model switch is on the roadmap,
decide **before** the bulk load.

## Cross-corpus data conventions

### D6 — `case.title`

RS / ECHR: native title. CJEU: **synthesized by the loader** as
`"C-123/22, X v Y"` (case number from CELEX + party names) — CELLAR's
`work_title` is populated for only 1.3% of cases.

### D7 — `case.importance`: harmonized 1–4 scale

`1` = most important (HUDOC convention). Per corpus:

| Corpus | Source |
|---|---|
| ECHR | HUDOC importance, as-is |
| RS | native importance, mapped to 1–4 |
| CJEU | formation-based proxy set by the loader: Full Court / Grand Chamber → 1, five-judge chamber → 2, three-judge chamber → 3, sole judge / simple order → 4 |

All three are proxies of varying quality; the column is for coarse ranking,
not scholarship.

### D8 — CJEU classifications and parties: no denormalized copies

Legacy-draft `cjeu_document.subject_matter` and `parties_text` are dropped.
No CJEU data has been loaded into Postgres yet (clean slate), so
classifications go straight to `domain`/`case_domain` (schemes
`cjeu_subject_matter`, `eurovoc`, `cjeu_keyword`, `cjeu_directory_code`)
and parties to `party`/`case_party`.

### D9 — `rs_law_element` / `rs_law_alias` are legislation, not RS metadata

They catalog Dutch legislation (BWB register + LIDO/JuriConnect ids), so
they fold into the generic tables: `wet` rows → `legislation`
(scheme=`'bwb'`), deeper elements → `legal_provision` (with `element_type`
and a `parent_id` hierarchy that also serves EU acts), aliases →
`legislation_alias`.

## Ratified structural choices

| # | Choice | Rationale |
|---|---|---|
| R1 | One shared `case_text` (case × language) for all corpora | replaces 2 per-corpus text tables; multi-language is first-class |
| R2 | One shared `case_citation` + trigger-maintained `case_citation_counts` | replaces `echr_edge`/`rs_edge` + 2 count tables; enables cross-corpus graph queries |
| R3 | `case.id` (bigint) is the FK anchor; ECLI is a UNIQUE business key | fast joins; ECLI stays the external identifier |
| R4 | `echr_document` is per (case × language) | legacy production granularity; HUDOC has per-language variants |
| R5 | Unresolved citations keep `target_ecli_raw` / `target_celex_raw`; dedup via partial unique indexes; target FK `ON DELETE SET NULL` | external-target citations (~3% for CJEU) neither lost nor duplicated |
| R6 | `case_party` PK includes `ordinal` | multiple parties in the same role on one case |
| R7 | Legacy staging tables (`case_law`, `legal_case`, `ecli_*`, `law_*`) not ported | content maps into the new tables; see migration notes in schema_full.sql |

## Future ingest project: EuroVoc labels

**What**: CJEU cases carry EuroVoc concept tags (~60% of the corpus).
EuroVoc is the EU's multilingual thesaurus (Publications Office): ~7,000
concepts, labels in 24 languages, broader/narrower hierarchy, stable
concept URIs (`http://eurovoc.europa.eu/<id>`).

**Why**: today we store only the tag string as it appears on the case.
With the thesaurus loaded, tags become navigable (hierarchy roll-ups,
"all competition-law cases including narrower concepts") and localizable
(display the Dutch label to a Dutch user).

**How the schema accommodates it** (already in place, no migration needed):

- `domain.uri` — holds the EuroVoc concept URI (stable join key)
- `domain.parent_id` — holds the broader-term hierarchy
- `domain.name` — canonical (English) prefLabel
- `domain_label (domain_id, language, label)` — the other 23 languages,
  ~170k rows total. Created empty; filled by this ingest.

**Ingest steps** (not scheduled yet):

1. Download the EuroVoc SKOS distribution from the Publications Office
   (op.europa.eu → EU Vocabularies → EuroVoc, RDF/SKOS format).
2. Upsert `domain` rows for scheme=`'eurovoc'` keyed on concept URI;
   set `name` = English prefLabel, `parent_id` via `skos:broader`.
3. Insert `domain_label` rows for every (concept, language) prefLabel.
4. Re-point `case_domain` rows: match the raw tag strings loaded from the
   corpora against prefLabels (any language) → the URI-keyed domain row.
   Unmatched strings stay as label-only domain rows (no uri).

**Size**: ~7k `domain` rows + ~170k `domain_label` rows — trivial.

## Open at loader phase (not blocking the schema)

- CJEU party-name parsing quality (splitting "X v Y" reliably across 24
  procedural languages — loader heuristics, EN/FR fallback).
- Judge-name normalization across decades (aliases like "P.J.G. Kapteyn" /
  "Kapteyn").
- Whether `case_citation` fan-out of `rs_document_formal_relation` should
  carry `aanleg`/`gevolg` in `relation_type` or stay lossy (details remain
  in the RS table either way).
