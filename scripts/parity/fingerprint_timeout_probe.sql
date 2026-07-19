-- phaze-qmc2.1 calibration spike -- READ-ONLY probes used to measure the fingerprint
-- work-time distribution and to establish claimed-but-unrun vs genuinely-running SAQ rows.
--
-- Rig recipe (psql is NOT on lux.lan's non-interactive PATH):
--   B64=$(printf '%s' "<one probe>" | base64)
--   ssh datum@lux.lan "echo $B64 | base64 -d | docker exec -i postgres psql -U phaze -d phaze -tA -F'|'"
--
-- Every probe here is READ-ONLY (SELECT). SAQ 0.26.4 stores the job blob as JSON in the
-- BYTEA `job` column; `convert_from(job,'UTF8')::jsonb` exposes queued/started/touched/
-- completed/attempts/timeout/retries (timestamps are epoch MILLISECONDS). SAQ `to_dict`
-- OMITS any field equal to its dataclass default, so an ABSENT `attempts` key == attempts=0
-- == the worker's process() loop (saq/worker.py:356) NEVER ran the job. That absence is the
-- reliable "never executed" signal; `touched` is NOT (the sweeper's abort->ABORTING update
-- bumps touched via Queue.update(), saq/queue/base.py:117).

-- (1) Queue/status census -- shows the claim/execute mismatch at a glance.
SELECT queue, status, count(*)
FROM saq_jobs
GROUP BY queue, status
ORDER BY queue, status;

-- (2) Q2 proof: split the 'active' fingerprint rows into genuinely-running (attempts key
--     present, i.e. >=1) vs claimed-but-never-run (attempts key absent, i.e. =0). The
--     running count equals the lane concurrency; the rest are buffered-but-unrun.
SELECT
  count(*) AS active_total,
  count(*) FILTER (WHERE (convert_from(job,'UTF8')::jsonb ? 'attempts'))       AS genuinely_running,
  count(*) FILTER (WHERE NOT (convert_from(job,'UTF8')::jsonb ? 'attempts'))   AS claimed_unrun,
  count(*) FILTER (WHERE (convert_from(job,'UTF8')::jsonb->>'started')::bigint
                       = (convert_from(job,'UTF8')::jsonb->>'touched')::bigint) AS started_eq_touched
FROM saq_jobs
WHERE queue = 'phaze-agent-nox-fingerprint' AND status = 'active';

-- (3) Q1 distribution: genuine completion durations (completed-started, seconds). keep_result
--     ttl bounds this to the live window, but the sample is large and the ceiling is hard.
WITH d AS (
  SELECT ((convert_from(job,'UTF8')::jsonb->>'completed')::bigint
        - (convert_from(job,'UTF8')::jsonb->>'started')::bigint) / 1000.0 AS dur_s
  FROM saq_jobs
  WHERE queue = 'phaze-agent-nox-fingerprint' AND status = 'complete'
    AND (convert_from(job,'UTF8')::jsonb ? 'completed')
)
SELECT
  count(*) AS n,
  round(min(dur_s)::numeric, 1)                                              AS min_s,
  round(percentile_cont(0.50) WITHIN GROUP (ORDER BY dur_s)::numeric, 1)     AS p50,
  round(percentile_cont(0.95) WITHIN GROUP (ORDER BY dur_s)::numeric, 1)     AS p95,
  round(percentile_cont(0.99) WITHIN GROUP (ORDER BY dur_s)::numeric, 1)     AS p99,
  round(max(dur_s)::numeric, 1)                                              AS max_s,
  count(*) FILTER (WHERE dur_s > 300) AS over_300s,
  count(*) FILTER (WHERE dur_s > 600) AS over_600s
FROM d;

-- (4) Q1 counter-examples: duration vs file size -- the largest files finish FASTEST, so the
--     600s timeout is not a size cutoff. Join the blob's kwargs.file_id to the files table.
WITH d AS (
  SELECT (convert_from(job,'UTF8')::jsonb->'kwargs'->>'file_id')::uuid AS fid,
         ((convert_from(job,'UTF8')::jsonb->>'completed')::bigint
        - (convert_from(job,'UTF8')::jsonb->>'started')::bigint) / 1000.0 AS dur_s
  FROM saq_jobs
  WHERE queue = 'phaze-agent-nox-fingerprint' AND status = 'complete'
    AND (convert_from(job,'UTF8')::jsonb ? 'completed')
)
SELECT round((f.file_size / 1048576.0)::numeric, 0) AS mb,
       round(d.dur_s::numeric, 1)                   AS dur_s,
       f.file_type
FROM d
JOIN files f ON f.id = d.fid
ORDER BY f.file_size DESC
LIMIT 8;

-- MEASURED 2026-07-19 (recorded on phaze-qmc2.1 / grx3 / e57w):
--   (1) phaze-agent-nox-fingerprint: active 3457, queued 7012, complete 578, aborting 1.
--   (2) active_total 3449 -> genuinely_running 2 (== lane concurrency), claimed_unrun 3447.
--   (3) n 589: min 4.1 p50 29.3 p95 58.3 p99 84.4 max 94.0 ; over_300s 0 ; over_600s 0.
--   (4) 2681 MB avi -> 23.8s ; 2402 MB mkv -> 22.9s ; 1614 MB mkv -> 34.7s ; 1068 MB mp4 -> 20.3s ;
--       slowest were 328-400 MB mp3s (64-87s). => size is NOT the cost driver; 600s is generous.
