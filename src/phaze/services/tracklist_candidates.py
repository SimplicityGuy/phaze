"""Candidate-set builder for the 1001Tracklists drain: classify, dedup, and identify unique sets.

WHY THIS MODULE IS THE HIGH-LEVERAGE ONE (phaze-fq9h.3)
-------------------------------------------------------
The drain cannot be made faster. robots.txt asks for an 8s crawl-delay against the whole
1001Tracklists HOST, so the entire system -- every worker, every process -- shares ~10,800
requests/day, i.e. ~4,300 lookups at the measured ~2.5 requests per lookup. Against ~250,000 files
(~60% of them set-like, measured 6,911/11,428) a naive pass is 35+ days of continuous crawling and
CANNOT be shortened by hardware. The only lever is to look up FEWER things. This module is that
lever: every duplicate it collapses, every track it declines to classify as a set, and every file
it recognizes as already-tracklisted is a request never spent.

WHAT WAS LOST WITH FINGERPRINTING, STATED PLAINLY
--------------------------------------------------
This bead originally deduped via phaze-vprd's cross-set audio similarity graph. Audio
fingerprinting was removed from the product entirely (epic phaze-0jpe, docs/design/
0002-fingerprint-removal.md) and phaze-vprd is closed. Fingerprinting would have recognized two
encodings of one set FROM THE AUDIO, immune to whatever a scene release did to the filename. The
replacement here is text and duration heuristics, and it is worse in both directions:

* It MISSES duplicates whose filenames disagree (a mis-scrubbed release, a hand-renamed copy).
  Cost: a wasted lookup, and a slower drain.
* It can MERGE two genuinely different sets that share an artist, an event and a length -- two
  nights of the same residency being the obvious case. Cost: the wrong tracklist propagated.

Those two failure modes are traded against each other explicitly rather than hidden: grouping is
tuned for RECALL (missing a duplicate costs the scarce resource), while each link carries a
:class:`~phaze.enums.tracklist_candidate.DuplicateConfidence` so the drain can gate PROPAGATION --
the operation that a false merge actually corrupts -- at whatever tier it wants. Collapsing is
cheap to be wrong about; propagating is not, so only propagation is gated.

THE QUERY SEAM
--------------
Real query derivation from a messy scene-tagged filename is phaze-fq9h.2's job. This module never
imports it: :class:`CandidateSignals` carries an optional ``derived_query`` the caller supplies,
and falls back to :func:`normalize_query` over the filename when it is absent. ``derived_query``
is itself passed through :func:`normalize_query` before comparison, so the two sources of query
text are compared on identical terms and an upstream change in derivation cannot silently split a
cluster on whitespace or case.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import re
from typing import TYPE_CHECKING, Any
import unicodedata

from phaze.enums.tracklist_candidate import CandidateClass, DuplicateConfidence, meets_confidence


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    import uuid


# --------------------------------------------------------------------------------------------
# Tunables. Every value here trades drain budget against false merges; the comments say which.
# --------------------------------------------------------------------------------------------

SET_DURATION_MIN_SECONDS: float = 1200.0
"""20 minutes. At or above this a file is a set regardless of what its filename claims.

The corpus median for set-like files is 58.9 minutes, so this sits far below the mass of real
sets while staying above essentially every single track. Radio-edit-length "sets" do not exist;
20-minute *tracks* (prog, ambient, live jams) do, which is why the filename still gets a vote in
the ambiguous band rather than duration alone deciding at a single hard threshold."""

TRACK_DURATION_MAX_SECONDS: float = 900.0
"""15 minutes. At or below this a file is an individual track even if its filename says "live @".

This is the deliberate asymmetry: a 6-minute file named ``Artist - Live @ Event 2024.04.12.mp3``
is one track lifted OUT of that set, and looking it up spends a request to attach a 2-hour
tracklist to 6 minutes of audio."""

DURATION_ABS_TOLERANCE_SECONDS: float = 45.0
"""Absolute half-width of the duration band two files must share to be called the same set.

Re-encodes of one recording differ by encoder padding and trim, which is seconds. 45s is loose
enough to absorb a lame/ffmpeg round-trip plus a trimmed intro, tight enough that two different
sets in a festival lineup rarely collide on it alone -- and they must ALSO share a query."""

DURATION_REL_TOLERANCE: float = 0.02
"""Relative half-width (2%), used when it exceeds the absolute one -- ~72s on a 60-minute set.

Long recordings accumulate proportionally larger differences (a 3-hour set trimmed of its intro
loses minutes, not seconds), so a flat tolerance would systematically miss duplicates exactly
where sets are longest."""

KEY_DURATION_BUCKET_SECONDS: float = 300.0
"""5-minute bucket width used in the cache KEY (not in grouping).

Grouping uses the tolerance band above; the key needs a discrete value. A cluster whose median
duration sits on a bucket boundary can shift buckets between runs if its membership changes,
which costs ONE duplicate lookup. It can never produce a wrong cache HIT for THAT cluster, because
a shifted key simply misses. That asymmetry is why a coarse bucket is acceptable here.

Being coarser than :data:`DURATION_ABS_TOLERANCE_SECONDS` does mean two DIFFERENT clusters that
grouping legitimately left unmerged (durations >45s apart) can floor-divide into the same bucket
and collide on this key WITHIN one run (phaze-3t8kc) -- a distinct hazard from the cross-run
shift above, and one this constant cannot avoid by itself. :func:`group_unique_sets` closes it
structurally via :func:`_disambiguate_key_collisions`, which re-keys every collision it finds
before any :class:`UniqueSet` reaches the cache or the drain."""


# --------------------------------------------------------------------------------------------
# Query normalization
# --------------------------------------------------------------------------------------------

_SCENE_TOKENS: frozenset[str] = frozenset(
    {
        # Encoding / quality
        "128",
        "160",
        "192",
        "224",
        "256",
        "320",
        "kbps",
        "kbs",
        "cbr",
        "vbr",
        "abr",
        "16bit",
        "24bit",
        "44khz",
        "48khz",
        "96khz",
        # Containers / codecs
        "mp3",
        "m4a",
        "aac",
        "flac",
        "ogg",
        "opus",
        "wav",
        "wma",
        "aiff",
        "mp4",
        "mkv",
        "avi",
        "webm",
        "mov",
        "x264",
        "x265",
        "h264",
        "h265",
        "hevc",
        "xvid",
        "divx",
        # Video quality
        "480p",
        "720p",
        "1080p",
        "1080i",
        "2160p",
        "4k",
        "uhd",
        "hd",
        "sd",
        "hdtv",
        "pdtv",
        "webrip",
        "bdrip",
        "dvdrip",
        # Source
        "web",
        "dab",
        "sat",
        "sbd",
        "aud",
        "cd",
        "cdr",
        "cdm",
        "cds",
        "vinyl",
        "tape",
        "line",
        "fm",
        "stream",
        "rip",
        # Scene status words
        "proper",
        "repack",
        "rerip",
        "dirfix",
        "nfofix",
        "logfix",
        "int",
        "retail",
        "promo",
        "bootleg",
        "unofficial",
        "read",
        "nfo",
        "sfv",
        "incl",
        "tracklist",
        "tracklisted",
        "complete",
        "full",
    }
)
"""Tokens that describe the FILE rather than the SET, dropped before comparing queries.

This is the whole point of normalization: ``Artist-Event-2024-04-12-WEB-320-GROUP`` and
``Artist - Event 2024.04.12 [FLAC]`` are one set and must produce one query. Membership is
conservative -- a token is listed only when it cannot plausibly be part of an artist or event
name, because dropping a real name token merges two different sets, while keeping a scene token
merely misses a duplicate."""

_PART_RE = re.compile(r"(?:\b|_)(?:part|pt|cd|disc|disk)[\s._-]?(\d{1,2})\b|\b(\d{1,2})\s?of\s?\d{1,2}\b", re.IGNORECASE)
"""Multi-part markers. Parts of ONE recording are one set for lookup purposes, so they are pulled
out of the query text and re-joined by :func:`group_unique_sets` -- their durations legitimately
differ by a lot, so the duration band alone would split them and buy two lookups for one set."""

_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]|\(([^()]*)\)")
_EXTENSION_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,4}$")
"""File extension. The first character MUST be a letter: a digits-allowed extension pattern
amputates the day off a trailing ``...2024.04.12``, which is the same silent date-destruction
failure ``_SCENE_GROUP_SUFFIX_RE`` guards against."""
_SCENE_GROUP_SUFFIX_RE = re.compile(r"-([A-Za-z][A-Za-z0-9_]{1,11})$")
"""Trailing scene-group tag (``...-CREW``). MUST start with a letter.

A digits-allowed first character made this eat the YEAR off ``artist-event-04-05-2024`` -- the
single worst thing normalization can do, because it silently deletes the strongest disambiguator
there is and turns two different nights into one query. Scene group tags also never contain a
space, so ``Artist - Event - Live`` (spaced hyphens) cannot match."""
_DOMAIN_RE = re.compile(r"\b(?:www\.)?[a-z0-9-]+\.(?:com|net|org|info|ru|co\.uk)\b", re.IGNORECASE)
_LEADING_INDEX_RE = re.compile(r"^\d{1,2}\s*[.\-_)]\s+")
_ISO_DATE_RE = re.compile(r"\b((?:19|20)\d{2})[-._]?(\d{2})[-._]?(\d{2})\b")
_DMY_DATE_RE = re.compile(r"\b(\d{2})[-._](\d{2})[-._]((?:19|20)\d{2})\b")
_DATE_TOKEN_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NON_WORD_RE = re.compile(r"[^a-z0-9\x00]+")
_WS_RE = re.compile(r"\s+")

_LIVE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\blive\s*[@at]\b|\blive\s+at\b|\blive\s+@", re.IGNORECASE),
    re.compile(r"\bb2b\b", re.IGNORECASE),
    re.compile(r"\bdj\s?-?\s?set\b", re.IGNORECASE),
    re.compile(r"\bset\b", re.IGNORECASE),
    re.compile(r"\bessential\s+mix\b", re.IGNORECASE),
    re.compile(r"\bmainstage\b|\bmain\s+stage\b", re.IGNORECASE),
    re.compile(r"\bfestival\b", re.IGNORECASE),
    re.compile(r"\bcontinuous\s+mix\b|\bdj\s+mix\b", re.IGNORECASE),
    re.compile(r"\bradio\s+show\b|\bpodcast\b|\bepisode\b|\bep\.?\s?\d{2,4}\b", re.IGNORECASE),
)
"""Filename evidence FOR a set. Deliberately generic -- no festival, venue or artist names.

Hardcoding real event names would both rot and risk committing identifiers lifted from the
operator's actual archive, which CLAUDE.md forbids in tracked files."""

_TRACK_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d{1,2}\s*[.\-_)]\s+"),
    re.compile(r"\btrack\s?\d{1,2}\b", re.IGNORECASE),
    re.compile(r"\b(?:original|extended|radio|club|dub|vocal)\s+(?:mix|edit|version)\b", re.IGNORECASE),
    re.compile(r"\bremix\b|\bbootleg\s+remix\b|\bvip\s+mix\b", re.IGNORECASE),
    re.compile(r"\bfeat\.?\b|\bft\.?\b", re.IGNORECASE),
)
"""Filename evidence AGAINST a set: track indices, mix/edit suffixes, featured-artist credits."""

_CONNECTOR_TOKENS: frozenset[str] = frozenset({"at", "the"})
"""Connectors dropped so ``Live @ Event`` and ``Live at Event`` produce one query.

``@`` vanishes in the punctuation collapse while ``at`` survives, which would otherwise split
those two spellings of the SAME set into two lookups -- and they are the two commonest spellings
in the corpus. Kept to two words: every additional stopword is a chance to merge two artists whose
names differ only by it."""

_TIMESTAMP_LINE_RE = re.compile(r"(?:^|\n)[^\n]{0,40}?\b(\d{1,2}:\d{2}(?::\d{2})?)\b")
_EMBEDDED_TRACKLIST_KEYS: frozenset[str] = frozenset(
    {"comment", "comments", "description", "desc", "lyrics", "unsyncedlyrics", "tracklist", "chapters", "chapter", "podcastdesc", "purl"}
)
_EMBEDDED_TRACKLIST_MIN_TIMESTAMPS: int = 3
"""Three timestamped lines in a text tag. Two can be an ordinary "recorded 21:30" note; three in
one field is a tracklist somebody already wrote down, and re-scraping it spends a request to
learn what the file already carries."""


def _strip_diacritics(text: str) -> str:
    """Fold accented characters to ASCII so ``Móvil`` and ``Movil`` compare equal.

    Scene releases routinely transliterate; the mojibake-repaired filename
    (``files.original_filename_repaired``) fixes encoding damage but not transliteration.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _canonicalize_dates(text: str) -> str:
    """Rewrite recognizable date forms to a single ``YYYY-MM-DD`` token.

    ``YYYY``-first forms are unambiguous and are normalized fully. The ``NN-NN-YYYY`` form is NOT:
    ``04-05-2024`` is April 5th to an American release and May 4th to a European one, and nothing
    in the filename says which. Rather than guess -- a guess would invent a date and could merge
    two different nights -- the two components are kept in their ORIGINAL order. Two files written
    in the same convention still normalize identically (they merge); files written in different
    conventions do not (a missed duplicate, costing one request). That is the conservative side of
    the trade, chosen deliberately.
    """

    def _iso(match: re.Match[str]) -> str:
        year, month, day = match.group(1), match.group(2), match.group(3)
        if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
            return match.group(0)
        return f" {year}-{month}-{day} "

    def _dmy(match: re.Match[str]) -> str:
        first, second, year = match.group(1), match.group(2), match.group(3)
        if not (1 <= int(first) <= 31 and 1 <= int(second) <= 31):
            return match.group(0)
        return f" {year}-{first}-{second} "

    return _DMY_DATE_RE.sub(_dmy, _ISO_DATE_RE.sub(_iso, text))


def _strip_noise_brackets(text: str) -> str:
    """Drop bracketed/parenthesized chunks whose contents are ENTIRELY scene noise.

    ``[FLAC-320-GROUP]`` goes; ``(Live @ Some Club)`` stays. The all-noise test matters -- brackets
    carry the event name often enough that blanket removal would shred real queries.
    """

    def _replace(match: re.Match[str]) -> str:
        inner = match.group(1) if match.group(1) is not None else match.group(2) or ""
        if _ISO_DATE_RE.search(inner) or _DMY_DATE_RE.search(inner):
            return match.group(0)  # a parenthesized DATE is the opposite of noise
        tokens = [t for t in _NON_WORD_RE.split(inner.lower()) if t]
        # A bare 4-digit run is a year, not a bitrate: keep it. Every other digit run inside an
        # otherwise-noisy chunk is a quality/encoding number.
        if tokens and all(t in _SCENE_TOKENS or (t.isdigit() and len(t) != 4) for t in tokens):
            return " "
        return match.group(0)

    return _BRACKET_RE.sub(_replace, text)


def extract_part_index(filename: str) -> int | None:
    """Return the 1-based part number encoded in ``filename``, or None.

    Recognizes ``part 2`` / ``pt2`` / ``cd2`` / ``disc 2`` / ``2 of 3``. Only the FIRST marker is
    used; a name carrying two different markers is ambiguous and the first is as good a guess as
    any (it only affects grouping, never the query text, which strips all of them).
    """
    match = _PART_RE.search(filename)
    if match is None:
        return None
    raw = match.group(1) or match.group(2)
    return int(raw) if raw is not None else None


def normalize_query(raw: str | None) -> str:
    """Reduce a filename (or an upstream-derived query) to a comparable set identity string.

    Applied to BOTH sources of query text so an upstream derivation change cannot split a cluster
    on punctuation. Returns "" when nothing survives -- callers must treat that as "no query
    signal", never as a group everything-with-no-query can join.
    """
    if not raw:
        return ""
    text = _EXTENSION_RE.sub("", raw.strip())
    text = _SCENE_GROUP_SUFFIX_RE.sub(" ", text)
    text = _strip_noise_brackets(text)
    text = _DOMAIN_RE.sub(" ", text)
    text = _LEADING_INDEX_RE.sub("", text)
    text = _PART_RE.sub(" ", text)
    # Underscores are word characters to ``re``, so a scene name's ``_`` separators suppress the
    # ``\b`` anchors in the date patterns -- ``artist_event_20240412`` would never be recognized
    # as dated. Convert them to spaces BEFORE date canonicalization (but after the part/group
    # patterns above, which are written against the raw scene form).
    text = text.replace("_", " ")
    text = _canonicalize_dates(text)
    text = _strip_diacritics(text).lower()
    # Protect the canonical date token's hyphens from the punctuation collapse below. NUL is the
    # sentinel because it cannot occur in a filename; ``_NON_WORD_RE`` exempts it explicitly.
    text = _DATE_TOKEN_RE.sub(lambda m: m.group(0).replace("-", "\x00"), text)
    text = _NON_WORD_RE.sub(" ", text)
    text = text.replace("\x00", "-")
    tokens = [t for t in _WS_RE.split(text) if t and t not in _SCENE_TOKENS and t not in _CONNECTOR_TOKENS]
    return " ".join(tokens)


def query_has_date(normalized_query: str) -> bool:
    """True when a normalized query carries a canonical ``YYYY-MM-DD`` token.

    The single strongest disambiguator available without audio: two sets by one artist at one
    event on one date, of near-identical length, is a far rarer coincidence than the same three
    minus the date (which is just a residency).
    """
    return _DATE_TOKEN_RE.search(normalized_query) is not None


def detect_embedded_tracklist(raw_tags: Mapping[str, Any] | None) -> bool:
    """True when the file's own tags already contain a tracklist.

    Looks for at least :data:`_EMBEDDED_TRACKLIST_MIN_TIMESTAMPS` timestamps inside a single
    descriptive tag. Cheap, offline, and it removes the file from the queue entirely -- the best
    possible outcome per the budget.
    """
    if not raw_tags:
        return False
    for key, value in raw_tags.items():
        if key.lower().replace(" ", "").replace("_", "") not in _EMBEDDED_TRACKLIST_KEYS:
            continue
        for text in _iter_text_values(value):
            if len(_TIMESTAMP_LINE_RE.findall(text)) >= _EMBEDDED_TRACKLIST_MIN_TIMESTAMPS:
                return True
    return False


def _iter_text_values(value: Any) -> Iterable[str]:
    """Yield the string leaves of a tag value (mutagen hands back scalars AND lists)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                yield item


# --------------------------------------------------------------------------------------------
# Inputs and outputs
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateSignals:
    """Everything the builder needs about one file, with no ORM or session attached.

    Deliberately a plain value object: classification and grouping are the parts most likely to be
    tuned against real corpus measurements, and they must be runnable over a fixture list without
    a database. ``phaze.services.tracklist_candidate_queue`` is the only place that knows SQL.
    """

    file_id: uuid.UUID
    filename: str
    """Best-known-clean filename -- ``COALESCE(original_filename_repaired, original_filename)``."""
    sha256_hash: str = ""
    original_path: str = ""
    file_type: str = ""
    file_size: int = 0
    duration_seconds: float | None = None
    bitrate: int | None = None
    track_number: int | None = None
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    derived_query: str | None = None
    """phaze-fq9h.2's output when available; falls back to ``filename``."""
    has_scraped_tracklist: bool = False
    has_cue_companion: bool = False
    has_embedded_tracklist: bool = False

    @property
    def already_tracklisted(self) -> bool:
        """True when this file's tracks are already known from any source -- skip it."""
        return self.has_scraped_tracklist or self.has_cue_companion or self.has_embedded_tracklist

    @property
    def tracklist_source(self) -> str | None:
        """Which existing source makes this file a skip, for the operator-facing counts."""
        if self.has_scraped_tracklist:
            return "scraped"
        if self.has_cue_companion:
            return "cue"
        if self.has_embedded_tracklist:
            return "embedded"
        return None


@dataclass(frozen=True, slots=True)
class Classification:
    """A classification plus the evidence for it, so a wrong call can be diagnosed."""

    candidate_class: CandidateClass
    confidence: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DuplicateMember:
    """One file in a unique set, with the strength of its link to the canonical file."""

    file_id: uuid.UUID
    confidence: DuplicateConfidence
    reason: str


@dataclass(frozen=True, slots=True)
class UniqueSet:
    """One thing to look up, and every file the answer should propagate to."""

    key: str
    """Stable cache identity: sha256 of ``query|duration-bucket``. See :data:`KEY_DURATION_BUCKET_SECONDS`."""
    query_text: str
    canonical_file_id: uuid.UUID
    duration_seconds: float | None
    classification: Classification
    members: tuple[DuplicateMember, ...]
    """Includes the canonical file itself (always :attr:`DuplicateConfidence.EXACT` -- it is itself)."""

    @property
    def file_count(self) -> int:
        """Number of files this one lookup would serve."""
        return len(self.members)

    @property
    def requests_saved(self) -> int:
        """Lookups avoided by collapsing this cluster -- ``file_count - 1``."""
        return len(self.members) - 1

    def members_at_least(self, minimum: DuplicateConfidence) -> tuple[DuplicateMember, ...]:
        """Members whose link is at least ``minimum`` -- the drain's propagation gate."""
        return tuple(m for m in self.members if meets_confidence(m.confidence, minimum))


# --------------------------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------------------------


def classify(signals: CandidateSignals) -> Classification:
    """Decide whether ``signals`` describes a live set worth a 1001TL lookup.

    Duration is trusted first where it is decisive, because it is the one signal a scene release
    cannot corrupt: it comes from the audio itself via the metadata stage. Filenames only get a
    vote when duration is missing or lands in the ambiguous 15-20 minute band.

    ``UNKNOWN`` is a real answer, not a rounding of ``TRACK``. A file with no duration and no
    filename signal is genuinely undecidable here, and quietly guessing "set" would spend requests
    on the least-informative files in the corpus while guessing "track" would hide them from the
    operator entirely.
    """
    reasons: list[str] = []
    duration = signals.duration_seconds

    if duration is not None and duration >= SET_DURATION_MIN_SECONDS:
        reasons.append(f"duration {duration / 60:.1f}min >= {SET_DURATION_MIN_SECONDS / 60:.0f}min")
        return Classification(CandidateClass.LIVE_SET, 0.95, tuple(reasons))

    if duration is not None and duration <= TRACK_DURATION_MAX_SECONDS:
        reasons.append(f"duration {duration / 60:.1f}min <= {TRACK_DURATION_MAX_SECONDS / 60:.0f}min")
        if signals.track_number is not None:
            reasons.append(f"track number {signals.track_number}")
        return Classification(CandidateClass.TRACK, 0.95, tuple(reasons))

    score, filename_reasons = _filename_score(signals)
    reasons.extend(filename_reasons)

    if duration is not None:
        # The 15-20 minute band: long for a track, short for a set. Nudge toward "set" and let the
        # filename decide, since a long track that also reads like a set is the rarer animal.
        score += 1
        reasons.append(f"duration {duration / 60:.1f}min in ambiguous band")

    if score > 0:
        return Classification(CandidateClass.LIVE_SET, min(0.5 + 0.15 * score, 0.9), tuple(reasons))
    if score < 0:
        return Classification(CandidateClass.TRACK, min(0.5 + 0.15 * -score, 0.9), tuple(reasons))
    if not reasons:
        reasons.append("no duration and no filename signal")
    return Classification(CandidateClass.UNKNOWN, 0.0, tuple(reasons))


def _filename_score(signals: CandidateSignals) -> tuple[int, list[str]]:
    """Net filename evidence: positive means set-like, negative means track-like."""
    score = 0
    reasons: list[str] = []
    name = signals.filename

    for pattern in _LIVE_MARKERS:
        match = pattern.search(name)
        if match is not None:
            score += 1
            reasons.append(f"set marker {match.group(0).strip().lower()!r}")

    for pattern in _TRACK_MARKERS:
        match = pattern.search(name)
        if match is not None:
            score -= 1
            reasons.append(f"track marker {match.group(0).strip().lower()!r}")

    if signals.track_number is not None:
        score -= 1
        reasons.append(f"track number {signals.track_number}")

    return score, reasons


# --------------------------------------------------------------------------------------------
# Dedup to unique sets
# --------------------------------------------------------------------------------------------


class _UnionFind:
    """Minimal union-find over list indices, with path compression and union by size.

    Grouping is transitive by nature -- A matches B by content hash while B matches C by query --
    and expressing that with pairwise dict-of-sets bookkeeping is where this kind of code usually
    grows its bugs. Union-find makes the transitive closure the data structure's problem.
    """

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._size = [1] * size

    def find(self, node: int) -> int:
        """Return the representative of ``node``'s cluster."""
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[node] != root:
            self._parent[node], node = root, self._parent[node]
        return root

    def union(self, left: int, right: int) -> None:
        """Merge the clusters containing ``left`` and ``right``."""
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self._size[left_root] < self._size[right_root]:
            left_root, right_root = right_root, left_root
        self._parent[right_root] = left_root
        self._size[left_root] += self._size[right_root]


@dataclass(slots=True)
class _Prepared:
    """Per-file derived values, computed once before the O(n) grouping passes."""

    signals: CandidateSignals
    query: str
    part_index: int | None
    classification: Classification = field(default_factory=lambda: Classification(CandidateClass.UNKNOWN, 0.0, ()))


def duration_tolerance(duration_seconds: float) -> float:
    """Half-width of the duration band around ``duration_seconds``.

    ``max(absolute, relative)`` -- see the two tunables for why neither alone suffices.
    """
    return max(DURATION_ABS_TOLERANCE_SECONDS, abs(duration_seconds) * DURATION_REL_TOLERANCE)


def durations_match(left: float | None, right: float | None) -> bool:
    """True when two durations are close enough to be the same recording.

    ``None`` never matches: an unknown duration is not evidence of agreement, and treating it as
    one would let every duration-less file in a query bucket fuse into a single set.
    """
    if left is None or right is None:
        return False
    return abs(left - right) <= max(duration_tolerance(left), duration_tolerance(right))


def set_key(query_text: str, duration_seconds: float | None) -> str:
    """Deterministic cache identity for a unique set.

    Hashed rather than stored raw so the column is fixed-width and index-friendly, and so a query
    containing archive-derived text never lands in a log line or an error message that quotes the
    key. The plain ``query_text`` is stored alongside it in the cache row for operator display.
    """
    bucket = "na" if duration_seconds is None else str(int(duration_seconds // KEY_DURATION_BUCKET_SECONDS))
    return hashlib.sha256(f"{query_text}\x00{bucket}".encode()).hexdigest()


def group_unique_sets(candidates: Sequence[CandidateSignals]) -> list[UniqueSet]:
    """Collapse ``candidates`` into unique sets, each carrying the files it stands for.

    Three linking passes, strongest first, all fed into one union-find so their transitive closure
    is automatic:

    1. **Content hash.** Equal sha256 is identity, full stop. Applied ACROSS query buckets, so a
       hand-renamed copy still collapses onto its original.
    2. **Query + duration band.** Within a normalized-query bucket, files are clustered around a
       representative (not chained pairwise) so a run of slightly-differing durations cannot walk
       a cluster arbitrarily far from where it started.
    3. **Multi-part.** Within a query bucket whose part markers are all DISTINCT, the parts are one
       recording. Distinctness is the guard: repeated indices mean two parted sets share a query,
       and merging those would be exactly the false merge this module is trying not to make.

    Duration-less files join their query bucket only when it holds exactly one cluster; with two
    or more there is no signal saying which, and picking one would be a coin flip on the operator's
    behalf.

    Ordering of the returned list is by descending file count then key, so the caller sees the
    highest-leverage lookups first and the order is stable across runs.
    """
    prepared = [
        _Prepared(
            signals=signals,
            query=normalize_query(signals.derived_query) or normalize_query(signals.filename),
            part_index=extract_part_index(signals.filename),
        )
        for signals in candidates
    ]
    for item in prepared:
        item.classification = classify(item.signals)

    union = _UnionFind(len(prepared))
    _link_by_content_hash(prepared, union)
    _link_within_query_buckets(prepared, union)

    clusters: dict[int, list[int]] = {}
    for index in range(len(prepared)):
        clusters.setdefault(union.find(index), []).append(index)

    unique_sets = [_build_unique_set([prepared[i] for i in members]) for members in clusters.values()]
    unique_sets = _disambiguate_key_collisions(unique_sets)
    unique_sets.sort(key=lambda s: (-s.file_count, s.key))
    return unique_sets


def _link_by_content_hash(prepared: Sequence[_Prepared], union: _UnionFind) -> None:
    """Union every pair sharing a non-empty sha256."""
    by_hash: dict[str, int] = {}
    for index, item in enumerate(prepared):
        digest = item.signals.sha256_hash
        if not digest:
            continue
        first = by_hash.setdefault(digest, index)
        if first != index:
            union.union(first, index)


def _link_within_query_buckets(prepared: Sequence[_Prepared], union: _UnionFind) -> None:
    """Union files that share a normalized query, using duration bands and part markers."""
    buckets: dict[str, list[int]] = {}
    for index, item in enumerate(prepared):
        if item.query:
            buckets.setdefault(item.query, []).append(index)

    for indices in buckets.values():
        _link_parts(prepared, union, indices)
        representatives = _link_by_duration(prepared, union, indices)
        _attach_duration_less(union, indices, prepared, representatives)


def _link_parts(prepared: Sequence[_Prepared], union: _UnionFind, indices: Sequence[int]) -> None:
    """Union the parts of one multi-part recording (only when every part index is distinct)."""
    parted = [i for i in indices if prepared[i].part_index is not None]
    if len(parted) < 2:
        return
    seen: set[int | None] = set()
    for index in parted:
        part = prepared[index].part_index
        if part in seen:
            return  # repeated index => two parted sets share this query; refuse to merge
        seen.add(part)
    for index in parted[1:]:
        union.union(parted[0], index)


def _link_by_duration(prepared: Sequence[_Prepared], union: _UnionFind, indices: Sequence[int]) -> list[int]:
    """Cluster a query bucket by duration around representatives; return the representatives."""
    with_duration = sorted(
        (i for i in indices if prepared[i].signals.duration_seconds is not None),
        key=lambda i: (prepared[i].signals.duration_seconds or 0.0, str(prepared[i].signals.file_id)),
    )
    representatives: list[int] = []
    for index in with_duration:
        duration = prepared[index].signals.duration_seconds
        for rep in representatives:
            if durations_match(duration, prepared[rep].signals.duration_seconds):
                union.union(rep, index)
                break
        else:
            representatives.append(index)
    return representatives


def _attach_duration_less(union: _UnionFind, indices: Sequence[int], prepared: Sequence[_Prepared], representatives: Sequence[int]) -> None:
    """Attach duration-less files to their query bucket's cluster -- only if there is just one."""
    duration_less = [i for i in indices if prepared[i].signals.duration_seconds is None]
    if not duration_less:
        return
    distinct_clusters = {union.find(rep) for rep in representatives}
    if len(distinct_clusters) > 1:
        return  # ambiguous: two real sets share this query, so we cannot place these files
    anchor = representatives[0] if representatives else duration_less[0]
    for index in duration_less:
        union.union(anchor, index)


def _canonical_index(members: Sequence[_Prepared]) -> int:
    """Pick the cluster's canonical file: best quality first, ties broken deterministically.

    Highest bitrate, then most complete tags, then shortest path -- the same ranking
    ``services.dedup.score_group`` already uses for sha256 duplicate groups, so the operator sees
    one notion of "the good copy" across both surfaces. ``file_id`` is the final tiebreak so the
    choice does not depend on input ordering.
    """

    def rank(pair: tuple[int, _Prepared]) -> tuple[int, int, int, str]:
        _, item = pair
        signals = item.signals
        tag_count = sum(1 for value in (signals.artist, signals.title, signals.album) if value)
        return (-(signals.bitrate or 0), -tag_count, len(signals.original_path), str(signals.file_id))

    return min(enumerate(members), key=rank)[0]


def _build_unique_set(members: Sequence[_Prepared]) -> UniqueSet:
    """Assemble a :class:`UniqueSet` from one cluster: canonical, key, and per-member confidence."""
    canonical_pos = _canonical_index(members)
    canonical = members[canonical_pos]
    query_text = _cluster_query(members)
    duration = _cluster_duration(members)
    scored = tuple(
        DuplicateMember(file_id=item.signals.file_id, confidence=confidence, reason=reason)
        for item, (confidence, reason) in ((item, _member_confidence(canonical, item, query_text)) for item in members)
    )
    return UniqueSet(
        key=set_key(query_text, duration),
        query_text=query_text,
        canonical_file_id=canonical.signals.file_id,
        duration_seconds=duration,
        classification=canonical.classification,
        members=scored,
    )


def _disambiguate_key_collisions(unique_sets: Sequence[UniqueSet]) -> list[UniqueSet]:
    """Re-key any UniqueSets that collided on ``set_key`` despite being genuinely distinct clusters.

    phaze-3t8kc: :data:`KEY_DURATION_BUCKET_SECONDS` (300s) is coarser than
    :data:`DURATION_ABS_TOLERANCE_SECONDS` (45s), so grouping can legitimately leave two clusters
    in the same query bucket UNMERGED (their durations are more than 45s apart, so
    :func:`durations_match` is False) whose cluster durations nonetheless floor-divide into the
    SAME 300s bucket -- producing two :class:`UniqueSet` objects with a byte-identical ``key``.
    The persisted cache and the drain's per-candidate re-check both key solely on that value, so a
    collision here silently suppresses the second set's lookup: whichever set the drain reaches
    first writes the cache row, and the second's pre-spend re-check sees a HIT and is skipped
    forever, indistinguishable from "already answered".

    Grouping by key isolates collisions without touching the (overwhelming) common case: a group
    of exactly one keeps its key unchanged, so cache identity and warmth are unaffected for sets
    that never collided. A colliding group is ordered deterministically by canonical file id (so
    the same corpus state always assigns the same salted keys) and every member after the first
    gets its ordinal position folded into a re-hashed key -- guaranteeing every UniqueSet this
    function returns has a key none of its siblings share.
    """
    by_key: dict[str, list[UniqueSet]] = {}
    for unique_set in unique_sets:
        by_key.setdefault(unique_set.key, []).append(unique_set)

    result: list[UniqueSet] = []
    for key, group in by_key.items():
        if len(group) == 1:
            result.append(group[0])
            continue
        ordered = sorted(group, key=lambda s: str(s.canonical_file_id))
        for ordinal, unique_set in enumerate(ordered):
            if ordinal == 0:
                result.append(unique_set)
                continue
            salted_key = hashlib.sha256(f"{key}\x00collision\x00{ordinal}".encode()).hexdigest()
            result.append(replace(unique_set, key=salted_key))
    return result


def _cluster_query(members: Sequence[_Prepared]) -> str:
    """The cluster's query: the most common non-empty one, ties to the lexicographically first.

    Chosen independently of the canonical file so the cache key stays stable when a new, better
    copy of the same set is scanned later -- otherwise a fresh higher-bitrate rip could rotate the
    key and buy a second lookup for a set already answered.
    """
    counts: dict[str, int] = {}
    for item in members:
        if item.query:
            counts[item.query] = counts.get(item.query, 0) + 1
    if not counts:
        return ""
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def _cluster_duration(members: Sequence[_Prepared]) -> float | None:
    """The cluster's representative duration: the median of the known ones, or None.

    Median rather than mean so one mis-tagged outlier cannot drag the cache key into another
    bucket. Parts of a multi-part recording deliberately have wildly different durations; the
    median is still a stable, reproducible choice for them.
    """
    durations = sorted(m.signals.duration_seconds for m in members if m.signals.duration_seconds is not None)
    if not durations:
        return None
    return durations[len(durations) // 2]


def _member_confidence(canonical: _Prepared, member: _Prepared, cluster_query: str) -> tuple[DuplicateConfidence, str]:
    """Grade one member's link to the canonical file, recomputed PAIRWISE.

    Deliberately not inherited from whichever pass happened to union the member: a file pulled in
    transitively (A-hash-B, B-query-C) gets the confidence its OWN relationship to the canonical
    justifies, which is the weaker and more honest answer.
    """
    if member.signals.file_id == canonical.signals.file_id:
        return DuplicateConfidence.EXACT, "canonical file"

    digest = member.signals.sha256_hash
    if digest and digest == canonical.signals.sha256_hash:
        return DuplicateConfidence.EXACT, "identical sha256"

    same_query = bool(member.query) and member.query == canonical.query
    if same_query and durations_match(member.signals.duration_seconds, canonical.signals.duration_seconds):
        if query_has_date(cluster_query):
            return DuplicateConfidence.HIGH, "same dated query, duration within tolerance"
        return DuplicateConfidence.MEDIUM, "same query (no date), duration within tolerance"

    if same_query and member.part_index is not None and canonical.part_index is not None:
        return DuplicateConfidence.MEDIUM, f"part {member.part_index} of the same multi-part query"

    if same_query:
        return DuplicateConfidence.LOW, "same query, duration unavailable or outside tolerance"

    return DuplicateConfidence.LOW, "linked transitively; no direct hash or query match"


def collapse_ratio(file_count: int, unique_set_count: int) -> float:
    """Files per unique set -- the multiplier by which dedup shortens the drain.

    ``1.0`` means dedup bought nothing. Returns ``0.0`` for an empty corpus rather than raising,
    because this feeds a report that must render for a fresh install.
    """
    if unique_set_count <= 0:
        return 0.0
    return file_count / unique_set_count
