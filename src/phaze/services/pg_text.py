"""Sanitize free text for storage in a PostgreSQL UTF8 ``text``/``jsonb`` column.

Stdlib-only by design. Extracted from ``services/metadata.py`` (which imports ``mutagen``) so the
control-plane routers can sanitize agent-supplied ``error_message`` free text without dragging the
audio-tag stack into the API import graph.

Why this exists: PostgreSQL cannot store NUL (``U+0000``) or lone Unicode surrogates
(``U+D800``-``U+DFFF``) in a UTF8 column — the driver raises ``CharacterNotInRepertoireError``
("invalid byte sequence for encoding UTF8: 0x00") and the enclosing transaction aborts. For the
per-stage failure writers that is worse than a lost row: the marker upsert and the scheduling-ledger
clear share one transaction, so a rejected ``error_message`` rolls BOTH back. The ledger row survives,
recovery re-enqueues the file, it fails again on the same NUL-bearing exception text, forever. That is
the unbounded-recovery-loop the version-skew guard (T-81-03-03) exists to prevent, reached by a
different door.

Bounded length is a separate concern and is handled by the callers' ``max_length`` / slice.
"""

from __future__ import annotations

import math
import re


# Only NUL and lone surrogates are unstorable. Every other C0/C1 control, DEL, and Unicode
# noncharacter (U+FFFE/U+FFFF, U+FDD0-U+FDEF) is VALID in a UTF8 database and is deliberately left
# intact -- stripping those would corrupt legitimate text. See PostgreSQL §8.14.
_PG_INVALID_CHARS = re.compile(r"[\x00\ud800-\udfff]")


def sanitize_pg_text(s: str) -> str:
    """Strip the characters PostgreSQL cannot store in a UTF8 text/jsonb column.

    Removes only NUL (U+0000) and lone Unicode surrogates (U+D800-U+DFFF); all other control
    characters and noncharacters are preserved.
    """
    return _PG_INVALID_CHARS.sub("", s)


def contains_pg_invalid_chars(s: str) -> bool:
    """Return ``True`` if ``s`` contains NUL (U+0000) or a lone Unicode surrogate.

    For callers that must REJECT unstorable input rather than silently strip it -- e.g. a
    filesystem path, where dropping a NUL would resolve to a different path than the operator
    typed. Uses the same character class as :func:`sanitize_pg_text`.
    """
    return _PG_INVALID_CHARS.search(s) is not None


def find_pg_unsafe_json_reason(value: object) -> str | None:
    """Recursively find the first PG-unstorable leaf in a JSON-shaped value (phaze-hvve5).

    A ``dict``/``list`` payload bound for a ``jsonb`` column is not caught by
    :func:`sanitize_pg_text` / :func:`contains_pg_invalid_chars` (both take a single ``str``) nor
    by a scalar ``Field(ge=..., le=...)`` bound (which only guards a top-level float, not one
    buried inside a dict/list value) -- so a NUL/lone-surrogate string or a non-finite float
    nested anywhere in the structure sails through Pydantic validation and aborts the transaction
    at ``session.commit()`` (``CharacterNotInRepertoireError`` for the text case,
    ``DataError``/``invalid input syntax for type json`` for NaN/Infinity, since Python's default
    JSON encoder emits the bare, non-JSON-standard tokens ``NaN``/``Infinity`` that PostgreSQL's
    ``jsonb`` input parser rejects).

    Walks ``dict`` (both keys and values), ``list``, ``str``, and ``float``; every other scalar
    type (``bool``, ``int``, ``None``) is always PG-safe and is skipped. Returns a short
    human-readable reason suitable for a Pydantic ``ValueError`` detail, or ``None`` if the value
    is safe to store as-is.
    """
    if isinstance(value, str):
        return "contains a NUL byte or a lone Unicode surrogate" if contains_pg_invalid_chars(value) else None
    if isinstance(value, float):
        return "is NaN or Infinity, which PostgreSQL's jsonb parser rejects" if math.isnan(value) or math.isinf(value) else None
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and contains_pg_invalid_chars(key):
                return f"has a key ({key!r}) containing a NUL byte or a lone Unicode surrogate"
            reason = find_pg_unsafe_json_reason(item)
            if reason is not None:
                return reason
        return None
    if isinstance(value, list):
        for item in value:
            reason = find_pg_unsafe_json_reason(item)
            if reason is not None:
                return reason
        return None
    return None
