"""The re-runnable CUE conflict count documented on phaze-drote.

Read-only SQL that counts recordings with more than one current CUE file, whether those files agree,
multi-FILE CUEs, and the candidates that exist only because an older parser version also parsed the
same file. ``test_cue_conflict_review`` runs it against seeded data and checks it agrees with the
conflicts queue. To print it for ``psql``::

    uv run python -m tests.integration._cue_conflict_count
"""

from __future__ import annotations

import sys


COUNT_SQL = r"""
-- phaze-drote: recordings with more than one current CUE FILE, whether those files agree, multi-FILE CUEs,
-- and the candidates that only exist because an older parser version also parsed the same file.
-- Read-only. Grouped by CUE file (provider_source_objects), never by candidate: one file parsed by two
-- parser versions (local-tracklist-v1 and -v4) has two candidates but is ONE CUE. A file's tracks are
-- compared using its newest parser version. A CUE is current when its tracklist observation's revision is the
-- file's present sha256, the file is present, and still linked to the recording. Release-metadata
-- observations (local-release-*, no tracks) are excluded.
WITH cand AS (
  SELECT c.media_id, c.id AS candidate_id, c.status AS candidate_status, so.id AS cue_file_id, o.revision,
         o.parser_version, (regexp_replace(o.parser_version, '\D', '', 'g'))::int AS parser_n,
         md5(COALESCE((SELECT jsonb_agg(jsonb_build_object(
                  'p', t->'position', 'a', t->'artist', 't', t->'title',
                  'o', t->'timestamp'->'offset', 'u', t->'timestamp'->'offset_usability') ORDER BY (t->>'position')::int)
                FROM jsonb_array_elements(o.payload->'tracks') t)::text, '')) AS tracks_md5,
         (SELECT count(DISTINCT e) FROM jsonb_array_elements(o.payload->'tracks') t,
                 jsonb_array_elements_text(CASE WHEN jsonb_typeof(t->'evidence') = 'array' THEN t->'evidence' ELSE '[]'::jsonb END) e
           WHERE left(e, 5) = 'FILE:') AS file_entries
  FROM provider_recording_candidates c
  JOIN provider_source_observations o ON o.id = c.observation_id
  JOIN provider_source_objects so ON so.id = o.object_id
  JOIN files src ON src.id = so.source_file_id
  JOIN file_companions fc ON fc.companion_id = src.id AND fc.media_id = c.media_id
  WHERE o.payload->>'source_format' = 'cue'
    AND o.parser_version LIKE 'local-tracklist-v%'
    AND c.status <> 'rejected'
    AND o.status = 'found' AND o.payload->'completeness'->>'state' = 'complete'
    AND jsonb_array_length(COALESCE(o.payload->'tracks', '[]'::jsonb)) > 0
    AND src.missing_at IS NULL AND o.revision = src.sha256_hash
),
per_file AS (  -- one row per (recording, CUE file): its newest parser version's parse
  SELECT DISTINCT ON (media_id, cue_file_id) media_id, cue_file_id, tracks_md5, file_entries
  FROM cand ORDER BY media_id, cue_file_id, parser_n DESC
),
per_media AS (
  SELECT media_id, count(*) AS files, count(DISTINCT tracks_md5) AS distinct_tracks, bool_or(file_entries > 1) AS has_multi_file
  FROM per_file GROUP BY media_id
),
superseded AS (  -- candidates whose file and revision also have a newer-parser candidate for the same recording
  SELECT a.candidate_id, a.candidate_status FROM cand a
  WHERE EXISTS (SELECT 1 FROM cand b WHERE b.media_id = a.media_id AND b.cue_file_id = a.cue_file_id
                AND b.revision = a.revision AND b.parser_n > a.parser_n)
)
SELECT (SELECT count(*) FROM per_media)                                             AS media_with_a_current_cue,
       (SELECT count(*) FROM per_file)                                              AS current_cue_files,
       (SELECT count(*) FROM cand)                                                  AS current_cue_candidates,
       (SELECT count(*) FROM per_media WHERE files > 1)                             AS media_with_multiple_cue_files,
       (SELECT count(*) FROM per_media WHERE files > 1 AND distinct_tracks = 1)     AS of_which_identical_tracks,
       (SELECT count(*) FROM per_media WHERE files > 1 AND distinct_tracks > 1)     AS of_which_differing,
       (SELECT count(*) FROM per_media WHERE has_multi_file)                        AS media_with_a_multi_file_cue,
       (SELECT count(*) FROM superseded)                                            AS superseded_parser_candidates,
       (SELECT count(*) FROM superseded WHERE candidate_status = 'pending')         AS of_which_pending;
"""


if __name__ == "__main__":
    sys.stdout.write(COUNT_SQL.lstrip())
