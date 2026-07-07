-- GENERATED from schema_full.sql for chartdb/drawdb import. DO NOT DEPLOY.

-- =============================================================================
-- CLE unified schema — full target (CJEU + ECHR + Rechtspraak)
-- =============================================================================
-- Consolidates:
--   1. The prior ideal draft (shared "case + extension" architecture)
--   2. The legacy production schema (docs/postgres-schema/legacy.sql) — every
--      column, trigger, view, and index that carries real information
--   3. The CJEU additions we agreed on
--
-- Design rules
--   • Full text lives in ONE shared table:      case_text  (one row per case × language)
--   • Citations live in ONE shared table:       case_citation
--   • Materialised counts live in:              case_citation_counts (trigger-maintained)
--   • Corpus-specific structured metadata:      echr_document, rs_document, cjeu_document,
--                                                cjeu_national_document, cjeu_ag_opinion
--   • Corpus-specific detail tables kept when they carry semantics the shared
--     model can't express cleanly:
--       ECHR: echr_document_appno, echr_document_article, echr_extractor_segments
--       RS:   rs_document_publication, rs_document_formal_relation,
--             rs_document_external_authority
--             (legacy rs_law_element / rs_law_alias fold into the generic
--              legislation tables; legacy rs_document_law_reference folds
--              into the shared case_law_reference — see those sections)
--   • ECLI is the natural business key on `case`; internal integer id is the FK anchor.
--   • Legacy staging tables (case_law, legal_case, law_*, ecli_*) are NOT copied — the
--     new model absorbs them via the shared tables + rs_law_* tables.
--
-- Decisions are finalized — see docs/postgres-schema/DECISIONS.md for the
-- full log. The load-bearing ones:
--   • DEPLOY TARGET: a dedicated schema (suggested name: cle_v2), NOT public —
--     table names collide with the legacy tables (echr_document, rs_document, …).
--     This file says "public" only so visualization tools import it cleanly;
--     at apply time run:  sed 's/"public"/"cle_v2"/g' schema_full.sql | psql …
--   • LANGUAGE CODES: lowercase ISO 639-1 everywhere ('en','fr','nl','bg',…),
--     normalized at load (HUDOC ENG→en, FRE→fr; CJEU EN→en). Seeded below.
--   • fulltext_tsv: stays a GENERATED column; the loader truncates input past
--     ~1M chars to stay under the 1 MB tsvector limit. Summary is searched
--     separately by the API (no legacy-RS-style fold-in).
--   • relation_type / role: plain text for the first full load; CHECK
--     constraints added once the value sets are empirically stable.
--   • Embeddings stay vector(768) (legacy model dimension).
-- =============================================================================


-- =============================================================================
-- Extensions (from legacy production)
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS "public";

   -- gin_trgm_ops on docname / issue
    -- pgvector — HNSW on embeddings


-- =============================================================================
-- Utility functions (adapted from legacy)
-- =============================================================================

-- Generic updated_at touch trigger. Replaces echr_touch_updated_at +
-- rs_touch_updated_at from legacy with one shared function.


-- Date-to-ISO helper preserved from legacy — used by the
-- rs_v_document_law_reference view to build version-dated deeplink URLs.


-- Generalised citation-count maintenance. One function drives
-- case_citation_counts for all three corpora (replaces the two per-corpus
-- functions in legacy). Trigger definition further below.


-- =============================================================================
-- Lookups
-- =============================================================================

-- DECIDED: canonical language codes are lowercase ISO 639-1 ('en', 'fr',
-- 'nl', 'bg', …), normalized at load time. Everything that joins on
-- language (case_text UNIQUE, echr_document PK, the views, all FKs to
-- this table) uses this convention.
-- CAUTION (verified against live legacy data): HUDOC carries translations
-- in 30+ languages beyond the EU 24 (TUR, RUS, UKR, SRP, …) and uses ISO
-- 639-2/B codes (FRE→fr, GER→de, RUM→ro, CZE→cs). The seed below covers
-- the EU 24 only — the loader MUST upsert unseen languages
-- (INSERT … ON CONFLICT DO NOTHING) before inserting dependent rows.
CREATE TABLE "public"."language" (
    "iso_code" text NOT NULL,
    "name" text,
    PRIMARY KEY ("iso_code")
);

CREATE TABLE "public"."jurisdiction" (
    "id" bigserial NOT NULL,
    "iso_code" text UNIQUE,
    "name" text,
    "type" text,                     -- e.g. 'country' | 'supranational' | 'international'
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."court" (
    "id" bigserial NOT NULL,
    "code" text UNIQUE,
    "name" text,
    "level" text,
    "jurisdiction_id" bigint,
    "parent_court_id" bigint,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."instance" (
    "id" bigserial NOT NULL,
    "code" text UNIQUE,
    "name" text,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."document_type" (
    "id" bigserial NOT NULL,
    "code" text UNIQUE,
    "name" text,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."procedure_type" (
    "id" bigserial NOT NULL,
    "code" text UNIQUE,
    "name" text,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."judge" (
    "id" bigserial NOT NULL,
    "full_name" text,
    "aliases" text[],
    "court_id" bigint,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."party" (
    "id" bigserial NOT NULL,
    "canonical_name" text,
    "aliases" text[],
    "role_class" text,
    "country_iso" text,
    PRIMARY KEY ("id")
);

-- Legislation catalog is multi-jurisdiction. Dutch legislation (BWB register,
-- enriched with LIDO / JuriConnect identifiers — the legacy rs_law_element /
-- rs_law_alias tables) folds in here rather than living as rs_* tables:
-- it is jurisdiction-level reference data, not Rechtspraak-corpus metadata.
--   legacy rs_law_element type='wet'   → legislation  (scheme='bwb')
--   legacy rs_law_element other types  → legal_provision (element_type + parent_id hierarchy)
--   legacy rs_law_alias                → legislation_alias
CREATE TABLE "public"."legislation" (
    "id" bigserial NOT NULL,
    "identifier" text,               -- CELEX number | BWB id | treaty id
    "scheme" text,                   -- 'celex' | 'bwb' | 'echr_treaty' | ...
    "title" text,
    "jurisdiction_id" bigint,
    "document_type" text,
    "enacted_date" date,
    "lido_id" text UNIQUE,           -- LIDO URI id (Dutch laws; was rs_law_element.lido_id)
    "jc_id" text UNIQUE,             -- JuriConnect id (Dutch laws; was rs_law_element.jc_id)
    "snapshot_date" date,            -- catalog snapshot (was rs_law_element.snapshot_date)
    PRIMARY KEY ("id")
);
CREATE INDEX "legislation_idx_scheme_identifier" ON "public"."legislation" ("scheme", "identifier");

CREATE TABLE "public"."legislation_alias" (
    "id" bigserial NOT NULL,
    "legislation_id" bigint,
    "alias" text,
    "source" text,                   -- 'opschrift' | 'bwbidlist' | ...
    PRIMARY KEY ("id")
);
CREATE INDEX "legislation_alias_idx_alias_lower" ON "public"."legislation_alias" (lower("alias"));
-- linkextractor alias containment search (ILIKE with leading wildcard)
CREATE INDEX "legislation_alias_idx_alias_trgm"  ON "public"."legislation_alias" USING gin ("alias" gin_trgm_ops);

CREATE TABLE "public"."legal_provision" (
    "id" bigserial NOT NULL,
    "legislation_id" bigint,
    "parent_id" bigint,              -- self-FK: nested structure (NL: boek→titeldeel→artikel; EU: article→paragraph→annex)
    "element_type" text,             -- NL: 'boek'|'deel'|'titeldeel'|'hoofdstuk'|'afdeling'|'paragraaf'|'subparagraaf'|'artikel'; EU: 'article'|'paragraph'|'annex'
    "article_label" text,            -- was rs_law_element.number
    "title" text,                    -- display label (was rs_law_element.title)
    "paragraph" text,
    "text" text,
    "bwb_label_id" bigint,           -- BWB label id — join key from case_law_reference.raw_label_id
    "lido_id" text UNIQUE,
    "jc_id" text UNIQUE,
    "effective_from" date,
    "effective_to" date,
    "snapshot_date" date,
    PRIMARY KEY ("id")
);
CREATE INDEX "legal_provision_idx_bwb_label" ON "public"."legal_provision" ("bwb_label_id");
CREATE INDEX "legal_provision_idx_lookup"    ON "public"."legal_provision" ("legislation_id", lower("article_label"), "element_type");

CREATE TABLE "public"."domain" (
    "id" bigserial NOT NULL,
    "scheme" text,                   -- 'eurovoc' | 'cjeu_subject_matter' | 'cjeu_keyword' | 'cjeu_directory_code' | 'rs_domain' | 'echr_article' | ...
    "name" text,                     -- canonical label (English for eurovoc)
    "uri" text,                      -- concept URI (e.g. http://eurovoc.europa.eu/<id>) — the stable join key for thesaurus ingests
    "parent_id" bigint,                 -- hierarchy (eurovoc broader-term, directory-code nesting)
    PRIMARY KEY ("id")
);

-- Multilingual labels for domain terms. Empty until the EuroVoc ingest runs
-- (see DECISIONS.md → "EuroVoc label ingest"): the Publications Office SKOS
-- distribution carries ~7k concepts × 24 language prefLabels (~170k rows).
-- Any scheme can use it; eurovoc is the first customer.
CREATE TABLE "public"."domain_label" (
    "domain_id" bigint NOT NULL,
    "language" text NOT NULL,
    "label" text NOT NULL,
    PRIMARY KEY ("domain_id", "language")
);

-- CJEU-specific formation lookup (~15 rows, seeded at bottom)
CREATE TABLE "public"."court_formation" (
    "id" bigserial NOT NULL,
    "code" text UNIQUE,              -- 'GC' | 'FC' | '1C' | ... | 'PR' | 'SOLE'
    "label" text,
    "judge_count" smallint,
    PRIMARY KEY ("id")
);


-- =============================================================================
-- Core case data (shared across CJEU / ECHR / Rechtspraak)
-- =============================================================================

CREATE TABLE "public"."cases" (
    "id" bigserial NOT NULL,
    "ecli" text UNIQUE,
    "item_id" text UNIQUE,           -- external primary identifier (HUDOC itemid, CELLAR cellar_id, RS ecli)
    "sources" text[] NOT NULL,       -- corpus coverage, e.g. '{RS}' or '{RS,CJEU}' (D13).
                                     -- sources[1] is the ORIGIN corpus: the loader that
                                     -- created the row and populated the shared columns;
                                     -- later corpora APPEND when their satellite attaches.
                                     -- Loaders set it during bulk load; the
                                     -- trg_*_sources_attach/detach triggers keep it in
                                     -- sync with satellite existence afterwards.
    "celex_id" text UNIQUE,
    "title" text,                    -- RS/ECHR: native title. CJEU: synthesized by the
                                     -- loader ("C-123/22, X v Y") — CELLAR's work_title
                                     -- is populated for only 1.3% of cases
    "date_decision" date,
    "date_published" date,
    "court_id" bigint,
    "language_iso" text,             -- procedure language (best-guess primary)
    "document_type_id" bigint,
    "procedure_type_id" bigint,
    "instance_id" bigint,
    "case_number" text,
    "importance" smallint,           -- harmonized 1–4 scale, 1 = most important
                                     -- (ECHR/HUDOC convention). ECHR: HUDOC value as-is.
                                     -- RS: native importance mapped to 1–4.
                                     -- CJEU: formation-based proxy set by the loader
                                     -- (Full Court/Grand Chamber→1, 5-judge→2,
                                     --  3-judge→3, sole judge/order→4)
    "is_landmark" boolean,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id")
);
CREATE INDEX "case_idx_court"          ON "public"."cases" ("court_id");
CREATE INDEX "case_idx_date_decision"  ON "public"."cases" ("date_decision");
CREATE INDEX "case_idx_ecli"           ON "public"."cases" ("ecli");
CREATE INDEX "case_idx_sources"        ON "public"."cases" USING gin ("sources");
CREATE INDEX "case_idx_item_id"        ON "public"."cases" ("item_id");
CREATE INDEX "case_idx_title_trgm"     ON "public"."cases" USING gin ("title" gin_trgm_ops);
-- API keyset pagination: ORDER BY date_decision DESC NULLS LAST, ecli
CREATE INDEX "case_idx_date_ecli"      ON "public"."cases" ("date_decision" DESC, "ecli");
CREATE INDEX "case_idx_importance"     ON "public"."cases" ("importance") WHERE "importance" IS NOT NULL;
-- API zaaknummer/case-number substring search (ILIKE '%…%')
CREATE INDEX "case_idx_case_number_trgm" ON "public"."cases" USING gin ("case_number" gin_trgm_ops);
CREATE INDEX "case_idx_case_number" ON "public"."cases" ("case_number");

CREATE TABLE "public"."case_text" (
    "id" bigserial NOT NULL,
    "case_id" bigint NOT NULL,
    "language" text NOT NULL,
    "fulltext" text,
    "summary" text,
    "summary_source" text,                                 -- provenance of the upstream summary (CELLAR / RS inhoudsindicatie / …)
    -- DECIDED: fulltext_tsv stays a GENERATED column (covers fulltext only).
    -- The loader truncates to_tsvector input past ~1M chars so oversized
    -- judgments can't trip the 1 MB tsvector limit and fail the INSERT
    -- (word positions clamp at 16,383 anyway, so the search-quality loss is
    -- negligible). Unlike the legacy RS trigger, summary and legal
    -- provisions are NOT folded into this vector — the API queries summary
    -- separately.
    "fulltext_tsv" tsvector,
    "summary_tsv" tsvector,
    "summary_embedding" vector(768),                       -- DECIDED: 768 (legacy model dimension); a model switch means an index rebuild, decide before bulk load
    "embedding_model" text,
    "source" text NOT NULL,                                -- 'INFOCURIA_BLOB_HTML' | 'CELLAR_ITEM' | 'EXTRACTOR_FALLBACK_TEXT' | 'HUDOC' | 'RECHTSPRAAK' | ...
    "text_format" text,                                    -- xhtml | html | pdf | fmx4
    "missing_reasons" text,                                -- 'FULLTEXT_UNAVAILABLE_UPSTREAM' | ...
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id"),
    -- D12: one row per case × language × SOURCE (dual-source cross-corpus texts)
    UNIQUE ("case_id", "language", "source")
);
CREATE INDEX "case_text_idx_case_id"          ON "public"."case_text" ("case_id");
CREATE INDEX "case_text_idx_fulltext_tsv"     ON "public"."case_text" USING gin ("fulltext_tsv");
CREATE INDEX "case_text_idx_summary_tsv"      ON "public"."case_text" USING gin ("summary_tsv");
CREATE INDEX "case_text_idx_summary_embedding" ON "public"."case_text" USING hnsw ("summary_embedding" vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);


CREATE TABLE "public"."case_judge" (
    "id" bigserial NOT NULL,
    "case_id" bigint,
    "judge_id" bigint,
    "role" text,                     -- 'rapporteur' | 'president' | 'judge' | ...
    PRIMARY KEY ("id"),
    UNIQUE ("case_id", "judge_id", "role")
);
CREATE INDEX "case_judge_idx_case_id"  ON "public"."case_judge" ("case_id");
CREATE INDEX "case_judge_idx_judge_id" ON "public"."case_judge" ("judge_id");

CREATE TABLE "public"."case_party" (
    "case_id" bigint,
    "party_id" bigint,
    "role" text,                     -- 'applicant' | 'defendant' | 'referring_state' | 'defendant_agent' | ...
    "ordinal" smallint DEFAULT 1,    -- disambiguate multiple parties in same role
    PRIMARY KEY ("case_id", "party_id", "role", "ordinal")
);
CREATE INDEX "case_party_idx_party" ON "public"."case_party" ("party_id");

CREATE TABLE "public"."case_domain" (
    "case_id" bigint,
    "domain_id" bigint,
    PRIMARY KEY ("case_id", "domain_id")
);
CREATE INDEX "case_domain_idx_domain_id" ON "public"."case_domain" ("domain_id");

-- THE single case→law reference table for all corpora. Mirrors the
-- case_citation design: resolved FK targets and raw source-shaped targets
-- live side by side in one table (there is deliberately NO per-corpus
-- rs_document_law_reference — Dutch BWB references load here with
-- raw_scheme='bwb'; the legacy API shape incl. deeplink URLs is
-- reconstructed by the rs_v_document_law_reference view).
CREATE TABLE "public"."case_law_reference" (
    "id" bigserial NOT NULL,
    "case_id" bigint NOT NULL,
    "legislation_id" bigint,         -- resolved act (nullable until resolution)
    "provision_id" bigint,           -- resolved specific provision (nullable: courts often cite whole acts)
    "raw_scheme" text,               -- 'bwb' | 'celex' | 'echr_treaty' | ... (identifier system of the raw target)
    "raw_resource" text,             -- raw act identifier as cited ('BWBR0005290', '32016R0679')
    "raw_subdivision" text,          -- raw element as cited ('658', '6-1', 'Bijlage II')
    "raw_label_id" bigint,           -- numeric sub-identifier in the raw scheme (BWB label id; resolution join key)
    "raw_reference" text,            -- verbatim citation string (RS 'opschrift', CDM literal, …)
    "version_date" date,             -- temporal pin of the cited version (BWB version date; EU consolidated-version date)
    "role" text NOT NULL DEFAULT 'cited',
    -- Allowed values (advisory — not enforced until value set stabilises)
    -- CJEU adds: based_on_treaty, legal_basis, affects, amends, amends_by_correction,
    --           confirms, interprets, interprets_judgement, declares_void,
    --           declares_void_by_preliminary_ruling, incidentally_declares_void,
    --           declares_valid, declares_incidentally_valid, states_failure,
    --           suspends_application, immediately_enforces, incorporates, corrects
    -- RS adds:  applied, cited
    -- ECHR adds: applied, violation, nonviolation
    "source_dataset" text NOT NULL,  -- provenance: 'cellar_sparql' | 'rs_lido_ref' | 'rs_lido_linkt' | 'echr_document_article' | ...
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id")
);
CREATE INDEX "case_law_reference_idx_case_id"     ON "public"."case_law_reference" ("case_id");
CREATE INDEX "case_law_reference_idx_legislation" ON "public"."case_law_reference" ("legislation_id");
CREATE INDEX "case_law_reference_idx_provision"   ON "public"."case_law_reference" ("provision_id");
CREATE INDEX "case_law_reference_idx_raw"         ON "public"."case_law_reference" ("raw_scheme", "raw_resource");
-- /api/links/laws + /api/links/cases: lookups/counts by BWB label id
CREATE INDEX "case_law_reference_idx_raw_label"   ON "public"."case_law_reference" ("raw_label_id") WHERE "raw_label_id" IS NOT NULL;
-- Dedup per resolution state (same pattern as case_citation — a single
-- UNIQUE constraint would treat NULL targets as always-distinct):
CREATE UNIQUE INDEX "case_law_reference_uk_provision" ON "public"."case_law_reference"
    ("case_id", "provision_id", "role", "source_dataset")
    WHERE "provision_id" IS NOT NULL AND "raw_resource" IS NULL;
CREATE UNIQUE INDEX "case_law_reference_uk_legislation" ON "public"."case_law_reference"
    ("case_id", "legislation_id", "role", "source_dataset")
    WHERE "provision_id" IS NULL AND "legislation_id" IS NOT NULL AND "raw_resource" IS NULL;
CREATE UNIQUE INDEX "case_law_reference_uk_raw" ON "public"."case_law_reference"
    ("case_id", "raw_scheme", "raw_resource", COALESCE("raw_subdivision", ''), "role", "source_dataset")
    WHERE "provision_id" IS NULL AND "legislation_id" IS NULL AND "raw_resource" IS NOT NULL;

CREATE TABLE "public"."case_citation" (
    "id" bigserial NOT NULL,
    "source_case_id" bigint,
    "target_case_id" bigint,                   -- nullable if target not yet in `case`
    "target_ecli_raw" text,                    -- unresolved ECLI (RS/ECHR) when target_case_id IS NULL
    "target_celex_raw" text,                   -- unresolved CELEX (CJEU) when target_case_id IS NULL
    "relation_type" text,
    -- Union of legacy relation types across corpora:
    --   Shared: 'cites' | 'cited_by'
    --   CJEU:  'joins' | 'subject_to_appeal' | 'reexamined_by' |
    --          'is_about_concept' | 'referred_for_preliminary_ruling' |
    --          'interprets_judgement' | 'logical_successor_of'
    --   ECHR:  (echr_edge is untyped; use 'cites')
    --   RS:    values from rs_document_formal_relation.relation_type
    "source_dataset" text NOT NULL,            -- 'cellar_sparql' | 'echr_edge' | 'rs_edge' | 'rs_formal_relation' | 'lido'
    "weight" int DEFAULT 1,                    -- ECHR/RS: how many times cited in body text
    "context_segment_id" bigint,               -- text-extracted (RS body-cite): the segment where the cite lives
    "is_cross_jurisdiction" boolean DEFAULT false,
    "extractor_at" timestamptz DEFAULT now(),
    "extractor_version" text,
    PRIMARY KEY ("id")
);
-- Dedup is enforced per resolution state. A single UNIQUE constraint over
-- (source, target, relation, dataset) would NOT deduplicate unresolved
-- citations — Postgres treats NULL target_case_id as always-distinct.
CREATE UNIQUE INDEX "case_citation_uk_resolved" ON "public"."case_citation"
    ("source_case_id", "target_case_id", "relation_type", "source_dataset")
    WHERE "target_case_id" IS NOT NULL;
CREATE UNIQUE INDEX "case_citation_uk_unresolved_celex" ON "public"."case_citation"
    ("source_case_id", "target_celex_raw", "relation_type", "source_dataset")
    WHERE "target_case_id" IS NULL AND "target_celex_raw" IS NOT NULL;
CREATE UNIQUE INDEX "case_citation_uk_unresolved_ecli" ON "public"."case_citation"
    ("source_case_id", "target_ecli_raw", "relation_type", "source_dataset")
    WHERE "target_case_id" IS NULL AND "target_ecli_raw" IS NOT NULL;
CREATE INDEX "case_citation_idx_source"        ON "public"."case_citation" ("source_case_id");
CREATE INDEX "case_citation_idx_target"        ON "public"."case_citation" ("target_case_id");
CREATE INDEX "case_citation_idx_relation_type" ON "public"."case_citation" ("relation_type");
CREATE INDEX "case_citation_idx_source_target" ON "public"."case_citation" ("source_case_id", "target_case_id");
CREATE INDEX "case_citation_idx_weight"        ON "public"."case_citation" ("weight") WHERE weight > 1;
CREATE INDEX "case_citation_idx_target_ecli_raw"  ON "public"."case_citation" ("target_ecli_raw")  WHERE target_ecli_raw  IS NOT NULL;
CREATE INDEX "case_citation_idx_target_celex_raw" ON "public"."case_citation" ("target_celex_raw") WHERE target_celex_raw IS NOT NULL;

CREATE TABLE "public"."case_citation_counts" (
    -- Pre-computed cite counts per case. Maintained by trigger on case_citation.
    -- Replaces per-corpus echr_citation_counts + rs_citation_counts from legacy.
    "case_id" bigint NOT NULL,
    "cites_count" int DEFAULT 0 NOT NULL,
    "cited_by_count" int DEFAULT 0 NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("case_id")
);


-- =============================================================================
-- CJEU-specific extensions
-- =============================================================================

CREATE TABLE "public"."cjeu_document" (
    "id" bigserial NOT NULL,
    "case_id" bigint NOT NULL,
    "celex_id" text,                 -- denormalized from cases.celex_id for convenience
    "ecli" text,                     -- denormalized from cases.ecli
    "sector" text,                   -- '6' (CJEU direct) | '8' (national CJEU-referred)
    "case_number" text,
    "formation_id" bigint,              -- FK → court_formation (replaces legacy typo "formation timestamp")
    "proc_type" text,
    "procedure_result" text,         -- 'successful' | 'unfounded' | 'inadmissible' (parsed from type_procedure)
    -- (legacy draft had subject_matter + parties_text here — dropped: no CJEU
    --  data loaded yet, so these go straight to case_domain / case_party)
    "date_lodged" date,
    "cellar_uri" text,
    "work_uri" text,
    "journal_refs" text,             -- OJ references
    "erecueil_ref" text,             -- European Court Reports citation
    "local_identifier" text,
    "citations_extra_info" text,     -- cited-case names + outcome descriptors (72% populated; outcome parse = future)
    "national_judgement_xml" text,   -- raw XML of national proceedings behind preliminary rulings (22.5%; cross-corpus fanout = future)
    "dossier_uri" text,              -- groups Opinion + Judgment + Order (18.8% populated)
    "dossier_parent_case_id" bigint, -- resolved post-load
    PRIMARY KEY ("id"),
    UNIQUE ("case_id")
);

CREATE TABLE "public"."cjeu_ag_opinion" (
    "id" bigserial NOT NULL,
    "case_id" bigint NOT NULL,
    "parent_case_id" bigint,         -- the judgment this opinion is for
    "advocate_general" text,
    "opinion_uri" text,
    "delivered_date" date,
    PRIMARY KEY ("id"),
    UNIQUE ("case_id")
);

-- Sector-8 satellite (~1,800 CJEU cases involving national court referrals)
CREATE TABLE "public"."cjeu_national_document" (
    "id" bigserial NOT NULL,
    "case_id" bigint NOT NULL,
    "national_court_uri" text,
    "national_decision_internal_id" text,
    "national_parties_raw" text,
    "national_keywords" text,
    "national_reference_publication" text,
    "national_reference_publication_conclusion" text,
    "national_follow_up" text,
    "national_judgement_reference" text,
    "national_act_reference_national" text,
    "national_act_reference_international" text,
    "national_act_reference_european" text,
    "national_based_on_resource_legal" text, -- CELLAR URI(s) of the EU act the national decision was based on
    PRIMARY KEY ("id"),
    UNIQUE ("case_id")
);


-- =============================================================================
-- ECHR-specific extensions — enriched from legacy production
-- =============================================================================

CREATE TABLE "public"."echr_document" (
    -- One row per HUDOC document variant (per language, sometimes several per
    -- (case, language)). Preserves every HUDOC variant.
    -- IMPORTANT (verified against live legacy data): each language variant has
    -- its OWN HUDOC itemid — itemid does NOT identify the case. The per-variant
    -- itemid lives here; cases.item_id holds the canonical one, selected
    -- deterministically: ENG variant (76% of cases) → FRE (24%, the
    -- French-only Commission era) → any other language (10 cases); within a
    -- language, doctype rank JUD > DEC > COM > other, then lowest itemid.
    -- echr_edge citation itemids resolve through THIS column (~95% ENG).
    "item_id" text NOT NULL,                               -- this variant's HUDOC itemid — the true unique key
    "case_id" bigint NOT NULL,
    "language" text NOT NULL,                              -- normalized from HUDOC languageisocode (30+ observed: ENG, FRE, TUR, RUS, UKR, RUM, GER, CZE, …)
    "extractedappno" text,
    "docname" text,
    "doctype" text,
    "doctype_branch" text,                                 -- was: doctypebranch
    "judgement_date" timestamptz,                          -- was: judgementdate
    "reference_date" timestamptz,                          -- was: referencedate
    "article" text,                                        -- raw article field
    "conclusion" text,
    "violation" text,
    "nonviolation" text,
    "respondent" text,
    "originating_body" int,                                -- was: originatingbody (integer)
    "represented_by" text,                                 -- was: representedby
    "published_by" text,                                   -- was: publishedby
    "rules_of_court" text,                                 -- was: rulesofcourt
    "applicability" text,
    "separate_opinion" text,                               -- was: separateopinion
    "issue" text,
    "importance" smallint,                                 -- HUDOC importance level (1-4)
    "rank" numeric,
    "scl" text,                                            -- raw case references field
    "external_sources" text,                               -- was: externalsources
    "judgement_year" int,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    -- PK is item_id, NOT (case_id, language): verified against live data,
    -- 3,261 (ecli, language) pairs carry MULTIPLE variants (6,901 rows —
    -- e.g. two admissibility decisions sharing one ECLI+ENG). All variants
    -- are preserved; case_text picks one text per (case, language).
    PRIMARY KEY ("item_id")
);
CREATE INDEX "echr_document_idx_case_lang" ON "public"."echr_document" ("case_id", "language");
CREATE INDEX "echr_document_idx_doctype"          ON "public"."echr_document" ("doctype");
CREATE INDEX "echr_document_idx_doctype_branch"   ON "public"."echr_document" ("doctype_branch");
CREATE INDEX "echr_document_idx_judgement_date"   ON "public"."echr_document" ("judgement_date");
CREATE INDEX "echr_document_idx_reference_date"   ON "public"."echr_document" ("reference_date");
CREATE INDEX "echr_document_idx_judgement_year"   ON "public"."echr_document" ("judgement_year");
CREATE INDEX "echr_document_idx_originating_body" ON "public"."echr_document" ("originating_body");
CREATE INDEX "echr_document_idx_docname_trgm"     ON "public"."echr_document" USING gin ("docname" gin_trgm_ops);
CREATE INDEX "echr_document_idx_issue_trgm"       ON "public"."echr_document" USING gin ("issue" gin_trgm_ops);


CREATE TABLE "public"."echr_document_appno" (
    -- Normalized appnos — one row per (variant × appno × source), anchored on
    -- the variant's HUDOC item_id (mirrors the legacy key structure).
    -- 'source' distinguishes case's own appno from those parsed from references.
    "item_id" text NOT NULL,
    "appno" text NOT NULL,
    "source" text NOT NULL,                                -- 'appno' | 'extractedappno'
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("item_id", "appno", "source")
);
CREATE INDEX "echr_document_appno_idx_appno"      ON "public"."echr_document_appno" ("appno");
CREATE INDEX "echr_document_appno_idx_source"     ON "public"."echr_document_appno" ("source");

CREATE TABLE "public"."echr_document_article" (
    -- ECHR Convention articles applied to / violated by / not-violated by this
    -- case, anchored on the variant's HUDOC item_id.
    "item_id" text NOT NULL,
    "kind" text NOT NULL,                                  -- 'applied' | 'violation' | 'nonviolation'
    "article_code" text NOT NULL,                          -- e.g. '6' | '6-1' | '13' | 'P1-1'
    "protocol" text,                                       -- extracted protocol ('P1', 'P4', …) — NULL for Convention articles
    "raw" text,                                            -- verbatim source fragment the row was parsed from
    PRIMARY KEY ("item_id", "kind", "article_code"),
    CONSTRAINT echr_document_article_kind_check
        CHECK ("kind" IN ('applied', 'violation', 'nonviolation'))
);
CREATE INDEX "echr_document_article_idx_filter" ON "public"."echr_document_article" ("kind", "article_code");

CREATE TABLE "public"."echr_extractor_segments" (
    -- Section-level text extraction (procedure / facts / law / operative / …).
    -- Preserved from legacy — this is a segmentation that carries ECHR-specific
    -- semantics not captured by the generic case_segment table.
    "item_id" text NOT NULL,
    "parser_mode" text,
    "error" text,
    "procedure" text,
    "facts" text,
    "complaints" text,
    "law" text,
    "operative" text,
    "subject_matter" text,
    "court_assessment" text,
    "separate_opinion" text,
    "appendix" text,
    "num_sections" int DEFAULT 0 NOT NULL,
    "segmented_at" timestamptz DEFAULT now() NOT NULL,
    "extractor_version" text,
    PRIMARY KEY ("item_id")
);

CREATE TABLE "public"."echr_document_secondary_text" (
    -- Fulltexts of NON-canonical ECHR variants (see schema_full.sql).
    "item_id" text NOT NULL,
    "fulltext" text NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("item_id")
);
CREATE INDEX "echr_extractor_segments_idx_parser"      ON "public"."echr_extractor_segments" ("parser_mode");
CREATE INDEX "echr_extractor_segments_idx_num_sections" ON "public"."echr_extractor_segments" ("num_sections");


-- =============================================================================
-- Rechtspraak-specific extensions — enriched from legacy production
-- =============================================================================

CREATE TABLE "public"."rs_document" (
    -- One row per case_id. All Rechtspraak-specific metadata from legacy.
    "case_id" bigint PRIMARY KEY,
    "date_decision" date,                                  -- Rechtspraak's own date_decision (may differ from cases.date_decision if late correction)
    "document_type" text,
    "instance" text,
    "domains" text[],
    "source" text DEFAULT 'Rechtspraak' NOT NULL,
    "jurisdiction_country" text DEFAULT 'NL' NOT NULL,
    "procedure_type" text,
    "url_publication" text,
    -- NOTE: legacy rs_document.summary moved to case_text.summary (language='nl').
    -- The rs_v_document_with_text view re-exposes it for API compatibility.
    "legal_provisions" text[],                             -- denormalized display cache; canonical rows live in case_law_reference (raw_scheme='bwb')
    "predecessor_successor_cases" text,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    "date_published" date,
    "date_issued" date,
    "date_modified" timestamptz,
    "title" text,
    "language" text,
    "access_rights" text,
    "zittingsplaats" text,
    "replaces_identifier" text,
    "creator_uri" text,
    "vindplaatsen" text[],
    "subject_uris" text[],
    "zaaknummer" text,
    "opendata_status" text DEFAULT 'public' NOT NULL,
    CONSTRAINT rs_document_opendata_status_check
        CHECK ("opendata_status" IN ('public', 'depublicated'))
);
CREATE INDEX "rs_document_idx_date_decision" ON "public"."rs_document" ("date_decision");
-- /api/rechtspraak domains && ARRAY[...] filter
CREATE INDEX "rs_document_idx_domains_gin"   ON "public"."rs_document" USING gin ("domains");
CREATE INDEX "rs_document_idx_date_issued"   ON "public"."rs_document" ("date_issued");
CREATE INDEX "rs_document_idx_date_modified" ON "public"."rs_document" ("date_modified");


CREATE TABLE "public"."rs_document_external_authority" (
    "case_id" bigint NOT NULL,
    "kind" text DEFAULT 'other' NOT NULL,
    "name" text NOT NULL,
    "article" text,
    "raw" text NOT NULL,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("case_id", "raw")
);

CREATE TABLE "public"."rs_document_formal_relation" (
    -- Structured ECLI-to-ECLI relations from Rechtspraak's dcterms:relation.
    -- Also fanned out into case_citation for cross-corpus querying.
    "case_id" bigint NOT NULL,
    "target_ecli" text,                                    -- resolved when target is in `case`
    "target_identifier" text NOT NULL,                     -- raw target identifier from source
    "relation_type" text DEFAULT 'unknown' NOT NULL,
    "aanleg" text DEFAULT 'unknown' NOT NULL,              -- procedural stage (eerste aanleg / hoger beroep / cassatie / …)
    "name" text,
    "disposition" text,
    "gevolg" text,                                         -- outcome: 'vernietiging en zelf afgedaan' | 'gevolgd' | 'bekrachtiging/bevestiging' | 'niet ontvankelijk' | …
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("case_id", "target_identifier", "relation_type", "aanleg")
);

CREATE TABLE "public"."rs_document_publication" (
    "case_id" bigint NOT NULL,
    "raw" text NOT NULL,
    "kind" text DEFAULT 'other' NOT NULL,
    "journal_abbr" text,
    "year" int,
    "locator" text,
    "annotator" text,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("case_id", "raw")
);
CREATE INDEX "rs_document_publication_idx_journal" ON "public"."rs_document_publication" ("journal_abbr");

-- NOTE: legacy rs_document_law_reference is NOT ported as an rs_* table.
-- Dutch BWB references load into the shared case_law_reference with
-- raw_scheme='bwb' (raw_resource=bwb_resource, raw_subdivision=article,
-- raw_label_id=bwb_label_id, raw_reference=opschrift, version_date). The
-- legacy API shape — including the wetten.overheid.nl / LIDO deeplink
-- URLs, formerly GENERATED columns — is reconstructed by the
-- rs_v_document_law_reference view below.

-- NOTE: legacy rs_law_element and rs_law_alias are NOT ported as rs_* tables.
-- They are a catalog of Dutch LEGISLATION (BWB register + LIDO/JuriConnect
-- identifiers), not Rechtspraak-corpus metadata — they fold into the generic
-- legislation / legal_provision / legislation_alias tables (see the
-- Legislation section above and the migration notes at the bottom).


-- =============================================================================
-- Cross-corpus bridges
-- =============================================================================

CREATE TABLE "public"."lido_link" (
    -- LIDO (Dutch government) links: usually RS → ECLI or RS → BWB provision.
    -- Complements case_citation (structured cases) and case_law_reference
    -- (structured law refs); lido_link keeps the raw fetched URI shape.
    "id" bigserial NOT NULL,
    "source_case_id" bigint,
    "target_case_id" bigint,
    "source_ecli" text,
    "source_uri" text,
    "target_ecli" text,
    "target_uri" text,
    "target_provision_id" bigint,
    "link_type" text,
    "fetched_at" timestamptz DEFAULT now(),
    PRIMARY KEY ("id")
);
CREATE INDEX "lido_link_idx_source_case" ON "public"."lido_link" ("source_case_id");
CREATE INDEX "lido_link_idx_target_case" ON "public"."lido_link" ("target_case_id");


-- =============================================================================
-- Downstream analytics (populated by pipelines from case_text / case_citation)
-- =============================================================================

CREATE TABLE "public"."case_segment" (
    "id" bigserial NOT NULL,
    "case_id" bigint,
    "language" text,
    "segment_type" text,             -- 'paragraph' | 'sentence' | 'section' | ...
    "segment_index" int,
    "segment_text" text,
    "segment_hash" text,
    "embedding" vector(768),
    "embedding_model" text,
    "extractor_version" text,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id"),
    UNIQUE ("case_id", "segment_hash")
);
CREATE INDEX "case_segment_idx_case_id"        ON "public"."case_segment" ("case_id");
CREATE INDEX "case_segment_idx_embedding_hnsw" ON "public"."case_segment" USING hnsw ("embedding" vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE TABLE "public"."case_entity" (
    "id" bigserial NOT NULL,
    "case_id" bigint,
    "entity_type" text,
    "canonical_name" text,
    "surface_form" text,
    "uri" text,
    "char_start" int,
    "char_end" int,
    "confidence" real,
    PRIMARY KEY ("id")
);
CREATE INDEX "case_entity_idx_case_id" ON "public"."case_entity" ("case_id");

-- Versioned GENERATED summaries (LLM pipeline) with human-review workflow.
-- Distinct from case_text.summary, which holds upstream/source summaries.
-- Adopted from the interim schema draft (2026-07-06).
CREATE TABLE "public"."case_summary_version" (
    "id" bigserial NOT NULL,
    "case_id" bigint NOT NULL,
    "language" text,
    "summary_text" text NOT NULL,
    "summary_embedding" vector(768),
    "embedding_model" text,
    "summarization_model" text,
    "segment_scope" text NOT NULL,               -- what was summarized: 'full' | 'facts' | 'operative' | …
    "version_number" int NOT NULL DEFAULT 1,
    "is_current" boolean NOT NULL DEFAULT true,
    "generation_source" text NOT NULL,           -- pipeline/run identifier
    "rejected_at" timestamptz,
    "rejection_reason" text,
    "parent_version_id" bigint,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id")
);
-- One current, non-rejected summary per (case, scope, model)
CREATE UNIQUE INDEX "case_summary_version_uk_current"
    ON "public"."case_summary_version" ("case_id", "segment_scope", "summarization_model")
    WHERE "is_current" = true AND "rejected_at" IS NULL;
CREATE INDEX "case_summary_version_idx_case" ON "public"."case_summary_version" ("case_id");

CREATE TABLE "public"."case_cluster" (
    "id" bigserial NOT NULL,
    "snapshot_id" bigint,
    "algorithm" text,
    "label" text,
    "size" int,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."case_cluster_membership" (
    "cluster_id" bigint,
    "case_id" bigint,
    PRIMARY KEY ("cluster_id", "case_id")
);

CREATE TABLE "public"."case_network_metric" (
    "snapshot_id" bigint,
    "case_id" bigint,
    "in_degree" int,
    "out_degree" int,
    "pagerank" real,
    "betweenness" real,
    "hub_score" real,
    "authority_score" real,
    "eigenvector" real,
    PRIMARY KEY ("snapshot_id", "case_id")
);
CREATE INDEX "case_network_metric_idx_case" ON "public"."case_network_metric" ("case_id");

CREATE TABLE "public"."network_snapshot" (
    "id" bigserial NOT NULL,
    "snapshot_date" date,
    "description" text,
    "node_count" int,
    "edge_count" int,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."search_query_log" (
    "id" bigserial NOT NULL,
    "user_id" text,
    "raw_query" text,
    "parsed_intent" jsonb,
    "filters" jsonb,
    "strategy" text,
    "result_count" int,
    "clicked_case_ids" text[],
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id")
);


-- =============================================================================
-- Views (adapted from legacy)
-- =============================================================================

-- Convenience view: ECHR document + fulltext joined on (case_id, language).


-- Judgments and decisions only (excludes press releases / communications).


-- Rechtspraak document + fulltext (only Dutch text — RS is monolingual).
-- Re-exposes summary from case_text for API compatibility with the legacy
-- rs_document.summary column.


-- Legacy-shaped Dutch law-reference rows, reconstructed from the shared
-- case_law_reference (raw_scheme='bwb'). Replaces the legacy
-- rs_document_law_reference table 1:1 — including the two deeplink URL
-- columns that used to be GENERATED columns on that table.


-- Legal-provision display labels for /api/rechtspraak — mirrors the legacy view,
-- reading from the shared case_law_reference (raw_scheme='bwb').


-- =============================================================================
-- Foreign key constraints
-- =============================================================================

-- Lookups
ALTER TABLE "public"."court"             ADD CONSTRAINT fk_court_jurisdiction         FOREIGN KEY ("jurisdiction_id")     REFERENCES "public"."jurisdiction"("id");
ALTER TABLE "public"."court"             ADD CONSTRAINT fk_court_parent_court         FOREIGN KEY ("parent_court_id")     REFERENCES "public"."court"("id");
ALTER TABLE "public"."judge"             ADD CONSTRAINT fk_judge_court                FOREIGN KEY ("court_id")            REFERENCES "public"."court"("id");
ALTER TABLE "public"."party"             ADD CONSTRAINT fk_party_country              FOREIGN KEY ("country_iso")         REFERENCES "public"."jurisdiction"("iso_code");
ALTER TABLE "public"."legislation"       ADD CONSTRAINT fk_legislation_jurisdiction   FOREIGN KEY ("jurisdiction_id")     REFERENCES "public"."jurisdiction"("id");
ALTER TABLE "public"."legislation_alias" ADD CONSTRAINT fk_legislation_alias          FOREIGN KEY ("legislation_id")      REFERENCES "public"."legislation"("id");
ALTER TABLE "public"."legal_provision"   ADD CONSTRAINT fk_legal_provision            FOREIGN KEY ("legislation_id")      REFERENCES "public"."legislation"("id");
ALTER TABLE "public"."legal_provision"   ADD CONSTRAINT fk_legal_provision_parent     FOREIGN KEY ("parent_id")           REFERENCES "public"."legal_provision"("id");
ALTER TABLE "public"."domain"            ADD CONSTRAINT fk_domain_parent              FOREIGN KEY ("parent_id")           REFERENCES "public"."domain"("id");
ALTER TABLE "public"."domain_label"      ADD CONSTRAINT fk_domain_label_domain        FOREIGN KEY ("domain_id")           REFERENCES "public"."domain"("id") ON DELETE CASCADE;
ALTER TABLE "public"."domain_label"      ADD CONSTRAINT fk_domain_label_language      FOREIGN KEY ("language")            REFERENCES "public"."language"("iso_code");

-- Case & satellites
ALTER TABLE "public"."cases"              ADD CONSTRAINT fk_case_court                 FOREIGN KEY ("court_id")            REFERENCES "public"."court"("id");
ALTER TABLE "public"."cases"              ADD CONSTRAINT fk_case_document_type         FOREIGN KEY ("document_type_id")    REFERENCES "public"."document_type"("id");
ALTER TABLE "public"."cases"              ADD CONSTRAINT fk_case_procedure_type        FOREIGN KEY ("procedure_type_id")   REFERENCES "public"."procedure_type"("id");
ALTER TABLE "public"."cases"              ADD CONSTRAINT fk_case_instance              FOREIGN KEY ("instance_id")         REFERENCES "public"."instance"("id");
ALTER TABLE "public"."cases"              ADD CONSTRAINT fk_case_language              FOREIGN KEY ("language_iso")        REFERENCES "public"."language"("iso_code");

ALTER TABLE "public"."case_text"         ADD CONSTRAINT fk_case_text_case             FOREIGN KEY ("case_id")             REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_text"         ADD CONSTRAINT fk_case_text_language         FOREIGN KEY ("language")            REFERENCES "public"."language"("iso_code");

ALTER TABLE "public"."case_judge"        ADD CONSTRAINT fk_case_judge_case            FOREIGN KEY ("case_id")             REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_judge"        ADD CONSTRAINT fk_case_judge_judge           FOREIGN KEY ("judge_id")            REFERENCES "public"."judge"("id");

ALTER TABLE "public"."case_party"        ADD CONSTRAINT fk_case_party_case            FOREIGN KEY ("case_id")             REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_party"        ADD CONSTRAINT fk_case_party_party           FOREIGN KEY ("party_id")            REFERENCES "public"."party"("id");

ALTER TABLE "public"."case_domain"       ADD CONSTRAINT fk_case_domain_case           FOREIGN KEY ("case_id")             REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_domain"       ADD CONSTRAINT fk_case_domain_domain         FOREIGN KEY ("domain_id")           REFERENCES "public"."domain"("id");

ALTER TABLE "public"."case_law_reference" ADD CONSTRAINT fk_case_law_reference_case   FOREIGN KEY ("case_id")             REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_law_reference" ADD CONSTRAINT fk_case_law_reference_leg    FOREIGN KEY ("legislation_id")      REFERENCES "public"."legislation"("id");
ALTER TABLE "public"."case_law_reference" ADD CONSTRAINT fk_case_law_reference_prov   FOREIGN KEY ("provision_id")        REFERENCES "public"."legal_provision"("id");

ALTER TABLE "public"."case_citation"     ADD CONSTRAINT fk_case_citation_source       FOREIGN KEY ("source_case_id")      REFERENCES "public"."cases"("id") ON DELETE CASCADE;
-- ON DELETE SET NULL: deleting a cited case degrades the citation to
-- "unresolved" (target_*_raw keeps the identifier) instead of blocking the
-- delete or dropping the edge. Loader must therefore ALWAYS populate
-- target_ecli_raw / target_celex_raw, even when target_case_id resolves.
ALTER TABLE "public"."case_citation"     ADD CONSTRAINT fk_case_citation_target       FOREIGN KEY ("target_case_id")      REFERENCES "public"."cases"("id") ON DELETE SET NULL;
ALTER TABLE "public"."case_citation"     ADD CONSTRAINT fk_case_citation_context      FOREIGN KEY ("context_segment_id")  REFERENCES "public"."case_segment"("id") ON DELETE SET NULL;

ALTER TABLE "public"."case_citation_counts" ADD CONSTRAINT fk_case_citation_counts    FOREIGN KEY ("case_id")             REFERENCES "public"."cases"("id") ON DELETE CASCADE;

-- CJEU
ALTER TABLE "public"."cjeu_document"           ADD CONSTRAINT fk_cjeu_document_case          FOREIGN KEY ("case_id")               REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."cjeu_document"           ADD CONSTRAINT fk_cjeu_document_formation     FOREIGN KEY ("formation_id")          REFERENCES "public"."court_formation"("id");
ALTER TABLE "public"."cjeu_document"           ADD CONSTRAINT fk_cjeu_document_dossier       FOREIGN KEY ("dossier_parent_case_id") REFERENCES "public"."cases"("id") ON DELETE SET NULL;
ALTER TABLE "public"."cjeu_ag_opinion"         ADD CONSTRAINT fk_cjeu_ag_opinion_case        FOREIGN KEY ("case_id")               REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."cjeu_ag_opinion"         ADD CONSTRAINT fk_cjeu_ag_opinion_parent      FOREIGN KEY ("parent_case_id")        REFERENCES "public"."cases"("id") ON DELETE SET NULL;
ALTER TABLE "public"."cjeu_national_document"  ADD CONSTRAINT fk_cjeu_national_document_case FOREIGN KEY ("case_id")               REFERENCES "public"."cases"("id") ON DELETE CASCADE;

-- ECHR
ALTER TABLE "public"."echr_document"           ADD CONSTRAINT fk_echr_document_case          FOREIGN KEY ("case_id")             REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."echr_document"           ADD CONSTRAINT fk_echr_document_language      FOREIGN KEY ("language")            REFERENCES "public"."language"("iso_code");
-- Satellites FK to echr_document(item_id) — an appno / article / segmentation
-- row can only exist for a HUDOC variant we actually hold. (case existence is
-- enforced transitively via echr_document's own FK to cases.)
ALTER TABLE "public"."echr_document_appno"     ADD CONSTRAINT fk_echr_document_appno_doc     FOREIGN KEY ("item_id") REFERENCES "public"."echr_document"("item_id") ON DELETE CASCADE;
ALTER TABLE "public"."echr_document_article"   ADD CONSTRAINT fk_echr_document_article_doc   FOREIGN KEY ("item_id") REFERENCES "public"."echr_document"("item_id") ON DELETE CASCADE;
ALTER TABLE "public"."echr_extractor_segments" ADD CONSTRAINT fk_echr_extractor_segments_doc FOREIGN KEY ("item_id") REFERENCES "public"."echr_document"("item_id") ON DELETE CASCADE;
ALTER TABLE "public"."echr_document_secondary_text" ADD CONSTRAINT fk_echr_document_secondary_text_doc FOREIGN KEY ("item_id") REFERENCES "public"."echr_document"("item_id") ON DELETE CASCADE;

-- Rechtspraak
ALTER TABLE "public"."rs_document"                    ADD CONSTRAINT fk_rs_document_case             FOREIGN KEY ("case_id")             REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."rs_document_external_authority" ADD CONSTRAINT fk_rs_document_ext_authority    FOREIGN KEY ("case_id")             REFERENCES "public"."rs_document"("case_id") ON DELETE CASCADE;
ALTER TABLE "public"."rs_document_formal_relation"    ADD CONSTRAINT fk_rs_document_formal_source    FOREIGN KEY ("case_id")             REFERENCES "public"."rs_document"("case_id") ON DELETE CASCADE;
ALTER TABLE "public"."rs_document_formal_relation"    ADD CONSTRAINT fk_rs_document_formal_target    FOREIGN KEY ("target_ecli")         REFERENCES "public"."cases"("ecli") ON DELETE SET NULL;
ALTER TABLE "public"."rs_document_publication"        ADD CONSTRAINT fk_rs_document_publication_case FOREIGN KEY ("case_id")             REFERENCES "public"."rs_document"("case_id") ON DELETE CASCADE;

-- Cross-corpus bridge
ALTER TABLE "public"."lido_link" ADD CONSTRAINT fk_lido_link_source_case      FOREIGN KEY ("source_case_id")      REFERENCES "public"."cases"("id") ON DELETE SET NULL;
ALTER TABLE "public"."lido_link" ADD CONSTRAINT fk_lido_link_target_case      FOREIGN KEY ("target_case_id")      REFERENCES "public"."cases"("id") ON DELETE SET NULL;
ALTER TABLE "public"."lido_link" ADD CONSTRAINT fk_lido_link_target_provision FOREIGN KEY ("target_provision_id") REFERENCES "public"."legal_provision"("id");

-- Downstream
ALTER TABLE "public"."case_segment"            ADD CONSTRAINT fk_case_segment_case             FOREIGN KEY ("case_id")     REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_segment"            ADD CONSTRAINT fk_case_segment_language         FOREIGN KEY ("language")    REFERENCES "public"."language"("iso_code");
ALTER TABLE "public"."case_entity"             ADD CONSTRAINT fk_case_entity_case              FOREIGN KEY ("case_id")     REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_summary_version"    ADD CONSTRAINT fk_case_summary_version_case     FOREIGN KEY ("case_id")     REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_summary_version"    ADD CONSTRAINT fk_case_summary_version_language FOREIGN KEY ("language")    REFERENCES "public"."language"("iso_code");
ALTER TABLE "public"."case_summary_version"    ADD CONSTRAINT fk_case_summary_version_parent   FOREIGN KEY ("parent_version_id") REFERENCES "public"."case_summary_version"("id") ON DELETE SET NULL;
ALTER TABLE "public"."case_cluster"            ADD CONSTRAINT fk_case_cluster_snapshot         FOREIGN KEY ("snapshot_id") REFERENCES "public"."network_snapshot"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_cluster_membership" ADD CONSTRAINT fk_case_cluster_membership_case  FOREIGN KEY ("case_id")     REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_cluster_membership" ADD CONSTRAINT fk_case_cluster_membership_clus  FOREIGN KEY ("cluster_id")  REFERENCES "public"."case_cluster"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_network_metric"     ADD CONSTRAINT fk_case_network_metric_case      FOREIGN KEY ("case_id")     REFERENCES "public"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_network_metric"     ADD CONSTRAINT fk_case_network_metric_snapshot  FOREIGN KEY ("snapshot_id") REFERENCES "public"."network_snapshot"("id") ON DELETE CASCADE;


-- =============================================================================
-- Seed data — language lookup (24 official EU languages, lowercase ISO 639-1)
-- =============================================================================

INSERT INTO "public"."language" (iso_code, name) VALUES
    ('bg', 'Bulgarian'), ('cs', 'Czech'),      ('da', 'Danish'),
    ('de', 'German'),    ('el', 'Greek'),      ('en', 'English'),
    ('es', 'Spanish'),   ('et', 'Estonian'),   ('fi', 'Finnish'),
    ('fr', 'French'),    ('ga', 'Irish'),      ('hr', 'Croatian'),
    ('hu', 'Hungarian'), ('it', 'Italian'),    ('lt', 'Lithuanian'),
    ('lv', 'Latvian'),   ('mt', 'Maltese'),    ('nl', 'Dutch'),
    ('pl', 'Polish'),    ('pt', 'Portuguese'), ('ro', 'Romanian'),
    ('sk', 'Slovak'),    ('sl', 'Slovenian'),  ('sv', 'Swedish');

-- =============================================================================
-- Seed data — court_formation lookup (CJEU only)
-- =============================================================================

INSERT INTO "public"."court_formation" (code, label, judge_count) VALUES
    ('GC',   'Grand Chamber',              15),
    ('FC',   'Full Court',                 27),
    ('1C',   'First Chamber (5 judges)',    5),
    ('2C',   'Second Chamber (5 judges)',   5),
    ('3C',   'Third Chamber (5 judges)',    5),
    ('4C',   'Fourth Chamber (5 judges)',   5),
    ('5C',   'Fifth Chamber (5 judges)',    5),
    ('6C',   'Sixth Chamber (3 judges)',    3),
    ('7C',   'Seventh Chamber (3 judges)',  3),
    ('8C',   'Eighth Chamber (3 judges)',   3),
    ('9C',   'Ninth Chamber (3 judges)',    3),
    ('10C',  'Tenth Chamber (3 judges)',    3),
    ('PR',   'President sitting alone',     1),
    ('SOLE', 'Single judge',                1);


-- =============================================================================
-- Migration notes (for the loader / for team reference)
-- =============================================================================
-- Legacy → new column-level moves
--
--   ECHR
--     echr_document.itemid                → cases.item_id
--     echr_document.languageisocode       → case_text.language + echr_document.language
--     echr_document.fulltext              → case_text.fulltext (per language)
--     echr_document.fulltext_tsv          → case_text.fulltext_tsv (generated)
--     echr_document_text                  → merged into case_text
--     echr_edge                           → case_citation (relation_type='cites',
--                                             source_dataset='echr_edge')
--     echr_citation_counts                → case_citation_counts
--
--   Rechtspraak
--     rs_document.ecli (PK)               → resolves via cases.ecli, keeps case_id FK
--     rs_document.summary                 → case_text.summary (language='nl')
--     rs_document_text                    → merged into case_text
--     rs_edge                             → case_citation
--     rs_citation_counts                  → case_citation_counts
--     rs_document_formal_relation         → kept as-is AND fanned out to case_citation
--     rs_document_law_reference           → case_law_reference (raw_scheme='bwb',
--                                             raw_resource=bwb_resource,
--                                             raw_subdivision=article,
--                                             raw_label_id=bwb_label_id,
--                                             raw_reference=opschrift,
--                                             version_date, source_dataset per
--                                             extraction source; deeplink URLs
--                                             now live on the
--                                             rs_v_document_law_reference view)
--
--   CJEU (loads from the HF parquet corpus, not from legacy Postgres)
--     cases.title                          → synthesized "C-123/22, X v Y"
--                                           (work_title is 1.3% populated upstream)
--     cases.importance                     → formation-based proxy on the
--                                           harmonized 1–4 scale (GC/FC→1,
--                                           5-judge→2, 3-judge→3, sole/order→4)
--     subject_matter / eurovoc /
--     keywords / directory_codes          → domain (per scheme) + case_domain
--     judge_rapporteur + delivered_by     → case_judge (roles rapporteur/judge)
--     parties + agents                    → party + case_party
--     citing / cited_by / work_cites_work → case_citation
--     18 CDM legal predicates             → case_law_reference (role per predicate)
--
--   Dutch legislation catalog (BWB + LIDO — was rs_law_*, misfiled as RS metadata)
--     rs_law_element  type='wet'          → legislation (scheme='bwb',
--                                             identifier=bwb_id, title, lido_id,
--                                             jc_id, snapshot_date)
--     rs_law_element  other types         → legal_provision (element_type=type,
--                                             article_label=number, title,
--                                             bwb_label_id, lido_id, jc_id,
--                                             parent_id from BWB hierarchy)
--     rs_law_alias                        → legislation_alias (alias,
--                                             legislation_id via bwb_id lookup,
--                                             source='bwbidlist')
--
-- Legacy tables NOT ported (staging / deprecated)
--     case_law, legal_case, ecli_bwb_opschrift, ecli_keywords, ecli_segments,
--     ecli_texts, law_alias, law_element
-- Their information now lives in: case, case_law_reference, case_domain,
-- case_segment, legislation, legal_provision, legislation_alias.
-- =============================================================================
