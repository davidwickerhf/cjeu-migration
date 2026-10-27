-- FDW link to the legacy caselawexplorer database (READ ONLY).
-- psql vars required: :legacy_host :legacy_db :legacy_user :legacy_password
-- (the runner passes them from env; never hardcode credentials here)

CREATE EXTENSION IF NOT EXISTS postgres_fdw;

DROP SERVER IF EXISTS legacy CASCADE;
CREATE SERVER legacy FOREIGN DATA WRAPPER postgres_fdw
  OPTIONS (host :'legacy_host', port '5432', dbname :'legacy_db',
           fetch_size '10000', updatable 'false');

CREATE USER MAPPING FOR CURRENT_USER SERVER legacy
  OPTIONS (user :'legacy_user', password :'legacy_password');

DROP SCHEMA IF EXISTS legacy CASCADE;
CREATE SCHEMA legacy;

IMPORT FOREIGN SCHEMA public
  LIMIT TO (rs_document, rs_document_text, rs_document_publication,
            rs_document_external_authority, rs_document_formal_relation,
            rs_document_law_reference, rs_edge,
            rs_law_element, rs_law_alias,
            echr_document, echr_document_text, echr_document_appno,
            echr_document_article, echr_extractor_segments, echr_edge,
            ecli_segments, case_law, legal_case, law_element)
  FROM SERVER legacy INTO legacy
  OPTIONS (import_generated 'false');  -- legacy generated cols reference functions we don't have; import as plain columns

-- migration manifest: which steps completed (idempotent re-runs skip them)
CREATE TABLE IF NOT EXISTS cle_v2.migration_manifest (
    step text PRIMARY KEY,
    completed_at timestamptz NOT NULL DEFAULT now(),
    rows_affected bigint,
    note text
);
