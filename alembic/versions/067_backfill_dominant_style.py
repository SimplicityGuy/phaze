"""Make ``analysis.style`` a ranked score array and preserve the dominant label.

Revision ID: 067
Revises: 066
Create Date: 2026-09-21

phaze-z66hq: the old ``String(50)`` stored a truncated ``genre=score,...`` string.
The 2026-09-11 investigation counted 5,095 affected rows. A read-only production
remeasurement on 2026-09-21 found 5,639 populated style rows, 5,551 at exactly 50
characters, and complete genre predictions on all 94,276 coarse windows (941,960
well-formed score entries). This migration reconstructs file-level scores from those
windows, weighted by duration. It separately restores the duration-modal top label,
matching ``aggregate_dominant``'s first-window tie break. The two top choices differ
for 447 of 5,640 files with window scores, so both representations carry meaning.

The GIN index supports JSONB containment queries by style name; the B-tree index
supports category filters and grouping on ``dominant_style``. The score array is
validated at the API boundary as a list of ``{name, score}`` objects.

Downgrade discards the derived array and retains the repaired dominant label in the
old string column. The truncated composite cannot be reconstructed exactly, while
the score array can always be rebuilt from the persisted windows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op


revision: str = "067"
down_revision: str | None = "066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Restore both queryable representations from coarse windows without replaying audio."""
    op.alter_column("analysis", "style", new_column_name="dominant_style", existing_type=sa.String(length=50))
    op.add_column("analysis", sa.Column("style", JSONB, nullable=True))
    op.execute(
        """
        WITH coarse AS (
            SELECT file_id, window_index, style, GREATEST(end_sec - start_sec, 0) AS seconds, features
            FROM analysis_window
            WHERE tier = 'coarse' AND end_sec > start_sec
        ), totals AS (
            SELECT file_id, sum(seconds) AS seconds FROM coarse GROUP BY file_id
        ), label_weights AS (
            SELECT file_id, style, sum(seconds) AS seconds, min(window_index) AS first_index
            FROM coarse WHERE style IS NOT NULL AND style <> ''
            GROUP BY file_id, style
        ), dominant AS (
            SELECT DISTINCT ON (file_id) file_id, style
            FROM label_weights ORDER BY file_id, seconds DESC, first_index
        ), scores AS (
            SELECT c.file_id, replace(entry->>'label', '---', '/') AS name,
                   sum((entry->>'confidence')::double precision * c.seconds) / NULLIF(t.seconds, 0) AS score
            FROM coarse c
            JOIN totals t ON t.file_id = c.file_id
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(c.features #> '{genre,predictions}') = 'array'
                     THEN c.features #> '{genre,predictions}' ELSE '[]'::jsonb END
            ) AS item(entry)
            WHERE jsonb_typeof(entry->'label') = 'string'
              AND jsonb_typeof(entry->'confidence') = 'number'
            GROUP BY c.file_id, replace(entry->>'label', '---', '/'), t.seconds
        ), ranked AS (
            SELECT file_id, jsonb_agg(jsonb_build_object('name', name, 'score', score)
                                       ORDER BY score DESC, name) AS styles
            FROM scores WHERE score IS NOT NULL GROUP BY file_id
        )
        UPDATE analysis AS a
        SET dominant_style = d.style, style = r.styles
        FROM dominant d LEFT JOIN ranked r ON r.file_id = d.file_id
        WHERE a.file_id = d.file_id
        """
    )
    # Rows predating window persistence cannot recover scores, but their truncated string's
    # leading label remains useful as a category until those files are reanalyzed.
    op.execute("UPDATE analysis SET dominant_style = split_part(dominant_style, '=', 1) WHERE style IS NULL AND dominant_style LIKE '%=%'")
    op.create_index("ix_analysis_dominant_style", "analysis", ["dominant_style"])
    op.create_index("ix_analysis_style_gin", "analysis", ["style"], postgresql_using="gin", postgresql_ops={"style": "jsonb_path_ops"})


def downgrade() -> None:
    """Keep the category label; ranked scores remain recoverable from windows."""
    op.drop_index("ix_analysis_style_gin", table_name="analysis")
    op.drop_index("ix_analysis_dominant_style", table_name="analysis")
    op.drop_column("analysis", "style")
    op.alter_column("analysis", "dominant_style", new_column_name="style", existing_type=sa.String(length=50))
