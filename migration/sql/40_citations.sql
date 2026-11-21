-- Citations: rs_edge + rs formal-relation fanout + echr_edge -> case_citation.
-- MUST run after ALL corpora are in cases (cross-corpus resolution, §6.6).
-- The citation-counts trigger is not installed yet; 41_counts.sql rebuilds.
SET search_path TO cle_v2, public;

-- 1. rs_edge (body-cite / legacy-ddb / formal-relation)
INSERT INTO case_citation (source_case_id, target_case_id, target_ecli_raw,
    relation_type, source_dataset, is_cross_jurisdiction)
SELECT s.id, t.id,
       CASE WHEN t.id IS NULL THEN upper(btrim(e.target_ecli)) END,
       coalesce(e.relation_type, 'cites'),
       CASE e.source WHEN 'body-cite' THEN 'rs_body_cite'
                     WHEN 'legacy-ddb' THEN 'rs_legacy_ddb'
                     WHEN 'formal-relation' THEN 'rs_formal_relation'
                     ELSE 'rs_' || e.source END,
       (t.id IS NOT NULL AND NOT t.sources @> '{RS}')
FROM legacy.rs_edge e
JOIN cases s ON s.ecli = upper(btrim(e.source_ecli))
LEFT JOIN cases t ON t.ecli = upper(btrim(e.target_ecli))
ON CONFLICT DO NOTHING;

-- 2. echr_edge (itemid -> case via echr_document; weight preserved)
INSERT INTO case_citation (source_case_id, target_case_id, target_ecli_raw,
    relation_type, source_dataset, weight, is_cross_jurisdiction)
SELECT ds.case_id, dt.case_id,
       CASE WHEN dt.case_id IS NULL THEN upper(btrim(e.target_ecli)) END,
       'cites', 'echr_edge', e.weight, false
FROM legacy.echr_edge e
JOIN echr_document ds ON ds.item_id = e.source_itemid
LEFT JOIN echr_document dt ON dt.item_id = e.target_itemid
ON CONFLICT DO NOTHING;

INSERT INTO migration_manifest (step) VALUES ('40_citations')
ON CONFLICT (step) DO UPDATE SET completed_at = now();
