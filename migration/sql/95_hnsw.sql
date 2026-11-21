-- OPTIONAL: HNSW vector indexes. Hours of build time on 3.7M vectors.
-- SKIPPED on staging — pg_dump carries only definitions, so the Coolify
-- restore rebuilds these regardless. Run this file only if you need
-- vector-similarity queries against the staging DB itself.
SET search_path TO cle_v2, public;
SET maintenance_work_mem = '512MB';

CREATE INDEX IF NOT EXISTS "case_text_idx_summary_embedding" ON "cle_v2"."case_text" USING hnsw ("summary_embedding" vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS "case_segment_idx_embedding_hnsw" ON "cle_v2"."case_segment" USING hnsw ("embedding" vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
