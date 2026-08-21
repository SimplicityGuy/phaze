"""Shared upsert-SET-clause builder for the agent-write callback routers (phaze-bk9el.9).

``agent_analysis.put_analysis`` and ``agent_metadata.put_metadata`` each upsert a 1:1 result row
keyed on ``file_id``, and both built the SAME ``ON CONFLICT DO UPDATE`` SET clause shape: every
field the client explicitly set (D-14 field-level last-write-wins, via ``exclude_unset`` dump)
PLUS an unconditional failure-marker clear PLUS an explicit ``updated_at`` stamp -- 32 shared
lines, the file pair's worst clone, co-changed 3 times (repowise dry_violation measurement,
2026-08-21). This module is that one shared shape.

Each router still owns its OWN empty-body branch, and this extraction deliberately does NOT touch
that divergence: ``agent_analysis.py`` falls back to ``ON CONFLICT DO NOTHING`` on an empty PUT
(a status-less no-op has nothing to clear), while ``agent_metadata.py``'s D-13 sharp edge instead
clears the failure marker even on an empty PUT (an empty-body success PUT after a failure still
means extraction ran). That is a real, individually-documented difference between the two write
paths, not accidental duplication -- see each router's own comment at its empty-body branch.
"""

from typing import Any

from sqlalchemy import func


def build_field_lww_set_clause(stmt: Any, dumped: dict[str, Any]) -> dict[str, Any]:
    """The D-14 field-level-last-write-wins SET clause shared by ``put_analysis``/``put_metadata``.

    Covers every field the client explicitly set (``dumped``, already ``exclude_unset``-dumped)
    PLUS an UNCONDITIONAL failure-marker clear: a real write success must wipe any
    ``failed_at``/``error_message`` a prior failure report left, else a successful retry reads
    FAILED forever (``failed_at``/``error_message`` sit OUTSIDE ``exclude_unset`` -- the wire body
    never carries them). ``updated_at`` is stamped explicitly because ``TimestampMixin.updated_at``'s
    ORM ``onupdate=func.now()`` never fires on this Core ``ON CONFLICT DO UPDATE`` path
    (phaze-c8nz) -- without it a retried write freezes ``updated_at`` at first write instead of
    bumping it. Excludes ``file_id`` and ``id`` (both are conflict-target / immutable PK).

    Args:
        stmt: The ``pg_insert(...).values(...)`` statement being built; ``stmt.excluded`` supplies
            each set field's incoming value.
        dumped: The client's ``exclude_unset`` dump. An empty dict is a valid call (the caller's
            own empty-body branch, if it chooses to reuse this rather than special-case it) and
            yields exactly ``{"failed_at": None, "error_message": None, "updated_at": func.now()}``.
    """
    return {
        **{k: stmt.excluded[k] for k in dumped},
        "failed_at": None,
        "error_message": None,
        "updated_at": func.now(),
    }
