"""Pure recovery classification and replay planning.

This module owns no database session, queue, router, or logging side effect.  It decides which
ledger rows recovery owns and partitions those rows into the replay protocols that adapters carry
out.  ``phaze.tasks.reenqueue`` remains the supported compatibility facade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Any
import uuid

from phaze.services.stage_status import CLOUD_LANE_FUNCTIONS
from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS
from phaze.tasks._shared.replay_safety import LEDGER_REPLAY_REGENERATED


if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from phaze.models.scheduling_ledger import SchedulingLedger


_DOMAIN_COMPLETED_STAGES: frozenset[str] = frozenset({"process_file", "extract_file_metadata", "push_file", "s3_upload"})
"""File-keyed agent functions whose unreliable remote ledger clear needs a completion net."""

_CLOUD_OWNED_FUNCTIONS: frozenset[str] = frozenset({"process_file", "push_file", "s3_upload", "submit_cloud_job"})
"""Functions whose re-drive is owned by an in-flight or awaiting cloud lane."""

TALLY_KEYS: tuple[str, ...] = ("reenqueued", "skipped", "errored", "unreplayable")
_ALL_KEYED_FUNCTIONS: tuple[str, ...] = tuple(_KEY_BUILDERS)


@dataclass(frozen=True)
class _DoneSets:
    """Ledger-scoped completion facts materialized once per recovery run."""

    analyze_done: set[str]
    metadata_domain_completed: set[str]
    metadata_failed_at: dict[str, datetime]
    metadata_skipped: set[str]
    cloud_lane_done: set[str]


def _zero() -> dict[str, int]:
    """Return a fresh total per-stage tally."""
    return dict.fromkeys(TALLY_KEYS, 0)


def _natural_id(row: SchedulingLedger) -> str | None:
    """Return the row's file-id natural key, if its stored payload has one."""
    payload = row.payload or {}
    fid = payload.get("file_id")
    return str(fid) if fid is not None else None


def _ledger_fids(rows: Sequence[SchedulingLedger]) -> list[uuid.UUID]:
    """Return distinct UUID natural ids in first-encounter order for query scoping."""
    out: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for row in rows:
        natural_id = _natural_id(row)
        if natural_id is None:
            continue
        try:
            fid = uuid.UUID(natural_id)
        except ValueError:
            continue
        if fid not in seen:
            seen.add(fid)
            out.append(fid)
    return out


def _is_metadata_domain_completed(fid: str, enqueued_at: datetime, done_sets: _DoneSets) -> bool:
    """Apply skip precedence and the metadata D-10 per-ledger-row failure gate."""
    if fid in done_sets.metadata_skipped:
        return True
    if fid not in done_sets.metadata_domain_completed:
        return False
    failed_at = done_sets.metadata_failed_at.get(fid)
    if failed_at is None:
        return True
    if enqueued_at.tzinfo is None:
        enqueued_at = enqueued_at.replace(tzinfo=UTC)
    return enqueued_at <= failed_at


def is_domain_completed(row: SchedulingLedger, done_sets: _DoneSets) -> bool:
    """Return whether a predicate-covered row is terminal for its stage."""
    function = row.function
    if function not in _DOMAIN_COMPLETED_STAGES:
        return False
    fid = _natural_id(row)
    if fid is None:
        return False
    if function == "process_file":
        return fid in done_sets.analyze_done
    if function in CLOUD_LANE_FUNCTIONS:
        return fid in done_sets.cloud_lane_done
    return _is_metadata_domain_completed(fid, row.enqueued_at, done_sets)


def _has_live_job(row: SchedulingLedger, live: set[str]) -> bool:
    """Return whether the deterministic key is already queued or active."""
    return row.key in live


def _is_owned_by_the_cloud_lane(row: SchedulingLedger, in_flight: set[str], awaiting_cloud: set[str]) -> bool:
    """Return whether another cloud component exclusively owns this row's re-drive."""
    if row.function not in _CLOUD_OWNED_FUNCTIONS:
        return False
    natural_id = _natural_id(row)
    return natural_id in in_flight or natural_id in awaiting_cloud


def _is_orphaned(
    row: SchedulingLedger,
    *,
    live: set[str],
    done_sets: _DoneSets,
    in_flight: set[str],
    awaiting_cloud: set[str],
) -> bool:
    """Return whether ledger recovery, rather than a live/domain/cloud owner, owns the row."""
    return (
        not _has_live_job(row, live) and not is_domain_completed(row, done_sets) and not _is_owned_by_the_cloud_lane(row, in_flight, awaiting_cloud)
    )


@dataclass(frozen=True)
class _RegenTarget:
    """A detached ledger-row snapshot safe across adapter commit and rollback boundaries."""

    key: str
    function: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class _OwnerGroup:
    """One owning agent's rows, preserving their first-encounter order."""

    owner_id: str
    rows: tuple[SchedulingLedger, ...]


@dataclass(frozen=True)
class _OwnerPlan:
    """Resolvable owner groups and the rows whose true owner is unknowable."""

    groups: tuple[_OwnerGroup, ...]
    ownerless: tuple[SchedulingLedger, ...]


def _plan_owner_groups(rows: Sequence[SchedulingLedger]) -> _OwnerPlan:
    """Group agent rows by stored owner while preserving both encounter orderings."""
    grouped: dict[str, list[SchedulingLedger]] = {}
    ownerless: list[SchedulingLedger] = []
    for row in rows:
        owner_id = (row.payload or {}).get("agent_id")
        if owner_id is None:
            ownerless.append(row)
            continue
        grouped.setdefault(str(owner_id), []).append(row)
    return _OwnerPlan(
        groups=tuple(_OwnerGroup(owner_id=owner_id, rows=tuple(owned)) for owner_id, owned in grouped.items()),
        ownerless=tuple(ownerless),
    )


@dataclass(frozen=True)
class _ReplayPlan:
    """The four disjoint replay protocols, each retaining encounter order."""

    regenerated: tuple[_RegenTarget, ...]
    controller_rows: tuple[SchedulingLedger, ...]
    push_rows: tuple[SchedulingLedger, ...]
    other_agent_rows: tuple[SchedulingLedger, ...]


def _plan_replay(orphaned: Sequence[SchedulingLedger]) -> _ReplayPlan:
    """Partition orphaned rows without performing database, queue, or routing effects."""
    regenerated = tuple(
        _RegenTarget(key=row.key, function=row.function, payload=dict(row.payload or {}))
        for row in orphaned
        if row.function in LEDGER_REPLAY_REGENERATED
    )
    replayable = [row for row in orphaned if row.function not in LEDGER_REPLAY_REGENERATED]
    agent_rows = [row for row in replayable if row.routing == "agent"]
    return _ReplayPlan(
        regenerated=regenerated,
        controller_rows=tuple(row for row in replayable if row.routing == "controller"),
        push_rows=tuple(row for row in agent_rows if row.function == "push_file"),
        other_agent_rows=tuple(row for row in agent_rows if row.function != "push_file"),
    )
