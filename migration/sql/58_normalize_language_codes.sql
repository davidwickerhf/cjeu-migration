-- Normalize full-word language codes left by the CJEU loader
-- (KNOWN_ISSUES.md #2): language_procedure holds words ("English"), the
-- loader lowercased them verbatim, producing 'english'-style codes in
-- cases.language_iso, 31k summary-only case_text rows, and 24 bogus
-- language lookup entries.
--
-- Idempotent; statements are independent and each fits the sql-runner's
-- 30s statement timeout. Run in order — 4 must run before 5, and 6 fails
-- loudly (FK) if any reference survives.
SET search_path TO cle_v2, public;

-- The 24-entry mapping used by every statement below:
--   ('english','en'),('french','fr'),('german','de'),('italian','it'),
--   ('spanish','es'),('dutch','nl'),('polish','pl'),('greek','el'),
--   ('portuguese','pt'),('romanian','ro'),('bulgarian','bg'),('swedish','sv'),
--   ('danish','da'),('finnish','fi'),('slovak','sk'),('slovenian','sl'),
--   ('croatian','hr'),('estonian','et'),('latvian','lv'),('lithuanian','lt'),
--   ('maltese','mt'),('irish','ga'),('hungarian','hu'),('czech','cs')

-- 1. cases.language_iso: word -> ISO  (fast: 38k-row update)
-- 2. Stage the full-word case_text rows into an UNLOGGED table so later
--    statements are index probes, not 1.6M-row self-joins (the naive join
--    form blows a 30s statement timeout).
-- 3. Carry the stranded summary onto the ISO sibling BEFORE any delete —
--    running the delete first loses summaries (it happened; they had to be
--    rebuilt from cases.parquet).
-- 4. Delete full-word rows that have an ISO sibling.
-- 5. Re-key the remainder to ISO (no sibling left, so no unique conflicts).
-- 6. Drop the staging table.
-- 7. Prune the bogus language rows ONE PER STATEMENT — each delete
--    FK-checks every referencing table (case_segment alone is 3.7M rows);
--    batched deletes exceed the timeout.

-- (1)
UPDATE cases c SET language_iso = m.iso FROM _lang_map() m WHERE c.language_iso = m.word;

-- (2)
CREATE UNLOGGED TABLE IF NOT EXISTS _lang_fix AS
SELECT f.id, f.case_id, f.source, m.iso, f.summary, f.summary_source
FROM case_text f JOIN _lang_map() m ON f.language = m.word;

-- (3)
UPDATE case_text s SET summary = x.summary,
       summary_source = COALESCE(x.summary_source, s.summary_source)
FROM _lang_fix x
WHERE s.case_id = x.case_id AND s.source = x.source AND s.language = x.iso
  AND s.summary IS NULL AND x.summary IS NOT NULL;

-- (4)
DELETE FROM case_text f USING _lang_fix x
WHERE f.id = x.id
  AND EXISTS (SELECT 1 FROM case_text s WHERE s.case_id = x.case_id
              AND s.source = x.source AND s.language = x.iso);

-- (5)
UPDATE case_text f SET language = x.iso FROM _lang_fix x WHERE f.id = x.id;

-- (6)
DROP TABLE _lang_fix;

-- (7) one statement per word:
-- DELETE FROM language WHERE iso_code = 'english';  (repeat for all 24)

-- helper referenced above (inline the VALUES list when running through the
-- sql-runner, which sends one statement per request):
CREATE OR REPLACE FUNCTION _lang_map()
RETURNS TABLE(word text, iso text) LANGUAGE sql IMMUTABLE AS $$
  VALUES ('english','en'),('french','fr'),('german','de'),('italian','it'),
   ('spanish','es'),('dutch','nl'),('polish','pl'),('greek','el'),
   ('portuguese','pt'),('romanian','ro'),('bulgarian','bg'),('swedish','sv'),
   ('danish','da'),('finnish','fi'),('slovak','sk'),('slovenian','sl'),
   ('croatian','hr'),('estonian','et'),('latvian','lv'),('lithuanian','lt'),
   ('maltese','mt'),('irish','ga'),('hungarian','hu'),('czech','cs')
$$;
