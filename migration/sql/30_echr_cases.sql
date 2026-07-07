-- ECHR: variant grouping -> cases, per-variant echr_document, satellites,
-- case_text with doctype-rank pick, respondent explode.
-- psql var :echr_filter — predicate on legacy.echr_document ('true' = full).
SET search_path TO cle_v2, public;
SET temp_buffers = '512MB';   -- _echr_variants holds ~170k wide rows; default 8MB starves local buffers

-- 0. Working set: non-PR/CLIN variants with normalized language + case grouping key.
--    Group = ECLI when present; ECLI-less communicated cases group by appno+refdate
--    (MIGRATION_MAPPING §2.1 / §5.2).
CREATE TEMP TABLE _echr_variants AS
SELECT d.itemid,
       upper(btrim(d.ecli))                                   AS ecli,
       coalesce(upper(btrim(d.ecli)),
                -- appno lists can be multi-KB (mass applications) — hash the
                -- fallback key so it stays groupable AND btree-indexable
                'APPNO:' || md5(coalesce(d.appno,'?') || ':' ||
                                coalesce(d.referencedate::date::text,'')))  AS case_key,
       coalesce(m.iso, lower(d.languageisocode))              AS lang,
       d.languageisocode                                      AS hudoc_lang,
       d.doctype,
       CASE WHEN d.doctype LIKE '%JUD%' THEN 1
            WHEN d.doctype LIKE '%DEC%' THEN 2
            WHEN d.doctype LIKE '%COM%' THEN 3 ELSE 4 END     AS doctype_rank,
       CASE WHEN d.languageisocode = 'ENG' THEN 0
            WHEN d.languageisocode = 'FRE' THEN 1 ELSE 2 END  AS lang_rank,
       d.appno, d.extractedappno, d.docname, d.doctypebranch,
       d.judgementdate, d.referencedate, d.article, d.conclusion,
       d.violation, d.nonviolation, d.respondent, d.originatingbody,
       d.representedby, d.publishedby, d.rulesofcourt, d.applicability,
       d.separateopinion, d.issue, d.importance, d.rank, d.scl,
       d.externalsources, d.created_at, d.updated_at
FROM legacy.echr_document d
LEFT JOIN _lang_map m ON m.hudoc = d.languageisocode
WHERE d.doctype NOT IN ('PR','CLIN','CLINF')          -- §5.1: skip non-decisions
  AND :echr_filter;

CREATE INDEX ON _echr_variants (case_key, lang_rank, doctype_rank, itemid);
ANALYZE _echr_variants;

-- 1. cases: one per case_key; canonical variant = ENG > FRE > any,
--    doctype rank JUD > DEC > COM, lowest itemid. date: judgement ->
--    reference -> parsed from the ECLI segment (§6.2).
INSERT INTO cases (ecli, item_id, source, title, date_decision, importance,
                   court_id, document_type_id, created_at)
SELECT DISTINCT ON (v.case_key)
       v.ecli, v.itemid, 'ECHR', v.docname,
       coalesce(v.judgementdate::date, v.referencedate::date,
                CASE WHEN v.ecli ~ '^ECLI:CE:ECHR:[0-9]{4}:[0-9]{4}'
                     THEN to_date(split_part(v.ecli, ':', 4) ||
                                  left(split_part(v.ecli, ':', 5), 4), 'YYYYMMDD')
                END),
       v.importance,
       (SELECT id FROM court WHERE code='ECTHR'),
       (SELECT id FROM document_type WHERE code = CASE v.doctype_rank
            WHEN 1 THEN 'judgment' WHEN 2 THEN 'decision'
            WHEN 3 THEN 'communicated' ELSE 'other' END),
       now()
FROM _echr_variants v
ORDER BY v.case_key, v.lang_rank, v.doctype_rank, v.itemid
ON CONFLICT (item_id) DO NOTHING;

-- 2. echr_document: EVERY variant, anchored by its own itemid
INSERT INTO echr_document (item_id, case_id, language, extractedappno, docname,
    doctype, doctype_branch, judgement_date, reference_date, article, conclusion,
    violation, nonviolation, respondent, originating_body, represented_by,
    published_by, rules_of_court, applicability, separate_opinion, issue,
    importance, rank, scl, external_sources, created_at, updated_at)
SELECT v.itemid, k.id, v.lang, v.extractedappno, v.docname,
    v.doctype, v.doctypebranch, v.judgementdate, v.referencedate, v.article, v.conclusion,
    v.violation, v.nonviolation, v.respondent, v.originatingbody, v.representedby,
    v.publishedby, v.rulesofcourt, v.applicability, v.separateopinion, v.issue,
    v.importance, v.rank, v.scl, v.externalsources, v.created_at, v.updated_at
FROM _echr_variants v
JOIN _echr_variants canon ON canon.case_key = v.case_key
JOIN cases k ON k.item_id = canon.itemid
WHERE (canon.itemid) = (SELECT c2.itemid FROM _echr_variants c2
                        WHERE c2.case_key = v.case_key
                        ORDER BY c2.lang_rank, c2.doctype_rank, c2.itemid LIMIT 1)
ON CONFLICT (item_id) DO NOTHING;

-- 3. case_text: best variant per (case, language) — doctype rank then lowest itemid
INSERT INTO case_text (case_id, language, fulltext, source)
SELECT DISTINCT ON (d.case_id, d.language)
       d.case_id, d.language, t.fulltext, 'HUDOC'
FROM echr_document d
JOIN legacy.echr_document_text t ON t.itemid = d.item_id
WHERE t.fulltext IS NOT NULL
ORDER BY d.case_id, d.language,
         CASE WHEN d.doctype LIKE '%JUD%' THEN 1 WHEN d.doctype LIKE '%DEC%' THEN 2 ELSE 3 END,
         d.item_id
ON CONFLICT (case_id, language, source) DO NOTHING;

-- 4. appnos + articles (per-variant satellites; protocol parsed for pure P-codes)
INSERT INTO echr_document_appno (item_id, appno, source, created_at)
SELECT a.itemid, a.appno, a.source, a.created_at
FROM legacy.echr_document_appno a
JOIN echr_document d ON d.item_id = a.itemid
ON CONFLICT (item_id, appno, source) DO NOTHING;

INSERT INTO echr_document_article (item_id, kind, article_code, protocol)
SELECT a.itemid, a.kind, a.article_code,
       CASE WHEN a.article_code ~ '^P[0-9]+(-|$)'
            THEN split_part(a.article_code, '-', 1) END
FROM legacy.echr_document_article a
JOIN echr_document d ON d.item_id = a.itemid
ON CONFLICT (item_id, kind, article_code) DO NOTHING;

-- 5. extractor segments
INSERT INTO echr_extractor_segments (item_id, parser_mode, error, procedure, facts,
    complaints, law, operative, subject_matter, court_assessment, separate_opinion,
    appendix, num_sections, segmented_at, extractor_version)
SELECT s.itemid, s.parser_mode, s.error, s.procedure, s.facts,
    s.complaints, s.law, s.operative, s.subject_matter, s.court_assessment, s.separate_opinion,
    s.appendix, s.num_sections, s.segmented_at, s.extractor_version
FROM legacy.echr_extractor_segments s
JOIN echr_document d ON d.item_id = s.itemid
ON CONFLICT (item_id) DO NOTHING;

-- 6. respondent explode -> party + case_party (fixes legacy API matching bug, §6.3)
INSERT INTO party (canonical_name, role_class)
SELECT DISTINCT x.state, 'state'
FROM echr_document d, regexp_split_to_table(d.respondent, ';') AS x(state)
WHERE d.respondent IS NOT NULL AND x.state <> ''
  AND NOT EXISTS (SELECT 1 FROM party p WHERE p.canonical_name = x.state AND p.role_class='state');

INSERT INTO case_party (case_id, party_id, role)
SELECT DISTINCT d.case_id, p.id, 'respondent_state'
FROM echr_document d
CROSS JOIN LATERAL regexp_split_to_table(d.respondent, ';') AS x(state)
JOIN party p ON p.canonical_name = x.state AND p.role_class = 'state'
WHERE d.respondent IS NOT NULL AND x.state <> ''
ON CONFLICT (case_id, party_id, role, ordinal) DO NOTHING;

DROP TABLE _echr_variants;
INSERT INTO migration_manifest (step) VALUES ('30_echr_cases')
ON CONFLICT (step) DO UPDATE SET completed_at = now();
