-- Rebuild citation counts in one pass (the trigger only maintains future
-- mutations; it is installed by 90_post_load.sql AFTER this step).
SET search_path TO cle_v2, public;

TRUNCATE case_citation_counts;

INSERT INTO case_citation_counts (case_id, cites_count, cited_by_count)
SELECT case_id, sum(cites), sum(cited_by)
FROM (
    SELECT source_case_id AS case_id, count(*) AS cites, 0 AS cited_by
    FROM case_citation WHERE source_case_id IS NOT NULL GROUP BY 1
    UNION ALL
    SELECT target_case_id, 0, count(*)
    FROM case_citation WHERE target_case_id IS NOT NULL GROUP BY 1
) u
GROUP BY case_id;

INSERT INTO migration_manifest (step) VALUES ('41_counts')
ON CONFLICT (step) DO UPDATE SET completed_at = now();
