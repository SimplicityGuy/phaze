"""Extract the scene release-group token from a filename -- the corrected extractor (phaze-5fta.1).

## Why this module exists

The phaze-5fta epic rests on one measurement: of the corpus filenames carrying a
``\\d{2}-\\d{2}-\\d{4}`` date, 35.2% are individually ambiguous (both components ``<= 12``, so
nothing in the string says whether it is ``DD-MM`` or ``MM-DD``), and grouping the self-resolving
remainder by trailing scene release-group tag shows unanimous per-group conventions -- talion
MM-DD (1,373 supporting / 0 contradicting), 1king DD-MM (232 / 0), and so on.

That measurement was taken with a **naive trailing-token regex, and it mistook YEARS for release
groups**. The epic records the resulting MIXED rows explicitly as artifacts of that bug. Everything
downstream -- the ``filename_convention`` store (phaze-5fta.2), the learner (phaze-5fta.3), the
gated rename fallback (phaze-5fta.4) -- is keyed on the token this module returns, so a token that
does not actually mean "release group" poisons the entire molecule. Hence the epic's DESIGN note 3:
*extractor first, validated independently, before the learner lands on top of it.*

This module is that corrected extractor. It is a pure function: no I/O, no database, no filesystem
access, no clock. It takes a filename (or a path -- only the final component is examined) and
returns the release-group token, lowercased, or ``None``.

## The rules, and what each one is defending against

A trailing token is claimed as a release group only when **all** of these hold:

1. **It is not purely numeric.** This is the defect the measurement hit: ``2014`` and ``2009`` are
   years, ``04`` is a date component, and neither is a release group. A group token in this corpus
   always contains at least one letter (``talion``, ``1king``, ``1real``, ``cin_int``, ``edc``), so
   "must contain a letter" rejects every bare year by construction rather than by blacklisting
   plausible year values -- a blacklist would have to be bounded, and would then leak
   ``1969``/``2049`` back in.
2. **It is not scene vocabulary.** Source tags (``sbd``, ``cable``, ``sat``, ``stream``, ``line``,
   ``web``, ``fm``, ...), quality/format tokens (``flac``, ``320``, ``24bit``, ``1080p``, ...) and
   scene status markers (``proper``, ``repack``, ``int``, ...) describe the FILE, never its
   uploader. These are recognized as a whole token, case-insensitively, and never as a substring:
   ``ffm`` is a release group and must not be clipped to the source tag ``fm``.
3. **Its shape is plausible.** 2-20 characters of ASCII letters, digits and interior underscores
   only. The underscore allowance is load-bearing: ``cin_int`` and ``coin_int`` are single group
   keys in the corpus, not a group followed by the ``int`` marker (see ``_merge_internal_suffix``).
4. **Something corroborates that it is a scene tail field at all.** Shape alone equally matches the
   last word of an ordinary prose title, so a candidate is accepted only when the separator before
   it is "glued" (hyphen/underscore/dot/semicolon, no whitespace -- this corpus's scene convention
   writes ``...-sbd-talion``), or a recognized scene field sits immediately around it (a popped
   trailing status token, or a preceding source/quality/status token or 4-digit year). Without that,
   ``"Artist - Live @ EDC.mp3"`` would yield ``edc`` from a prose venue acronym.

Scene noise AFTER the group is popped before judging, which is what handles the ``-proper-``
re-release shape seen on hsalive releases in both of its orders (``...-sbd-proper-hsalive`` and
``...-sbd-hsalive-proper``).

**The bias is deliberately toward ``None``.** A missing group costs the learner one row of
evidence; a wrong group invents a convention for a group that never existed, or -- worse -- files
one uploader's evidence under another's key. :func:`extract_release_group_detail` reports WHY a
filename yielded nothing (:class:`RejectionReason`) so the learner can tally rejections instead of
silently discarding them, per the epic's "logged and counted, never dropped" precedence rule.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal


__all__ = [
    "QUALITY_TOKENS",
    "SOURCE_TOKENS",
    "STATUS_TOKENS",
    "RejectionReason",
    "ReleaseGroupExtraction",
    "extract_release_group",
    "extract_release_group_detail",
]


# --------------------------------------------------------------------------------------------
# Vocabulary -- tokens that describe the FILE, never the uploader
# --------------------------------------------------------------------------------------------

SOURCE_TOKENS: frozenset[str] = frozenset(
    {
        "aud",
        "audience",
        "bluray",
        "cable",
        "cd",
        "cda",
        "cdm",
        "cdr",
        "cds",
        "dab",
        "dat",
        "dsr",
        "dvb",
        "dvd",
        "dvdrip",
        "fm",
        "hdtv",
        "line",
        "md",
        "mtv",
        "pdtv",
        "sat",
        "satellite",
        "sbd",
        "soundboard",
        "stream",
        "tape",
        "tv",
        "vinyl",
        "vinylrip",
        "web",
        "webdl",
        "webrip",
    }
)
"""How the recording was captured. Named explicitly in this bead's acceptance criteria (``sbd``,
``cable``, ``sat``, ``stream``, ``line``, ``web``, ``fm``): the naive extractor returned these as
release groups whenever an uploader put the source tag last."""

QUALITY_TOKENS: frozenset[str] = frozenset(
    {
        "1080i",
        "1080p",
        "128",
        "16bit",
        "160",
        "192",
        "224",
        "24bit",
        "2160p",
        "256",
        "320",
        "44khz",
        "480p",
        "48khz",
        "4k",
        "720p",
        "96khz",
        "aac",
        "ac3",
        "alac",
        "aiff",
        "bdrip",
        "divx",
        "flac",
        "h264",
        "h265",
        "hd",
        "hevc",
        "hdrip",
        "lossless",
        "m4a",
        "mono",
        "mp3",
        "mp4",
        "ogg",
        "opus",
        "sd",
        "stereo",
        "uhd",
        "wav",
        "wma",
        "x264",
        "x265",
        "xvid",
    }
)
"""Codec / bitrate / resolution. Enumerated for the common literals, backed by
:data:`_UNIT_SHAPED_RE` for the open-ended numeric-with-unit family (``192kbps``, ``11025hz``)."""

STATUS_TOKENS: frozenset[str] = frozenset(
    {
        "advance",
        "bootleg",
        "complete",
        "dirfix",
        "dupe",
        "fix",
        "full",
        "incl",
        "int",
        "internal",
        "limited",
        "live",
        "logfix",
        "ltd",
        "nfo",
        "nfofix",
        "promo",
        "proper",
        "read",
        "remaster",
        "remastered",
        "repack",
        "rerip",
        "retail",
        "reup",
        "sample",
        "samplefix",
        "set",
        "sfv",
        "sfvfix",
        "subfix",
        "tracklist",
        "unofficial",
    }
)
"""Scene status / re-release markers. ``proper`` is the load-bearing one: the hsalive ``-proper-``
re-release shape puts it either side of the group tag, so it must be popped rather than claimed.
Note ``int`` -- the scene "internal" marker -- is here as a STANDALONE token only;
``_merge_internal_suffix`` keeps the underscore-glued ``cin_int`` / ``coin_int`` spelling intact."""

_AUDIO_VIDEO_EXTENSIONS = (
    "aac",
    "aiff",
    "alac",
    "avi",
    "flac",
    "m4a",
    "m4v",
    "mkv",
    "mov",
    "mp3",
    "mp4",
    "ogg",
    "opus",
    "wav",
    "webm",
    "wma",
)
_EXTENSION_RE = re.compile(rf"\.(?:{'|'.join(_AUDIO_VIDEO_EXTENSIONS)})$", re.IGNORECASE)
"""Known media extension, anchored to the end. Deliberately a known-extension list rather than a
generic ``\\.\\w+$``: a generic pattern amputates the day off a filename ending ``...2014.04.12``,
which is the same class of silent date destruction this whole module exists to prevent."""

_FIELD_SEPARATORS = r"\s._;,\-"
"""Characters that delimit scene tail fields. Semicolons and commas are in here because the corpus
contains messy names that use them as separators (or leave them behind as artifacts of one), and a
separator run may mix them freely (``"Artist; Event - ..."``, ``"..._-_..."``)."""

_TAIL_FIELD_RE = re.compile(rf"(?P<sep>[{_FIELD_SEPARATORS}]+)(?P<field>[^{_FIELD_SEPARATORS}]+)\s*$")
"""One tail field plus the separator run in front of it, anchored to the END of the string.

Tail-anchored and applied repeatedly, never as a whole-string split: matching only at the end means
this can never fragment a hyphenated date sitting earlier in the filename."""

_GROUP_TOKEN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_]*[a-z0-9])?$")
"""Plausible group shape: ASCII alphanumerics with interior underscores. Applied to the lowercased,
punctuation-trimmed token, so a leading/trailing underscore (a space-substitute artifact) and any
other punctuation are already gone by the time this runs."""

_UNIT_SHAPED_RE = re.compile(r"^\d+(?:kbps|kb|khz|hz|bit|fps|k|p|i)$")
"""Open-ended numeric-with-unit quality tokens, so :data:`QUALITY_TOKENS` need not enumerate every
bitrate and resolution that ever appears."""

_PART_MARKER_RE = re.compile(r"^(?:cd|dvd|disc|disk|d|pt|part|vol|side|tape)\d{1,3}$")
"""Multi-part markers (``cd2``, ``pt3``, ``side1``). Part of the file's identity, not the group's."""

_YEAR_RE = re.compile(r"^(?:19[5-9]\d|20[0-4]\d)$")
"""A bare 4-digit year. Used ONLY as positive evidence that the tail is a scene date field -- never
to decide that a token is *not* a group (rule 1 handles that, unbounded, via "must have a letter").
"""

_TRIM_CHARS = "_()[]{}'\"`!?*+~@#$%^&=|<>/\\"
"""Punctuation stripped from both ends of a candidate token before shape-checking. Ends only: an
INTERIOR stray character means the token is not a clean group tag and should be rejected, not
repaired."""

_MAX_TAIL_FIELDS = 8
"""How far back from the end to read fields. Generous enough for ``DATE-SOURCE-QUALITY-...-GROUP``
plus re-release markers, bounded so this never walks a long prose title looking for a group."""

_MAX_NOISE_POPPED = 5
"""How many recognized scene-noise fields may be popped from the tail before giving up. Bounds the
blast radius of the walk: without it, a title made entirely of vocabulary-adjacent words could be
chewed through until some real title word was claimed as a group."""


RejectionReason = Literal[
    "empty",
    "no_tail_field",
    "numeric",
    "scene_vocabulary",
    "unlikely_shape",
    "no_scene_evidence",
]
"""Why a filename yielded no group. ``"numeric"`` is the one the original measurement got wrong: the
trailing field is a bare year or a date component, and the naive regex returned it as a group."""


@dataclass(frozen=True, slots=True)
class ReleaseGroupExtraction:
    """The full result of one extraction attempt.

    ``group`` is the normalized (lowercased) key downstream storage should use; ``raw`` is the token
    exactly as it appeared in the filename, kept so a caller can show the operator what it read;
    ``reason`` is set if and only if ``group`` is ``None``.
    """

    group: str | None
    raw: str | None
    reason: RejectionReason | None


@dataclass(frozen=True, slots=True)
class _TailField:
    """One field read off the tail: its preceding separator run, its raw text, its clean token."""

    sep: str
    raw: str
    token: str


def extract_release_group(filename: str) -> str | None:
    """Return the scene release-group token in *filename*, lowercased, or ``None``.

    Pure and total: any string is accepted, nothing is raised, and no I/O is performed. Only the
    final path component is examined, so callers may pass a bare filename or a full path.

    ``None`` means "no release group could be claimed with confidence" -- including the cases the
    naive measurement got wrong, where the trailing token is a year (``...-04-05-2014.mp3``) or a
    source/quality tag (``...-sbd.mp3``). Use :func:`extract_release_group_detail` when the reason
    matters.
    """
    return extract_release_group_detail(filename).group


def extract_release_group_detail(filename: str) -> ReleaseGroupExtraction:
    """Same as :func:`extract_release_group`, plus the raw token and the rejection reason."""
    name = _basename(filename)
    if not name:
        return ReleaseGroupExtraction(group=None, raw=None, reason="empty")

    fields = _tail_fields(_EXTENSION_RE.sub("", name))
    if not fields:
        return ReleaseGroupExtraction(group=None, raw=None, reason="no_tail_field")

    index = len(fields) - 1
    popped = 0
    while index >= 0 and popped < _MAX_NOISE_POPPED and _is_scene_noise(fields[index].token):
        index -= 1
        popped += 1
    if index < 0:
        return ReleaseGroupExtraction(group=None, raw=None, reason="scene_vocabulary")

    candidate = fields[index]
    if _is_scene_noise(candidate.token):
        # The walk hit _MAX_NOISE_POPPED and stopped on more vocabulary: this tail is all file
        # description and no uploader, same verdict as running off the front of the field list.
        return ReleaseGroupExtraction(group=None, raw=candidate.raw, reason="scene_vocabulary")
    if candidate.token.isdigit():
        return ReleaseGroupExtraction(group=None, raw=candidate.raw, reason="numeric")
    if not _is_group_shaped(candidate.token):
        return ReleaseGroupExtraction(group=None, raw=candidate.raw, reason="unlikely_shape")
    if not _has_scene_evidence(fields, index, popped=popped):
        return ReleaseGroupExtraction(group=None, raw=candidate.raw, reason="no_scene_evidence")
    return ReleaseGroupExtraction(group=candidate.token, raw=candidate.raw, reason=None)


def _basename(filename: str) -> str:
    """Final path component, both separators honoured. String slicing only -- never touches disk."""
    return filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1].strip()


def _tail_fields(text: str) -> list[_TailField]:
    """Read up to :data:`_MAX_TAIL_FIELDS` fields off the END of *text*, in filename order."""
    fields: list[_TailField] = []
    remaining = text.rstrip()
    while len(fields) < _MAX_TAIL_FIELDS:
        match = _TAIL_FIELD_RE.search(remaining)
        if match is None:
            break
        raw = match.group("field")
        fields.append(_TailField(sep=match.group("sep"), raw=raw, token=raw.strip(_TRIM_CHARS).lower()))
        remaining = remaining[: match.start()]
    fields.reverse()
    return _merge_internal_suffix(fields)


def _merge_internal_suffix(fields: list[_TailField]) -> list[_TailField]:
    """Re-join an underscore-glued ``int`` / ``internal`` suffix onto the field before it.

    ``cin_int`` and ``coin_int`` are release-group keys in this corpus's own measurement, not a
    group plus a separate marker, so they must survive as single tokens. The underscore is a field
    separator everywhere else (plenty of scene filenames use it for spaces), so the two are
    reconciled here rather than by exempting underscores from :data:`_FIELD_SEPARATORS` -- which
    would leave a fully underscore-delimited filename with one enormous unparsable tail field.

    The glue must be underscores ONLY: a hyphen-delimited ``...-cin-int`` is the marker written as
    its own field, and the ``int`` there is popped as scene noise like any other status token.

    The field being glued ONTO must itself be group-shaped, which is what keeps this from
    reintroducing the very defect this module exists to fix: ``...-04-05-2014_int`` must not merge
    into a group-shaped ``2014_int`` and smuggle a year past the numeric check.
    """
    merged: list[_TailField] = []
    for field in fields:
        if merged and _glues_internal_suffix(merged[-1], field):
            previous = merged.pop()
            token = f"{previous.token}_{field.token}"
            merged.append(_TailField(sep=previous.sep, raw=f"{previous.raw}{field.sep}{field.raw}", token=token))
            continue
        merged.append(field)
    return merged


def _glues_internal_suffix(previous: _TailField, field: _TailField) -> bool:
    """True when *field* is an underscore-glued ``int``/``internal`` suffix of a group-shaped *previous*."""
    if field.token not in {"int", "internal"} or set(field.sep) != {"_"}:
        return False
    return _is_group_shaped(previous.token) and not _is_scene_noise(previous.token)


def _is_scene_noise(token: str) -> bool:
    """True when *token* describes the file rather than its uploader, so the walk may pop it."""
    if not token:
        return False
    return (
        token in SOURCE_TOKENS
        or token in QUALITY_TOKENS
        or token in STATUS_TOKENS
        or _UNIT_SHAPED_RE.match(token) is not None
        or _PART_MARKER_RE.match(token) is not None
    )


def _is_group_shaped(token: str) -> bool:
    """True when *token* has a plausible release-group SHAPE (module doc rules 1 and 3).

    Shape only -- vocabulary (rule 2) is the walk's job, and is already settled by the time this
    runs. "Contains at least one letter" is the unbounded form of rule 1: it rejects every bare
    year, and every other purely numeric field, without a year blacklist that would need an end.
    """
    if not 2 <= len(token) <= 20:
        return False
    if _GROUP_TOKEN_RE.match(token) is None:
        return False
    return any(character.isalpha() for character in token)


def _has_scene_evidence(fields: list[_TailField], index: int, *, popped: int) -> bool:
    """True when something other than the candidate's own shape says the tail is a scene tail.

    Three independent forms of corroboration, any one of which suffices:

    * a **glued separator** in front of the candidate -- this corpus writes its scene tail fields
      with hyphens/underscores/dots and no surrounding whitespace (``...-sbd-talion``), while prose
      titles separate their last word with a space (``"Live @ EDC"``);
    * **scene noise popped from behind it** -- a trailing ``-proper`` marker only appears in a scene
      tail;
    * a **recognized field immediately before it** -- a source/quality/status token or a 4-digit
      year, which is what makes the spaced-out messy shape ``"... - 03-04-2014 - sbd - talion.mp3"``
      readable despite its whitespace separators.
    """
    if _is_glued_separator(fields[index].sep):
        return True
    if popped > 0:
        return True
    if index == 0:
        return False
    previous = fields[index - 1].token
    return _is_scene_noise(previous) or _YEAR_RE.match(previous) is not None


def _is_glued_separator(separator: str) -> bool:
    """True for a separator run containing no whitespace -- the scene tail's own delimiter shape."""
    return len(separator) > 0 and not any(character.isspace() for character in separator)
