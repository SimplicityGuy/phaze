"""Derive a clean 1001Tracklists search query from a messy, scene-tagged filename (phaze-fq9h.2).

The naive heuristic already in this codebase (``tracklist_matcher.parse_live_set_filename`` +
``tasks/tracklist.py``'s ``f"{file_artist} {file_event}"``) only recognizes ONE rigid shape --
``"{Artist} - Live @ {Event} {YYYY.MM.DD}.{ext}"`` -- and falls back to a bare artist tag
otherwise. The real archive is dominated by scene-release-style filenames: track-number
prefixes, underscore-for-space fields, source/quality tags (``WEB``, ``FLAC``, ``320``, ...),
trailing release-group tags, and radio-show parentheticals, all of which the naive heuristic
either passes through verbatim (polluting the search query with noise) or fails to match at all
(falling back to "no query"). This module is the fix: :func:`derive_query` strips that noise
while being careful NOT to eat a real date or source token while stripping a scene-group tag --
the exact failure mode this bead's acceptance criteria calls out.

## Pipeline (see ``derive_query`` for the concrete order)

1. Repair mojibake (``services.text_repair.repair_mojibake``) -- a double-encoded filename must
   be fixed BEFORE any of the token-shape heuristics below run on it, or e.g. a mis-decoded
   accented character can shift where a separator appears to be.
2. Strip a known audio/video extension.
3. Strip bracketed/parenthesized noise: source tags (``[WEB]``), and radio-show/broadcast
   markers (``(BBC Radio 1 Essential Mix)``) are dropped entirely; anything else bracketed is
   unwrapped (kept, minus the brackets) rather than discarded, since it may be real event text.
4. Strip a leading track-number prefix (``"01 - "``, ``"Track 02. "``, ...).
5. Pop a trailing release-group tag and any trailing source/quality tags, working ONLY from the
   end of the string. This is tail-only (never a blind whole-string split) specifically so it
   cannot fragment a date that itself contains hyphens/dots elsewhere in the filename.
6. Extract a date (ISO ``YYYY-MM-DD``, ambiguous ``DD-MM-YYYY``/``MM-DD-YYYY``, or a bare year)
   from whatever remains, now that step 5 has already removed the trailing noise that would
   otherwise sit right next to it.
7. Split what is left into artist / event, preferring the existing "Live @" convention
   (:data:`_LIVE_AT_RE`) when present, else treating the first ``-``-delimited field as the
   artist and the rest as the event.

## The phaze-5fta seam

Per the epic (phaz-fq9h) and the dispatcher's scoping decision for this bead, query derivation
must NOT depend on phaze-5fta (a separate, entirely unstarted epic that will learn per-release-
-group filename conventions -- e.g. whether a given scene group writes dates ``DD-MM`` or
``MM-DD`` -- from real corpus evidence and persist them in a ``filename_convention`` table).
Where that store would let us do BETTER than a local heuristic, this module calls
:func:`resolve_ambiguous_date_order` instead of guessing: today that function always returns
``None`` (see its docstring), so an ambiguous date degrades gracefully to year-only granularity
rather than silently picking a day/month order with no evidence behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
import re

from phaze.services.text_repair import repair_mojibake


__all__ = [
    "DerivedQuery",
    "derive_query",
    "resolve_ambiguous_date_order",
]


# --------------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------------

_AUDIO_VIDEO_EXTENSIONS = (
    "mp3",
    "flac",
    "wav",
    "m4a",
    "aac",
    "ogg",
    "wma",
    "opus",
    "alac",
    "mp4",
    "mkv",
    "avi",
    "mov",
    "m4v",
    "webm",
)
_EXTENSION_RE = re.compile(rf"\.(?:{'|'.join(_AUDIO_VIDEO_EXTENSIONS)})$", re.IGNORECASE)

# Source / rip / quality tokens a scene release commonly tacks on, checked as a WHOLE token
# (case-insensitive) only -- never as a substring -- so an artist/event word that merely
# CONTAINS one of these (e.g. an artist named "Webster") is never clipped.
_SOURCE_TOKENS: frozenset[str] = frozenset(
    {
        "WEB",
        "WEBRIP",
        "WEBDL",
        "DL",
        "HDTV",
        "SAT",
        "CABLE",
        "SBD",
        "SOUNDBOARD",
        "AUD",
        "AUDIENCE",
        "FM",
        "DAT",
        "MD",
        "CDR",
        "CDA",
        "VINYL",
        "VINYLRIP",
        "FLAC",
        "MP3",
        "AAC",
        "ALAC",
        "OGG",
        "320",
        "256",
        "192",
        "128",
        "24BIT",
        "16BIT",
        "44KHZ",
        "48KHZ",
        "96KHZ",
        "LOSSLESS",
        "PROPER",
        "REPACK",
        "RETAIL",
        "REMASTER",
        "REMASTERED",
        "STEREO",
        "MONO",
    }
)

# Filler words that are not source tags but also never belong to an artist/event name in this
# corpus's scene-release convention -- excluded both from the general field text AND from the
# scene-group heuristic below (a trailing "LIVE" is filler, not a release-group tag).
_NOISE_TOKENS: frozenset[str] = frozenset(
    {"LIVE", "FULL", "FULLSET", "SET", "MIX", "PART1", "PART2", "UNOFFICIAL", "BOOTLEG", "PROMO", "VIP", "REUP", "REUP2"}
)

# Bracket/parenthetical content matching any of these (case-insensitive) marks the WHOLE bracket
# as a radio-show/broadcast marker to drop -- one of this bead's four required test categories.
_RADIO_SHOW_RE = re.compile(r"\b(radio(?:\s*show)?|essential\s*mix|broadcast|on[\s-]*air|radio\s*rip)\b", re.IGNORECASE)

_BRACKET_RE = re.compile(r"[(\[{]([^()\[\]{}]*)[)\]}]")

# A track-number prefix: 1-2 digits (optionally preceded by "Track"), then a separator, then a
# LETTER. The letter lookahead is what keeps this from ever eating the leading digits of a
# 4-digit year that happens to open the filename (e.g. "2020-08-05 - Artist...").
_TRACK_NUMBER_RE = re.compile(r"^(?:track\s*)?\d{1,2}[\s._-]+(?=[A-Za-z])", re.IGNORECASE)

# The last "tight" token (no internal space/dot/underscore/hyphen) in the string, with whatever
# separator precedes it captured separately (see _pop_trailing_group_and_sources -- the SHAPE of
# that separator is itself evidence for whether the token is a scene-tail field or just the last
# word of a prose phrase). Matched repeatedly from the end ONLY -- see _pop_trailing_group_and_sources
# for why this must never become a blind whole-string split.
_TRAILING_TOKEN_RE = re.compile(r"(?P<sep>[\s._-]+)(?P<field>[^\s._-]+)\s*$")

# ISO-shaped date, unambiguous: the year always comes first, so there is nothing to disambiguate.
_ISO_DATE_RE = re.compile(r"(?P<y>19[7-9]\d|20[0-3]\d)[-._](?P<m>0[1-9]|1[0-2])[-._](?P<d>0[1-9]|[12]\d|3[01])")

# DD-MM-YYYY / MM-DD-YYYY shaped date -- ambiguous unless the first component is > 12 (which can
# then only be a day).
_AMBIGUOUS_DATE_RE = re.compile(r"(?P<a>0[1-9]|[12]\d|3[01])[-._](?P<b>0[1-9]|1[0-2])[-._](?P<y>19[7-9]\d|20[0-3]\d)")

# A bare 4-digit year standing on its own (never glued to other digits, so it can't accidentally
# grab half of an 8-digit date or a bitrate-adjacent number).
_BARE_YEAR_RE = re.compile(r"(?<!\d)(19[7-9]\d|20[0-3]\d)(?!\d)")

# The "Live @ Event" convention tracklist_matcher.parse_live_set_filename already recognizes --
# recognized here too, but WITHOUT that heuristic's rigid "must end in a full YYYY.MM.DD date and
# nothing else" requirement, so it still fires once scene noise around it has been stripped.
_LIVE_AT_RE = re.compile(r"^(?P<artist>.+?)\s*-?\s*live\s*@\s*(?P<event>.+)$", re.IGNORECASE)

# This module's own separator literals -- a token built ENTIRELY from these is always a stray
# parsing artifact (e.g. a dangling "@" left after _LIVE_AT_RE's "at" separator loses its
# neighboring word to noise-filtering), never a legitimate artist/event token, and is dropped in
# `_clean_field`. Deliberately narrow and NOT "any non-alphanumeric token" -- see that function's
# docstring for why a blanket rule would wrongly eat real punctuation-joined names.
_STRUCTURAL_SYMBOLS = frozenset({"@"})

_WHITESPACE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------------------------
# Public result type
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DerivedQuery:
    """The result of deriving a 1001Tracklists search query from one filename.

    ``query`` is the string to hand to ``TracklistScraper.search()``. The remaining fields are
    the structured signals derivation extracted along the way, kept for downstream consumers --
    ``date``/``year``/``date_ambiguous`` feed exactly the same disambiguation shape
    ``tracklist_matcher.compute_match_confidence`` already scores on, and ``scene_group`` /
    ``source_tags`` / ``radio_show`` are provenance a future result-scorer (phaze-fq9h.6) can use
    to explain or sanity-check a match without re-deriving it.
    """

    query: str
    artist: str | None
    event: str | None
    date: _date | None
    year: int | None
    date_ambiguous: bool
    scene_group: str | None
    source_tags: tuple[str, ...]
    radio_show: bool


# --------------------------------------------------------------------------------------------
# The phaze-5fta integration seam
# --------------------------------------------------------------------------------------------


def resolve_ambiguous_date_order(*, scene_group: str | None, year: int, first: int, second: int) -> _date | None:
    """Resolve a DD-MM vs MM-DD ambiguity using per-release-group evidence -- SEAM for phaze-5fta.

    ``first``/``second`` are the two ``<=12`` numeric components read in filename order (a
    filename read as ``"05-08-2021"`` arrives as ``first=5, second=8``): exactly one of them is
    the day and the other the month, and nothing in the string itself can tell them apart. This
    is not a rare edge case -- phaze-5fta's own epic description measures it directly: 35.2% of
    this corpus's dated scene filenames are individually ambiguous this way.

    phaze-5fta is building a ``filename_convention`` table, keyed by
    ``(scope='release_group', scope_value=<scene_group>)``, holding corpus-learned evidence for
    which order a given release group's uploader used (e.g. "1373 supporting / 0 contradicting ->
    MM-DD"). Once that table and its query layer exist, AND phaze-5fta.5's external-validation
    gate has flipped its enable flag, this function is where that lookup belongs: query the store
    for ``scene_group``, and return the resolved date when the evidence clears the confidence
    threshold phaze-5fta defines.

    No such store exists yet, so this ALWAYS returns ``None`` -- callers must treat the date as
    genuinely ambiguous and fall back to year-only granularity (see ``DerivedQuery.date_ambiguous``).
    This is deliberately not a stub that pretends to resolve anything: guessing an order here with
    no evidence behind it would silently defeat phaze-5fta's own fail-closed rollout design (that
    epic's DESIGN note 5) the moment this module's caller trusted the result.
    """
    del scene_group, year, first, second  # unused until the phaze-5fta store exists
    return None


# --------------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------------


def _strip_extension(text: str) -> str:
    return _EXTENSION_RE.sub("", text)


def _strip_brackets(text: str) -> tuple[str, list[str], bool]:
    """Drop source-tag and radio-show brackets; unwrap (keep, minus brackets) everything else."""
    source_tags: list[str] = []
    radio_show = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal radio_show
        content = match.group(1).strip()
        if not content:
            return " "
        subtokens = [t for t in re.split(r"[^A-Za-z0-9]+", content) if t]
        if subtokens and all(t.upper() in _SOURCE_TOKENS for t in subtokens):
            source_tags.extend(t.upper() for t in subtokens)
            return " "
        if _RADIO_SHOW_RE.search(content):
            radio_show = True
            return " "
        return f" {content} "

    stripped = _BRACKET_RE.sub(_replace, text)
    return stripped, source_tags, radio_show


def _strip_track_number(text: str) -> str:
    return _TRACK_NUMBER_RE.sub("", text)


def _source_token(token: str) -> str | None:
    upper = token.upper()
    return upper if upper in _SOURCE_TOKENS else None


def _is_tight_separator(sep: str) -> bool:
    """True when *sep* is a "glued" scene-tail delimiter (hyphen/underscore only, no whitespace).

    The positional half of the scene-group corroboration check -- see
    ``_pop_trailing_group_and_sources``'s docstring for why this specific distinction (glued vs.
    whitespace-containing) is what actually separates a scene-tail field from the last word of an
    ordinary prose phrase.
    """
    return len(sep) > 0 and not any(ch.isspace() for ch in sep)


def _looks_like_scene_group(token: str) -> bool:
    """Best-effort SHAPE heuristic for a scene release-group tag: is this token even plausible?

    Self-contained per this bead's scope -- NOT the corpus-validated group extractor phaze-5fta.3
    will build (that one is trained on real evidence and explicitly designed to reject years and
    source tokens too; see that epic's DESIGN note 3). It only claims a token as plausible when it
    is alphanumeric AND either ALL CAPS or contains a digit, which is a common shape for
    scene-release group monikers (``GRVMSTR``, ``C4``, ``0DAY``, ...). The tradeoff: an
    all-lowercase-letters group tag with no digit is NEVER stripped by this heuristic and is left
    in the event text instead -- a false negative rather than the false positive of eating real
    event words, and exactly the gap phaze-5fta's corpus-learned evidence is meant to close later
    via the seam above.

    NOTE this is shape-only and deliberately under-inclusive-by-itself: on its own it also matches
    an ordinary trailing ALL-CAPS acronym in a prose event name (festival/venue names in this
    corpus are routinely acronyms -- ``EDC``, city/country codes like ``NYC``/``USA``/``DE``).
    ``_pop_trailing_group_and_sources`` is what supplies the SECOND, POSITIONAL piece of evidence
    (see its docstring) that keeps this function's permissiveness from mis-firing on those; do not
    call this alone as the whole test for "is this a group tag".
    """
    if not token or not token.isalnum():
        return False
    if token.isdigit():
        return False
    upper = token.upper()
    if upper in _SOURCE_TOKENS or upper in _NOISE_TOKENS:
        return False
    if not 2 <= len(token) <= 20:
        return False
    return token.isupper() or any(ch.isdigit() for ch in token)


def _pop_trailing_group_and_sources(text: str) -> tuple[str, list[str], str | None]:
    """Strip a trailing release-group tag and any trailing source tags, tail-only.

    Deliberately never a whole-string split: matching only ``_TRAILING_TOKEN_RE`` against the END
    of the string means this cannot fragment a date that itself contains hyphens/dots sitting
    earlier in the filename (e.g. ``"Artist-Event-2020-08-05-WEB-FLAC-GROUP"``) -- it only ever
    removes exactly the trailing source/group tokens, leaving the date intact for `_extract_date`
    below.

    Order matters: the scene convention this targets is ``...-DATE-SOURCE-SOURCE-GROUP`` (the
    group tag is the LAST token, per phaze-5fta's own corpus finding -- "grouping ... by trailing
    scene release-group tag"), so the group is popped FIRST, from the true tail, and only THEN do
    we loop popping source tags from what is now the new tail. Popping sources first would stop
    at the very first trailing token (the group itself, since it is not a source token) and never
    reach the source tags sitting behind it.

    THE POSITIONAL GUARD (fixes phaze-fq9h.2 review round 1): ``_looks_like_scene_group`` alone is
    shape-only and, by itself, equally matches a real trailing ALL-CAPS event acronym in a prose
    filename (``"Carl Cox - Live @ EDC.mp3"`` -> would wrongly claim ``EDC``; likewise trailing
    country/city codes like ``NYC``/``USA``/``DE``). What actually distinguishes a scene-tail
    field from the last word of a prose phrase is the SEPARATOR immediately before it: this
    corpus's scene convention glues its trailing fields together with a TIGHT hyphen/underscore
    (``...-WEB-FLAC-GRVMSTR``, no surrounding spaces), never a plain word-boundary space
    (``"Live @ EDC"``, ``"Miami USA"``). So a candidate is only accepted as a group when its
    separator contains no whitespace -- weak/no positional evidence means the token stays in the
    event text, the same conservative direction ``_looks_like_scene_group`` already takes for
    all-lowercase tags. A tight-hyphen-delimited short acronym that is genuinely NOT a group (e.g.
    a country code some uploader tacked on with a bare hyphen) is a known residual ambiguity this
    heuristic cannot resolve without corpus evidence -- exactly phaze-5fta seam material, not a
    case this bead can fix by construction.
    """
    group: str | None = None
    match = _TRAILING_TOKEN_RE.search(text)
    if match is not None and _is_tight_separator(match.group("sep")) and _looks_like_scene_group(match.group("field")):
        group = match.group("field")
        text = text[: match.start()]

    sources: list[str] = []
    while True:
        match = _TRAILING_TOKEN_RE.search(text)
        if match is None:
            break
        token = _source_token(match.group("field"))
        if token is None:
            break
        sources.append(token)
        text = text[: match.start()]

    return text, sources, group


def _safe_date(year: int, month: int, day: int) -> _date | None:
    try:
        return _date(year, month, day)
    except ValueError:
        return None


def _remove_span(text: str, match: re.Match[str]) -> str:
    return f"{text[: match.start()]} {text[match.end() :]}"


@dataclass(frozen=True, slots=True)
class _DateExtraction:
    text: str
    date: _date | None
    year: int | None
    ambiguous: bool


def _extract_date(text: str, *, scene_group: str | None) -> _DateExtraction:
    match = _ISO_DATE_RE.search(text)
    if match is not None:
        year, month, day = int(match.group("y")), int(match.group("m")), int(match.group("d"))
        return _DateExtraction(_remove_span(text, match), _safe_date(year, month, day), year, ambiguous=False)

    match = _AMBIGUOUS_DATE_RE.search(text)
    if match is not None:
        first, second, year = int(match.group("a")), int(match.group("b")), int(match.group("y"))
        remaining = _remove_span(text, match)
        if first > 12:
            # Only valid as DD-MM-YYYY -- the second component is constrained to <=12 by the
            # regex itself, so a first component >12 can never be a month.
            return _DateExtraction(remaining, _safe_date(year, second, first), year, ambiguous=False)
        resolved = resolve_ambiguous_date_order(scene_group=scene_group, year=year, first=first, second=second)
        return _DateExtraction(remaining, resolved, year, ambiguous=resolved is None)

    match = _BARE_YEAR_RE.search(text)
    if match is not None:
        return _DateExtraction(_remove_span(text, match), None, int(match.group(1)), ambiguous=False)

    return _DateExtraction(text, None, None, ambiguous=False)


def _clean_field(field: str) -> str:
    """Normalize one artist/event field: spaces for underscores, noise words and stray structural
    separators dropped.

    The structural-separator filter (fixes phaze-fq9h.2 review round 1) exists because upstream
    steps can leave one of THIS MODULE'S OWN separator literals standing alone once its
    neighboring word is removed as noise -- e.g. ``"Live @ EDC"`` with a (now-fixed) upstream bug
    popping ``EDC`` left the field ``"Live @"``, and dropping ``"LIVE"`` as a noise word left the
    bare token ``"@"`` -- the literal ``_LIVE_AT_RE`` uses to mean "at" -- standing in for an
    entire event name. This filters ONLY tokens built purely from :data:`_STRUCTURAL_SYMBOLS`
    (today just ``@``), deliberately NOT a blanket "drop anything with no alphanumeric character"
    rule: that would also eat legitimate punctuation-joined names genuinely present in this
    corpus, like the duo "Above & Beyond" -- "&" must survive this filter.
    """
    field = field.replace("_", " ")
    field = _WHITESPACE_RE.sub(" ", field).strip(" .-_")
    tokens = [t for t in field.split(" ") if t.upper() not in _NOISE_TOKENS and not _STRUCTURAL_SYMBOLS.issuperset(t)]
    return " ".join(tokens).strip()


def _finalize_fields(text: str) -> tuple[str | None, str | None]:
    text = text.strip(" -_.")
    if not text:
        return None, None

    live_match = _LIVE_AT_RE.match(text.replace("_", " "))
    if live_match is not None:
        artist = _clean_field(live_match.group("artist"))
        event = _clean_field(live_match.group("event"))
        return (artist or None), (event or None)

    fields = [_clean_field(f) for f in re.split(r"\s*-\s*", text)]
    fields = [f for f in fields if f]
    if not fields:
        return None, None
    if len(fields) == 1:
        return fields[0], None
    return fields[0], " ".join(fields[1:])


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------


def derive_query(filename: str) -> DerivedQuery:
    """Derive a 1001Tracklists search query (plus structured signals) from one filename.

    Never raises: every step degrades gracefully (a filename that doesn't parse into artist +
    event just falls back to whatever text is left, same shape as the pre-existing "bare
    artist" fallback in ``tasks/tracklist.py``), so an unexpectedly-shaped input yields a
    lower-quality query rather than an exception 250,000 files could trip on.
    """
    text = repair_mojibake(filename)
    text = _strip_extension(text)
    text, bracket_sources, radio_show = _strip_brackets(text)
    text = _strip_track_number(text)
    text, tail_sources, scene_group = _pop_trailing_group_and_sources(text)
    date_extraction = _extract_date(text, scene_group=scene_group)
    artist, event = _finalize_fields(date_extraction.text)

    source_tags = tuple(sorted(set(bracket_sources) | set(tail_sources)))

    query_parts = [part for part in (artist, event) if part]
    if date_extraction.year is not None:
        # The year is trustworthy even when the day/month order is ambiguous (see
        # resolve_ambiguous_date_order) -- only the exact `date` is unsettled -- and it is a
        # useful disambiguator in search text for recurring festivals ("Awakenings 2019" vs
        # "Awakenings 2020").
        query_parts.append(str(date_extraction.year))
    query = " ".join(query_parts).strip()
    if not query:
        # Total parse failure (e.g. a filename that is ONLY noise tokens): fall back to the
        # mojibake-repaired original with underscores turned to spaces, same spirit as
        # tasks/tracklist.py's pre-existing "no artist -> no query" path, but derive_query
        # itself never returns an unusably empty string when there is ANY text to offer.
        query = _clean_field(repair_mojibake(filename).replace("_", " ")) or repair_mojibake(filename)

    return DerivedQuery(
        query=query,
        artist=artist,
        event=event,
        date=date_extraction.date,
        year=date_extraction.year,
        date_ambiguous=date_extraction.ambiguous,
        scene_group=scene_group,
        source_tags=source_tags,
        radio_show=radio_show,
    )
