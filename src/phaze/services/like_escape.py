"""Escape LIKE/ILIKE metacharacters so free-text search input is matched literally.

Stdlib-only by design, mirroring ``services/pg_text.py``. Extracted so every ``ilike()``/``like()``
call site that wraps *operator-typed* text in ``%...%`` wildcards can share one escaping rule instead
of re-deriving it (or, worse, omitting it) per call site.

Why this exists: SQLAlchemy's ``.ilike(pattern)`` binds ``pattern`` as a parameter -- there is no SQL
injection -- but PostgreSQL still interprets ``%``, ``_``, and ``\\`` *inside that bound value* as LIKE
pattern syntax, not literal characters. Music-archive filenames and artist tags are dense with
underscores (``Coachella_2024``) and occasionally carry a literal backslash, so an unescaped wildcard
wrap silently over-matches (``_`` = any single char) or drops the operator's literal backslash from the
effective pattern. ``services/companion.py`` already carries the correct fix for its own directory-
prefix LIKE (``_escape_like`` / ``_LIKE_ESCAPE_CHAR``); this module is the same rule, generalized for
substring (``%...%``) wildcarding so ``proposal_queries.py`` and ``search_queries.py`` can reuse it
instead of each re-deriving their own ``re.sub``.
"""

from __future__ import annotations


LIKE_ESCAPE_CHAR = "\\"


def escape_like(value: str) -> str:
    """Escape backslash, ``%``, and ``_`` so ``value`` matches only itself inside a LIKE/ILIKE pattern.

    Backslash MUST be escaped first -- escaping ``%``/``_`` before backslash would double-escape the
    backslashes those substitutions just introduced. Pair the result with ``escape=LIKE_ESCAPE_CHAR``
    on the ``.ilike()``/``.like()`` call (PostgreSQL's default LIKE escape character is already
    backslash, but passing it explicitly documents the contract and matches the existing
    ``companion.py`` call sites).
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def like_wildcard(value: str) -> str:
    """Build a ``%<escaped value>%`` substring-match pattern with metacharacters escaped.

    Convenience for the common "contains" search case shared by the proposal search filter and the
    unified-search facet filters -- callers still must pass ``escape=LIKE_ESCAPE_CHAR`` to
    ``.ilike()``/``.like()``.
    """
    return f"%{escape_like(value)}%"
