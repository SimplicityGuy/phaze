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

It also holds the ``files`` row shape the agent file routes build from a wire record
(:func:`file_row`) and the in-place re-point of a moved file (:func:`repoint_file`), shared with the
stale-row reconcile (phaze-5rfev) so the two can never derive a moved row's columns differently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import unicodedata
import uuid

from sqlalchemy import func

from phaze.services.text_repair import repair_mojibake


if TYPE_CHECKING:
    from phaze.models.file import FileRecord
    from phaze.schemas.agent_files import FileUpsertRecord


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


def file_row(record: FileUpsertRecord, agent_id: str, batch_id: uuid.UUID | None) -> dict[str, Any]:
    """Build one ``files`` row from a wire record, with every server-owned column stamped here.

    Shared by the agent file upsert and move routes (``routers/agent_files.py``) and the stale-row
    reconcile (``services/stale_rows.py``, phaze-5rfev), so a re-pointed row derives its columns
    exactly as an upserted one does.
    """
    data = record.model_dump()
    # RESEARCH Pitfall 7: NFC-normalize defensively
    data["original_path"] = unicodedata.normalize("NFC", data["original_path"])
    data["agent_id"] = agent_id  # AUTH-01 -- stamped from auth, NEVER from body
    data["id"] = uuid.uuid4()  # server-generates new id; ON CONFLICT preserves existing id
    data["batch_id"] = batch_id  # Phase 27 D-09/D-18 -- server resolves; never from body
    # phaze-x4ux: populate the derived, mojibake-repaired filename ONCE at ingest.
    # `original_filename` itself is left untouched (byte-faithful record of the on-disk
    # name); `original_filename_repaired` is what search/matching/rename-proposal code
    # should read instead. Always set (even when the repair is a no-op, in which case it
    # equals `original_filename`) so NULL unambiguously means "not yet backfilled" for
    # pre-phaze-x4ux rows (see services/text_repair_backfill.py).
    data["original_filename_repaired"] = repair_mojibake(data["original_filename"])
    return data


def repoint_file(record: FileRecord, row: dict[str, Any]) -> None:
    """Point ``record`` at a moved file: every column :func:`file_row` derives from the wire, never its id or tenancy.

    Used by the watcher move route (phaze-oxn2m) and the stale-row reconcile (phaze-5rfev). The
    file is, by construction, present at the new path, so the missing marker is cleared too.
    """
    for column in (
        "original_path",
        "original_filename",
        "original_filename_repaired",
        "current_path",
        "file_type",
        "file_size",
        "sha256_hash",
        "batch_id",
    ):
        setattr(record, column, row[column])
    record.missing_at = None
    record.companion_ambiguous_at = None
