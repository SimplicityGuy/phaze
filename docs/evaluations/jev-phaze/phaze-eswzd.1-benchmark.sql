-- Illustrative query only: pilot exclusion identifiers are redacted.
-- Read-only deterministic 500-track held-out corpus query for phaze-eswzd.1.
-- Run against the Phaze PostgreSQL database. It contains SELECT statements only.
--
-- Selection policy:
-- * exclude all twelve pilot file UUIDs and their artist/release leakage groups;
-- * define the leakage group as normalized artist + normalized album;
-- * retain at most two rows per leakage group, selected by a stable salted MD5;
-- * derive a joint stratum from dominant mood, top-two margin band, duration
--   band, and metadata completeness;
-- * round-robin across joint strata using within-stratum row number, then use
--   stable hashes as deterministic tie breakers; and
-- * take the first 500 rows.
WITH per_file AS (
    SELECT
        a.file_id,
        coalesce(nullif(btrim(m.artist), ''), 'Unknown artist') AS artist,
        coalesce(nullif(btrim(m.title), ''), 'Unknown title') AS title,
        nullif(btrim(m.album), '') AS album,
        m.duration,
        a.bpm,
        nullif(btrim(a.musical_key), '') AS musical_key,
        a.style,
        count(*) AS coarse_windows,
        avg((w.mood_scores ->> 'mood_acoustic')::double precision) AS acoustic,
        avg((w.mood_scores ->> 'mood_electronic')::double precision) AS electronic,
        avg((w.mood_scores ->> 'mood_aggressive')::double precision) AS aggressive,
        avg((w.mood_scores ->> 'mood_relaxed')::double precision) AS relaxed,
        avg((w.mood_scores ->> 'mood_happy')::double precision) AS happy,
        avg((w.mood_scores ->> 'mood_sad')::double precision) AS sad,
        avg((w.mood_scores ->> 'mood_party')::double precision) AS party,
        lower(regexp_replace(
            coalesce(nullif(btrim(m.artist), ''), 'unknown artist'),
            '\s+',
            ' ',
            'g'
        )) || '|' || lower(regexp_replace(
            coalesce(nullif(btrim(m.album), ''), 'unknown album'),
            '\s+',
            ' ',
            'g'
        )) AS leakage_group,
        CASE
            WHEN nullif(btrim(m.artist), '') IS NOT NULL
             AND nullif(btrim(m.title), '') IS NOT NULL
             AND nullif(btrim(m.album), '') IS NOT NULL
             AND m.duration IS NOT NULL
             AND a.bpm IS NOT NULL
             AND nullif(btrim(a.musical_key), '') IS NOT NULL
                THEN 'complete'
            ELSE 'incomplete'
        END AS metadata_completeness
    FROM analysis AS a
    JOIN files AS f ON f.id = a.file_id
    JOIN analysis_window AS w
      ON w.file_id = a.file_id
     AND w.tier = 'coarse'
     AND w.mood_scores IS NOT NULL
    LEFT JOIN metadata AS m ON m.file_id = a.file_id
    WHERE a.analysis_completed_at IS NOT NULL
      AND a.file_id NOT IN (
          'REDACTED_P01',
          'REDACTED_P02',
          'REDACTED_P03',
          'REDACTED_P04',
          'REDACTED_P05',
          'REDACTED_P06',
          'REDACTED_P07',
          'REDACTED_P08',
          'REDACTED_P09',
          'REDACTED_P10',
          'REDACTED_P11',
          'REDACTED_P12'
      )
    GROUP BY
        a.file_id,
        coalesce(nullif(btrim(m.artist), ''), 'Unknown artist'),
        coalesce(nullif(btrim(m.title), ''), 'Unknown title'),
        nullif(btrim(m.album), ''),
        m.duration,
        a.bpm,
        nullif(btrim(a.musical_key), ''),
        a.style,
        lower(regexp_replace(
            coalesce(nullif(btrim(m.artist), ''), 'unknown artist'),
            '\s+',
            ' ',
            'g'
        )) || '|' || lower(regexp_replace(
            coalesce(nullif(btrim(m.album), ''), 'unknown album'),
            '\s+',
            ' ',
            'g'
        )),
        CASE
            WHEN nullif(btrim(m.artist), '') IS NOT NULL
             AND nullif(btrim(m.title), '') IS NOT NULL
             AND nullif(btrim(m.album), '') IS NOT NULL
             AND m.duration IS NOT NULL
             AND a.bpm IS NOT NULL
             AND nullif(btrim(a.musical_key), '') IS NOT NULL
                THEN 'complete'
            ELSE 'incomplete'
        END
), ranked AS (
    SELECT
        per_file.*,
        top_mood.name AS dominant_mood,
        top_mood.score AS top_score,
        second_mood.score AS second_score,
        top_mood.score - second_mood.score AS top_margin,
        CASE
            WHEN top_mood.score - second_mood.score <= 0.05 THEN 'ambiguous'
            WHEN top_mood.score - second_mood.score <= 0.20 THEN 'close'
            ELSE 'clear'
        END AS margin_band,
        CASE
            WHEN duration IS NULL THEN 'unknown'
            WHEN duration < 180 THEN 'short_lt_180s'
            WHEN duration <= 360 THEN 'medium_180_360s'
            ELSE 'long_gt_360s'
        END AS duration_band,
        md5(file_id::text || ':phaze-eswzd-500-v1') AS selection_hash
    FROM per_file
    CROSS JOIN LATERAL (
        SELECT name, score
        FROM (VALUES
            (1, 'acoustic', acoustic),
            (2, 'electronic', electronic),
            (3, 'aggressive', aggressive),
            (4, 'relaxed', relaxed),
            (5, 'happy', happy),
            (6, 'sad', sad),
            (7, 'party', party)
        ) AS mood(ordinal, name, score)
        WHERE score IS NOT NULL
        ORDER BY score DESC, ordinal
        LIMIT 1
    ) AS top_mood
    CROSS JOIN LATERAL (
        SELECT score
        FROM (VALUES
            (1, 'acoustic', acoustic),
            (2, 'electronic', electronic),
            (3, 'aggressive', aggressive),
            (4, 'relaxed', relaxed),
            (5, 'happy', happy),
            (6, 'sad', sad),
            (7, 'party', party)
        ) AS mood(ordinal, name, score)
        WHERE score IS NOT NULL
        ORDER BY score DESC, ordinal
        OFFSET 1
        LIMIT 1
    ) AS second_mood
    WHERE leakage_group NOT IN (
        'greg champion the coodabeen champions|footy songs 98-07',
        'dangerfields|mainstream music is shit vol.2',
        'unknown artist|unknown album',
        'rasta4eyes|mainstream music is shit vol.2',
        'dj haus|rinse 11-22-2013',
        'october falls|the streams from the end',
        'bat for lashes|humo selecteert meer dan het beste uit 2012',
        'dj isaac|rave the city - the roman empi',
        'merkurius|unknown album',
        'marcello nunzio|youfm featuring-sat-12-06',
        'oscar l|1605 243 (proton radio)-sbd-12-04',
        'time warp 2023|ricardo villalobos live-web-04-01'
    )
), group_capped AS (
    SELECT ranked.*
    FROM (
        SELECT
            ranked.*,
            row_number() OVER (
                PARTITION BY leakage_group
                ORDER BY selection_hash, file_id
            ) AS leakage_group_rank
        FROM ranked
    ) AS ranked
    WHERE leakage_group_rank <= 2
), stratified AS (
    SELECT
        group_capped.*,
        dominant_mood || '|' || margin_band || '|' || duration_band || '|' ||
            metadata_completeness AS stratum_key,
        row_number() OVER (
            PARTITION BY
                dominant_mood,
                margin_band,
                duration_band,
                metadata_completeness
            ORDER BY selection_hash, file_id
        ) AS stratum_rank
    FROM group_capped
), selected AS (
    SELECT *
    FROM stratified
    ORDER BY stratum_rank, stratum_key, selection_hash, file_id
    LIMIT 500
), numbered AS (
    SELECT
        row_number() OVER (
            ORDER BY stratum_rank, stratum_key, selection_hash, file_id
        ) AS benchmark_index,
        selected.*
    FROM selected
)
SELECT jsonb_pretty(jsonb_build_object(
    'schema_version', 'phaze-jev-benchmark-corpus-v1',
    'query_date_utc', timezone('UTC', statement_timestamp()),
    'mood_order', jsonb_build_array(
        'acoustic',
        'electronic',
        'aggressive',
        'relaxed',
        'happy',
        'sad',
        'party'
    ),
    'selection', jsonb_build_object(
        'seed', 'phaze-eswzd-500-v1',
        'pilot_excluded_count', 12,
        'pilot_leakage_groups_excluded_count', 12,
        'eligible_after_pilot_exclusion', (SELECT count(*) FROM ranked),
        'eligible_after_leakage_cap', (SELECT count(*) FROM group_capped),
        'leakage_group_definition', 'normalized artist + normalized album',
        'maximum_tracks_per_leakage_group', 2,
        'margin_bands', jsonb_build_array(
            'ambiguous <= 0.05',
            'close > 0.05 and <= 0.20',
            'clear > 0.20'
        ),
        'duration_bands', jsonb_build_array(
            'short < 180 seconds',
            'medium 180 through 360 seconds',
            'long > 360 seconds',
            'unknown'
        ),
        'metadata_complete_fields', jsonb_build_array(
            'artist',
            'title',
            'album',
            'duration',
            'bpm',
            'musical_key'
        ),
        'selected_count', (SELECT count(*) FROM numbered)
    ),
    'tracks', (
        SELECT jsonb_agg(jsonb_build_object(
            'subject_code', 'B' || lpad(benchmark_index::text, 3, '0'),
            'benchmark_index', benchmark_index,
            'file_id', file_id,
            'artist', artist,
            'title', title,
            'album', album,
            'duration_seconds', duration,
            'bpm', bpm,
            'musical_key', musical_key,
            'style_summary', style,
            'coarse_windows', coarse_windows,
            'leakage_group', leakage_group,
            'leakage_group_rank', leakage_group_rank,
            'dominant_mood', dominant_mood,
            'top_margin', top_margin,
            'margin_band', margin_band,
            'duration_band', duration_band,
            'metadata_completeness', metadata_completeness,
            'stratum_key', stratum_key,
            'stratum_rank', stratum_rank,
            'selection_hash', selection_hash,
            'moods', jsonb_build_object(
                'acoustic', acoustic,
                'electronic', electronic,
                'aggressive', aggressive,
                'relaxed', relaxed,
                'happy', happy,
                'sad', sad,
                'party', party
            )
        ) ORDER BY benchmark_index)
        FROM numbered
    )
));
