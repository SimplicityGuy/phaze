"""Companion content features -- the pure, ON-DISK half, run by the agent that owns the files (phaze-osy6j).

The linking chain and the junk review both need what is INSIDE a companion (``.cue`` / ``.nfo`` /
``.txt`` / ``.m3u`` / ``.m3u8`` / ``.pls``), and only the agent can read it: the controller is
fileless (DIST-01). So the agent extracts a small, fixed set of features at ingest and reports them
to ``POST /api/internal/agent/companion-features``; everything control-side reads the stored row and
never the filesystem.

Every rule here is the one ``docs/spikes/phaze-lm73u-companion-content-survey.md`` measured over all
45,371 readable companions in the archive, cited by section:

- **Encoding (§4.5).** BOMs, then a UTF-16-LE NUL pattern, all-NUL, strict UTF-8, then CP437 when at
  least 20 high bytes are present and at least half of them fall in 0xB0-0xDF (the box-drawing and
  block range), else Windows-1252. **There is no Windows-1251 detection**: all 5 of the survey's
  Windows-1251 detections were CP437 art whose block bytes decode to Cyrillic capitals, and it found
  no true one. CP437 and Windows-1252 cannot be told apart from bytes on a file with few high bytes,
  so **no decision below depends on which of the two was picked**: on an 8-bit file every decision
  reads :func:`_decision_text`, in which every byte >= 0x80 is a space -- the two code pages agree on
  every byte below 0x80, so the decision text is byte-for-byte the same under either.
- **References (§4.2).** CUE ``FILE`` values, M3U/M3U8 entries and PLS ``FileN=`` values that end in
  a media extension, and in NFO/TXT any token ending in one. Each is reduced to its basename (both
  separators), NFC-normalized, and stored with its source. Resolving a reference -- exact, then
  case-insensitive, then the stem-with-another-extension rule, and the NFO/TXT token that merely
  ENDS with a media filename (``Files.: <name>.mp3``) -- is the linking chain's job, not this one's.
- **Tracklist (§4.3).** A CUE with at least 2 ``TRACK`` and 2 ``TITLE`` lines; NFO/TXT with at least 3
  consecutive track-like lines or at least 5 timestamped/numbered lines anywhere (sampled precision
  30/30 TXT, 23/28 NFO). A playlist is never a tracklist here: the survey's 3 sampled M3U "tracklists"
  were all ``.mp2`` file lists (0/3).
- **Per-file junk (§4, classifier rules 1 and 3).** ``empty`` (zero bytes, or fewer than 20
  alphanumerics), ``all_nul`` (non-zero size, every byte 0x00 -- 368 files, mostly an interrupted
  copy), ``site_ad`` (a URL domain, at most 40 non-blank lines, an ad word, and no tracklist, media
  reference or release block). Sampled precision of every junk label: 52/52.
- **Bounded work by construction.** The files are not ours, so nothing here may cost more than linear
  time in its input: at most :data:`MAX_FEATURE_BYTES` are read, every line is cut to
  :data:`MAX_LINE_CHARS` before any pattern sees it, no pattern can backtrack across a line (the
  ``Artist - Title`` test, the URL test and the filename-token scan are explicit linear scans, not
  backtracking regexes), and reference extraction stops once it holds one more than
  :data:`MAX_REFERENCES`. ``test_an_adversarial_megabyte_line_is_extracted_in_bounded_time`` pins it.
- **Known stamps are NOT decided here.** A stamp is a CONTENT seen in at least three folders beside
  different media, which no single file can know about itself; the control plane decides it over the
  stored fingerprints (``services/companion_content.py``). A stamp with real text appended has
  different bytes, so it is never in the stamp's group.

Stdlib + ``phaze.constants`` only (the agent worker and the watcher import this; neither may import
``phaze.database`` / ``sqlalchemy`` -- ``tests/shared/core/test_task_split.py``).
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
from typing import Final, Literal
import unicodedata

from phaze.constants import EXTENSION_MAP, FileCategory


EXTRACTOR_VERSION: Final = 1
"""Bump when a rule below changes, so ``phaze backfill companion-features`` re-extracts older rows."""

MAX_FEATURE_BYTES: Final = 1_048_576
"""Bytes read per companion. The archive's largest companion is 40,940 bytes (survey §2), so every real
companion is read whole; the cap bounds a mis-named multi-hundred-MB file."""

MAX_REFERENCES: Final = 200
"""References kept per companion. Extraction stops at one more, so ``reference_count`` is exact up to
the cap and ``MAX_REFERENCES + 1`` means "more than the cap"."""
MAX_LINE_CHARS: Final = 1024
"""Characters of each line any pattern sees. Real track, field and playlist lines are far shorter; the
bound is what keeps a 1 MiB file with no newline from turning a per-line test into a whole-file one."""
MAX_REFERENCE_LENGTH: Final = 255
MAX_FOLDER_MEDIA: Final = 256
"""Media names kept from the companion's own folder. A flat dump folder can hold 18,535 media files
(measured 2026-10-06, ``services/companion.py``); the full count is kept beside the capped list."""

Encoding = Literal["ascii", "utf-8", "utf-8-sig", "utf-16", "utf-16-le", "cp437", "cp1252", "all-nul"]
ReferenceSource = Literal["cue_file", "m3u", "pls", "text_token"]
ContentJunkClass = Literal["empty", "all_nul", "site_ad"]

MEDIA_EXTENSIONS: Final[frozenset[str]] = frozenset(
    ext for ext, category in EXTENSION_MAP.items() if category in (FileCategory.MUSIC, FileCategory.VIDEO)
)
_PLAYLIST_SUFFIXES: Final = frozenset({".m3u", ".m3u8", ".pls"})
_EIGHT_BIT: Final[frozenset[Encoding]] = frozenset({"cp437", "cp1252"})

_MEDIA_EXT_ALT = "|".join(sorted(ext.lstrip(".") for ext in MEDIA_EXTENSIONS))
_MEDIA_EXT_HIT = re.compile(r"\.(?:" + _MEDIA_EXT_ALT + r")\b", re.IGNORECASE)
_TOKEN_STOP: Final = frozenset("\\/\"'<>|*?\t\r\n")
_TOKEN_MAX_PREFIX: Final = 240
_ENDS_IN_MEDIA = re.compile(r"\.(?:" + _MEDIA_EXT_ALT + r")$", re.IGNORECASE)
# A URL is a scheme or ``www.`` prefix followed by a domain run holding a ``.<2+ letters>`` part after
# its first character. The run is matched possessively, so a line of repeated prefixes is one pass.
_URL_RUN = re.compile(r"(?:https?://|www\.)([a-z0-9][a-z0-9.\-]*+)", re.IGNORECASE)
_TLD = re.compile(r"\.[a-z]{2}", re.IGNORECASE)
_URL_ENTRY = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
# The survey's timestamp and numbered-line patterns, with their adjacent whitespace runs made
# possessive: ``[.)\-:\s]+\s*`` and ``\s*[.)\-:]?\s+`` match the same lines but backtrack
# quadratically over a long run of spaces.
_TS_LINE = re.compile(r"^\s*+(?:\d{1,3}[.)\-:\s]++)?[\[(]?\d{1,3}:\d{2}(?::\d{2})?[\])]?")
_NUM_LINE = re.compile(r"^\s*+(?:\d{1,3}|[A-Z]\d{1,2})(?:\s*+[.)\-:]\s++|\s++)\S")
# ``Artist - Title``: a spaced dash with a word character somewhere before it and somewhere after it.
# Overlapping separator positions via a lookahead; the word-character test is two index lookups.
_DASH_SEPARATOR = re.compile(r"(?=\s[-\u2013\u2014]\s)")
_WORD_CHAR = re.compile(r"\w")
_ARTIST_TITLE_MAX = 200
_FIELD = re.compile(r"^([A-Za-z][A-Za-z ./]{1,24}?)\s*[.:\[]{1,}\s*\S")
_WORDS = re.compile(r"[^\W\d_]{2,}")
_LEADING_ART = re.compile(r"^[^\w\[(]+")
_AD_WORD = re.compile(
    r"download|visit|forum|torrent|join us|upload|free &|irc|support us|donate|sponsor|subscribe|ripped by|encoded by|greetz|greets",
    re.IGNORECASE,
)
_BOX_DRAWING = re.compile("[─-▟]")
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
_CUE_KEYWORD = re.compile(r"^\s*(FILE|TRACK|TITLE)\b(.*)$", re.IGNORECASE)
_CUE_QUOTED = re.compile(r'^"(.*)"')
_PLS_FILE = re.compile(r"^\s*File\d+\s*=(.*)$", re.IGNORECASE)

# Labelled release fields (survey §4, classifier rule 6), compared after dropping non-letters. The
# first five (location, recordingquality, setlength, settype, venue) are not in the survey's list: its
# site-ad rule flagged 8 release notes in that layout (``Artist: | Date: | Location: | Set Type: |
# Set Length: | Recording Quality:``) as adverts, and the operator's check of the phaze-9aker labels
# corrected one of them to "a real release note ... for the one recording beside it". With them, all
# 8 leave site_ad and the other 47 of the survey's 55 site-ads are unchanged (replayed over the
# survey's own extracted fields).
_RELEASE_FIELDS: Final = frozenset(
    {
        "location",
        "recordingquality",
        "setlength",
        "settype",
        "venue",
        "airdate",
        "album",
        "artist",
        "bitrate",
        "cat",
        "catalog",
        "date",
        "encoder",
        "genre",
        "label",
        "length",
        "nroftracks",
        "playtime",
        "quality",
        "rel",
        "reldate",
        "releasedate",
        "ripdate",
        "ripper",
        "rlsdate",
        "runtime",
        "size",
        "source",
        "storedate",
        "streetdate",
        "time",
        "title",
        "totallength",
        "totalplaytime",
        "tracks",
        "type",
        "year",
    }
)

_MIN_ALNUM: Final = 20
_SITE_AD_MAX_LINES: Final = 40
_TRACK_RUN: Final = 3
_TRACK_LINES_ANYWHERE: Final = 5
_CUE_MIN_TRACKS: Final = 2
_RELEASE_MIN_FIELDS: Final = 3
_CP437_MIN_BOX_BYTES: Final = 20


@dataclass(frozen=True)
class MediaReference:
    """One media filename a companion names, as a normalized basename plus where it came from."""

    name: str
    source: ReferenceSource


@dataclass(frozen=True)
class ContentFeatures:
    """What one companion's bytes say, independent of where the file sits."""

    encoding: Encoding
    references: tuple[MediaReference, ...]
    reference_count: int
    is_tracklist: bool
    junk_class: ContentJunkClass | None


@dataclass(frozen=True)
class CompanionReading:
    """A companion read off disk: its content features plus its fingerprint, size and folder's media."""

    fingerprint: str
    byte_size: int
    truncated: bool
    features: ContentFeatures
    folder_media: tuple[str, ...] = field(default=())
    folder_media_count: int = 0


def detect_encoding(raw: bytes) -> Encoding:
    """Name the encoding of ``raw`` in the survey's measured order (§4.5); never Windows-1251."""
    if not raw:
        return "ascii"
    if raw.count(0) == len(raw):
        return "all-nul"
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    if len(raw) >= 4 and raw[1::2].count(0) > len(raw) // 4:
        return "utf-16-le"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    else:
        return "ascii" if raw.isascii() else "utf-8"
    high = [byte for byte in raw if byte >= 0x80]
    box = sum(1 for byte in high if 0xB0 <= byte <= 0xDF)
    if box >= _CP437_MIN_BOX_BYTES and box * 2 >= len(high):
        return "cp437"
    return "cp1252"


def _decision_text(text: str, encoding: Encoding) -> str:
    """The text every DECISION reads: identical for CP437 and Windows-1252 by construction.

    One character in, one character out, so its lines align with ``text``'s. On an 8-bit file every
    non-ASCII character becomes a space -- both code pages map bytes below 0x80 identically, so this is
    a function of the bytes alone. Otherwise only box-drawing characters become spaces (an NFO whose
    art was already converted to Unicode, survey §4.5).
    """
    if encoding in _EIGHT_BIT:
        return _NON_ASCII.sub(" ", text)
    return _BOX_DRAWING.sub(" ", text)


def _basename(value: str) -> str:
    """The NFC basename of a reference, across both separators, without quotes or edge whitespace."""
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip().strip("\"'").strip()
    return unicodedata.normalize("NFC", name)[:MAX_REFERENCE_LENGTH]


def _playlist_entry(value: str) -> str | None:
    """A playlist entry's basename when it names a local media file, else None (URLs, comments, blanks)."""
    entry = value.strip()
    if not entry or entry.startswith("#") or _URL_ENTRY.match(entry):
        return None
    name = _basename(entry)
    return name if _ENDS_IN_MEDIA.search(name) else None


def _cue_file_value(rest: str) -> str:
    """A CUE ``FILE`` value: the quoted name, or everything before the trailing type keyword."""
    value = rest.strip()
    quoted = _CUE_QUOTED.match(value)
    return quoted.group(1) if quoted else value.rsplit(" ", 1)[0]


def _has_url(text: str) -> bool:
    """Whether ``text`` carries a URL domain, in one linear pass (see ``_URL_RUN``)."""
    return any(_TLD.search(match.group(1), 1) for match in _URL_RUN.finditer(text))


def _text_tokens(line: str) -> list[str]:
    """Every token in ``line`` that ends in a media filename: up to 240 allowed characters before ``.<ext>``.

    The survey's lazy 1-240 character token regex without its backtracking: each extension hit
    walks back over allowed characters, never past the previous token's end, so the line is visited a
    bounded number of times. The token keeps the words before the filename (``Files.: <name>.mp3``);
    resolving the filename at its end is the linking chain's job.
    """
    tokens: list[str] = []
    floor = 0
    for hit in _MEDIA_EXT_HIT.finditer(line):
        begin = start = hit.start()
        limit = max(floor, start - _TOKEN_MAX_PREFIX)
        while begin > limit and line[begin - 1] not in _TOKEN_STOP:
            begin -= 1
        if begin < start:
            tokens.append(line[begin : hit.end()])
            floor = hit.end()
    return tokens


def _line_references(line: str, suffix: str) -> list[MediaReference]:
    """The references one line names, by the companion's type."""
    if suffix == ".cue":
        match = _CUE_KEYWORD.match(line)
        if match and match.group(1).upper() == "FILE" and (cue_name := _basename(_cue_file_value(match.group(2)))):
            return [MediaReference(cue_name, "cue_file")]
        return []
    if suffix in (".m3u", ".m3u8"):
        entry = _playlist_entry(line)
        return [MediaReference(entry, "m3u")] if entry else []
    if suffix == ".pls":
        pls_match = _PLS_FILE.match(line)
        entry = _playlist_entry(pls_match.group(1)) if pls_match else None
        return [MediaReference(entry, "pls")] if entry else []
    return [MediaReference(name, "text_token") for token in _text_tokens(line) if (name := _basename(token))]


def _references(lines: list[str], suffix: str) -> list[MediaReference]:
    """Distinct media references in order of appearance, stopping at one more than :data:`MAX_REFERENCES`."""
    found: dict[MediaReference, None] = {}
    for line in lines:
        for reference in _line_references(line, suffix):
            found[reference] = None
            if len(found) > MAX_REFERENCES:
                return list(found)
    return list(found)


def _is_artist_title(line: str) -> bool:
    """A spaced dash with a word character before it and another after it (the survey's ``\\w.*\\s-\\s.*\\w``)."""
    first = _WORD_CHAR.search(line)
    if first is None:
        return False
    last = max((match.start() for match in _WORD_CHAR.finditer(line, first.start())), default=first.start())
    return any(first.start() < sep.start() and sep.start() + 3 <= last for sep in _DASH_SEPARATOR.finditer(line))


def _line_kind(line: str) -> str | None:
    """Classify one art-stripped line as timestamped, numbered or ``Artist - Title`` (survey §4, rule 4)."""
    if _FIELD.match(line) and not _TS_LINE.match(line):
        return None
    if _TS_LINE.match(line) and _WORDS.search(line):
        return "ts"
    if _NUM_LINE.match(line) and len(_WORDS.findall(line)) >= 2:
        return "num"
    if len(line) < _ARTIST_TITLE_MAX and _is_artist_title(line) and not _has_url(line):
        return "at"
    return None


def _longest_run(kinds: list[str | None]) -> int:
    """Length of the longest consecutive run of track-like lines."""
    best = current = 0
    for kind in kinds:
        current = current + 1 if kind else 0
        best = max(best, current)
    return best


def _is_track_like(stripped: list[str], cue_counts: dict[str, int], suffix: str) -> bool:
    """Survey §4 rule 4, for every type (the site-ad rule reads it even where the tracklist flag does not)."""
    if suffix == ".cue":
        return cue_counts["TRACK"] >= _CUE_MIN_TRACKS and cue_counts["TITLE"] >= _CUE_MIN_TRACKS
    if _longest_run([_line_kind(line) for line in stripped]) >= _TRACK_RUN:
        return True
    numbered = sum(1 for line in stripped if _NUM_LINE.match(line)) + sum(1 for line in stripped if _TS_LINE.match(line))
    return numbered >= _TRACK_LINES_ANYWHERE


def _has_release_block(stripped: list[str]) -> bool:
    """At least three labelled release fields (``Artist ....: <artist>``, ``Genre``, ``Source`` ...)."""
    names = {re.sub(r"[^a-z]", "", match.group(1).lower()) for line in stripped if (match := _FIELD.match(line))}
    return len(names & _RELEASE_FIELDS) >= _RELEASE_MIN_FIELDS


def extract_content_features(raw: bytes, suffix: str) -> ContentFeatures:
    """Extract the content features of one companion from its bytes and its (lowercased) extension."""
    suffix = suffix.lower()
    encoding = detect_encoding(raw)
    if encoding == "all-nul":
        return ContentFeatures(encoding=encoding, references=(), reference_count=0, is_tracklist=False, junk_class="all_nul")

    text = raw.decode(encoding, errors="replace")
    decision = _decision_text(text, encoding)
    # Every line is cut before any pattern sees it; see the module docstring's "Bounded work".
    lines = [line[:MAX_LINE_CHARS] for line in text.splitlines()]
    decision_lines = [line[:MAX_LINE_CHARS] for line in decision.splitlines()]
    nonblank = [line for line in decision_lines if line.strip()]
    stripped = [_LEADING_ART.sub("", line).rstrip() for line in nonblank]

    references = _references(lines, suffix)
    cue_counts = {"TRACK": 0, "TITLE": 0}
    for line in decision_lines:
        if (match := _CUE_KEYWORD.match(line)) and (keyword := match.group(1).upper()) in cue_counts:
            cue_counts[keyword] += 1
    track_like = _is_track_like(stripped, cue_counts, suffix)

    junk_class: ContentJunkClass | None = None
    if not raw or sum(1 for char in decision if char.isalnum()) < _MIN_ALNUM:
        junk_class = "empty"
    elif (
        len(nonblank) <= _SITE_AD_MAX_LINES
        and any(_has_url(line) for line in nonblank)
        and _AD_WORD.search(decision)
        and not (track_like or references or _has_release_block(stripped))
    ):
        junk_class = "site_ad"

    return ContentFeatures(
        encoding=encoding,
        references=tuple(references[:MAX_REFERENCES]),
        reference_count=len(references),
        is_tracklist=track_like and suffix not in _PLAYLIST_SUFFIXES,
        junk_class=junk_class,
    )


def folder_media(directory: str) -> tuple[tuple[str, ...], int]:
    """The sorted NFC names of the media files directly in ``directory`` (capped), and how many there are.

    The control plane's known-stamp rule needs to know whether identical companions sit beside the
    SAME media or different media; the agent is the only side that can list the folder. A folder that
    cannot be listed reads as holding no media, which can only make a stamp verdict less likely.
    """
    try:
        with os.scandir(directory) as entries:
            names = sorted(
                unicodedata.normalize("NFC", entry.name)
                for entry in entries
                if PurePosixPath(entry.name).suffix.lower() in MEDIA_EXTENSIONS and entry.is_file(follow_symlinks=False)
            )
    except OSError:
        return (), 0
    return tuple(names[:MAX_FOLDER_MEDIA]), len(names)


def read_companion(path: str, *, follow_symlinks: bool = True) -> CompanionReading:
    """Read one companion at ``path`` (an on-disk handle) and extract everything the agent reports.

    Reads at most :data:`MAX_FEATURE_BYTES`; a larger file is fingerprinted by a streamed hash of the
    whole file, so the fingerprint is always the hash of the complete content (the same value as the
    ``files.sha256_hash`` an unchanged file was ingested with). Raises ``OSError`` when unreadable.
    ``follow_symlinks=False`` opens with ``O_NOFOLLOW``: a caller that has just containment-checked a
    RESOLVED path passes it, so a symlink swapped in after the check is refused (``ELOOP``) rather
    than followed out of the scan roots. Synchronous, blocking I/O: callers use ``asyncio.to_thread``.
    """
    handle = Path(path)
    flags = os.O_RDONLY | (0 if follow_symlinks else os.O_NOFOLLOW)
    with os.fdopen(os.open(handle, flags), "rb") as stream:
        raw = stream.read(MAX_FEATURE_BYTES + 1)
        truncated = len(raw) > MAX_FEATURE_BYTES
        digest = hashlib.sha256(raw)
        byte_size = len(raw)
        if truncated:
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
                byte_size += len(block)
            raw = raw[:MAX_FEATURE_BYTES]
    names, count = folder_media(str(handle.parent))
    return CompanionReading(
        fingerprint=digest.hexdigest(),
        byte_size=byte_size,
        truncated=truncated,
        features=extract_content_features(raw, handle.suffix),
        folder_media=names,
        folder_media_count=count,
    )
