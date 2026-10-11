"""Plain-text changed segments for the shared review row; rendering owns escaping."""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import NamedTuple


class DiffSegment(NamedTuple):
    """A contiguous piece of one value, with no HTML or presentation state."""

    text: str
    changed: bool


class DiffSegments(NamedTuple):
    """Both values reconstructed independently from their ordered segments."""

    before: tuple[DiffSegment, ...]
    after: tuple[DiffSegment, ...]


def changed_segments(before: str, after: str) -> DiffSegments:
    """Mark insertions, deletions and replacements without trusting either input as markup."""
    old: list[DiffSegment] = []
    new: list[DiffSegment] = []
    for operation, old_start, old_end, new_start, new_end in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        changed = operation != "equal"
        if old_start != old_end:
            old.append(DiffSegment(before[old_start:old_end], changed))
        if new_start != new_end:
            new.append(DiffSegment(after[new_start:new_end], changed))
    return DiffSegments(tuple(old), tuple(new))
