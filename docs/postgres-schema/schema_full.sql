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
--             rs_document_external_authority, rs_document_law_reference
--             (legacy rs_law_element / rs_law_alias are BWB+LIDO Dutch
--              LEGISLATION catalog data, not RS-corpus metadata — they fold
--              into legislation / legal_provision / legislation_alias)
--   • ECLI is the natural business key on `case`; internal integer id is the FK anchor.
--   • Legacy staging tables (case_law, legal_case, law_*, ecli_*) are NOT copied — the
--     new model absorbs them via the shared tables + rs_law_* tables.
--
-- Not applied here — left for the team to decide
--   • CHECK constraints on case_citation.relation_type / case_law_reference.role
--     (allowed-value lists inline as comments). Enable once the sets stabilise.
--   • pgvector column types on case_text.summary_embedding + case_segment.embedding
--     (kept as `vector` in this file since pgvector is confirmed installed in
--     legacy — `ecli_segments.embedding vector(768)`).
-- =============================================================================


-- =============================================================================
-- Extensions (from legacy production)
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS "public";

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- gin_trgm_ops on docname / issue
CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector — HNSW on embeddings


-- =============================================================================
-- Utility functions (adapted from legacy)
-- =============================================================================

-- Generic updated_at touch trigger. Replaces echr_touch_updated_at +
-- rs_touch_updated_at from legacy with one shared function.
CREATE OR REPLACE FUNCTION public.touch_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

-- Date-to-ISO helper preserved from legacy — used by rs_document_law_reference
-- generated columns.
CREATE OR REPLACE FUNCTION public.rs_date_to_iso(d date)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN d IS NULL THEN NULL ELSE to_char(d, 'YYYY-MM-DD') END;
$$;

-- Generalised citation-count maintenance. One function drives
-- case_citation_counts for all three corpora (replaces the two per-corpus
-- functions in legacy). Trigger definition further below.
CREATE OR REPLACE FUNCTION public.case_citation_counts_maintain()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.source_case_id IS NOT NULL THEN
      UPDATE public.case_citation_counts
         SET cites_count = GREATEST(cites_count - 1, 0), updated_at = now()
       WHERE case_id = OLD.source_case_id;
    END IF;
    IF OLD.target_case_id IS NOT NULL THEN
      UPDATE public.case_citation_counts
         SET cited_by_count = GREATEST(cited_by_count - 1, 0), updated_at = now()
       WHERE case_id = OLD.target_case_id;
    END IF;
    RETURN OLD;
  END IF;

  IF TG_OP = 'INSERT' THEN
    IF NEW.source_case_id IS NOT NULL THEN
      INSERT INTO public.case_citation_counts (case_id, cites_count, cited_by_count, updated_at)
      VALUES (NEW.source_case_id, 1, 0, now())
      ON CONFLICT (case_id) DO UPDATE
        SET cites_count = case_citation_counts.cites_count + 1, updated_at = now();
    END IF;
    IF NEW.target_case_id IS NOT NULL THEN
      INSERT INTO public.case_citation_counts (case_id, cites_count, cited_by_count, updated_at)
      VALUES (NEW.target_case_id, 0, 1, now())
      ON CONFLICT (case_id) DO UPDATE
        SET cited_by_count = case_citation_counts.cited_by_count + 1, updated_at = now();
    END IF;
    RETURN NEW;
  END IF;

  IF TG_OP = 'UPDATE' THEN
    IF OLD.source_case_id IS DISTINCT FROM NEW.source_case_id THEN
      IF OLD.source_case_id IS NOT NULL THEN
        UPDATE public.case_citation_counts
           SET cites_count = GREATEST(cites_count - 1, 0), updated_at = now()
         WHERE case_id = OLD.source_case_id;
      END IF;
      IF NEW.source_case_id IS NOT NULL THEN
        INSERT INTO public.case_citation_counts (case_id, cites_count, cited_by_count, updated_at)
        VALUES (NEW.source_case_id, 1, 0, now())
        ON CONFLICT (case_id) DO UPDATE
          SET cites_count = case_citation_counts.cites_count + 1, updated_at = now();
      END IF;
    END IF;
    IF OLD.target_case_id IS DISTINCT FROM NEW.target_case_id THEN
      IF OLD.target_case_id IS NOT NULL THEN
        UPDATE public.case_citation_counts
           SET cited_by_count = GREATEST(cited_by_count - 1, 0), updated_at = now()
         WHERE case_id = OLD.target_case_id;
      END IF;
      IF NEW.target_case_id IS NOT NULL THEN
        INSERT INTO public.case_citation_counts (case_id, cites_count, cited_by_count, updated_at)
        VALUES (NEW.target_case_id, 0, 1, now())
        ON CONFLICT (case_id) DO UPDATE
          SET cited_by_count = case_citation_counts.cited_by_count + 1, updated_at = now();
      END IF;
    END IF;
    RETURN NEW;
  END IF;
END;
$$;


-- =============================================================================
-- Lookups
-- =============================================================================

-- DECISION REQUIRED — language-code convention.
-- The three corpora use three different code systems:
--   CJEU  : uppercase ISO 639-1        ('EN', 'FR', 'BG', …, 24 langs)
--   ECHR  : HUDOC 3-letter codes       ('ENG', 'FRE', …)
--   RS    : lowercase ISO 639-1        ('nl')
-- Everything that joins on language (case_text UNIQUE, echr_document PK,
-- the *_v_document_with_text views, all FKs to this table) requires ONE
-- canonical convention. Recommendation: lowercase ISO 639-1 ('en', 'fr',
-- 'nl', 'bg', …), normalized at load time (HUDOC ENG→en, FRE→fr; CJEU EN→en).
-- The original upstream code can be preserved on the corpus-specific rows
-- if ever needed (not included here).
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
    "id" serial NOT NULL,
    "code" text UNIQUE,
    "name" text,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."document_type" (
    "id" serial NOT NULL,
    "code" text UNIQUE,
    "name" text,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."procedure_type" (
    "id" serial NOT NULL,
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
    "id" serial NOT NULL,
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

CREATE TABLE "public"."legal_provision" (
    "id" bigserial NOT NULL,
    "legislation_id" bigint,
    "parent_id" bigint,              -- self-FK: nested structure (NL: boek→titeldeel→artikel; EU: article→paragraph→annex)
    "element_type" text,             -- NL: 'boek'|'deel'|'titeldeel'|'hoofdstuk'|'afdeling'|'paragraaf'|'subparagraaf'|'artikel'; EU: 'article'|'paragraph'|'annex'
    "article_label" text,            -- was rs_law_element.number
    "title" text,                    -- display label (was rs_law_element.title)
    "paragraph" text,
    "text" text,
    "bwb_label_id" bigint,           -- BWB label id — join key from rs_document_law_reference
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
    "id" serial NOT NULL,
    "scheme" text,                   -- 'eurovoc' | 'cjeu_subject_matter' | 'cjeu_keyword' | 'cjeu_directory_code' | 'rs_domain' | 'echr_article' | ...
    "name" text,
    "uri" text,
    "parent_id" int,
    PRIMARY KEY ("id")
);

-- CJEU-specific formation lookup (~15 rows, seeded at bottom)
CREATE TABLE "public"."court_formation" (
    "id" serial NOT NULL,
    "code" text UNIQUE,              -- 'GC' | 'FC' | '1C' | ... | 'PR' | 'SOLE'
    "label" text,
    "judge_count" smallint,
    PRIMARY KEY ("id")
);


-- =============================================================================
-- Core case data (shared across CJEU / ECHR / Rechtspraak)
-- =============================================================================

CREATE TABLE "public"."case" (
    "id" bigserial NOT NULL,
    "ecli" text UNIQUE,
    "item_id" text UNIQUE,           -- external primary identifier (HUDOC itemid, CELLAR cellar_id, RS ecli)
    "source" text,                   -- 'CJEU' | 'ECHR' | 'RS'
    "celex_id" text UNIQUE,
    "title" text,
    "date_decision" date,
    "date_published" date,
    "court_id" bigint,
    "language_iso" text,             -- procedure language (best-guess primary)
    "document_type_id" int,
    "procedure_type_id" int,
    "instance_id" int,
    "case_number" text,
    "importance" smallint,           -- ECHR importance level (1-4); NULL otherwise
    "is_landmark" boolean,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id")
);
CREATE INDEX "case_idx_court"          ON "public"."case" ("court_id");
CREATE INDEX "case_idx_date_decision"  ON "public"."case" ("date_decision");
CREATE INDEX "case_idx_ecli"           ON "public"."case" ("ecli");
CREATE INDEX "case_idx_source"         ON "public"."case" ("source");
CREATE INDEX "case_idx_item_id"        ON "public"."case" ("item_id");

CREATE TABLE "public"."case_text" (
    "id" bigserial NOT NULL,
    "case_id" bigint NOT NULL,
    "language" text NOT NULL,
    "fulltext" text,
    "summary" text,
    -- DECISION REQUIRED — two caveats on fulltext_tsv:
    --  (1) A tsvector has a hard 1 MB limit. A GENERATED column means an
    --      oversized judgment fails the whole INSERT. Legacy ECHR used this
    --      same generated pattern and survived production, but legacy RS used
    --      a trigger instead. If very large CJEU judgments trip the limit,
    --      switch to a trigger with a length guard.
    --  (2) The legacy RS trigger folded summary + legal_provisions into the
    --      vector, so legacy RS search also matched those. This generated
    --      column covers fulltext only — the API should query summary
    --      separately, or we reinstate the RS fold-in behavior via trigger.
    "fulltext_tsv" tsvector GENERATED ALWAYS AS (
        to_tsvector('simple'::regconfig, COALESCE("fulltext", ''::text))
    ) STORED,
    "summary_embedding" vector(768),                       -- dimension pinned to legacy model (ecli_segments used 768); change if the embedding model changes
    "embedding_model" text,
    "source" text,                                         -- 'INFOCURIA_BLOB_HTML' | 'CELLAR_ITEM' | 'EXTRACTOR_FALLBACK_TEXT' | 'HUDOC' | 'RECHTSPRAAK' | ...
    "text_format" text,                                    -- xhtml | html | pdf | fmx4
    "missing_reasons" text,                                -- 'FULLTEXT_UNAVAILABLE_UPSTREAM' | ...
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id"),
    UNIQUE ("case_id", "language")                         -- exactly one row per case × language
);
CREATE INDEX "case_text_idx_case_id"          ON "public"."case_text" ("case_id");
CREATE INDEX "case_text_idx_fulltext_tsv"     ON "public"."case_text" USING gin ("fulltext_tsv");
CREATE INDEX "case_text_idx_summary_embedding" ON "public"."case_text" USING hnsw ("summary_embedding" vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE TRIGGER trg_case_text_updated_at
BEFORE UPDATE ON "public"."case_text"
FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

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
    "party_id" int,
    "role" text,                     -- 'applicant' | 'defendant' | 'referring_state' | 'defendant_agent' | ...
    "ordinal" smallint DEFAULT 1,    -- disambiguate multiple parties in same role
    PRIMARY KEY ("case_id", "party_id", "role", "ordinal")
);
CREATE INDEX "case_party_idx_party" ON "public"."case_party" ("party_id");

CREATE TABLE "public"."case_domain" (
    "case_id" bigint,
    "domain_id" int,
    PRIMARY KEY ("case_id", "domain_id")
);
CREATE INDEX "case_domain_idx_domain_id" ON "public"."case_domain" ("domain_id");

CREATE TABLE "public"."case_law_reference" (
    "id" bigserial NOT NULL,
    "case_id" bigint,
    "provision_id" bigint,           -- nullable: CJEU / ECHR often cite whole acts
    "legislation_id" bigint,
    "raw_reference" text,
    "role" text,
    -- Allowed values (advisory — not enforced until value set stabilises)
    -- CJEU adds: based_on_treaty, legal_basis, affects, amends, amends_by_correction,
    --           confirms, interprets, interprets_judgement, declares_void,
    --           declares_void_by_preliminary_ruling, incidentally_declares_void,
    --           declares_valid, declares_incidentally_valid, states_failure,
    --           suspends_application, immediately_enforces, incorporates, corrects
    -- RS adds:  applied, cited, art_ref
    -- ECHR adds: applied, violation, nonviolation
    "source" text,                   -- provenance: 'cellar_sparql' | 'rs_law_ref' | 'echr_document_article' | ...
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("id")
);
CREATE INDEX "case_law_reference_idx_case_id"     ON "public"."case_law_reference" ("case_id");
CREATE INDEX "case_law_reference_idx_legislation" ON "public"."case_law_reference" ("legislation_id");
CREATE INDEX "case_law_reference_idx_provision"   ON "public"."case_law_reference" ("provision_id");

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

CREATE TRIGGER trg_case_citation_counts
AFTER INSERT OR UPDATE OR DELETE ON "public"."case_citation"
FOR EACH ROW EXECUTE FUNCTION public.case_citation_counts_maintain();


-- =============================================================================
-- CJEU-specific extensions
-- =============================================================================

CREATE TABLE "public"."cjeu_document" (
    "id" bigserial NOT NULL,
    "case_id" bigint NOT NULL,
    "celex_id" text,                 -- denormalized from case.celex_id for convenience
    "ecli" text,                     -- denormalized from case.ecli
    "sector" text,                   -- '6' (CJEU direct) | '8' (national CJEU-referred)
    "case_number" text,
    "formation_id" int,              -- FK → court_formation (replaces legacy typo "formation timestamp")
    "proc_type" text,
    "procedure_result" text,         -- 'successful' | 'unfounded' | 'inadmissible' (parsed from type_procedure)
    "date_lodged" date,
    "cellar_uri" text,
    "work_uri" text,
    "journal_refs" text,             -- OJ references
    "erecueil_ref" text,             -- European Court Reports citation
    "local_identifier" text,
    "dossier_uri" text,              -- groups Opinion + Judgment + Order (18.8% populated)
    "dossier_parent_case_id" bigint, -- resolved post-load
    PRIMARY KEY ("id"),
    UNIQUE ("case_id")
);

CREATE TABLE "public"."cjeu_ag_opinion" (
    "id" serial NOT NULL,
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
    PRIMARY KEY ("id"),
    UNIQUE ("case_id")
);


-- =============================================================================
-- ECHR-specific extensions — enriched from legacy production
-- =============================================================================

CREATE TABLE "public"."echr_document" (
    -- One row per (case_id, language). Preserves HUDOC's per-language variants.
    -- Every legacy column from public.echr_document is kept; itemid and languageisocode
    -- become (case_id via FK, language) — HUDOC itemid stored on case.item_id.
    "case_id" bigint NOT NULL,
    "language" text NOT NULL,                              -- HUDOC's languageisocode (ENG/FRE/…)
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
    "judgement_year" int GENERATED ALWAYS AS (
        EXTRACT(year FROM ("judgement_date" AT TIME ZONE 'UTC'))::int
    ) STORED,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "updated_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("case_id", "language")
);
CREATE INDEX "echr_document_idx_doctype"          ON "public"."echr_document" ("doctype");
CREATE INDEX "echr_document_idx_doctype_branch"   ON "public"."echr_document" ("doctype_branch");
CREATE INDEX "echr_document_idx_judgement_date"   ON "public"."echr_document" ("judgement_date");
CREATE INDEX "echr_document_idx_judgement_year"   ON "public"."echr_document" ("judgement_year");
CREATE INDEX "echr_document_idx_originating_body" ON "public"."echr_document" ("originating_body");
CREATE INDEX "echr_document_idx_docname_trgm"     ON "public"."echr_document" USING gin ("docname" gin_trgm_ops);
CREATE INDEX "echr_document_idx_issue_trgm"       ON "public"."echr_document" USING gin ("issue" gin_trgm_ops);
CREATE TRIGGER trg_echr_document_updated_at
BEFORE UPDATE ON "public"."echr_document"
FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

CREATE TABLE "public"."echr_document_appno" (
    -- Normalized appnos — one row per (case × language × appno × source).
    -- 'source' distinguishes case's own appno from those parsed from references.
    "case_id" bigint NOT NULL,
    "language" text NOT NULL,
    "appno" text NOT NULL,
    "source" text NOT NULL,                                -- 'appno' | 'extractedappno'
    "created_at" timestamptz DEFAULT now() NOT NULL,
    PRIMARY KEY ("case_id", "language", "appno", "source")
);
CREATE INDEX "echr_document_appno_idx_appno_left" ON "public"."echr_document_appno" (left("appno", 500));
CREATE INDEX "echr_document_appno_idx_case_lang"  ON "public"."echr_document_appno" ("case_id", "language");
CREATE INDEX "echr_document_appno_idx_source"     ON "public"."echr_document_appno" ("source");

CREATE TABLE "public"."echr_document_article" (
    -- ECHR Convention articles applied to / violated by / not-violated by this case.
    "case_id" bigint NOT NULL,
    "language" text NOT NULL,
    "kind" text NOT NULL,                                  -- 'applied' | 'violation' | 'nonviolation'
    "article_code" text NOT NULL,                          -- e.g. '6' | '6-1' | '13' | 'P1-1'
    PRIMARY KEY ("case_id", "language", "kind", "article_code"),
    CONSTRAINT echr_document_article_kind_check
        CHECK ("kind" IN ('applied', 'violation', 'nonviolation'))
);
CREATE INDEX "echr_document_article_idx_filter"    ON "public"."echr_document_article" ("kind", "article_code");
CREATE INDEX "echr_document_article_idx_case_lang" ON "public"."echr_document_article" ("case_id", "language");

CREATE TABLE "public"."echr_extractor_segments" (
    -- Section-level text extraction (procedure / facts / law / operative / …).
    -- Preserved from legacy — this is a segmentation that carries ECHR-specific
    -- semantics not captured by the generic case_segment table.
    "case_id" bigint NOT NULL,
    "language" text NOT NULL,
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
    PRIMARY KEY ("case_id", "language")
);
CREATE INDEX "echr_extractor_segments_idx_parser"      ON "public"."echr_extractor_segments" ("parser_mode");
CREATE INDEX "echr_extractor_segments_idx_num_sections" ON "public"."echr_extractor_segments" ("num_sections");


-- =============================================================================
-- Rechtspraak-specific extensions — enriched from legacy production
-- =============================================================================

CREATE TABLE "public"."rs_document" (
    -- One row per case_id. All Rechtspraak-specific metadata from legacy.
    "case_id" bigint PRIMARY KEY,
    "date_decision" date,                                  -- Rechtspraak's own date_decision (may differ from case.date_decision if late correction)
    "document_type" text,
    "instance" text,
    "domains" text[],
    "source" text DEFAULT 'Rechtspraak' NOT NULL,
    "jurisdiction_country" text DEFAULT 'NL' NOT NULL,
    "procedure_type" text,
    "url_publication" text,
    -- NOTE: legacy rs_document.summary moved to case_text.summary (language='nl').
    -- The rs_v_document_with_text view re-exposes it for API compatibility.
    "legal_provisions" text[],                             -- denormalized array; canonical values live in rs_document_law_reference
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
CREATE INDEX "rs_document_idx_date_issued"   ON "public"."rs_document" ("date_issued");
CREATE INDEX "rs_document_idx_date_modified" ON "public"."rs_document" ("date_modified");
CREATE TRIGGER trg_rs_document_updated_at
BEFORE UPDATE ON "public"."rs_document"
FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

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

CREATE TABLE "public"."rs_document_law_reference" (
    -- Dutch legislation references (BWB) with generated deeplinks.
    -- Preserved from legacy — the generated URL columns encode Dutch-law semantics
    -- (wetten.overheid.nl + LIDO) that don't generalise, so kept in the RS bucket.
    "case_id" bigint NOT NULL,
    "bwb_resource" text NOT NULL,
    "article" text DEFAULT '' NOT NULL,
    "version_date" date,
    "bwb_label_id" bigint,
    "source" text NOT NULL,
    "opschrift" text,
    "created_at" timestamptz DEFAULT now() NOT NULL,
    "legal_provision_url" text GENERATED ALWAYS AS (
        'http://wetten.overheid.nl/id/' || "bwb_resource" || '/' ||
        COALESCE(public.rs_date_to_iso("version_date"), '1900-01-01') || '/0'
    ) STORED,
    "legal_provision_url_lido" text GENERATED ALWAYS AS (
        CASE
            WHEN "bwb_label_id" IS NULL THEN NULL::text
            ELSE 'http://linkeddata.overheid.nl/terms/bwb/id/' || "bwb_resource" || '/'
                 || "bwb_label_id"::text || '/'
                 || COALESCE(public.rs_date_to_iso("version_date"), '1900-01-01') || '/'
                 || COALESCE(public.rs_date_to_iso("version_date"), '1900-01-01')
        END
    ) STORED,
    PRIMARY KEY ("case_id", "bwb_resource", "article", "source")
);

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
    -- Complements case_citation (structured cases) and rs_document_law_reference
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

CREATE TABLE "public"."case_cluster" (
    "id" serial NOT NULL,
    "snapshot_id" int,
    "algorithm" text,
    "label" text,
    "size" int,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."case_cluster_membership" (
    "cluster_id" int,
    "case_id" bigint,
    PRIMARY KEY ("cluster_id", "case_id")
);

CREATE TABLE "public"."case_network_metric" (
    "snapshot_id" int,
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
    "id" serial NOT NULL,
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
CREATE OR REPLACE VIEW "public"."echr_v_document_with_text" AS
SELECT d.*, t."fulltext", t."fulltext_tsv"
FROM "public"."echr_document" d
LEFT JOIN "public"."case_text" t
       ON t."case_id" = d."case_id"
      AND t."language" = d."language";

-- Judgments and decisions only (excludes press releases / communications).
CREATE OR REPLACE VIEW "public"."echr_v_judgments_decisions" AS
SELECT * FROM "public"."echr_document"
WHERE "doctype" ILIKE '%JUD%' OR "doctype" ILIKE '%DEC%';

-- Rechtspraak document + fulltext (only Dutch text — RS is monolingual).
-- Re-exposes summary from case_text for API compatibility with the legacy
-- rs_document.summary column.
CREATE OR REPLACE VIEW "public"."rs_v_document_with_text" AS
SELECT d.*, t."summary", t."fulltext", t."fulltext_tsv"
FROM "public"."rs_document" d
LEFT JOIN "public"."case_text" t ON t."case_id" = d."case_id" AND t."language" = 'nl';

-- Legal-provision display labels for /api/rechtspraak — mirrors the legacy view.
CREATE OR REPLACE VIEW "public"."rs_v_document_legal_provisions" AS
SELECT DISTINCT c."ecli", lr."opschrift" AS legal_provision
FROM "public"."rs_document_law_reference" lr
JOIN "public"."case" c ON c."id" = lr."case_id"
WHERE NULLIF(lr."opschrift", '') IS NOT NULL
UNION
SELECT DISTINCT c."ecli", lp."title" AS legal_provision
FROM "public"."rs_document_law_reference" lr
JOIN "public"."case" c ON c."id" = lr."case_id"
JOIN "public"."legal_provision" lp ON lp."bwb_label_id" = lr."bwb_label_id"
WHERE lr."bwb_label_id" IS NOT NULL AND NULLIF(lp."title", '') IS NOT NULL
UNION
SELECT DISTINCT c."ecli", lp."title" AS legal_provision
FROM "public"."rs_document_law_reference" lr
JOIN "public"."case" c ON c."id" = lr."case_id"
JOIN "public"."legislation" lg
  ON lg."scheme" = 'bwb' AND lg."identifier" = lr."bwb_resource"
JOIN "public"."legal_provision" lp
  ON lp."legislation_id" = lg."id"
 AND lower(lp."article_label") = lower(lr."article")
 AND lp."element_type" = 'artikel'
WHERE NULLIF(lp."title", '') IS NOT NULL
UNION
SELECT DISTINCT c."ecli", lg."title" AS legal_provision
FROM "public"."rs_document_law_reference" lr
JOIN "public"."case" c ON c."id" = lr."case_id"
JOIN "public"."legislation" lg
  ON lg."scheme" = 'bwb' AND lg."identifier" = lr."bwb_resource"
WHERE NULLIF(lg."title", '') IS NOT NULL
UNION
SELECT DISTINCT c."ecli", (lg."title" || ', Artikel ' || lr."article") AS legal_provision
FROM "public"."rs_document_law_reference" lr
JOIN "public"."case" c ON c."id" = lr."case_id"
JOIN "public"."legislation" lg
  ON lg."scheme" = 'bwb' AND lg."identifier" = lr."bwb_resource"
WHERE NULLIF(lg."title", '') IS NOT NULL AND NULLIF(lr."article", '') IS NOT NULL
UNION
SELECT DISTINCT c."ecli", (lg."title" || ', Bijlage ' || lr."article") AS legal_provision
FROM "public"."rs_document_law_reference" lr
JOIN "public"."case" c ON c."id" = lr."case_id"
JOIN "public"."legislation" lg
  ON lg."scheme" = 'bwb' AND lg."identifier" = lr."bwb_resource"
WHERE NULLIF(lg."title", '') IS NOT NULL AND NULLIF(lr."article", '') IS NOT NULL
  AND lr."opschrift" ILIKE '%bijlage%';


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

-- Case & satellites
ALTER TABLE "public"."case"              ADD CONSTRAINT fk_case_court                 FOREIGN KEY ("court_id")            REFERENCES "public"."court"("id");
ALTER TABLE "public"."case"              ADD CONSTRAINT fk_case_document_type         FOREIGN KEY ("document_type_id")    REFERENCES "public"."document_type"("id");
ALTER TABLE "public"."case"              ADD CONSTRAINT fk_case_procedure_type        FOREIGN KEY ("procedure_type_id")   REFERENCES "public"."procedure_type"("id");
ALTER TABLE "public"."case"              ADD CONSTRAINT fk_case_instance              FOREIGN KEY ("instance_id")         REFERENCES "public"."instance"("id");
ALTER TABLE "public"."case"              ADD CONSTRAINT fk_case_language              FOREIGN KEY ("language_iso")        REFERENCES "public"."language"("iso_code");

ALTER TABLE "public"."case_text"         ADD CONSTRAINT fk_case_text_case             FOREIGN KEY ("case_id")             REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_text"         ADD CONSTRAINT fk_case_text_language         FOREIGN KEY ("language")            REFERENCES "public"."language"("iso_code");

ALTER TABLE "public"."case_judge"        ADD CONSTRAINT fk_case_judge_case            FOREIGN KEY ("case_id")             REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_judge"        ADD CONSTRAINT fk_case_judge_judge           FOREIGN KEY ("judge_id")            REFERENCES "public"."judge"("id");

ALTER TABLE "public"."case_party"        ADD CONSTRAINT fk_case_party_case            FOREIGN KEY ("case_id")             REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_party"        ADD CONSTRAINT fk_case_party_party           FOREIGN KEY ("party_id")            REFERENCES "public"."party"("id");

ALTER TABLE "public"."case_domain"       ADD CONSTRAINT fk_case_domain_case           FOREIGN KEY ("case_id")             REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_domain"       ADD CONSTRAINT fk_case_domain_domain         FOREIGN KEY ("domain_id")           REFERENCES "public"."domain"("id");

ALTER TABLE "public"."case_law_reference" ADD CONSTRAINT fk_case_law_reference_case   FOREIGN KEY ("case_id")             REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_law_reference" ADD CONSTRAINT fk_case_law_reference_leg    FOREIGN KEY ("legislation_id")      REFERENCES "public"."legislation"("id");
ALTER TABLE "public"."case_law_reference" ADD CONSTRAINT fk_case_law_reference_prov   FOREIGN KEY ("provision_id")        REFERENCES "public"."legal_provision"("id");

ALTER TABLE "public"."case_citation"     ADD CONSTRAINT fk_case_citation_source       FOREIGN KEY ("source_case_id")      REFERENCES "public"."case"("id") ON DELETE CASCADE;
-- ON DELETE SET NULL: deleting a cited case degrades the citation to
-- "unresolved" (target_*_raw keeps the identifier) instead of blocking the
-- delete or dropping the edge. Loader must therefore ALWAYS populate
-- target_ecli_raw / target_celex_raw, even when target_case_id resolves.
ALTER TABLE "public"."case_citation"     ADD CONSTRAINT fk_case_citation_target       FOREIGN KEY ("target_case_id")      REFERENCES "public"."case"("id") ON DELETE SET NULL;
ALTER TABLE "public"."case_citation"     ADD CONSTRAINT fk_case_citation_context      FOREIGN KEY ("context_segment_id")  REFERENCES "public"."case_segment"("id");

ALTER TABLE "public"."case_citation_counts" ADD CONSTRAINT fk_case_citation_counts    FOREIGN KEY ("case_id")             REFERENCES "public"."case"("id") ON DELETE CASCADE;

-- CJEU
ALTER TABLE "public"."cjeu_document"           ADD CONSTRAINT fk_cjeu_document_case          FOREIGN KEY ("case_id")               REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."cjeu_document"           ADD CONSTRAINT fk_cjeu_document_formation     FOREIGN KEY ("formation_id")          REFERENCES "public"."court_formation"("id");
ALTER TABLE "public"."cjeu_document"           ADD CONSTRAINT fk_cjeu_document_dossier       FOREIGN KEY ("dossier_parent_case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."cjeu_ag_opinion"         ADD CONSTRAINT fk_cjeu_ag_opinion_case        FOREIGN KEY ("case_id")               REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."cjeu_ag_opinion"         ADD CONSTRAINT fk_cjeu_ag_opinion_parent      FOREIGN KEY ("parent_case_id")        REFERENCES "public"."case"("id");
ALTER TABLE "public"."cjeu_national_document"  ADD CONSTRAINT fk_cjeu_national_document_case FOREIGN KEY ("case_id")               REFERENCES "public"."case"("id") ON DELETE CASCADE;

-- ECHR
ALTER TABLE "public"."echr_document"           ADD CONSTRAINT fk_echr_document_case          FOREIGN KEY ("case_id")             REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."echr_document"           ADD CONSTRAINT fk_echr_document_language      FOREIGN KEY ("language")            REFERENCES "public"."language"("iso_code");
-- Satellites FK to echr_document(case_id, language) — an appno / article /
-- segmentation row can only exist for a language variant we actually hold.
-- (case existence is enforced transitively via echr_document's own FK.)
ALTER TABLE "public"."echr_document_appno"     ADD CONSTRAINT fk_echr_document_appno_doc     FOREIGN KEY ("case_id", "language") REFERENCES "public"."echr_document"("case_id", "language") ON DELETE CASCADE;
ALTER TABLE "public"."echr_document_article"   ADD CONSTRAINT fk_echr_document_article_doc   FOREIGN KEY ("case_id", "language") REFERENCES "public"."echr_document"("case_id", "language") ON DELETE CASCADE;
ALTER TABLE "public"."echr_extractor_segments" ADD CONSTRAINT fk_echr_extractor_segments_doc FOREIGN KEY ("case_id", "language") REFERENCES "public"."echr_document"("case_id", "language") ON DELETE CASCADE;

-- Rechtspraak
ALTER TABLE "public"."rs_document"                    ADD CONSTRAINT fk_rs_document_case             FOREIGN KEY ("case_id")             REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."rs_document_external_authority" ADD CONSTRAINT fk_rs_document_ext_authority    FOREIGN KEY ("case_id")             REFERENCES "public"."rs_document"("case_id") ON DELETE CASCADE;
ALTER TABLE "public"."rs_document_formal_relation"    ADD CONSTRAINT fk_rs_document_formal_source    FOREIGN KEY ("case_id")             REFERENCES "public"."rs_document"("case_id") ON DELETE CASCADE;
ALTER TABLE "public"."rs_document_formal_relation"    ADD CONSTRAINT fk_rs_document_formal_target    FOREIGN KEY ("target_ecli")         REFERENCES "public"."case"("ecli");
ALTER TABLE "public"."rs_document_publication"        ADD CONSTRAINT fk_rs_document_publication_case FOREIGN KEY ("case_id")             REFERENCES "public"."rs_document"("case_id") ON DELETE CASCADE;
ALTER TABLE "public"."rs_document_law_reference"      ADD CONSTRAINT fk_rs_document_law_reference_case FOREIGN KEY ("case_id")           REFERENCES "public"."rs_document"("case_id") ON DELETE CASCADE;

-- Cross-corpus bridge
ALTER TABLE "public"."lido_link" ADD CONSTRAINT fk_lido_link_source_case      FOREIGN KEY ("source_case_id")      REFERENCES "public"."case"("id");
ALTER TABLE "public"."lido_link" ADD CONSTRAINT fk_lido_link_target_case      FOREIGN KEY ("target_case_id")      REFERENCES "public"."case"("id");
ALTER TABLE "public"."lido_link" ADD CONSTRAINT fk_lido_link_target_provision FOREIGN KEY ("target_provision_id") REFERENCES "public"."legal_provision"("id");

-- Downstream
ALTER TABLE "public"."case_segment"            ADD CONSTRAINT fk_case_segment_case             FOREIGN KEY ("case_id")     REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_segment"            ADD CONSTRAINT fk_case_segment_language         FOREIGN KEY ("language")    REFERENCES "public"."language"("iso_code");
ALTER TABLE "public"."case_entity"             ADD CONSTRAINT fk_case_entity_case              FOREIGN KEY ("case_id")     REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_cluster"            ADD CONSTRAINT fk_case_cluster_snapshot         FOREIGN KEY ("snapshot_id") REFERENCES "public"."network_snapshot"("id");
ALTER TABLE "public"."case_cluster_membership" ADD CONSTRAINT fk_case_cluster_membership_case  FOREIGN KEY ("case_id")     REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_cluster_membership" ADD CONSTRAINT fk_case_cluster_membership_clus  FOREIGN KEY ("cluster_id")  REFERENCES "public"."case_cluster"("id");
ALTER TABLE "public"."case_network_metric"     ADD CONSTRAINT fk_case_network_metric_case      FOREIGN KEY ("case_id")     REFERENCES "public"."case"("id") ON DELETE CASCADE;
ALTER TABLE "public"."case_network_metric"     ADD CONSTRAINT fk_case_network_metric_snapshot  FOREIGN KEY ("snapshot_id") REFERENCES "public"."network_snapshot"("id");


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
--     echr_document.itemid                → case.item_id
--     echr_document.languageisocode       → case_text.language + echr_document.language
--     echr_document.fulltext              → case_text.fulltext (per language)
--     echr_document.fulltext_tsv          → case_text.fulltext_tsv (generated)
--     echr_document_text                  → merged into case_text
--     echr_edge                           → case_citation (relation_type='cites',
--                                             source_dataset='echr_edge')
--     echr_citation_counts                → case_citation_counts
--
--   Rechtspraak
--     rs_document.ecli (PK)               → resolves via case.ecli, keeps case_id FK
--     rs_document.summary                 → case_text.summary (language='nl')
--     rs_document_text                    → merged into case_text
--     rs_edge                             → case_citation
--     rs_citation_counts                  → case_citation_counts
--     rs_document_formal_relation         → kept as-is AND fanned out to case_citation
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
-- case_segment, legislation, legal_provision, legislation_alias,
-- rs_document_law_reference.
-- =============================================================================
