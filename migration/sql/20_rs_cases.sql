-- Rechtspraak: rs_document -> cases + rs_document satellite + case_text +
-- domains + publications + external authorities + formal relations + law refs.
-- psql var :rs_filter — predicate on legacy.rs_document ('true' = full corpus).
SET search_path TO cle_v2, public;

-- 1. cases (ECLI normalized: trim + upper; MIGRATION_MAPPING §6.4)
INSERT INTO cases (ecli, source, title, date_decision, date_published,
                   court_id, language_iso, document_type_id, procedure_type_id,
                   case_number, created_at, updated_at)
SELECT upper(btrim(d.ecli)), 'RS', d.title, d.date_decision, d.date_published,
       c.id, 'nl',
       (SELECT id FROM document_type WHERE code = CASE d.document_type
            WHEN 'Uitspraak' THEN 'judgment' WHEN 'Conclusie' THEN 'opinion' ELSE 'other' END),
       pt.id, d.zaaknummer, d.created_at, d.updated_at
FROM legacy.rs_document d
LEFT JOIN court c ON c.code = 'RS:' || d.instance
LEFT JOIN procedure_type pt ON pt.code = d.procedure_type
WHERE :rs_filter
ON CONFLICT (ecli) DO NOTHING;

-- 2. rs_document satellite
INSERT INTO rs_document (case_id, date_decision, document_type, instance, domains,
    source, jurisdiction_country, procedure_type, url_publication, legal_provisions,
    predecessor_successor_cases, created_at, updated_at, date_published, date_issued,
    date_modified, title, language, access_rights, zittingsplaats, replaces_identifier,
    creator_uri, vindplaatsen, subject_uris, zaaknummer, opendata_status)
SELECT k.id, d.date_decision, d.document_type, d.instance, d.domains,
    d.source, d.jurisdiction_country, d.procedure_type, d.url_publication, d.legal_provisions,
    d.predecessor_successor_cases, d.created_at, d.updated_at, d.date_published, d.date_issued,
    d.date_modified, d.title, d.language, d.access_rights, d.zittingsplaats, d.replaces_identifier,
    d.creator_uri, d.vindplaatsen, d.subject_uris, d.zaaknummer, d.opendata_status
FROM legacy.rs_document d
JOIN cases k ON k.ecli = upper(btrim(d.ecli))
WHERE :rs_filter
  AND NOT EXISTS (SELECT 1 FROM rs_document r WHERE r.case_id = k.id);

-- 3. case_text ('nl'): fulltext from rs_document_text, summary from rs_document
INSERT INTO case_text (case_id, language, fulltext, summary, source)
SELECT k.id, 'nl', t.fulltext, d.summary, 'RECHTSPRAAK'
FROM legacy.rs_document d
LEFT JOIN legacy.rs_document_text t ON t.ecli = d.ecli
JOIN cases k ON k.ecli = upper(btrim(d.ecli))
WHERE :rs_filter AND (t.fulltext IS NOT NULL OR d.summary IS NOT NULL)
ON CONFLICT (case_id, language) DO NOTHING;

-- 4. domains -> domain + case_domain
INSERT INTO domain (scheme, name)
SELECT DISTINCT 'rs_domain', x.dom
FROM legacy.rs_document d, unnest(d.domains) AS x(dom)
WHERE :rs_filter AND x.dom IS NOT NULL AND x.dom <> ''
  AND NOT EXISTS (SELECT 1 FROM domain o WHERE o.scheme='rs_domain' AND o.name = x.dom);

INSERT INTO case_domain (case_id, domain_id)
SELECT DISTINCT k.id, o.id
FROM legacy.rs_document d
JOIN cases k ON k.ecli = upper(btrim(d.ecli))
CROSS JOIN LATERAL unnest(d.domains) AS x(dom)
JOIN domain o ON o.scheme = 'rs_domain' AND o.name = x.dom
WHERE :rs_filter
ON CONFLICT (case_id, domain_id) DO NOTHING;

-- 5. publications + external authorities (per-case satellites)
INSERT INTO rs_document_publication (case_id, raw, kind, journal_abbr, year, locator, annotator, created_at)
SELECT k.id, p.raw, p.kind, p.journal_abbr, p.year, p.locator, p.annotator, p.created_at
FROM legacy.rs_document_publication p
JOIN cases k ON k.ecli = upper(btrim(p.ecli))
WHERE EXISTS (SELECT 1 FROM rs_document r WHERE r.case_id = k.id)
ON CONFLICT (case_id, raw) DO NOTHING;

INSERT INTO rs_document_external_authority (case_id, kind, name, article, raw, created_at)
SELECT k.id, a.kind, a.name, a.article, a.raw, a.created_at
FROM legacy.rs_document_external_authority a
JOIN cases k ON k.ecli = upper(btrim(a.ecli))
WHERE EXISTS (SELECT 1 FROM rs_document r WHERE r.case_id = k.id)
ON CONFLICT (case_id, raw) DO NOTHING;

-- 6. formal relations (kept 1:1; fan-out to case_citation happens in 40_citations)
INSERT INTO rs_document_formal_relation (case_id, target_ecli, target_identifier,
    relation_type, aanleg, name, disposition, gevolg, created_at)
SELECT k.id, k2.ecli, fr.target_identifier,   -- target_ecli only when it resolves (FK); raw stays in target_identifier
       fr.relation_type, fr.aanleg, fr.name, fr.disposition, fr.gevolg, fr.created_at
FROM legacy.rs_document_formal_relation fr
JOIN cases k ON k.ecli = upper(btrim(fr.ecli))
LEFT JOIN cases k2 ON k2.ecli = upper(btrim(fr.target_ecli))
WHERE EXISTS (SELECT 1 FROM rs_document r WHERE r.case_id = k.id)
ON CONFLICT (case_id, target_identifier, relation_type, aanleg) DO NOTHING;

-- 7. BWB law references -> case_law_reference (raw + resolved in one pass)
INSERT INTO case_law_reference (case_id, legislation_id, provision_id,
    raw_scheme, raw_resource, raw_subdivision, raw_label_id, raw_reference,
    version_date, role, source_dataset, created_at)
SELECT k.id, lg.id, lp.id,
       'bwb', lr.bwb_resource, nullif(lr.article,''), lr.bwb_label_id, lr.opschrift,
       lr.version_date, 'cited',
       CASE lr.source WHEN 'lido-ref' THEN 'rs_lido_ref'
                      WHEN 'lido-linkt' THEN 'rs_lido_linkt'
                      ELSE 'rs_' || lr.source END,
       lr.created_at
FROM legacy.rs_document_law_reference lr
JOIN cases k ON k.ecli = upper(btrim(lr.ecli))
LEFT JOIN legislation lg ON lg.scheme='bwb' AND lg.identifier = lr.bwb_resource
LEFT JOIN LATERAL (   -- bwb_label_id matches multiple provision rows (snapshots) — pick latest
    SELECT id FROM legal_provision p WHERE p.bwb_label_id = lr.bwb_label_id
    ORDER BY p.snapshot_date DESC NULLS LAST, p.id LIMIT 1
) lp ON lr.bwb_label_id IS NOT NULL
WHERE EXISTS (SELECT 1 FROM rs_document r WHERE r.case_id = k.id)
ON CONFLICT DO NOTHING;

INSERT INTO migration_manifest (step) VALUES ('20_rs_cases')
ON CONFLICT (step) DO UPDATE SET completed_at = now();
