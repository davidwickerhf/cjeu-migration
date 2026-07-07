-- GENERATED from docs/postgres-schema/schema_full.sql — DO NOT EDIT.
-- Applied AFTER bulk load: indexes, FKs, triggers, views.
-- NOTE: run 41_counts.sql (citation counts rebuild) BEFORE this file installs
-- the trigger, or run it right after — the trigger only tracks NEW mutations.

SET search_path TO cle_v2, public;
SET maintenance_work_mem = '512MB';

-- Dedupe before unique-index builds: bulk steps run without the dedup
-- indexes (created below), so a partially-failed-then-resumed load can
-- have double-inserted. Keep the lowest id of each logical row.
-- raw-bearing rows: the raw tuple IS the identity; keep the row with the
-- best (highest) provision resolution, then lowest id — resolution columns
-- deliberately NOT in the partition (snapshot fanout made them differ)
DELETE FROM "cle_v2"."case_law_reference" a USING (
    SELECT id, row_number() OVER (
        PARTITION BY case_id, role, source_dataset, raw_scheme, raw_resource,
                     COALESCE(raw_subdivision,'')
        ORDER BY provision_id DESC NULLS LAST, id) AS rn
    FROM "cle_v2"."case_law_reference" WHERE raw_resource IS NOT NULL
) d WHERE a.id = d.id AND d.rn > 1;

-- resolved-only rows (no raw identifier): dedupe on the resolved tuple
DELETE FROM "cle_v2"."case_law_reference" a USING "cle_v2"."case_law_reference" b
WHERE a.id > b.id AND a.raw_resource IS NULL AND b.raw_resource IS NULL
  AND a.case_id = b.case_id AND a.role = b.role AND a.source_dataset = b.source_dataset
  AND a.legislation_id IS NOT DISTINCT FROM b.legislation_id
  AND a.provision_id   IS NOT DISTINCT FROM b.provision_id;

DELETE FROM "cle_v2"."case_citation" a USING "cle_v2"."case_citation" b
WHERE a.id > b.id AND a.source_case_id = b.source_case_id
  AND a.relation_type = b.relation_type AND a.source_dataset = b.source_dataset
  AND a.target_case_id   IS NOT DISTINCT FROM b.target_case_id
  AND a.target_ecli_raw  IS NOT DISTINCT FROM b.target_ecli_raw
  AND a.target_celex_raw IS NOT DISTINCT FROM b.target_celex_raw;

-- ============ Indexes (deferred until after bulk load) ============
CREATE INDEX IF NOT EXISTS "legislation_idx_scheme_identifier" ON "cle_v2"."legislation" ("scheme", "identifier");
CREATE INDEX IF NOT EXISTS "legislation_alias_idx_alias_lower" ON "cle_v2"."legislation_alias" (lower("alias"));
CREATE INDEX IF NOT EXISTS "legislation_alias_idx_alias_trgm"  ON "cle_v2"."legislation_alias" USING gin ("alias" gin_trgm_ops);
CREATE INDEX IF NOT EXISTS "legal_provision_idx_bwb_label" ON "cle_v2"."legal_provision" ("bwb_label_id");
CREATE INDEX IF NOT EXISTS "legal_provision_idx_lookup"    ON "cle_v2"."legal_provision" ("legislation_id", lower("article_label"), "element_type");
CREATE INDEX IF NOT EXISTS "case_idx_court"          ON "cle_v2"."cases" ("court_id");
CREATE INDEX IF NOT EXISTS "case_idx_date_decision"  ON "cle_v2"."cases" ("date_decision");
CREATE INDEX IF NOT EXISTS "case_idx_ecli"           ON "cle_v2"."cases" ("ecli");
CREATE INDEX IF NOT EXISTS "case_idx_source"         ON "cle_v2"."cases" ("source");
CREATE INDEX IF NOT EXISTS "case_idx_item_id"        ON "cle_v2"."cases" ("item_id");
CREATE INDEX IF NOT EXISTS "case_idx_title_trgm"     ON "cle_v2"."cases" USING gin ("title" gin_trgm_ops);
CREATE INDEX IF NOT EXISTS "case_idx_date_ecli"      ON "cle_v2"."cases" ("date_decision" DESC, "ecli");
CREATE INDEX IF NOT EXISTS "case_idx_importance"     ON "cle_v2"."cases" ("importance") WHERE "importance" IS NOT NULL;
CREATE INDEX IF NOT EXISTS "case_idx_case_number_trgm" ON "cle_v2"."cases" USING gin ("case_number" gin_trgm_ops);
CREATE INDEX IF NOT EXISTS "case_idx_case_number" ON "cle_v2"."cases" ("case_number");
CREATE INDEX IF NOT EXISTS "case_text_idx_case_id"          ON "cle_v2"."case_text" ("case_id");
CREATE INDEX IF NOT EXISTS "case_text_idx_fulltext_tsv"     ON "cle_v2"."case_text" USING gin ("fulltext_tsv");
CREATE INDEX IF NOT EXISTS "case_text_idx_summary_tsv"      ON "cle_v2"."case_text" USING gin ("summary_tsv");
CREATE INDEX IF NOT EXISTS "case_judge_idx_case_id"  ON "cle_v2"."case_judge" ("case_id");
CREATE INDEX IF NOT EXISTS "case_judge_idx_judge_id" ON "cle_v2"."case_judge" ("judge_id");
CREATE INDEX IF NOT EXISTS "case_party_idx_party" ON "cle_v2"."case_party" ("party_id");
CREATE INDEX IF NOT EXISTS "case_domain_idx_domain_id" ON "cle_v2"."case_domain" ("domain_id");
CREATE INDEX IF NOT EXISTS "case_law_reference_idx_case_id"     ON "cle_v2"."case_law_reference" ("case_id");
CREATE INDEX IF NOT EXISTS "case_law_reference_idx_legislation" ON "cle_v2"."case_law_reference" ("legislation_id");
CREATE INDEX IF NOT EXISTS "case_law_reference_idx_provision"   ON "cle_v2"."case_law_reference" ("provision_id");
CREATE INDEX IF NOT EXISTS "case_law_reference_idx_raw"         ON "cle_v2"."case_law_reference" ("raw_scheme", "raw_resource");
CREATE INDEX IF NOT EXISTS "case_law_reference_idx_raw_label"   ON "cle_v2"."case_law_reference" ("raw_label_id") WHERE "raw_label_id" IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS "case_law_reference_uk_provision" ON "cle_v2"."case_law_reference"
    ("case_id", "provision_id", "role", "source_dataset")
    WHERE "provision_id" IS NOT NULL AND "raw_resource" IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS "case_law_reference_uk_legislation" ON "cle_v2"."case_law_reference"
    ("case_id", "legislation_id", "role", "source_dataset")
    WHERE "provision_id" IS NULL AND "legislation_id" IS NOT NULL AND "raw_resource" IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS "case_law_reference_uk_raw" ON "cle_v2"."case_law_reference"
    ("case_id", "raw_scheme", "raw_resource", COALESCE("raw_subdivision", ''), "role", "source_dataset")
    WHERE "provision_id" IS NULL AND "legislation_id" IS NULL AND "raw_resource" IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS "case_citation_uk_resolved" ON "cle_v2"."case_citation"
    ("source_case_id", "target_case_id", "relation_type", "source_dataset")
    WHERE "target_case_id" IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS "case_citation_uk_unresolved_celex" ON "cle_v2"."case_citation"
    ("source_case_id", "target_celex_raw", "relation_type", "source_dataset")
    WHERE "target_case_id" IS NULL AND "target_celex_raw" IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS "case_citation_uk_unresolved_ecli" ON "cle_v2"."case_citation"
    ("source_case_id", "target_ecli_raw", "relation_type", "source_dataset")
    WHERE "target_case_id" IS NULL AND "target_ecli_raw" IS NOT NULL;
CREATE INDEX IF NOT EXISTS "case_citation_idx_source"        ON "cle_v2"."case_citation" ("source_case_id");
CREATE INDEX IF NOT EXISTS "case_citation_idx_target"        ON "cle_v2"."case_citation" ("target_case_id");
CREATE INDEX IF NOT EXISTS "case_citation_idx_relation_type" ON "cle_v2"."case_citation" ("relation_type");
CREATE INDEX IF NOT EXISTS "case_citation_idx_source_target" ON "cle_v2"."case_citation" ("source_case_id", "target_case_id");
CREATE INDEX IF NOT EXISTS "case_citation_idx_weight"        ON "cle_v2"."case_citation" ("weight") WHERE weight > 1;
CREATE INDEX IF NOT EXISTS "case_citation_idx_target_ecli_raw"  ON "cle_v2"."case_citation" ("target_ecli_raw")  WHERE target_ecli_raw  IS NOT NULL;
CREATE INDEX IF NOT EXISTS "case_citation_idx_target_celex_raw" ON "cle_v2"."case_citation" ("target_celex_raw") WHERE target_celex_raw IS NOT NULL;
CREATE INDEX IF NOT EXISTS "echr_document_idx_case_lang" ON "cle_v2"."echr_document" ("case_id", "language");
CREATE INDEX IF NOT EXISTS "echr_document_idx_doctype"          ON "cle_v2"."echr_document" ("doctype");
CREATE INDEX IF NOT EXISTS "echr_document_idx_doctype_branch"   ON "cle_v2"."echr_document" ("doctype_branch");
CREATE INDEX IF NOT EXISTS "echr_document_idx_judgement_date"   ON "cle_v2"."echr_document" ("judgement_date");
CREATE INDEX IF NOT EXISTS "echr_document_idx_reference_date"   ON "cle_v2"."echr_document" ("reference_date");
CREATE INDEX IF NOT EXISTS "echr_document_idx_judgement_year"   ON "cle_v2"."echr_document" ("judgement_year");
CREATE INDEX IF NOT EXISTS "echr_document_idx_originating_body" ON "cle_v2"."echr_document" ("originating_body");
CREATE INDEX IF NOT EXISTS "echr_document_idx_docname_trgm"     ON "cle_v2"."echr_document" USING gin ("docname" gin_trgm_ops);
CREATE INDEX IF NOT EXISTS "echr_document_idx_issue_trgm"       ON "cle_v2"."echr_document" USING gin ("issue" gin_trgm_ops);
CREATE INDEX IF NOT EXISTS "echr_document_appno_idx_appno"      ON "cle_v2"."echr_document_appno" ("appno");
CREATE INDEX IF NOT EXISTS "echr_document_appno_idx_source"     ON "cle_v2"."echr_document_appno" ("source");
CREATE INDEX IF NOT EXISTS "echr_document_article_idx_filter" ON "cle_v2"."echr_document_article" ("kind", "article_code");
CREATE INDEX IF NOT EXISTS "echr_extractor_segments_idx_parser"      ON "cle_v2"."echr_extractor_segments" ("parser_mode");
CREATE INDEX IF NOT EXISTS "echr_extractor_segments_idx_num_sections" ON "cle_v2"."echr_extractor_segments" ("num_sections");
CREATE INDEX IF NOT EXISTS "rs_document_idx_date_decision" ON "cle_v2"."rs_document" ("date_decision");
CREATE INDEX IF NOT EXISTS "rs_document_idx_domains_gin"   ON "cle_v2"."rs_document" USING gin ("domains");
CREATE INDEX IF NOT EXISTS "rs_document_idx_date_issued"   ON "cle_v2"."rs_document" ("date_issued");
CREATE INDEX IF NOT EXISTS "rs_document_idx_date_modified" ON "cle_v2"."rs_document" ("date_modified");
CREATE INDEX IF NOT EXISTS "rs_document_publication_idx_journal" ON "cle_v2"."rs_document_publication" ("journal_abbr");
CREATE INDEX IF NOT EXISTS "lido_link_idx_source_case" ON "cle_v2"."lido_link" ("source_case_id");
CREATE INDEX IF NOT EXISTS "lido_link_idx_target_case" ON "cle_v2"."lido_link" ("target_case_id");
CREATE INDEX IF NOT EXISTS "case_segment_idx_case_id"        ON "cle_v2"."case_segment" ("case_id");
CREATE INDEX IF NOT EXISTS "case_entity_idx_case_id" ON "cle_v2"."case_entity" ("case_id");
CREATE UNIQUE INDEX IF NOT EXISTS "case_summary_version_uk_current"
    ON "cle_v2"."case_summary_version" ("case_id", "segment_scope", "summarization_model")
    WHERE "is_current" = true AND "rejected_at" IS NULL;
CREATE INDEX IF NOT EXISTS "case_summary_version_idx_case" ON "cle_v2"."case_summary_version" ("case_id");
CREATE INDEX IF NOT EXISTS "case_network_metric_idx_case" ON "cle_v2"."case_network_metric" ("case_id");

-- ============ Foreign keys ============
ALTER TABLE "cle_v2"."court" DROP CONSTRAINT IF EXISTS fk_court_jurisdiction;
ALTER TABLE "cle_v2"."court"             ADD CONSTRAINT fk_court_jurisdiction         FOREIGN KEY ("jurisdiction_id")     REFERENCES "cle_v2"."jurisdiction"("id");
ALTER TABLE "cle_v2"."court" DROP CONSTRAINT IF EXISTS fk_court_parent_court;
ALTER TABLE "cle_v2"."court"             ADD CONSTRAINT fk_court_parent_court         FOREIGN KEY ("parent_court_id")     REFERENCES "cle_v2"."court"("id");
ALTER TABLE "cle_v2"."judge" DROP CONSTRAINT IF EXISTS fk_judge_court;
ALTER TABLE "cle_v2"."judge"             ADD CONSTRAINT fk_judge_court                FOREIGN KEY ("court_id")            REFERENCES "cle_v2"."court"("id");
ALTER TABLE "cle_v2"."party" DROP CONSTRAINT IF EXISTS fk_party_country;
ALTER TABLE "cle_v2"."party"             ADD CONSTRAINT fk_party_country              FOREIGN KEY ("country_iso")         REFERENCES "cle_v2"."jurisdiction"("iso_code");
ALTER TABLE "cle_v2"."legislation" DROP CONSTRAINT IF EXISTS fk_legislation_jurisdiction;
ALTER TABLE "cle_v2"."legislation"       ADD CONSTRAINT fk_legislation_jurisdiction   FOREIGN KEY ("jurisdiction_id")     REFERENCES "cle_v2"."jurisdiction"("id");
ALTER TABLE "cle_v2"."legislation_alias" DROP CONSTRAINT IF EXISTS fk_legislation_alias;
ALTER TABLE "cle_v2"."legislation_alias" ADD CONSTRAINT fk_legislation_alias          FOREIGN KEY ("legislation_id")      REFERENCES "cle_v2"."legislation"("id");
ALTER TABLE "cle_v2"."legal_provision" DROP CONSTRAINT IF EXISTS fk_legal_provision;
ALTER TABLE "cle_v2"."legal_provision"   ADD CONSTRAINT fk_legal_provision            FOREIGN KEY ("legislation_id")      REFERENCES "cle_v2"."legislation"("id");
ALTER TABLE "cle_v2"."legal_provision" DROP CONSTRAINT IF EXISTS fk_legal_provision_parent;
ALTER TABLE "cle_v2"."legal_provision"   ADD CONSTRAINT fk_legal_provision_parent     FOREIGN KEY ("parent_id")           REFERENCES "cle_v2"."legal_provision"("id");
ALTER TABLE "cle_v2"."domain" DROP CONSTRAINT IF EXISTS fk_domain_parent;
ALTER TABLE "cle_v2"."domain"            ADD CONSTRAINT fk_domain_parent              FOREIGN KEY ("parent_id")           REFERENCES "cle_v2"."domain"("id");
ALTER TABLE "cle_v2"."domain_label" DROP CONSTRAINT IF EXISTS fk_domain_label_domain;
ALTER TABLE "cle_v2"."domain_label"      ADD CONSTRAINT fk_domain_label_domain        FOREIGN KEY ("domain_id")           REFERENCES "cle_v2"."domain"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."domain_label" DROP CONSTRAINT IF EXISTS fk_domain_label_language;
ALTER TABLE "cle_v2"."domain_label"      ADD CONSTRAINT fk_domain_label_language      FOREIGN KEY ("language")            REFERENCES "cle_v2"."language"("iso_code");
ALTER TABLE "cle_v2"."cases" DROP CONSTRAINT IF EXISTS fk_case_court;
ALTER TABLE "cle_v2"."cases"              ADD CONSTRAINT fk_case_court                 FOREIGN KEY ("court_id")            REFERENCES "cle_v2"."court"("id");
ALTER TABLE "cle_v2"."cases" DROP CONSTRAINT IF EXISTS fk_case_document_type;
ALTER TABLE "cle_v2"."cases"              ADD CONSTRAINT fk_case_document_type         FOREIGN KEY ("document_type_id")    REFERENCES "cle_v2"."document_type"("id");
ALTER TABLE "cle_v2"."cases" DROP CONSTRAINT IF EXISTS fk_case_procedure_type;
ALTER TABLE "cle_v2"."cases"              ADD CONSTRAINT fk_case_procedure_type        FOREIGN KEY ("procedure_type_id")   REFERENCES "cle_v2"."procedure_type"("id");
ALTER TABLE "cle_v2"."cases" DROP CONSTRAINT IF EXISTS fk_case_instance;
ALTER TABLE "cle_v2"."cases"              ADD CONSTRAINT fk_case_instance              FOREIGN KEY ("instance_id")         REFERENCES "cle_v2"."instance"("id");
ALTER TABLE "cle_v2"."cases" DROP CONSTRAINT IF EXISTS fk_case_language;
ALTER TABLE "cle_v2"."cases"              ADD CONSTRAINT fk_case_language              FOREIGN KEY ("language_iso")        REFERENCES "cle_v2"."language"("iso_code");
ALTER TABLE "cle_v2"."case_text" DROP CONSTRAINT IF EXISTS fk_case_text_case;
ALTER TABLE "cle_v2"."case_text"         ADD CONSTRAINT fk_case_text_case             FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_text" DROP CONSTRAINT IF EXISTS fk_case_text_language;
ALTER TABLE "cle_v2"."case_text"         ADD CONSTRAINT fk_case_text_language         FOREIGN KEY ("language")            REFERENCES "cle_v2"."language"("iso_code");
ALTER TABLE "cle_v2"."case_judge" DROP CONSTRAINT IF EXISTS fk_case_judge_case;
ALTER TABLE "cle_v2"."case_judge"        ADD CONSTRAINT fk_case_judge_case            FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_judge" DROP CONSTRAINT IF EXISTS fk_case_judge_judge;
ALTER TABLE "cle_v2"."case_judge"        ADD CONSTRAINT fk_case_judge_judge           FOREIGN KEY ("judge_id")            REFERENCES "cle_v2"."judge"("id");
ALTER TABLE "cle_v2"."case_party" DROP CONSTRAINT IF EXISTS fk_case_party_case;
ALTER TABLE "cle_v2"."case_party"        ADD CONSTRAINT fk_case_party_case            FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_party" DROP CONSTRAINT IF EXISTS fk_case_party_party;
ALTER TABLE "cle_v2"."case_party"        ADD CONSTRAINT fk_case_party_party           FOREIGN KEY ("party_id")            REFERENCES "cle_v2"."party"("id");
ALTER TABLE "cle_v2"."case_domain" DROP CONSTRAINT IF EXISTS fk_case_domain_case;
ALTER TABLE "cle_v2"."case_domain"       ADD CONSTRAINT fk_case_domain_case           FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_domain" DROP CONSTRAINT IF EXISTS fk_case_domain_domain;
ALTER TABLE "cle_v2"."case_domain"       ADD CONSTRAINT fk_case_domain_domain         FOREIGN KEY ("domain_id")           REFERENCES "cle_v2"."domain"("id");
ALTER TABLE "cle_v2"."case_law_reference" DROP CONSTRAINT IF EXISTS fk_case_law_reference_case;
ALTER TABLE "cle_v2"."case_law_reference" ADD CONSTRAINT fk_case_law_reference_case   FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_law_reference" DROP CONSTRAINT IF EXISTS fk_case_law_reference_leg;
ALTER TABLE "cle_v2"."case_law_reference" ADD CONSTRAINT fk_case_law_reference_leg    FOREIGN KEY ("legislation_id")      REFERENCES "cle_v2"."legislation"("id");
ALTER TABLE "cle_v2"."case_law_reference" DROP CONSTRAINT IF EXISTS fk_case_law_reference_prov;
ALTER TABLE "cle_v2"."case_law_reference" ADD CONSTRAINT fk_case_law_reference_prov   FOREIGN KEY ("provision_id")        REFERENCES "cle_v2"."legal_provision"("id");
ALTER TABLE "cle_v2"."case_citation" DROP CONSTRAINT IF EXISTS fk_case_citation_source;
ALTER TABLE "cle_v2"."case_citation"     ADD CONSTRAINT fk_case_citation_source       FOREIGN KEY ("source_case_id")      REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_citation" DROP CONSTRAINT IF EXISTS fk_case_citation_target;
ALTER TABLE "cle_v2"."case_citation"     ADD CONSTRAINT fk_case_citation_target       FOREIGN KEY ("target_case_id")      REFERENCES "cle_v2"."cases"("id") ON DELETE SET NULL;
ALTER TABLE "cle_v2"."case_citation" DROP CONSTRAINT IF EXISTS fk_case_citation_context;
ALTER TABLE "cle_v2"."case_citation"     ADD CONSTRAINT fk_case_citation_context      FOREIGN KEY ("context_segment_id")  REFERENCES "cle_v2"."case_segment"("id") ON DELETE SET NULL;
ALTER TABLE "cle_v2"."case_citation_counts" DROP CONSTRAINT IF EXISTS fk_case_citation_counts;
ALTER TABLE "cle_v2"."case_citation_counts" ADD CONSTRAINT fk_case_citation_counts    FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."cjeu_document" DROP CONSTRAINT IF EXISTS fk_cjeu_document_case;
ALTER TABLE "cle_v2"."cjeu_document"           ADD CONSTRAINT fk_cjeu_document_case          FOREIGN KEY ("case_id")               REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."cjeu_document" DROP CONSTRAINT IF EXISTS fk_cjeu_document_formation;
ALTER TABLE "cle_v2"."cjeu_document"           ADD CONSTRAINT fk_cjeu_document_formation     FOREIGN KEY ("formation_id")          REFERENCES "cle_v2"."court_formation"("id");
ALTER TABLE "cle_v2"."cjeu_document" DROP CONSTRAINT IF EXISTS fk_cjeu_document_dossier;
ALTER TABLE "cle_v2"."cjeu_document"           ADD CONSTRAINT fk_cjeu_document_dossier       FOREIGN KEY ("dossier_parent_case_id") REFERENCES "cle_v2"."cases"("id") ON DELETE SET NULL;
ALTER TABLE "cle_v2"."cjeu_ag_opinion" DROP CONSTRAINT IF EXISTS fk_cjeu_ag_opinion_case;
ALTER TABLE "cle_v2"."cjeu_ag_opinion"         ADD CONSTRAINT fk_cjeu_ag_opinion_case        FOREIGN KEY ("case_id")               REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."cjeu_ag_opinion" DROP CONSTRAINT IF EXISTS fk_cjeu_ag_opinion_parent;
ALTER TABLE "cle_v2"."cjeu_ag_opinion"         ADD CONSTRAINT fk_cjeu_ag_opinion_parent      FOREIGN KEY ("parent_case_id")        REFERENCES "cle_v2"."cases"("id") ON DELETE SET NULL;
ALTER TABLE "cle_v2"."cjeu_national_document" DROP CONSTRAINT IF EXISTS fk_cjeu_national_document_case;
ALTER TABLE "cle_v2"."cjeu_national_document"  ADD CONSTRAINT fk_cjeu_national_document_case FOREIGN KEY ("case_id")               REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."echr_document" DROP CONSTRAINT IF EXISTS fk_echr_document_case;
ALTER TABLE "cle_v2"."echr_document"           ADD CONSTRAINT fk_echr_document_case          FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."echr_document" DROP CONSTRAINT IF EXISTS fk_echr_document_language;
ALTER TABLE "cle_v2"."echr_document"           ADD CONSTRAINT fk_echr_document_language      FOREIGN KEY ("language")            REFERENCES "cle_v2"."language"("iso_code");
ALTER TABLE "cle_v2"."echr_document_appno" DROP CONSTRAINT IF EXISTS fk_echr_document_appno_doc;
ALTER TABLE "cle_v2"."echr_document_appno"     ADD CONSTRAINT fk_echr_document_appno_doc     FOREIGN KEY ("item_id") REFERENCES "cle_v2"."echr_document"("item_id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."echr_document_article" DROP CONSTRAINT IF EXISTS fk_echr_document_article_doc;
ALTER TABLE "cle_v2"."echr_document_article"   ADD CONSTRAINT fk_echr_document_article_doc   FOREIGN KEY ("item_id") REFERENCES "cle_v2"."echr_document"("item_id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."echr_extractor_segments" DROP CONSTRAINT IF EXISTS fk_echr_extractor_segments_doc;
ALTER TABLE "cle_v2"."echr_extractor_segments" ADD CONSTRAINT fk_echr_extractor_segments_doc FOREIGN KEY ("item_id") REFERENCES "cle_v2"."echr_document"("item_id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."rs_document" DROP CONSTRAINT IF EXISTS fk_rs_document_case;
ALTER TABLE "cle_v2"."rs_document"                    ADD CONSTRAINT fk_rs_document_case             FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."rs_document_external_authority" DROP CONSTRAINT IF EXISTS fk_rs_document_ext_authority;
ALTER TABLE "cle_v2"."rs_document_external_authority" ADD CONSTRAINT fk_rs_document_ext_authority    FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."rs_document"("case_id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."rs_document_formal_relation" DROP CONSTRAINT IF EXISTS fk_rs_document_formal_source;
ALTER TABLE "cle_v2"."rs_document_formal_relation"    ADD CONSTRAINT fk_rs_document_formal_source    FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."rs_document"("case_id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."rs_document_formal_relation" DROP CONSTRAINT IF EXISTS fk_rs_document_formal_target;
ALTER TABLE "cle_v2"."rs_document_formal_relation"    ADD CONSTRAINT fk_rs_document_formal_target    FOREIGN KEY ("target_ecli")         REFERENCES "cle_v2"."cases"("ecli") ON DELETE SET NULL;
ALTER TABLE "cle_v2"."rs_document_publication" DROP CONSTRAINT IF EXISTS fk_rs_document_publication_case;
ALTER TABLE "cle_v2"."rs_document_publication"        ADD CONSTRAINT fk_rs_document_publication_case FOREIGN KEY ("case_id")             REFERENCES "cle_v2"."rs_document"("case_id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."lido_link" DROP CONSTRAINT IF EXISTS fk_lido_link_source_case;
ALTER TABLE "cle_v2"."lido_link" ADD CONSTRAINT fk_lido_link_source_case      FOREIGN KEY ("source_case_id")      REFERENCES "cle_v2"."cases"("id") ON DELETE SET NULL;
ALTER TABLE "cle_v2"."lido_link" DROP CONSTRAINT IF EXISTS fk_lido_link_target_case;
ALTER TABLE "cle_v2"."lido_link" ADD CONSTRAINT fk_lido_link_target_case      FOREIGN KEY ("target_case_id")      REFERENCES "cle_v2"."cases"("id") ON DELETE SET NULL;
ALTER TABLE "cle_v2"."lido_link" DROP CONSTRAINT IF EXISTS fk_lido_link_target_provision;
ALTER TABLE "cle_v2"."lido_link" ADD CONSTRAINT fk_lido_link_target_provision FOREIGN KEY ("target_provision_id") REFERENCES "cle_v2"."legal_provision"("id");
ALTER TABLE "cle_v2"."case_segment" DROP CONSTRAINT IF EXISTS fk_case_segment_case;
ALTER TABLE "cle_v2"."case_segment"            ADD CONSTRAINT fk_case_segment_case             FOREIGN KEY ("case_id")     REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_segment" DROP CONSTRAINT IF EXISTS fk_case_segment_language;
ALTER TABLE "cle_v2"."case_segment"            ADD CONSTRAINT fk_case_segment_language         FOREIGN KEY ("language")    REFERENCES "cle_v2"."language"("iso_code");
ALTER TABLE "cle_v2"."case_entity" DROP CONSTRAINT IF EXISTS fk_case_entity_case;
ALTER TABLE "cle_v2"."case_entity"             ADD CONSTRAINT fk_case_entity_case              FOREIGN KEY ("case_id")     REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_summary_version" DROP CONSTRAINT IF EXISTS fk_case_summary_version_case;
ALTER TABLE "cle_v2"."case_summary_version"    ADD CONSTRAINT fk_case_summary_version_case     FOREIGN KEY ("case_id")     REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_summary_version" DROP CONSTRAINT IF EXISTS fk_case_summary_version_language;
ALTER TABLE "cle_v2"."case_summary_version"    ADD CONSTRAINT fk_case_summary_version_language FOREIGN KEY ("language")    REFERENCES "cle_v2"."language"("iso_code");
ALTER TABLE "cle_v2"."case_summary_version" DROP CONSTRAINT IF EXISTS fk_case_summary_version_parent;
ALTER TABLE "cle_v2"."case_summary_version"    ADD CONSTRAINT fk_case_summary_version_parent   FOREIGN KEY ("parent_version_id") REFERENCES "cle_v2"."case_summary_version"("id") ON DELETE SET NULL;
ALTER TABLE "cle_v2"."case_cluster" DROP CONSTRAINT IF EXISTS fk_case_cluster_snapshot;
ALTER TABLE "cle_v2"."case_cluster"            ADD CONSTRAINT fk_case_cluster_snapshot         FOREIGN KEY ("snapshot_id") REFERENCES "cle_v2"."network_snapshot"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_cluster_membership" DROP CONSTRAINT IF EXISTS fk_case_cluster_membership_case;
ALTER TABLE "cle_v2"."case_cluster_membership" ADD CONSTRAINT fk_case_cluster_membership_case  FOREIGN KEY ("case_id")     REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_cluster_membership" DROP CONSTRAINT IF EXISTS fk_case_cluster_membership_clus;
ALTER TABLE "cle_v2"."case_cluster_membership" ADD CONSTRAINT fk_case_cluster_membership_clus  FOREIGN KEY ("cluster_id")  REFERENCES "cle_v2"."case_cluster"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_network_metric" DROP CONSTRAINT IF EXISTS fk_case_network_metric_case;
ALTER TABLE "cle_v2"."case_network_metric"     ADD CONSTRAINT fk_case_network_metric_case      FOREIGN KEY ("case_id")     REFERENCES "cle_v2"."cases"("id") ON DELETE CASCADE;
ALTER TABLE "cle_v2"."case_network_metric" DROP CONSTRAINT IF EXISTS fk_case_network_metric_snapshot;
ALTER TABLE "cle_v2"."case_network_metric"     ADD CONSTRAINT fk_case_network_metric_snapshot  FOREIGN KEY ("snapshot_id") REFERENCES "cle_v2"."network_snapshot"("id") ON DELETE CASCADE;

-- ============ Triggers ============
CREATE OR REPLACE TRIGGER trg_case_text_updated_at
BEFORE UPDATE ON "cle_v2"."case_text"
FOR EACH ROW EXECUTE FUNCTION "cle_v2".touch_updated_at();
CREATE OR REPLACE TRIGGER trg_case_citation_counts
AFTER INSERT OR UPDATE OR DELETE ON "cle_v2"."case_citation"
FOR EACH ROW EXECUTE FUNCTION "cle_v2".case_citation_counts_maintain();
CREATE OR REPLACE TRIGGER trg_cases_preserve_citation_raw
BEFORE DELETE ON "cle_v2"."cases"
FOR EACH ROW EXECUTE FUNCTION "cle_v2".case_delete_preserve_citation_raw();
CREATE OR REPLACE TRIGGER trg_echr_document_updated_at
BEFORE UPDATE ON "cle_v2"."echr_document"
FOR EACH ROW EXECUTE FUNCTION "cle_v2".touch_updated_at();
CREATE OR REPLACE TRIGGER trg_rs_document_updated_at
BEFORE UPDATE ON "cle_v2"."rs_document"
FOR EACH ROW EXECUTE FUNCTION "cle_v2".touch_updated_at();

-- ============ Views ============
CREATE OR REPLACE VIEW "cle_v2"."case_text_canonical" AS
SELECT DISTINCT ON (t."case_id", t."language") t.*
FROM "cle_v2"."case_text" t
ORDER BY t."case_id", t."language",
         CASE t."source" WHEN 'RECHTSPRAAK' THEN 1 WHEN 'HUDOC' THEN 2
                         WHEN 'INFOCURIA_BLOB_HTML' THEN 3 WHEN 'CELLAR_ITEM' THEN 4
                         ELSE 5 END,
         t."id";
CREATE OR REPLACE VIEW "cle_v2"."echr_v_document_with_text" AS
SELECT d.*, t."fulltext", t."fulltext_tsv"
FROM "cle_v2"."echr_document" d
LEFT JOIN "cle_v2"."case_text_canonical" t
       ON t."case_id" = d."case_id"
      AND t."language" = d."language";
CREATE OR REPLACE VIEW "cle_v2"."echr_v_judgments_decisions" AS
SELECT * FROM "cle_v2"."echr_document"
WHERE "doctype" ILIKE '%JUD%' OR "doctype" ILIKE '%DEC%';
CREATE OR REPLACE VIEW "cle_v2"."rs_v_document_with_text" AS
SELECT d.*, t."summary", t."fulltext", t."fulltext_tsv"
FROM "cle_v2"."rs_document" d
LEFT JOIN "cle_v2"."case_text_canonical" t ON t."case_id" = d."case_id" AND t."language" = 'nl';
CREATE OR REPLACE VIEW "cle_v2"."rs_v_document_law_reference" AS
SELECT
    r."case_id",
    c."ecli",
    r."raw_resource"                  AS "bwb_resource",
    COALESCE(r."raw_subdivision", '') AS "article",
    r."version_date",
    r."raw_label_id"                  AS "bwb_label_id",
    r."source_dataset"                AS "source",
    r."raw_reference"                 AS "opschrift",
    'http://wetten.overheid.nl/id/' || r."raw_resource" || '/' ||
        COALESCE("cle_v2".rs_date_to_iso(r."version_date"), '1900-01-01') || '/0'
        AS "legal_provision_url",
    CASE
        WHEN r."raw_label_id" IS NULL THEN NULL::text
        ELSE 'http://linkeddata.overheid.nl/terms/bwb/id/' || r."raw_resource" || '/'
             || r."raw_label_id"::text || '/'
             || COALESCE("cle_v2".rs_date_to_iso(r."version_date"), '1900-01-01') || '/'
             || COALESCE("cle_v2".rs_date_to_iso(r."version_date"), '1900-01-01')
    END AS "legal_provision_url_lido"
FROM "cle_v2"."case_law_reference" r
JOIN "cle_v2"."cases" c ON c."id" = r."case_id"
WHERE r."raw_scheme" = 'bwb';
CREATE OR REPLACE VIEW "cle_v2"."rs_v_document_legal_provisions" AS
SELECT DISTINCT c."ecli", lr."raw_reference" AS legal_provision
FROM "cle_v2"."case_law_reference" lr
JOIN "cle_v2"."cases" c ON c."id" = lr."case_id"
WHERE lr."raw_scheme" = 'bwb' AND NULLIF(lr."raw_reference", '') IS NOT NULL
UNION
SELECT DISTINCT c."ecli", lp."title" AS legal_provision
FROM "cle_v2"."case_law_reference" lr
JOIN "cle_v2"."cases" c ON c."id" = lr."case_id"
JOIN "cle_v2"."legal_provision" lp ON lp."bwb_label_id" = lr."raw_label_id"
WHERE lr."raw_scheme" = 'bwb' AND lr."raw_label_id" IS NOT NULL
  AND NULLIF(lp."title", '') IS NOT NULL
UNION
SELECT DISTINCT c."ecli", lp."title" AS legal_provision
FROM "cle_v2"."case_law_reference" lr
JOIN "cle_v2"."cases" c ON c."id" = lr."case_id"
JOIN "cle_v2"."legislation" lg
  ON lg."scheme" = 'bwb' AND lg."identifier" = lr."raw_resource"
JOIN "cle_v2"."legal_provision" lp
  ON lp."legislation_id" = lg."id"
 AND lower(lp."article_label") = lower(lr."raw_subdivision")
 AND lp."element_type" = 'artikel'
WHERE lr."raw_scheme" = 'bwb' AND NULLIF(lp."title", '') IS NOT NULL
UNION
SELECT DISTINCT c."ecli", lg."title" AS legal_provision
FROM "cle_v2"."case_law_reference" lr
JOIN "cle_v2"."cases" c ON c."id" = lr."case_id"
JOIN "cle_v2"."legislation" lg
  ON lg."scheme" = 'bwb' AND lg."identifier" = lr."raw_resource"
WHERE lr."raw_scheme" = 'bwb' AND NULLIF(lg."title", '') IS NOT NULL
UNION
SELECT DISTINCT c."ecli", (lg."title" || ', Artikel ' || lr."raw_subdivision") AS legal_provision
FROM "cle_v2"."case_law_reference" lr
JOIN "cle_v2"."cases" c ON c."id" = lr."case_id"
JOIN "cle_v2"."legislation" lg
  ON lg."scheme" = 'bwb' AND lg."identifier" = lr."raw_resource"
WHERE lr."raw_scheme" = 'bwb' AND NULLIF(lg."title", '') IS NOT NULL
  AND NULLIF(lr."raw_subdivision", '') IS NOT NULL
UNION
SELECT DISTINCT c."ecli", (lg."title" || ', Bijlage ' || lr."raw_subdivision") AS legal_provision
FROM "cle_v2"."case_law_reference" lr
JOIN "cle_v2"."cases" c ON c."id" = lr."case_id"
JOIN "cle_v2"."legislation" lg
  ON lg."scheme" = 'bwb' AND lg."identifier" = lr."raw_resource"
WHERE lr."raw_scheme" = 'bwb' AND NULLIF(lg."title", '') IS NOT NULL
  AND NULLIF(lr."raw_subdivision", '') IS NOT NULL
  AND lr."raw_reference" ILIKE '%bijlage%';