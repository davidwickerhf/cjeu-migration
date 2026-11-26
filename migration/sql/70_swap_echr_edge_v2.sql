-- R6.1 ECtHR edge rebuild: 1-transaction atomic swap (echr_edge -> echr_edge_v2).
--
-- DO NOT RUN until:
--   1. cle_v2.echr_edge_v2_staging has been built and verified with build_version = 'r6.1-v3'.
--   2. The stratified >= 300-row drop gate & kept-edge precision audit have passed
--      (see migration/verify/r6_1_echr_edge_v2_gate.md).
--   3. The owner has explicitly approved the swap.
--
-- Note on 148 post-2026-10-27 weekly-ETL ECtHR sources whose `echr_document.scl`
-- is NULL (due to PostgresECHRProcessor._detail_row omitting `scl`):
--   This swap replaces ONLY `echr_edge` rows whose `source_case_id` has a non-empty
--   `echr_document.scl` (18,981 of 19,129 sources; 424,509 of 428,440 resolved rows).
--   Wait: if `echr_document.scl` is backfilled for those 148 cases first and
--   `echr_edge_v2_staging` is re-run, EVERY source has `scl` and all 428,729
--   `echr_edge` rows are archived & swapped cleanly. Either way, `echr_edge` rows
--   with `scl IS NULL` are never silently lost!

BEGIN;

SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '300s';

-- 1. Archive legacy `echr_edge` rows for all sources that have non-empty `scl`
--    (explicit column list; preserves original `id` for exact rollback).
WITH scl_sources AS (
    SELECT DISTINCT case_id
      FROM cle_v2.echr_document
     WHERE scl IS NOT NULL AND btrim(scl) <> ''
)
INSERT INTO cle_v2.case_citation_archive (
    id, source_case_id, target_case_id, target_ecli_raw, target_celex_raw,
    relation_type, source_dataset, weight, context_segment_id, is_cross_jurisdiction,
    extractor_at, extractor_version, match_method, confidence,
    target_ref_raw, target_ref_scheme,
    archive_reason, archive_batch
)
SELECT
    cc.id, cc.source_case_id, cc.target_case_id, cc.target_ecli_raw, cc.target_celex_raw,
    cc.relation_type, cc.source_dataset, cc.weight, cc.context_segment_id, cc.is_cross_jurisdiction,
    cc.extractor_at, cc.extractor_version, cc.match_method, cc.confidence,
    cc.target_ref_raw, cc.target_ref_scheme,
    'R6.1 echr_edge rebuild', 'r6.1-v3'
  FROM cle_v2.case_citation cc
  JOIN scl_sources ss ON ss.case_id = cc.source_case_id
 WHERE cc.source_dataset = 'echr_edge'
ON CONFLICT (archive_batch, id) DO NOTHING;

-- 2. Insert staged `echr_edge_v2` rows into `cle_v2.case_citation`
--    (resolving natural keys source_ecli/source_item_id and target_ecli/target_item_id).
INSERT INTO cle_v2.case_citation (
    source_case_id, target_case_id, target_ecli_raw, relation_type,
    source_dataset, weight, is_cross_jurisdiction,
    extractor_at, extractor_version, match_method, confidence,
    target_ref_raw, target_ref_scheme
)
SELECT DISTINCT ON (sc.id, tc.id)
    sc.id AS source_case_id,
    tc.id AS target_case_id,
    tc.ecli AS target_ecli_raw,
    'cites' AS relation_type,
    'echr_edge_v2' AS source_dataset,
    1 AS weight,
    false AS is_cross_jurisdiction,
    now() AS extractor_at,
    st.build_version AS extractor_version,
    'appno/' || st.rule AS match_method,
    CASE
        WHEN st.rule LIKE '%earliest%' THEN 0.80::real
        WHEN st.rule LIKE '%decision_no_judgment%' THEN 0.85::real
        WHEN st.rule LIKE '%year%' THEN 0.90::real
        ELSE 0.95::real
    END AS confidence,
    st.appno AS target_ref_raw,
    'echr_appno' AS target_ref_scheme
  FROM cle_v2.echr_edge_v2_staging st
  JOIN cle_v2.cases sc
    ON (st.source_ecli IS NOT NULL AND sc.ecli = st.source_ecli)
    OR (st.source_ecli IS NULL AND st.source_item_id IS NOT NULL AND sc.item_id = st.source_item_id)
  JOIN cle_v2.cases tc
    ON (st.target_ecli IS NOT NULL AND tc.ecli = st.target_ecli)
    OR (st.target_ecli IS NULL AND st.target_item_id IS NOT NULL AND tc.item_id = st.target_item_id)
 WHERE st.build_version = 'r6.1-v3'
   AND sc.id <> tc.id
 ORDER BY sc.id, tc.id, st.id
ON CONFLICT DO NOTHING;

-- 3. Delete archived legacy `echr_edge` rows from `cle_v2.case_citation`.
DELETE FROM cle_v2.case_citation cc
 WHERE cc.source_dataset = 'echr_edge'
   AND EXISTS (
       SELECT 1
         FROM cle_v2.case_citation_archive a
        WHERE a.archive_batch = 'r6.1-v3'
          AND a.id = cc.id
   );

-- 4. Switch `echr_edge_v2` live & counted in `cle_v2.citation_source_dataset`.
--    Note: if all 148 post-Oct-27 sources also have `scl` backfilled, set
--    `echr_edge` active_in_api = false, counted = false as well.
UPDATE cle_v2.citation_source_dataset
   SET active_in_api = true,
       counted = true
 WHERE code = 'echr_edge_v2';

-- 5. Refresh derived citation counts across all touched ECHR cases.
SELECT cle_v2.refresh_case_citation_counts(ARRAY(
    SELECT DISTINCT cid FROM (
        SELECT source_case_id AS cid FROM cle_v2.case_citation_archive WHERE archive_batch = 'r6.1-v3'
        UNION
        SELECT target_case_id FROM cle_v2.case_citation_archive WHERE archive_batch = 'r6.1-v3' AND target_case_id IS NOT NULL
        UNION
        SELECT source_case_id FROM cle_v2.case_citation WHERE source_dataset = 'echr_edge_v2'
        UNION
        SELECT target_case_id FROM cle_v2.case_citation WHERE source_dataset = 'echr_edge_v2' AND target_case_id IS NOT NULL
    ) u
));

COMMIT;

-- After COMMIT, run `SELECT cle_v2.refresh_case_cited_by();` (with statement_timeout >= 120s).
--
-- =============================================================================
-- ROLLBACK (if ever needed):
-- =============================================================================
-- BEGIN;
-- INSERT INTO cle_v2.case_citation (
--     source_case_id, target_case_id, target_ecli_raw, target_celex_raw,
--     relation_type, source_dataset, weight, context_segment_id, is_cross_jurisdiction,
--     extractor_at, extractor_version, match_method, confidence,
--     target_ref_raw, target_ref_scheme
-- )
-- SELECT
--     source_case_id, target_case_id, target_ecli_raw, target_celex_raw,
--     relation_type, source_dataset, weight, context_segment_id, is_cross_jurisdiction,
--     extractor_at, extractor_version, match_method, confidence,
--     target_ref_raw, target_ref_scheme
--   FROM cle_v2.case_citation_archive
--  WHERE archive_batch = 'r6.1-v3'
-- ON CONFLICT DO NOTHING;
-- DELETE FROM cle_v2.case_citation WHERE source_dataset = 'echr_edge_v2';
-- UPDATE cle_v2.citation_source_dataset SET active_in_api = false, counted = false WHERE code = 'echr_edge_v2';
-- UPDATE cle_v2.citation_source_dataset SET active_in_api = true, counted = true WHERE code = 'echr_edge';
-- SELECT cle_v2.refresh_case_citation_counts(NULL);
-- COMMIT;
