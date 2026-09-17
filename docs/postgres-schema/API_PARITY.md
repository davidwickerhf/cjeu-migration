# citations-api parity — every legacy query answered by the new schema

> Built by reading the citations-api source (all endpoints, verbatim SQL) and
> probing the live legacy database. For each endpoint: how it queries today,
> how the same question is answered in `cle_v2`, which index serves it, and
> where the new schema is strictly better.

## Verdict on the two review points raised

1. **"source_case_id / target_case_id are text but should be int FKs"** —
   already the case in `cle_v2`: `case_citation.source_case_id` /
   `target_case_id` and `case_entity.case_id` are `bigint` FKs →
   `cases(id)`. Raw external identifiers live in dedicated
   `target_ecli_raw` / `target_celex_raw` columns, not in the FK columns.

2. **"Add a normalized echr_violation(item_id, article_label, violated)"** —
   `cle_v2` has the superset: `echr_document_article(item_id, kind,
   article_code, protocol, raw)` with `kind ∈ {applied, violation,
   nonviolation}` (the draft's boolean loses `applied`, which is 771,748 of
   932,132 rows in production) plus a `(kind, article_code)` index. Note
   the legacy API doesn't even have this today — it regex-scans the raw
   `violation`/`article`/`nonviolation` text columns with `~*` boundary
   patterns. The normalized table turns that into an indexed lookup.

---

## Endpoint-by-endpoint

### POST /api/echr — main search

| Legacy filter (verbatim behavior) | New-schema query | Index |
|---|---|---|
| article regex over `violation`/`article`/`nonviolation` text | `EXISTS (SELECT 1 FROM echr_document_article a WHERE a.item_id = d.item_id AND a.kind='violation' AND a.article_code = ANY(…))` — AND-mode: one `EXISTS` per article | `echr_document_article_idx_filter (kind, article_code)` |
| keywords: `issue ILIKE` / `docname ILIKE` / `fulltext_tsv @@` | same, on `echr_document.issue/docname` + `case_text.fulltext_tsv` | trgm GINs on `docname`,`issue`; GIN on `fulltext_tsv` |
| `judgementdate >= / <=` | `echr_document.judgement_date` | `echr_document_idx_judgement_date` |
| `referencedate >= / <=` | `echr_document.reference_date` | `echr_document_idx_reference_date` (NEW — legacy had none) |
| `languageisocode = ANY('ENG',…)` | `echr_document.language = ANY('en',…)` — **API must translate codes at cutover** | part of `(case_id, language)` index |
| `respondent = ANY(…)` | see **improvement #2** below | |
| `doctype = ANY(…)` | `echr_document.doctype` | `echr_document_idx_doctype` |
| `importance = ANY(…)` | `cases.importance` | `case_idx_importance` (NEW) |
| appno route: `echr_document_appno WHERE appno = ANY(…) AND source='appno'` | identical table, keyed `(item_id, appno, source)` | `echr_document_appno_idx_appno` (NEW — legacy's `left(appno,500)` expression index never matched the API's equality predicate!) |
| `DISTINCT ON (ecli) … ORDER BY ENG-first` (pick best language row) | **no longer needed**: `cases.item_id` IS the canonical (ENG-first) variant — `JOIN echr_document d ON d.item_id = cases.item_id` | PK |
| edges: `echr_edge WHERE source/target_itemid = ANY(…)` | `case_citation WHERE source/target_case_id = ANY(…) AND source_dataset='echr_edge'` — int keys, batch-friendly | `case_citation_idx_source/target` |
| cursor pagination on ecli | same on `cases.ecli` / `(date_decision, ecli)` | `case_idx_date_ecli` (NEW) |

### POST /api/echr/text

Legacy: `echr_document ⋈ echr_document_text` by (itemid, language), ordered
ENG→FRE→rest. New: `cases → case_text_canonical` (one row per case × language;
the base table is `UNIQUE(case_id, language, source)` since D12 — dual
renditions for cross-corpus cases — and the canonical view picks the
origin-preferred row), same ordering by language rank. One join instead
of a two-column text-cast join.

### POST /api/rechtspraak — main search

| Legacy filter | New-schema query | Index |
|---|---|---|
| `instance = ANY(court names)` | `cases.court_id IN (SELECT id FROM court WHERE name = ANY(…))` — court is a 1,230-row lookup | `case_idx_court` |
| `domains && ARRAY[…]` | kept 1:1 on `rs_document.domains` | `rs_document_idx_domains_gin` (NEW) — or the normalized `case_domain` route |
| `document_type/procedure_type = ANY` | lookup-id equality on `cases` | btree |
| `zaaknummer ILIKE '%…%'` | `cases.case_number` | `case_idx_case_number_trgm` (NEW — legacy seq-scanned 3.7M rows) |
| `date_decision/date_published` ranges + `ORDER BY date_decision DESC, ecli` | `cases.date_decision/date_published` | `case_idx_date_ecli` (NEW, matches the sort exactly) |
| keywords via `fulltext_tsv @@` (+ 30k CTE cap) | `case_text.fulltext_tsv` + `summary_tsv` | GINs |
| articles: `summary ILIKE` / unnest(legal_provisions) / fulltext ILIKE | `summary_tsv @@` (NEW, indexed) + `case_law_reference` raw columns | GIN + `case_law_reference_idx_raw` |
| `bwb_resources` via `rs_document_law_reference` | `case_law_reference WHERE raw_scheme='bwb' AND raw_resource = ANY(…)` | `case_law_reference_idx_raw` |
| `journal_abbrs` via `rs_document_publication` | same table | `rs_document_publication_idx_journal` (NEW) |
| `include_depublicated` | `rs_document.opendata_status` (46 depublicated rows — no index needed) | — |
| edges: `rs_edge` by ecli + `relation_type`/`source` filters | `case_citation` by case id + `relation_type` + `source_dataset` | source/target/relation indexes |

### GET/POST /api/links, /api/links/laws, /api/links/cases

Legacy queries the LIDO staging trio (`law_alias`, `law_element`,
`case_law ⋈ legal_case`). New equivalents:

| Legacy | New |
|---|---|
| `law_alias` ILIKE searches (trie + containment) | `legislation_alias` — `lower(alias)` btree + **trgm GIN (NEW)** |
| `law_element (bwb_id, bwb_label_id)` lookups, `(type, lower(number))` | `legislation (scheme,identifier)` + `legal_provision (legislation_id, lower(article_label), element_type)` + `bwb_label_id` index |
| `case_law ⋈ legal_case` counts per (bwb_id, bwb_label_id), `STRING_AGG(source)` | `case_law_reference WHERE raw_scheme='bwb' AND (raw_resource, raw_label_id) = …` grouped; `source_dataset` aggregates | `case_law_reference_idx_raw_label` (NEW) |

Note: the `case_law` registry (10.2M rows) spans corpora (incl. 83k ECHR +
42.6k EU ECLIs) — after fold-in (MIGRATION_MAPPING §5.6) `/api/links/cases`
can return CJEU/ECHR cases citing Dutch law, which legacy returned as bare
ECLI strings with no metadata.

### POST /api/combined + /api/combined/expand

Legacy: `echr_edge UNION rs_edge`, two key systems (itemid vs ecli), ES+PG
federation. New: **one** `case_citation` table, one int key system, filter
by `source_dataset`/`relation_type` — and the 161,922 cross-corpus edges
(rs→ECHR 50,920, rs→CJEU 111,002 — verified live) actually **resolve** to
target cases instead of dangling. `/api/combined/expand` becomes a single
indexed query regardless of corpus.

### /api/summaries

`case_summary_version` with the partial unique index (one current,
non-rejected summary per case × scope × model) — adopted verbatim from the
interim draft.

---

## Improvements the new schema unlocks (beyond parity)

1. **Article search goes from regex scan → indexed lookup** (932k normalized
   rows already exist in production; legacy API just never used them).
2. **Multi-respondent bug fixed**: legacy `respondent = ANY('RUS')` misses
   `'MDA;RUS'` (219 such rows, verified). Mapping now explodes respondent
   into `case_party` (role `respondent_state`) → correct indexed matching.
   The raw string stays on `echr_document.respondent` for display.
3. **ECHR dates recovered from ECLI**: only 35% of ECHR cases have
   `judgementdate`/`referencedate`, but the ECLI itself encodes the decision
   date (`ECLI:CE:ECHR:2020:0206JUD…` → 2020-02-06). Loader parses it —
   date coverage goes from ~35% to ~100% of ECLI-bearing cases.
4. **Cross-corpus citation graph** — one table, int keys, resolvable
   cross-jurisdiction edges with `is_cross_jurisdiction=true`.
5. **Appno lookups actually use an index** (legacy's expression index never
   matched the query).
6. **Keyset pagination has an exact-match index** (`date_decision DESC, ecli`).

## API changes required at cutover (breaking, must be planned)

- Table names: `cases` + per-corpus satellites; text lives in `case_text`.
- Language codes: lowercase ISO 639-1 everywhere (`ENG`→`en`).
- ECHR "best language variant" selection: use `cases.item_id` join instead
  of `DISTINCT ON … ORDER BY ENG-first`.
- Edges: itemid/ecli keys → `case_id` ints (resolve once at the API edge,
  or query through `cases.ecli`/`echr_document.item_id`).
- Citation counts: read `case_citation_counts` (trigger-maintained).
