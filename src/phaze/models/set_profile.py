"""SetProfile model -- the per-file half of the set projection (phaze-x1qr3.1).

No ``from __future__ import annotations`` here, deliberately: with it, ``uuid`` is referenced only
inside a ``Mapped[...]`` annotation, and ruff's TC003 then wants the import moved into a
``TYPE_CHECKING`` block -- the rewrite CLAUDE.md forbids, because SQLAlchemy resolves these
annotations at runtime via ``get_type_hints``. ``models/analysis.py`` and ``models/cloud_budget.py``
omit the future import for exactly the same reason.
"""

import uuid

from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from phaze.models.base import Base, TimestampMixin


class SetProfile(TimestampMixin, Base):
    """One row per file summarising its whole set, 1:1 with ``files`` (migration 063).

    THE PER-FILE HALF of section E's "one projection, then everything rides it". Its sibling
    is the three nullable columns migration 063 adds to ``analysis_window`` (``energy`` /
    ``camelot`` / ``mood_scores``, the per-WINDOW half). Together they are the narrow
    projection every later surface in the file-viewer epic queries, so that no request has to
    read the ~5 KB ``analysis_window.features`` JSONB back across millions of rows.

    WHY ITS OWN TABLE, not columns on ``analysis``. :class:`~phaze.models.analysis.
    AnalysisResult` is already 1:1 with files, so the columns below would physically fit
    there -- but its ``features`` column is "the longest window's dict", a SAMPLE, while every
    column here is a SUMMARY over the whole file. Two quantities that look alike, mean
    opposite things, and would sit one beside the other invite reading the wrong one. The
    separate table makes the mistake impossible to make silently, and follows the per-file
    sidecar precedent already set by ``cloud_budget`` and ``stage_skip``.

    ``mean_vector`` is positional in :data:`phaze.services.set_projection.MOOD_ORDER` -- the
    same 11 fixed names, in the same fixed archive-wide order, that key
    ``AnalysisWindow.mood_scores``. That constant's docstring carries why the order is pinned
    as a literal and why a change to it is a ``projection_version`` bump rather than an edit.

    NOTHING WRITES THIS YET. ``phaze-x1qr3.2`` computes the projection and ``phaze-x1qr3.3``
    writes it at analysis completion plus backfills existing files from stored JSONB, with no
    re-analysis. This bead is the schema alone.
    """

    __tablename__ = "set_profile"

    # file_id IS the primary key -- one profile per file, no surrogate id and no separate unique
    # index needed (the `cloud_budget` precedent). ondelete=CASCADE so this sidecar can never block
    # a scan deletion: `services/scan_deletion.py` deletes only the sidecars whose FK carries NO
    # ondelete rule, exactly as it already omits `analysis_window` for the same reason.
    file_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("files.id", ondelete="CASCADE"), primary_key=True)
    # The file's mean 11-d positive-class vector, positional in MOOD_ORDER. `float8[]`; Postgres
    # does not enforce array length, so "11" is a writer contract carried by MOOD_ORDER's own
    # length rather than by DDL. NULL on a file with no coarse windows -- 661 such fine-only files
    # exist today (phaze-hia9z), and the epic's honesty rule says they show a gap, never a zero
    # vector that would read as a real measurement of a silent set.
    mean_vector: Mapped[list[float] | None] = mapped_column(ARRAY(Float), nullable=True)
    # The energy series resampled to a FIXED 64 points, so two sets of different durations are
    # directly comparable (the Euclidean term in phaze-x1qr3.11's similarity) and the arc renders
    # at one width. Same length caveat as `mean_vector`, and NULL for the same fine-only case.
    arc: Mapped[list[float] | None] = mapped_column(ARRAY(Float), nullable=True)
    # The cached set glyph: per-COARSE-window (camelot_number, energy) cells, from which the
    # `ui/primitives.html` macro renders hue and lightness. Cached rather than recomputed because
    # phaze-x1qr3.9 draws this glyph once per ROW in the Files table.
    glyph: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # The file's modal Camelot position -- "8A", at most 3 characters, matching
    # `AnalysisWindow.camelot`'s width. The set's key, shown under the title and used for the
    # wheel-adjacency term in similarity.
    camelot_modal: Mapped[str | None] = mapped_column(String(3), nullable=True)
    # Share in [0, 1] of key transitions that are wheel-adjacent, after a flicker filter that
    # ignores runs shorter than two fine windows. Range is a writer contract, not a CHECK --
    # consistent with `analysis_window.energy`.
    harmonic_discipline: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Elapsed seconds of the set's energy peak. The page rests here with nothing hovered, so it is
    # stored rather than recomputed per request.
    peak_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Which version of the projection produced this row, so a weight change (phaze-x1qr3.12's
    # operator blind check settles the energy weights) re-backfills only STALE rows instead of the
    # whole corpus. NOT NULL: a row exists because the projection ran, so there is no "no version"
    # state to represent. The server_default exists for the DDL, not as an invitation to omit it --
    # the writer added in phaze-x1qr3.3 passes the value explicitly.
    projection_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
