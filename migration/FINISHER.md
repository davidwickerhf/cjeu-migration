# Post-90 finisher checklist (execute once 90_post_load lands in manifest)

Target: caselaw Neon (direct endpoint). All steps idempotent.

1. **Law-ref dedupe post-pass** — the running 90 predates the improved
   dedupe; run the raw-tuple `row_number()` DELETE from the current
   90_post_load.sql dedupe section standalone (expect ~15M rows removed,
   22.2M → ~7.2M), then `VACUUM ANALYZE case_law_reference`.
2. **55_backfill_cjeu_extras.py** (already on box) — citations_extra_info,
   national_judgement_xml, national_based_on, is_about domain scheme,
   qualifier referring-states.
3. **Drop the phantom `cjeu_keyword` scheme** — extractor aliased
   keywords≡eurovoc (verified 100% identical); delete case_domain links +
   domain rows where scheme='cjeu_keyword'. Real keywords blocked on
   cellar-extractor fix (task filed).
4. **ECHR case_number backfill** — cases.case_number ← primary appno of
   the canonical variant:
   `UPDATE cle_v2.cases c SET case_number = split_part(d.appno, ';', 1)
    FROM cle_v2.echr_document d WHERE d.item_id = c.item_id
    AND c.source='ECHR' AND c.case_number IS NULL AND d.appno IS NOT NULL`
   — note: legacy appno lives on the legacy variant row; new echr_document
   has no appno column → resolve via echr_document_appno instead:
   first appno with source='appno' for the canonical item_id.
5. **Reconcile + regenerate COVERAGE_PROOF.md** (gen_coverage_proof.py)
   with final numbers; correct §1.5/§1.6 mapping tables for the real
   legacy source values (lido-both 13.1M, linkextractor-lite 7.8M,
   parser-xml 972k, rs_replaces 16k edges).
6. **Report final reconciliation** + remind: destroy Vast box, untick
   Coolify public toggle, then cutover = pg_dump -n cle_v2 → restore into
   Coolify from a one-shot container (PG17→18 image note in README).
