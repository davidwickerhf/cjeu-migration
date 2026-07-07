-- Dutch legislation catalog: rs_law_element/rs_law_alias -> legislation /
-- legal_provision / legislation_alias, plus BWB stubs for laws that are
-- cited but not catalogued (5,734 observed live).
SET search_path TO cle_v2, public;

-- 1. Acts (type='wet') — catalog is snapshot-versioned: keep latest per bwb_id
INSERT INTO legislation (identifier, scheme, title, jurisdiction_id, lido_id, jc_id, snapshot_date)
SELECT DISTINCT ON (le.bwb_id)
       le.bwb_id, 'bwb', le.title,
       (SELECT id FROM jurisdiction WHERE iso_code='NL'),
       le.lido_id, le.jc_id, le.snapshot_date
FROM legacy.rs_law_element le
WHERE le.type = 'wet' AND :legis_filter
  AND NOT EXISTS (SELECT 1 FROM legislation lg WHERE lg.scheme='bwb' AND lg.identifier = le.bwb_id)
ORDER BY le.bwb_id, le.snapshot_date DESC
ON CONFLICT DO NOTHING;

-- 2. Stubs for cited-but-uncatalogued BWB ids (MIGRATION_MAPPING §6.5)
INSERT INTO legislation (identifier, scheme, jurisdiction_id)
SELECT DISTINCT le.bwb_resource, 'bwb', (SELECT id FROM jurisdiction WHERE iso_code='NL')
FROM legacy.rs_document_law_reference le   -- aliased le so :legis_filter applies
JOIN (SELECT 1) _ ON true
WHERE le.bwb_resource IS NOT NULL AND :legis_filter_stub
  AND NOT EXISTS (SELECT 1 FROM legislation lg WHERE lg.scheme='bwb' AND lg.identifier = le.bwb_resource);

-- 3. Provisions (every non-wet element) — dedupe to latest snapshot per lido_id
--    (live data: same lido_id appears under multiple snapshot_dates)
INSERT INTO legal_provision (legislation_id, element_type, article_label, title,
                             bwb_label_id, lido_id, jc_id, snapshot_date)
SELECT DISTINCT ON (le.lido_id)
       lg.id, le.type, le.number, le.title, le.bwb_label_id, le.lido_id, le.jc_id, le.snapshot_date
FROM legacy.rs_law_element le
JOIN legislation lg ON lg.scheme = 'bwb' AND lg.identifier = le.bwb_id
WHERE le.type <> 'wet' AND le.lido_id IS NOT NULL AND :legis_filter
ORDER BY le.lido_id, le.snapshot_date DESC
ON CONFLICT DO NOTHING;

--    lido-less elements: dedupe on the natural quad
INSERT INTO legal_provision (legislation_id, element_type, article_label, title,
                             bwb_label_id, jc_id, snapshot_date)
SELECT DISTINCT ON (le.bwb_id, le.bwb_label_id, le.type, le.number)
       lg.id, le.type, le.number, le.title, le.bwb_label_id, le.jc_id, le.snapshot_date
FROM legacy.rs_law_element le
JOIN legislation lg ON lg.scheme = 'bwb' AND lg.identifier = le.bwb_id
WHERE le.type <> 'wet' AND le.lido_id IS NULL AND :legis_filter
ORDER BY le.bwb_id, le.bwb_label_id, le.type, le.number, le.snapshot_date DESC
ON CONFLICT DO NOTHING;

-- 4. Aliases
-- Stub catalog rows for alias-only BWB ids: rs_law_alias carries the FULL
-- BWB register (~265k regulations — the bwbidlist), far beyond the ~8.8k
-- acts in the LIDO structural catalog. Loaded lossless; title = first
-- alias at the latest snapshot (the bwbidlist lists the official title
-- first). Loads the full register even in sampled runs (reference data).
INSERT INTO legislation (identifier, scheme, title, jurisdiction_id)
SELECT DISTINCT ON (la.bwb_id) la.bwb_id, 'bwb', la.alias,
       (SELECT id FROM jurisdiction WHERE iso_code='NL')
FROM legacy.rs_law_alias la
WHERE NOT EXISTS (SELECT 1 FROM legislation lg WHERE lg.scheme='bwb' AND lg.identifier = la.bwb_id)
ORDER BY la.bwb_id, la.snapshot_date DESC NULLS LAST, la.id;

-- All aliases, union across snapshots (lossless; DISTINCT guards
-- within-batch snapshot duplicates)
INSERT INTO legislation_alias (legislation_id, alias, source)
SELECT DISTINCT lg.id, la.alias, 'bwbidlist'
FROM legacy.rs_law_alias la
JOIN legislation lg ON lg.scheme = 'bwb' AND lg.identifier = la.bwb_id
WHERE NOT EXISTS (SELECT 1 FROM legislation_alias x WHERE x.legislation_id = lg.id AND x.alias = la.alias);

INSERT INTO migration_manifest (step) VALUES ('11_legislation')
ON CONFLICT (step) DO UPDATE SET completed_at = now();
