-- Read-only production-derived corpus query for phaze-eswzd.1.
-- Run against the Phaze PostgreSQL database. It contains SELECT statements only.
WITH per_file AS (
    SELECT
        a.file_id,
        coalesce(nullif(m.artist, ''), 'Unknown artist') AS artist,
        coalesce(nullif(m.title, ''), f.original_filename) AS title,
        m.album,
        m.duration,
        a.bpm,
        a.musical_key,
        a.style,
        count(*) AS coarse_windows,
        avg((w.mood_scores ->> 'mood_acoustic')::double precision) AS acoustic,
        avg((w.mood_scores ->> 'mood_electronic')::double precision) AS electronic,
        avg((w.mood_scores ->> 'mood_aggressive')::double precision) AS aggressive,
        avg((w.mood_scores ->> 'mood_relaxed')::double precision) AS relaxed,
        avg((w.mood_scores ->> 'mood_happy')::double precision) AS happy,
        avg((w.mood_scores ->> 'mood_sad')::double precision) AS sad,
        avg((w.mood_scores ->> 'mood_party')::double precision) AS party
    FROM analysis AS a
    JOIN files AS f ON f.id = a.file_id
    JOIN analysis_window AS w
      ON w.file_id = a.file_id
     AND w.tier = 'coarse'
     AND w.mood_scores IS NOT NULL
    LEFT JOIN metadata AS m ON m.file_id = a.file_id
    WHERE a.analysis_completed_at IS NOT NULL
    GROUP BY
        a.file_id,
        coalesce(nullif(m.artist, ''), 'Unknown artist'),
        coalesce(nullif(m.title, ''), f.original_filename),
        m.album,
        m.duration,
        a.bpm,
        a.musical_key,
        a.style
), ranked AS (
    SELECT
        per_file.*,
        top_mood.name AS dominant_mood,
        top_mood.score AS top_score,
        second_mood.score AS second_score,
        top_mood.score - second_mood.score AS top_margin
    FROM per_file
    CROSS JOIN LATERAL (
        SELECT name, score
        FROM (VALUES
            ('acoustic', acoustic),
            ('electronic', electronic),
            ('aggressive', aggressive),
            ('relaxed', relaxed),
            ('happy', happy),
            ('sad', sad),
            ('party', party)
        ) AS mood(name, score)
        WHERE score IS NOT NULL
        ORDER BY score DESC, name
        LIMIT 1
    ) AS top_mood
    CROSS JOIN LATERAL (
        SELECT score
        FROM (VALUES
            ('acoustic', acoustic),
            ('electronic', electronic),
            ('aggressive', aggressive),
            ('relaxed', relaxed),
            ('happy', happy),
            ('sad', sad),
            ('party', party)
        ) AS mood(name, score)
        WHERE score IS NOT NULL
        ORDER BY score DESC, name
        OFFSET 1
        LIMIT 1
    ) AS second_mood
), clear_ranked AS (
    SELECT
        ranked.*,
        row_number() OVER (
            PARTITION BY dominant_mood
            ORDER BY top_score DESC, file_id
        ) AS mood_rank
    FROM ranked
), clear AS (
    SELECT 'clear'::text AS selection_group, clear_ranked.*
    FROM clear_ranked
    WHERE mood_rank = 1
), ambiguous AS (
    SELECT 'ambiguous'::text AS selection_group, clear_ranked.*
    FROM clear_ranked
    WHERE top_margin <= 0.015
      AND file_id NOT IN (SELECT file_id FROM clear)
    ORDER BY top_margin, file_id
    LIMIT 5
), selected AS (
    SELECT * FROM clear
    UNION ALL
    SELECT * FROM ambiguous
)
SELECT jsonb_pretty(jsonb_agg(jsonb_build_object(
    'selection_group', selection_group,
    'file_id', file_id,
    'artist', artist,
    'title', title,
    'album', album,
    'duration_seconds', duration,
    'bpm', bpm,
    'musical_key', musical_key,
    'style_summary', style,
    'coarse_windows', coarse_windows,
    'dominant_mood', dominant_mood,
    'top_margin', top_margin,
    'moods', jsonb_build_object(
        'acoustic', acoustic,
        'electronic', electronic,
        'aggressive', aggressive,
        'relaxed', relaxed,
        'happy', happy,
        'sad', sad,
        'party', party
    )
) ORDER BY selection_group DESC, dominant_mood, top_margin, file_id))
FROM selected;
