"""Junk-companion review vocabulary (DB-free, phaze-bk5jp).

Lives outside :mod:`phaze.models` so the agent-side quarantine task (phaze-lwuf6) and its wire
contract can name the same statuses without importing SQLAlchemy (the D-25 agent import boundary).
:mod:`phaze.models.companion_junk_review` re-exports every symbol.
"""

from __future__ import annotations

import enum


class JunkReviewStatus(enum.StrEnum):
    """Where one ``companion_junk_review`` row stands. :data:`TRANSITIONS` is the whole state machine."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class JunkReviewReason(enum.StrEnum):
    """Why the detector proposed a file: its stored junk class, or a duplicate of a linked companion (decision 1)."""

    EMPTY = "empty"
    ALL_NUL = "all_nul"
    KNOWN_STAMP = "known_stamp"
    SITE_AD = "site_ad"
    DUPLICATE = "duplicate"


TERMINAL_STATUSES: frozenset[JunkReviewStatus] = frozenset({JunkReviewStatus.QUARANTINED, JunkReviewStatus.FAILED})
"""Statuses no transition leaves. Each is the outcome of an executed attempt, kept as the audit record.

A quarantined identity that reappears on disk, or a failed one still there, gets a NEW pending row
(operator decision 4 of 2026-10-07, "Back to review", recorded on epic phaze-4x319); the terminal
row is never reopened, so what was removed and when stays on record.
"""

TRANSITIONS: dict[JunkReviewStatus, frozenset[JunkReviewStatus]] = {
    JunkReviewStatus.PENDING: frozenset({JunkReviewStatus.APPROVED, JunkReviewStatus.REJECTED}),
    # Undo, a hash-wide rejection that reaches a copy approved earlier (decision 5), or dispatch.
    JunkReviewStatus.APPROVED: frozenset({JunkReviewStatus.PENDING, JunkReviewStatus.REJECTED, JunkReviewStatus.EXECUTING}),
    JunkReviewStatus.REJECTED: frozenset({JunkReviewStatus.PENDING}),
    JunkReviewStatus.EXECUTING: frozenset({JunkReviewStatus.QUARANTINED, JunkReviewStatus.FAILED}),
    JunkReviewStatus.QUARANTINED: frozenset(),
    JunkReviewStatus.FAILED: frozenset(),
}
"""Allowed ``from -> {to}`` moves. Terminal statuses map to the empty set, so nothing leaves them."""


def allowed_from(target: JunkReviewStatus) -> frozenset[JunkReviewStatus]:
    """Every status a row may move to ``target`` from."""
    return frozenset(source for source, targets in TRANSITIONS.items() if target in targets)
