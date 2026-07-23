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
