-- Lookups: languages (HUDOC upserts), jurisdictions, courts, document/procedure types.
-- psql var :rs_filter — predicate on legacy.rs_document for sampled runs ('true' = full).
SET search_path TO cle_v2, public;

-- Jurisdictions we know up front
INSERT INTO jurisdiction (iso_code, name, type) VALUES
  ('NL', 'Netherlands', 'country'),
  ('EU', 'European Union', 'supranational'),
  ('CE', 'Council of Europe', 'international')
ON CONFLICT (iso_code) DO NOTHING;

-- Languages beyond the EU-24 seed: normalize every HUDOC code we might meet.
-- ISO 639-2/B -> 639-1 for the codes observed live; anything unmapped keeps
-- its lowercased legacy code so the FK never breaks (flagged in verify/).
CREATE TABLE IF NOT EXISTS _lang_map (hudoc text PRIMARY KEY, iso text NOT NULL);
INSERT INTO _lang_map VALUES
  ('ENG','en'),('FRE','fr'),('GER','de'),('CZE','cs'),('RUM','ro'),
  ('TUR','tr'),('RUS','ru'),('UKR','uk'),('SRP','sr'),('HRV','hr'),
  ('BUL','bg'),('POL','pl'),('SLO','sk'),('SLV','sl'),('HUN','hu'),
  ('ITA','it'),('SPA','es'),('POR','pt'),('DUT','nl'),('GRE','el'),
  ('ALB','sq'),('ARM','hy'),('GEO','ka'),('AZE','az'),('MKD','mk'),
  ('LIT','lt'),('LAV','lv'),('EST','et'),('FIN','fi'),('SWE','sv'),
  ('DAN','da'),('NOR','no'),('ICE','is'),('BOS','bs'),('MLT','mt'),
  ('GLE','ga'),('CAT','ca'),('ARA','ar'),('CHI','zh'),('JPN','ja')
ON CONFLICT (hudoc) DO NOTHING;

INSERT INTO language (iso_code, name)
SELECT DISTINCT coalesce(m.iso, lower(d.languageisocode)), coalesce(m.iso, lower(d.languageisocode))
FROM legacy.echr_document d
LEFT JOIN _lang_map m ON m.hudoc = d.languageisocode
ON CONFLICT (iso_code) DO NOTHING;

-- Courts: ECtHR + CJEU trio + every distinct Rechtspraak instance name
INSERT INTO court (code, name, level, jurisdiction_id)
SELECT v.code, v.name, v.level, j.id
FROM (VALUES
  ('ECTHR', 'European Court of Human Rights', 'international', 'CE'),
  ('CJEU',  'Court of Justice',               'supranational', 'EU'),
  ('EGC',   'General Court',                  'supranational', 'EU'),
  ('CST',   'Civil Service Tribunal',         'supranational', 'EU')
) AS v(code, name, level, jur)
JOIN jurisdiction j ON j.iso_code = v.jur
ON CONFLICT (code) DO NOTHING;

INSERT INTO court (code, name, jurisdiction_id)
SELECT 'RS:' || d.instance, d.instance, (SELECT id FROM jurisdiction WHERE iso_code='NL')
FROM (SELECT DISTINCT d.instance FROM legacy.rs_document d
      WHERE d.instance IS NOT NULL AND :rs_filter) d
ON CONFLICT (code) DO NOTHING;

-- Document types (union of all corpora vocabularies)
INSERT INTO document_type (code, name) VALUES
  ('judgment', 'Judgment'), ('decision', 'Decision'),
  ('communicated', 'Communicated case'), ('opinion', 'Opinion (AG / Conclusie)'),
  ('order', 'Order'), ('ruling', 'Ruling'), ('other', 'Other')
ON CONFLICT (code) DO NOTHING;

-- Procedure types: RS distinct values (CJEU values load with the CJEU step)
INSERT INTO procedure_type (code, name)
SELECT DISTINCT procedure_type, procedure_type
FROM legacy.rs_document d WHERE d.procedure_type IS NOT NULL AND :rs_filter
ON CONFLICT (code) DO NOTHING;

INSERT INTO migration_manifest (step, note) VALUES ('10_lookups', NULL)
ON CONFLICT (step) DO UPDATE SET completed_at = now();
