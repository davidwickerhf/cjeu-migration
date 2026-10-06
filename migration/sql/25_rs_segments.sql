-- OPTIONAL heavy step (§5.4): port ecli_segments (32 GB, 3.65M embeddings).
-- Excluded from sampled runs unless :port_segments = 'true'.
SET search_path TO cle_v2, public;

INSERT INTO case_segment (case_id, language, segment_type, segment_text,
                          segment_hash, embedding, embedding_model)
SELECT k.id, 'nl', 'legacy', s.segment, s.segment_hash, s.embedding, 'legacy-768'
FROM legacy.ecli_segments s
JOIN cases k ON k.ecli = upper(btrim(s.ecli))
WHERE :port_segments
ON CONFLICT DO NOTHING;

INSERT INTO migration_manifest (step) VALUES ('25_rs_segments')
ON CONFLICT (step) DO UPDATE SET completed_at = now();
