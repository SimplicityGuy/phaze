"""Repair double-encoded UTF-8 ("mojibake") in already-decoded ``str`` values (phaze-x4ux).

Background: a file named with UTF-8 bytes (e.g. ``Väth``) gets misread somewhere upstream as
Windows-1252 or Latin-1 and re-encoded to UTF-8, producing garbled text such as ``VÃ¤th`` (single
mis-decode) or ``VÃƒÂ¤th`` (the same mistake applied twice -- a double round trip). That garbled
text then propagates verbatim into search, fuzzy tracklist matching, and AI rename proposals,
degrading all three silently rather than failing loudly.

Stdlib-only by design (mirrors ``services/pg_text``): the repair is a bounded, self-terminating
byte round trip, not a general-purpose Unicode-repair library. It intentionally trades breadth
(it will not catch every mojibake shape ``ftfy`` handles) for a small, fully-understood surface
with no third-party dependency.

This module does NOT touch ``agent_watcher/observer.py``'s ``os.fsdecode``/surrogateescape
handling -- that solves an orthogonal problem (bytes that cannot be decoded at all). This module
repairs strings that decoded "successfully" into the WRONG characters.

## The core safety property

For a round trip through a single-byte codec (``cp1252`` or ``latin-1``) followed by a UTF-8
decode to even be *attempted* as a repair, two independent gates must both pass:

1. **Lossless encode.** ``s.encode(codec)`` must succeed. A string containing any character
   outside the codec's 256-code-point repertoire (CJK, emoji, astral characters, ...) fails this
   immediately, so no repair is attempted on non-Latin text.
2. **Valid UTF-8 decode.** The resulting bytes must decode as UTF-8 without error.

Empirically, legitimate accented text (``Väth``, ``Björk``, ``Sigur Rós``, ``São Paulo``,
``Straße``, ...) almost never survives gate 2: encoding a single correctly-composed accented
character as cp1252/latin-1 produces a byte sequence that is essentially never itself valid UTF-8
(real UTF-8 continuation-byte patterns are specific and rare to hit by chance). That is *why* the
round trip is safe to attempt speculatively rather than only on text that looks suspicious.

A third gate makes the result monotonic and self-terminating: a **strict length decrease**. Every
real double-encoding round trip collapses one or more multi-byte UTF-8 sequences (2-4 encoded
bytes) that were themselves being read back as 1 character each into fewer, correctly-composed
characters, so a genuine repair pass always yields a *shorter* string. Requiring strict decrease
means a no-op round trip (pure ASCII, where cp1252/latin-1/UTF-8 all agree) is rejected rather
than looping forever, and a decode that happens to succeed without actually correcting anything
is rejected too.

Looping this bounded-and-monotonic step handles DOUBLE encoding (``VÃƒÂ¤th`` needs two passes:
first to ``VÃ¤th``, then to ``Väth``) while remaining safe against over-correction: the loop stops
the moment a pass fails to decode or fails to shrink, which happens immediately once the text is
correctly composed (idempotency falls out of the same gates, not a separate check).
"""

from __future__ import annotations


# Bounded well above the two passes the known production case (phaze-x4ux) needs, so a
# pathological input cannot loop unboundedly, while leaving headroom for a hypothetical
# triple-encoding without special-casing it.
_MAX_REPAIR_PASSES = 4

# Real-world mojibake is produced by either flavor of single-byte Windows codepage: cp1252 (the
# Windows-1252 superset, used by most Windows tooling) and latin-1/ISO-8859-1 (the strict Latin-1
# subset some tools use instead). They agree everywhere except 0x80-0x9F, where cp1252 defines
# printable characters (curly quotes, em dash, ...) and latin-1 defines C1 control codes -- trying
# both catches mojibake produced by either assumption. cp1252 is tried first since it is the more
# common real-world source and a strict superset of latin-1's printable repertoire.
_ROUND_TRIP_CODECS: tuple[str, ...] = ("cp1252", "latin-1")


def _round_trip_once(s: str) -> str | None:
    """Attempt one lossless single-byte-codec round trip; return the repaired text, or ``None``.

    Tries each codec in :data:`_ROUND_TRIP_CODECS` in order and accepts the first candidate that
    both decodes cleanly as UTF-8 AND is strictly shorter than the input (see the module
    docstring for why both gates matter). Returns ``None`` if no codec produces such a candidate,
    which is the signal the caller's loop uses to stop.
    """
    for codec in _ROUND_TRIP_CODECS:
        try:
            candidate = s.encode(codec).decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            continue
        if len(candidate) < len(s):
            return candidate
    return None


def repair_mojibake(s: str) -> str:
    """Repair double-encoded UTF-8 mojibake in *s*, conservatively.

    Repeatedly attempts a lossless cp1252/latin-1 -> UTF-8 round trip (see the module docstring
    for the exact safety gates), accepting each pass only when it is both a clean decode and a
    strict reduction in the mojibake signature (approximated by string length -- see above).
    Stops as soon as a pass is rejected, is idempotent (``repair_mojibake(repair_mojibake(x)) ==
    repair_mojibake(x)``), and NEVER raises: any ``UnicodeDecodeError``/``UnicodeEncodeError``
    along the way simply ends the loop and returns the best repair found so far (the original
    string, unchanged, if no pass ever qualified).

    A true no-op on:
      - pure ASCII (the round trip succeeds but never shrinks, so it is rejected on pass 1),
      - already-correct accented Unicode text (the round trip fails to decode as UTF-8 at all,
        which is the common case -- see the module docstring), and
      - undecodable-by-construction input (never raises; returns *s* verbatim).
    """
    current = s
    for _ in range(_MAX_REPAIR_PASSES):
        candidate = _round_trip_once(current)
        if candidate is None:
            break
        current = candidate
    return current
