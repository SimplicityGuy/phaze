"""Off-request snapshot of the existing Summary attention items for shared shell chrome."""

from __future__ import annotations

import time


ATTENTION_REFRESH_SECONDS = 5.0
_MAX_AGE_SECONDS = 3 * ATTENTION_REFRESH_SECONDS
_items: list[dict[str, str | int]] = []
_measured_at = 0.0
_available = False


def set_attention_snapshot(items: list[dict[str, str | int]], *, known: bool) -> None:
    """Publish a complete measured snapshot, or hide retained values after degradation."""
    global _items, _measured_at, _available
    _available = known
    if known:
        _items = [dict(item) for item in items]
        _measured_at = time.monotonic()


def get_attention_snapshot() -> tuple[list[dict[str, str | int]], bool]:
    """Return a defensive copy and freshness; an unmeasured/stalled refresh is unknown."""
    known = _available and _measured_at > 0 and time.monotonic() - _measured_at <= _MAX_AGE_SECONDS
    return [dict(item) for item in _items], known


def get_attention_counts() -> dict[str, int]:
    """O(1) counts for the existing DAG poll and server-first-paint store contract."""
    known = _available and _measured_at > 0 and time.monotonic() - _measured_at <= _MAX_AGE_SECONDS
    return {"attentionCount": len(_items), "attentionKnown": int(known)}
