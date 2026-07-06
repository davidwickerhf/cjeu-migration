-- Reconciliation: new-schema counts vs legacy (respecting sample filters).
SET search_path TO cle_v2, public;
\echo === row counts ===
SELECT 'cases_rs' k, count(*) FROM cases WHERE source='RS'
UNION ALL SELECT 'cases_echr', count(*) FROM cases WHERE source='ECHR'
UNION ALL SELECT 'cases_cjeu', count(*) FROM cases WHERE source='CJEU'
UNION ALL SELECT 'echr_variants', count(*) FROM echr_document
UNION ALL SELECT 'case_text', count(*) FROM case_text
UNION ALL SELECT 'citations', count(*) FROM case_citation
UNION ALL SELECT 'citations_resolved', count(*) FROM case_citation WHERE target_case_id IS NOT NULL
UNION ALL SELECT 'citations_xjur', count(*) FROM case_citation WHERE is_cross_jurisdiction
UNION ALL SELECT 'law_refs', count(*) FROM case_law_reference
UNION ALL SELECT 'counts_rows', count(*) FROM case_citation_counts;
\echo === orphan checks (must all be 0) ===
SELECT 'text_no_case' k, count(*) FROM case_text t LEFT JOIN cases c ON c.id=t.case_id WHERE c.id IS NULL
UNION ALL SELECT 'echrdoc_no_case', count(*) FROM echr_document d LEFT JOIN cases c ON c.id=d.case_id WHERE c.id IS NULL
UNION ALL SELECT 'lang_fk_violations', count(*) FROM case_text t LEFT JOIN language l ON l.iso_code=t.language WHERE l.iso_code IS NULL;
