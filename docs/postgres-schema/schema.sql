-- =============================================================================
-- CLE unified schema — CJEU + ECHR + Rechtspraak
-- =============================================================================
-- Delta from the prior draft (all changes are CJEU-driven):
--
--   NEW TABLES
--     + court_formation        — CJEU formation lookup (~15 rows), seeded below
--     + cjeu_national_document — sector-8 (national CJEU-referred) satellite
--
--   MODIFIED TABLES
--     • cjeu_document
--         − dropped  "formation timestamp"  (schema typo — formation is text/lookup, not a date)
--         + case_id int NOT NULL FK      (proper anchor to case; the prior
--                                          celex_id+ecli dual-FK is redundant and
--                                          fragile — kept those columns as
--                                          denormalized copies but dropped the FKs)
--         + sector text                  ('6' = CJEU direct, '8' = national referral)
--         + formation_id int FK          → court_formation
--         + procedure_result text        (parsed from type_procedure)
--         + journal_refs text            (was: cases.references_journals)
--         + erecueil_ref text            (was: cases.case_law_published_in_erecueil)
--         + local_identifier text        (rare, case-internal ID)
--         + dossier_uri text             (18.8% populated — groups Opinion + Judgment + Order)
--         + dossier_parent_case_id int FK→ case  (nullable; the "main" case in the dossier)
--     • cjeu_ag_opinion
--         + opinion_uri text             (URI of the opinion document — from cases.conclusions)
--     • case_text
--         + text_format text             (xhtml / html / pdf / fmx4)
--         + missing_reasons text         (e.g. FULLTEXT_UNAVAILABLE_UPSTREAM)
--         + UNIQUE(case_id, language)    (currently only "id PK" exists — nothing
--                                          stops two rows for the same case+lang)
--     • case_citation
--         + extractor_version text       (auditability across pipeline versions)
--         + target_celex_raw text        (nullable — for citations pointing outside
--                                          the loaded corpus, e.g. pre-1954 ECSC
--                                          decisions or external EPO cases)
--
--   NEW INDEXES (hot-path joins that were missing)
--     + case_law_reference (case_id, legislation_id, provision_id)
--     + case_judge         (case_id, judge_id)
--     + case_domain        (domain_id)
--     + case_citation      (relation_type)
--     + cjeu_document      UNIQUE(case_id)
--     + cjeu_ag_opinion    UNIQUE(case_id)
--     + cjeu_national_document UNIQUE(case_id)
--
--   NON-CHANGES (kept verbatim from the draft)
--     • echr_document, echr_document_appno
--     • rs_document, rs_document_publication, rs_document_formal_relation,
--       rs_document_external_authority
--     • lido_link
--     • case_segment / case_entity / case_cluster* / case_network_metric /
--       network_snapshot / search_query_log
--     • all lookups (court, judge, party, legislation, legal_provision,
--       language, domain, jurisdiction, instance, document_type, procedure_type)
--
--   ADVISORY — not applied
--     • case_text.summary_embedding and case_segment.embedding are typed `text`
--       in the current draft but should be `vector(N)` when pgvector is enabled.
--       HNSW indexes are similarly deferred.
--     • case_text.fulltext_tsv should be `tsvector` when the FTS pipeline is
--       enabled.
--     • CHECK constraints on case_citation.relation_type and
--       case_law_reference.role are provided as inline comments — enable them
--       once the value sets stabilise across all three corpora.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS "public";

-- =============================================================================
-- Lookups
-- =============================================================================

CREATE TABLE "public"."language" (
    "iso_code" text NOT NULL,
    "name" text,
    PRIMARY KEY ("iso_code")
);

CREATE TABLE "public"."jurisdiction" (
    "id" bigserial NOT NULL,
    "iso_code" text UNIQUE,
    "name" text,
    "type" text,
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

CREATE TABLE "public"."legislation" (
    "id" bigserial NOT NULL,
    "identifier" text,
    "scheme" text,
    "title" text,
    "jurisdiction_id" bigint,
    "document_type" text,
    "enacted_date" date,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."legislation_alias" (
    "id" bigserial NOT NULL,
    "legislation_id" bigint,
    "alias" text,
    "source" text,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."legal_provision" (
    "id" bigserial NOT NULL,
    "legislation_id" bigint,
    "article_label" text,
    "paragraph" text,
    "text" text,
    "effective_from" date,
    "effective_to" date,
    "snapshot_date" date,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."domain" (
    "id" serial NOT NULL,
    "scheme" text,           -- 'cjeu_subject_matter' | 'eurovoc' | 'cjeu_keyword' | 'cjeu_directory_code' | 'rs_...' | 'echr_...'
    "name" text,
    "uri" text,
    "parent_id" int,
    PRIMARY KEY ("id")
);

-- NEW — CJEU formation lookup
CREATE TABLE "public"."court_formation" (
    "id" serial NOT NULL,
    "code" text UNIQUE,        -- 'GC' | 'FC' | '1C' | ... | 'PR' | 'SOLE'
    "label" text,
    "judge_count" smallint,
    PRIMARY KEY ("id")
);

-- =============================================================================
-- Core case data (shared across CJEU / ECHR / Rechtspraak)
-- =============================================================================

CREATE TABLE "public"."case" (
    "id" int NOT NULL,
    "ecli" text UNIQUE,
    "item_id" text UNIQUE,
    "source" text,                   -- 'CJEU' | 'ECHR' | 'RS'
    "celex_id" text UNIQUE,
    "title" text,
    "date_decision" date,
    "date_published" text,
    "court_id" bigint,
    "language_iso" text,
    "document_type_id" int,
    "procedure_type_id" int,
    "instance_id" int,
    "case_number" text,
    "importance" smallint,
    "is_landmark" boolean,
    "created_at" timestamp,
    "updated_at" timestamp,
    PRIMARY KEY ("id")
);
CREATE INDEX "case_idx_case_court"       ON "public"."case" ("court_id");
CREATE INDEX "case_idx_case_date"        ON "public"."case" ("date_decision");
CREATE INDEX "case_idx_case_ecli"        ON "public"."case" ("ecli");

CREATE TABLE "public"."case_text" (
    "id" bigserial NOT NULL,
    "case_id" int,
    "language" text,
    "fulltext" text,
    "summary" text,
    "fulltext_tsv" text,             -- upgrade to tsvector when FTS pipeline is on
    "summary_embedding" text,        -- upgrade to vector(N) when pgvector is on
    "embedding_model" text,
    "source" text,                   -- 'INFOCURIA_BLOB_HTML' | 'CELLAR_ITEM' | 'EXTRACTOR_FALLBACK_TEXT' | RS/ECHR equivalents
    "text_format" text,              -- NEW: xhtml | html | pdf | fmx4
    "missing_reasons" text,          -- NEW: e.g. 'FULLTEXT_UNAVAILABLE_UPSTREAM'
    PRIMARY KEY ("id")
);
CREATE INDEX          "case_text_idx_case_text_case" ON "public"."case_text" ("case_id");
CREATE UNIQUE INDEX   "case_text_uk_case_language"    ON "public"."case_text" ("case_id", "language");

CREATE TABLE "public"."case_judge" (
    "id" bigserial NOT NULL,
    "case_id" int,
    "judge_id" bigint,
    "role" text,                     -- 'rapporteur' | 'judge' | 'president' | ...
    PRIMARY KEY ("id")
);
CREATE INDEX "case_judge_idx_case_id"  ON "public"."case_judge" ("case_id");
CREATE INDEX "case_judge_idx_judge_id" ON "public"."case_judge" ("judge_id");

CREATE TABLE "public"."case_party" (
    "case_id" int,
    "party_id" int,
    "role" text,                     -- 'applicant' | 'defendant' | 'referring_state' | 'defendant_agent' | ...
    PRIMARY KEY ("case_id", "party_id", "role")
);

CREATE TABLE "public"."case_domain" (
    "case_id" int,
    "domain_id" int,
    PRIMARY KEY ("case_id", "domain_id")
);
CREATE INDEX "case_domain_idx_domain_id" ON "public"."case_domain" ("domain_id");

CREATE TABLE "public"."case_law_reference" (
    "id" bigserial NOT NULL,
    "case_id" int,
    "provision_id" bigint,           -- nullable: CJEU often cites whole acts
    "legislation_id" bigint,
    "raw_reference" text,
    "role" text,
    -- CJEU adds ~18 values: based_on_treaty, legal_basis, affects, amends,
    -- amends_by_correction, confirms, interprets, interprets_judgement,
    -- declares_void, declares_void_by_preliminary_ruling,
    -- incidentally_declares_void, declares_valid, declares_incidentally_valid,
    -- states_failure, suspends_application, immediately_enforces,
    -- incorporates, corrects
    "source" text,
    PRIMARY KEY ("id")
);
CREATE INDEX "case_law_reference_idx_case_id"       ON "public"."case_law_reference" ("case_id");
CREATE INDEX "case_law_reference_idx_legislation"   ON "public"."case_law_reference" ("legislation_id");
CREATE INDEX "case_law_reference_idx_provision"     ON "public"."case_law_reference" ("provision_id");

CREATE TABLE "public"."case_citation" (
    "id" bigserial NOT NULL,
    "source_case_id" int,
    "target_case_id" int,            -- nullable when target is outside the corpus
    "target_celex_raw" text,         -- NEW: unresolved CELEX/ECLI when target_case_id IS NULL
    "relation_type" text,
    -- CJEU adds 8 values on top of existing ones:
    -- 'cites' | 'cited_by' | 'joins' | 'subject_to_appeal' |
    -- 'reexamined_by' | 'is_about_concept' |
    -- 'referred_for_preliminary_ruling' | 'interprets_judgement' |
    -- 'logical_successor_of'
    "source_dataset" text,           -- 'cellar_sparql' (CJEU) | 'rechtspraak_lido' | 'echr_hudoc' | ...
    "weight" int,
    "context_segment_id" bigint,     -- NULL for SPARQL-derived (CJEU); populated when text-extracted (RS)
    "is_cross_jurisdiction" boolean,
    "extractor_at" timestamptz,
    "extractor_version" text,        -- NEW: pipeline version stamp
    PRIMARY KEY ("id")
);
CREATE INDEX "case_citation_idx_citation_source"  ON "public"."case_citation" ("source_case_id");
CREATE INDEX "case_citation_idx_citation_target"  ON "public"."case_citation" ("target_case_id");
CREATE INDEX "case_citation_idx_relation_type"    ON "public"."case_citation" ("relation_type");

-- =============================================================================
-- CJEU-specific extensions
-- =============================================================================

CREATE TABLE "public"."cjeu_document" (
    "id" bigserial NOT NULL,
    "case_id" int NOT NULL,          -- NEW: proper anchor; replaces the celex_id+ecli dual-FK
    "celex_id" text,                 -- denormalized from case.celex_id
    "ecli" text,                     -- denormalized from case.ecli
    "sector" text,                   -- NEW: '6' or '8'
    "case_number" text,
    "formation_id" int,              -- NEW FK: replaces the schema-typo "formation timestamp"
    "proc_type" text,
    "procedure_result" text,         -- NEW: parsed from type_procedure ('successful' | 'unfounded' | 'inadmissible')
    "subject_matter" text,           -- kept for legacy; migrate readers to case_domain(scheme='cjeu_subject_matter')
    "parties_text" text,             -- kept for legacy; migrate readers to case_party
    "date_lodged" date,
    "cellar_uri" text,
    "work_uri" text,
    "journal_refs" text,             -- NEW: 'OJ C 123/45'
    "erecueil_ref" text,             -- NEW: European Court Reports citation
    "local_identifier" text,         -- NEW: rare, case-internal ID
    "dossier_uri" text,              -- NEW: groups Opinion + Judgment + Order in a dossier
    "dossier_parent_case_id" int,    -- NEW: resolved post-load; the main case in the dossier
    PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "cjeu_document_uk_case_id" ON "public"."cjeu_document" ("case_id");

CREATE TABLE "public"."cjeu_ag_opinion" (
    "id" serial NOT NULL,
    "case_id" int NOT NULL,          -- the Opinion's own case row
    "parent_case_id" int,            -- the judgment this opinion is for
    "advocate_general" text,
    "opinion_uri" text,              -- NEW: URI of the opinion document
    "delivered_date" date,
    PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "cjeu_ag_opinion_uk_case_id" ON "public"."cjeu_ag_opinion" ("case_id");

-- NEW: sector-8 satellite (~1,800 rows out of 46,180 CJEU cases)
CREATE TABLE "public"."cjeu_national_document" (
    "id" bigserial NOT NULL,
    "case_id" int NOT NULL,
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
    PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "cjeu_national_document_uk_case_id" ON "public"."cjeu_national_document" ("case_id");

-- =============================================================================
-- ECHR-specific extensions (unchanged from draft)
-- =============================================================================

CREATE TABLE "public"."echr_document" (
    "item_id" text NOT NULL,
    "ecli" text UNIQUE,
    "appno" text,
    "docname" text,
    "doctype_branch" text,
    "judgement_date" timestamp,
    "conclusion" text,
    "violation" text,
    "nonviolation" text,
    "respondent" text,
    "originating_body" text,
    "rules_of_court" text,
    PRIMARY KEY ("item_id")
);

CREATE TABLE "public"."echr_document_appno" (
    "id" bigserial NOT NULL,
    "ecli" text,
    "appno" text,
    PRIMARY KEY ("id")
);

-- =============================================================================
-- Rechtspraak-specific extensions (unchanged from draft)
-- =============================================================================

CREATE TABLE "public"."rs_document" (
    "ecli" text NOT NULL,
    "zaaknummer" text,
    "creator_uri" text,
    "replaces_identifier" text,
    "vindplaatsen" text[],
    "zittingsplaats" text,
    "access_rights" text,
    "opendata_status" text,
    "snapshot_date" text,
    PRIMARY KEY ("ecli")
);

CREATE TABLE "public"."rs_document_publication" (
    "id" bigserial NOT NULL,
    "ecli" text,
    "raw_text" text,
    "journal_abbr" text,
    "year" int,
    "locator" text,
    "annotator" text,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."rs_document_formal_relation" (
    "id" bigserial NOT NULL,
    "ecli" text,
    "target_ecli" text,
    "relation_type" text,
    "disposition" text,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."rs_document_external_authority" (
    "id" bigserial NOT NULL,
    "ecli" text,
    "name" text,
    "raw_text" text,
    PRIMARY KEY ("id")
);

CREATE TABLE "public"."lido_link" (
    "id" bigserial NOT NULL,
    "source_ecli" text,
    "source_uri" text,
    "target_ecli" text,
    "target_uri" text,
    "target_provision_id" bigint,
    "link_type" text,
    "fetched_at" timestamptz,
    PRIMARY KEY ("id")
);

-- =============================================================================
-- Downstream analytics (unchanged from draft)
-- =============================================================================

CREATE TABLE "public"."case_segment" (
    "id" bigserial NOT NULL,
    "case_id" int,
    "language" text,
    "segment_type" text,
    "segment_index" int,
    "segment_text" text,
    "segment_hash" text,
    "embedding" text,                -- upgrade to vector(N) when pgvector is on
    "embedding_model" text,
    "extractor_version" text,
    PRIMARY KEY ("id")
);
CREATE INDEX "case_segment_idx_case_segment_case" ON "public"."case_segment" ("case_id");

CREATE TABLE "public"."case_entity" (
    "id" bigserial NOT NULL,
    "case_id" int,
    "entity_type" text,
    "canonical_name" text,
    "surface_form" text,
    "uri" text,
    "char_start" int,
    "char_end" int,
    "confidence" real,
    PRIMARY KEY ("id")
);

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
    "case_id" int,
    PRIMARY KEY ("cluster_id", "case_id")
);

CREATE TABLE "public"."case_network_metric" (
    "snapshot_id" int,
    "case_id" int,
    "in_degree" int,
    "out_degree" int,
    "pagerank" real,
    "betweenness" real,
    "hub_score" real,
    "authority_score" real,
    "eigenvector" real,
    PRIMARY KEY ("snapshot_id", "case_id")
);
CREATE INDEX "case_network_metric_idx_metric_case" ON "public"."case_network_metric" ("case_id");

CREATE TABLE "public"."network_snapshot" (
    "id" serial NOT NULL,
    "snapshot_date" date,
    "description" text,
    "node_count" int,
    "edge_count" int,
    "created_at" timestamptz,
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
    "created_at" timestamptz,
    PRIMARY KEY ("id")
);

-- =============================================================================
-- Foreign key constraints
-- =============================================================================

-- Lookups
ALTER TABLE "public"."court"       ADD CONSTRAINT "fk_court_jurisdiction_id_jurisdiction_id" FOREIGN KEY("jurisdiction_id") REFERENCES "public"."jurisdiction"("id");
ALTER TABLE "public"."court"       ADD CONSTRAINT "fk_court_parent_court_id_court_id"        FOREIGN KEY("parent_court_id") REFERENCES "public"."court"("id");
ALTER TABLE "public"."judge"       ADD CONSTRAINT "fk_judge_court_id_court_id"                FOREIGN KEY("court_id") REFERENCES "public"."court"("id");
ALTER TABLE "public"."party"       ADD CONSTRAINT "fk_party_country_iso_jurisdiction_iso_code" FOREIGN KEY("country_iso") REFERENCES "public"."jurisdiction"("iso_code");
ALTER TABLE "public"."legislation" ADD CONSTRAINT "fk_legislation_jurisdiction_id_jurisdiction_id" FOREIGN KEY("jurisdiction_id") REFERENCES "public"."jurisdiction"("id");
ALTER TABLE "public"."legislation_alias" ADD CONSTRAINT "fk_legislation_alias_legislation_id_legislation_id" FOREIGN KEY("legislation_id") REFERENCES "public"."legislation"("id");
ALTER TABLE "public"."legal_provision"   ADD CONSTRAINT "fk_legal_provision_legislation_id_legislation_id"   FOREIGN KEY("legislation_id") REFERENCES "public"."legislation"("id");
ALTER TABLE "public"."domain"      ADD CONSTRAINT "fk_domain_parent_id_domain_id"            FOREIGN KEY("parent_id") REFERENCES "public"."domain"("id");

-- Case
ALTER TABLE "public"."case" ADD CONSTRAINT "fk_case_court_id_court_id"                     FOREIGN KEY("court_id") REFERENCES "public"."court"("id");
ALTER TABLE "public"."case" ADD CONSTRAINT "fk_case_document_type_id_document_type_id"     FOREIGN KEY("document_type_id") REFERENCES "public"."document_type"("id");
ALTER TABLE "public"."case" ADD CONSTRAINT "fk_case_procedure_type_id_procedure_type_id"   FOREIGN KEY("procedure_type_id") REFERENCES "public"."procedure_type"("id");
ALTER TABLE "public"."case" ADD CONSTRAINT "fk_case_instance_id_instance_id"                FOREIGN KEY("instance_id") REFERENCES "public"."instance"("id");
ALTER TABLE "public"."case" ADD CONSTRAINT "fk_case_language_iso_language_iso_code"         FOREIGN KEY("language_iso") REFERENCES "public"."language"("iso_code");

-- Case satellites
ALTER TABLE "public"."case_text" ADD CONSTRAINT "fk_case_text_case_id_case_id"              FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_text" ADD CONSTRAINT "fk_case_text_language_language_iso_code"   FOREIGN KEY("language") REFERENCES "public"."language"("iso_code");
ALTER TABLE "public"."case_judge" ADD CONSTRAINT "fk_case_judge_case_id_case_id"            FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_judge" ADD CONSTRAINT "fk_case_judge_judge_id_judge_id"          FOREIGN KEY("judge_id") REFERENCES "public"."judge"("id");
ALTER TABLE "public"."case_party" ADD CONSTRAINT "fk_case_party_case_id_case_id"            FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_party" ADD CONSTRAINT "fk_case_party_party_id_party_id"          FOREIGN KEY("party_id") REFERENCES "public"."party"("id");
ALTER TABLE "public"."case_domain" ADD CONSTRAINT "fk_case_domain_case_id_case_id"          FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_domain" ADD CONSTRAINT "fk_case_domain_domain_id_domain_id"      FOREIGN KEY("domain_id") REFERENCES "public"."domain"("id");
ALTER TABLE "public"."case_law_reference" ADD CONSTRAINT "fk_case_law_reference_case_id_case_id"           FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_law_reference" ADD CONSTRAINT "fk_case_law_reference_legislation_id_legislation_id" FOREIGN KEY("legislation_id") REFERENCES "public"."legislation"("id");
ALTER TABLE "public"."case_law_reference" ADD CONSTRAINT "fk_case_law_reference_provision_id_legal_provision_id" FOREIGN KEY("provision_id") REFERENCES "public"."legal_provision"("id");

-- Case citations (proper FKs to case.id; target_case_id nullable for unresolved)
ALTER TABLE "public"."case_citation" ADD CONSTRAINT "fk_case_citation_source_case_id_case_id"           FOREIGN KEY("source_case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_citation" ADD CONSTRAINT "fk_case_citation_target_case_id_case_id"           FOREIGN KEY("target_case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_citation" ADD CONSTRAINT "fk_case_citation_context_segment_id_case_segment_id" FOREIGN KEY("context_segment_id") REFERENCES "public"."case_segment"("id");

-- CJEU
ALTER TABLE "public"."cjeu_document"          ADD CONSTRAINT "fk_cjeu_document_case_id_case_id"                        FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."cjeu_document"          ADD CONSTRAINT "fk_cjeu_document_formation_id_court_formation_id"        FOREIGN KEY("formation_id") REFERENCES "public"."court_formation"("id");
ALTER TABLE "public"."cjeu_document"          ADD CONSTRAINT "fk_cjeu_document_dossier_parent_case_id_case_id"         FOREIGN KEY("dossier_parent_case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."cjeu_ag_opinion"        ADD CONSTRAINT "fk_cjeu_ag_opinion_case_id_case_id"                      FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."cjeu_ag_opinion"        ADD CONSTRAINT "fk_cjeu_ag_opinion_parent_case_id_case_id"               FOREIGN KEY("parent_case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."cjeu_national_document" ADD CONSTRAINT "fk_cjeu_national_document_case_id_case_id"               FOREIGN KEY("case_id") REFERENCES "public"."case"("id");

-- ECHR
ALTER TABLE "public"."echr_document"       ADD CONSTRAINT "fk_echr_document_ecli_case_ecli"                       FOREIGN KEY("ecli") REFERENCES "public"."case"("ecli");
ALTER TABLE "public"."echr_document"       ADD CONSTRAINT "fk_echr_document_item_id_case_item_id"                 FOREIGN KEY("item_id") REFERENCES "public"."case"("item_id");
ALTER TABLE "public"."echr_document_appno" ADD CONSTRAINT "fk_echr_document_appno_ecli_echr_document_ecli"        FOREIGN KEY("ecli") REFERENCES "public"."echr_document"("ecli");

-- Rechtspraak
ALTER TABLE "public"."rs_document"                    ADD CONSTRAINT "fk_rs_document_ecli_case_ecli"                              FOREIGN KEY("ecli") REFERENCES "public"."case"("ecli");
ALTER TABLE "public"."rs_document_external_authority" ADD CONSTRAINT "fk_rs_document_external_authority_ecli_rs_document_ecli"   FOREIGN KEY("ecli") REFERENCES "public"."rs_document"("ecli");
ALTER TABLE "public"."rs_document_formal_relation"    ADD CONSTRAINT "fk_rs_document_formal_relation_ecli_rs_document_ecli"      FOREIGN KEY("ecli") REFERENCES "public"."rs_document"("ecli");
ALTER TABLE "public"."rs_document_formal_relation"    ADD CONSTRAINT "fk_rs_document_formal_relation_target_ecli_case_ecli"      FOREIGN KEY("target_ecli") REFERENCES "public"."case"("ecli");
ALTER TABLE "public"."rs_document_publication"        ADD CONSTRAINT "fk_rs_document_publication_ecli_rs_document_ecli"          FOREIGN KEY("ecli") REFERENCES "public"."rs_document"("ecli");
ALTER TABLE "public"."lido_link"                      ADD CONSTRAINT "fk_lido_link_source_ecli_case_ecli"                        FOREIGN KEY("source_ecli") REFERENCES "public"."case"("ecli");
ALTER TABLE "public"."lido_link"                      ADD CONSTRAINT "fk_lido_link_target_ecli_case_ecli"                        FOREIGN KEY("target_ecli") REFERENCES "public"."case"("ecli");
ALTER TABLE "public"."lido_link"                      ADD CONSTRAINT "fk_lido_link_target_provision_id_legal_provision_id"       FOREIGN KEY("target_provision_id") REFERENCES "public"."legal_provision"("id");

-- Downstream
ALTER TABLE "public"."case_segment"            ADD CONSTRAINT "fk_case_segment_case_id_case_id"                  FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_segment"            ADD CONSTRAINT "fk_case_segment_language_language_iso_code"        FOREIGN KEY("language") REFERENCES "public"."language"("iso_code");
ALTER TABLE "public"."case_entity"             ADD CONSTRAINT "fk_case_entity_case_id_case_id"                    FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_cluster"            ADD CONSTRAINT "fk_case_cluster_snapshot_id_network_snapshot_id"   FOREIGN KEY("snapshot_id") REFERENCES "public"."network_snapshot"("id");
ALTER TABLE "public"."case_cluster_membership" ADD CONSTRAINT "fk_case_cluster_membership_case_id_case_id"        FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_cluster_membership" ADD CONSTRAINT "fk_case_cluster_membership_cluster_id_case_cluster_id" FOREIGN KEY("cluster_id") REFERENCES "public"."case_cluster"("id");
ALTER TABLE "public"."case_network_metric"     ADD CONSTRAINT "fk_case_network_metric_case_id_case_id"            FOREIGN KEY("case_id") REFERENCES "public"."case"("id");
ALTER TABLE "public"."case_network_metric"     ADD CONSTRAINT "fk_case_network_metric_snapshot_id_network_snapshot_id" FOREIGN KEY("snapshot_id") REFERENCES "public"."network_snapshot"("id");

-- =============================================================================
-- Seed data — court_formation lookup
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
