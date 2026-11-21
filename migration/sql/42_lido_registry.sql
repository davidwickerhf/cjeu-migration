-- Second-pass fold of the LIDO cross-corpus registry (§5.6):
-- case_law (10.2M) x legal_case -> case_law_reference for ECLIs we hold.
SET search_path TO cle_v2, public;

INSERT INTO case_law_reference (case_id, legislation_id, provision_id,
    raw_scheme, raw_resource, raw_label_id, raw_reference, role, source_dataset)
SELECT k.id, lg.id, lp.id,
       'bwb', le.bwb_id, le.bwb_label_id, cl.opschrift, 'cited', 'lido_registry'
FROM legacy.case_law cl
JOIN legacy.legal_case c ON c.id = cl.case_id
JOIN cases k ON k.ecli = upper(btrim(c.ecli_id))
JOIN legacy.law_element le ON le.id = cl.law_id
LEFT JOIN legislation lg ON lg.scheme='bwb' AND lg.identifier = le.bwb_id
LEFT JOIN legal_provision lp ON lp.bwb_label_id = le.bwb_label_id AND le.bwb_label_id IS NOT NULL
WHERE :port_lido
  AND NOT EXISTS (   -- skip pairs already loaded from rs_document_law_reference
    SELECT 1 FROM case_law_reference x
    WHERE x.case_id = k.id AND x.raw_scheme='bwb'
      AND x.raw_resource = le.bwb_id
      AND x.raw_label_id IS NOT DISTINCT FROM le.bwb_label_id AND x.role='cited')
ON CONFLICT DO NOTHING;

INSERT INTO migration_manifest (step) VALUES ('42_lido_registry')
ON CONFLICT (step) DO UPDATE SET completed_at = now();
