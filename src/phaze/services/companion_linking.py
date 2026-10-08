"""The companion linking chain: pure decisions over stored content features and one agent's media (phaze-rmhfr).

Every rule, threshold and tie rule here is the one ``docs/spikes/phaze-9aker-companion-matching-accuracy.md``
measured on 224 operator-checked labels (§2 method table, §4.3-§4.4, Recommendation 1), cited per
constant. The order is fixed, and the FIRST step that returns anything wins:

1. **Veto** (:func:`phaze.services.companion_content.never_link_reason`): a junk companion (empty,
   all-NUL, known stamp, site ad), or a byte-identical copy of an already linked companion sitting
   in a folder with no media (operator decision 1, 2026-10-07, "Junk review (Recommended)"; epic
   ``phaze-4x319``). Both go to the junk review, never to a recording.
2. **Content reference** (spike (c), including (b) and (b')): each stored reference resolved against
   the companion's own folder -- exact, case-insensitive, the NFO/TXT token that merely ENDS with a
   media filename, then the same stem with another extension -- and, when its own folder resolves
   none, against every media row on the agent: a unique basename links; a shared one links only when
   exactly one holder sits in a folder of the same release name.
3. **Own-folder stem** (spike (s)): own-folder media whose :func:`phaze.constants.companion_match_key`
   equals the companion's own stem key.
4. **Own-folder rule** (spike (a), the shipped rule WITHOUT its parent-folder fallback): every media
   file in the companion's own folder.
5. **Release-folder twin** (spike (e)): a companion whose folder holds no media, and exactly one other
   folder on the agent with the same release-folder name -> all of that folder's media.
6. **Close name** (spike (dw)): whole-agent close-name match at :data:`CLOSE_NAME_THRESHOLD` with the
   date guard, unique best by :data:`CLOSE_NAME_MARGIN`.

**The parent-folder name fallback is retired** (operator decision 8, 2026-10-07, "Yes, file it
(Recommended)"; epic ``phaze-4x319``). ``phaze-ehryj`` linked a companion with no media of its own to
name-matched media in its PARENT folder; the spike measured that rule (and the close-name variant (d)
over the parent pool) at link precision 0.125-0.345 with 7 of 8 links into an 18,537-file dump folder
wrong (§4.4, §4.5). The scan half went with ``phaze-gafl9``; this module has no parent-folder step.

**Collection folders** (operator decision 2, 2026-10-07, "Require a reference (Recommended)"; epic
``phaze-4x319``): a TRACKLIST in a folder of many episodes links only by a content reference or an
own-folder stem match -- never by steps 4-6. A folder is a collection when at least
:data:`COLLECTION_MIN_FILES` of its media files carry a full date and no date is common to all of them. That is the implementer's reading of "a folder of
many episodes", not the operator's wording: the spike's one wrong chain link was a tracklist in a
month folder of four dated episodes (§4.3), while every correct multi-file link in the labels was
one release's parts, which share a date or carry none. See :func:`is_collection_folder`.

Bounded work by construction: the names scored here are file and folder names, cut to
:data:`MAX_NAME_CHARS` before any pattern or :class:`difflib.SequenceMatcher` sees them, and no
pattern can backtrack across more than one name. The close-name step reads a token index whose
postings are capped by :data:`CLOSE_NAME_MAX_POSTINGS` and re-scores at most
:data:`CLOSE_NAME_SHORTLIST` candidates per companion.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import PurePosixPath
import re
from typing import TYPE_CHECKING, Final, Literal, NamedTuple
import unicodedata

from phaze.constants import EXTENSION_MAP, FileCategory, companion_match_key
from phaze.services.companion_content import folder_name_key, media_folder, never_link_reason


if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Sequence
    import uuid


LinkStep = Literal["junk", "duplicate", "reference", "stem", "folder", "twin", "close_name", "unlinked"]
"""Which step decided a companion. ``junk`` / ``duplicate`` / ``unlinked`` link nothing."""

CLOSE_NAME_THRESHOLD: Final = 0.9
"""Spike §4.4: the date-guarded whole-agent curve is flat at 18/18 correct on the labeled set from 0.8
to 1.0, and 23/23 on the population sample from 0.9 (23/25 at 0.8-0.85)."""

CLOSE_NAME_MARGIN: Final = 0.05
"""Spike §2, methods (d) and (dw): the best candidate must beat any rival in ANOTHER folder by this."""

CLOSE_NAME_SHORTLIST: Final = 30
"""Spike §2, method (dw): the best 30 candidates by token Jaccard are re-scored."""

CLOSE_NAME_MAX_POSTINGS: Final = 2_000
"""Spike §2, method (dw): a token held by this many media names or more selects no candidates."""

TWIN_MIN_KEY: Final = 12
"""Spike §2, method (e): a release-folder name needs at least 12 alphanumerics to have a twin."""

TEXT_TOKEN_MIN_NAME: Final = 5
"""Survey §4.2 via spike §2 (b): the shortest media filename an NFO/TXT token may merely end with."""

COLLECTION_MIN_FILES: Final = 2
"""Dated media files, sharing no full date, that make a folder a collection (operator decision 2)."""

MAX_NAME_CHARS: Final = 255
"""Characters of any file or folder name the close-name scoring reads (a POSIX name is at most 255 bytes)."""

MEDIA_EXTENSIONS: Final[frozenset[str]] = frozenset(
    ext for ext, category in EXTENSION_MAP.items() if category in (FileCategory.MUSIC, FileCategory.VIDEO)
)


@dataclass(frozen=True)
class LinkInput:
    """One companion as the chain sees it: where it is, and its stored content features."""

    path: str
    references: tuple[tuple[str, str], ...]
    """``(name, source)`` per stored reference, ``source`` as ``companion_content_features.media_references``."""
    junk_class: str | None
    is_tracklist: bool
    identical_copy_linked: bool = False


@dataclass(frozen=True)
class LinkDecision:
    """The step that decided a companion, and the media it links to (empty unless it links)."""

    step: LinkStep
    media_ids: tuple[uuid.UUID, ...] = ()


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _stem(name: str) -> str:
    return PurePosixPath(name).stem


# --- names: the spike's tag-stripping, tokens, similarity and dates (§2, method (d)) -----------------

_TAG = re.compile(
    r"\b(?:web|webrip|sat|dvbs|dvb|cable|fm|line|sbd|aud|stream|radio|live|mp3|320|256|192|128|kbps|cbr|vbr|flac|hq|lq|"
    r"proper|repack|rerip|internal|retail|promo|bootleg|cd\d?|cdm|cdr|vinyl|"
    r"tt|tracklist|tracklisting|playlist|copy|final|complete)\b",
    re.IGNORECASE,
)
_GROUP_SUFFIX = re.compile(r"-[A-Za-z0-9]{2,12}$")
_COPY_MARKER = re.compile(r"\s*\(\d{1,2}\)$|\s*-\s*copy(?:\s*\(\d+\))?$|\s+copy$", re.IGNORECASE)
_SCENE_INDEX = re.compile(r"^\d{1,3}[\s_.\-]+")
_BRACKETS = re.compile(r"[\[\](){}]")
_NON_WORD = re.compile(r"\W+")
_FULL_DATE = re.compile(r"(?<!\d)(\d{4})[-_. ](\d{1,2})[-_. ](\d{1,2})(?!\d)|(?<!\d)(\d{1,2})[-_. ](\d{1,2})[-_. ](\d{4})(?!\d)")


def name_tokens(name: str) -> list[str]:
    """Tag-stripped tokens of one name: copy marker, scene index, ``-GROUP`` tail and source/format tags dropped.

    Spike §2, method (d). A ``-GROUP`` tail is only dropped from a scene-style name (``_`` or ``.``
    separated, no spaces), where it is the release group rather than part of the title.
    """
    text = _COPY_MARKER.sub("", _nfc(name[:MAX_NAME_CHARS]))
    text = _SCENE_INDEX.sub("", text)
    if ("_" in text or "." in text) and " " not in text:
        text = _GROUP_SUFFIX.sub("", text)
    text = _BRACKETS.sub(" ", text).casefold().replace("_", " ").replace(".", " ").replace("-", " ")
    return [token for token in _NON_WORD.split(_TAG.sub(" ", text)) if token]


def name_similarity(first: Sequence[str], second: Sequence[str]) -> float:
    """``max(token Jaccard, SequenceMatcher ratio)`` of two token lists (spike §2, method (d))."""
    if not first or not second:
        return 0.0
    first_set, second_set = set(first), set(second)
    jaccard = len(first_set & second_set) / len(first_set | second_set)
    return max(jaccard, SequenceMatcher(None, " ".join(first), " ".join(second)).ratio())


class FullDate(NamedTuple):
    """One full date: the year from its 4-digit group, and its other two numbers as an unordered pair.

    Day/month order is free (spike §2, (dw)), so ``2012-03-04`` and ``04.03.2012`` are one date and
    so are ``03.04.2012``; the year is never inferred from the numbers' size, because a day of 19 or
    more read as ``day*100+month`` is larger than any year floor (phaze-4x319.5).
    """

    year: int
    day_month: frozenset[int]


def full_dates(name: str) -> set[FullDate]:
    """Every full date (``YYYY-MM-DD`` or ``DD-MM-YYYY``, any of ``-_. `` between) in one name."""
    found: set[FullDate] = set()
    for match in _FULL_DATE.finditer(name[:MAX_NAME_CHARS]):
        if match.group(1):
            year, first, second = match.group(1), match.group(2), match.group(3)
        else:
            year, first, second = match.group(6), match.group(4), match.group(5)
        found.add(FullDate(int(year), frozenset({int(first), int(second)})))
    return found


def dates_agree(companion: Collection[FullDate], media: Collection[FullDate]) -> bool:
    """The date guard: a companion naming a full date only matches media naming the same one (spike §2, (dw))."""
    return not companion or not set(companion).isdisjoint(media)


def is_collection_folder(media_names: Iterable[str]) -> bool:
    """Whether a folder holds several dated recordings: two or more dated media files sharing no full date.

    The collection test behind operator decision 2 (2026-10-07, epic phaze-4x319); the reading of "a
    folder of many episodes" as this test is the implementer's (module docstring).

    A month folder of weekly episodes names a different date on each file; one release's parts
    (CD1/CD2, a festival stage's sets) share their date or carry none. A single file naming two
    dates (a broadcast date and the event's) is one recording, so dates are compared per FILE: the
    folder is a collection once no date is common to every dated file. Day/month order is free, as
    in the date guard.
    """
    common: set[FullDate] | None = None
    dated = 0
    for name in media_names:
        dates = full_dates(_stem(name))
        if not dates:
            continue
        dated += 1
        common = dates if common is None else common & dates
        if dated >= COLLECTION_MIN_FILES and not common:
            return True
    return False


# --- the agent's media, indexed once per association run ---------------------------------------------


class AgentMediaIndex:
    """One agent's media rows, indexed by folder, basename and release-folder name.

    Built from ``(media id, original_path)`` pairs (``services/companion.py`` reads them in keyset
    pages). Media are held by ordinal, names and folders once each, and a basename shared by no other
    file costs one ``int`` -- so the index is a few tens of MiB for the archive's ~104,000 media rows
    on one agent rather than a dict of UUID lists. The close-name token index is built on first use
    only, because most companions are decided before step 6.
    """

    def __init__(self, media: Iterable[tuple[uuid.UUID, str]] = ()) -> None:
        self._ids: list[uuid.UUID] = []
        self._names: list[str] = []
        self._folder_of = array("i")
        self._folders: list[str] = []
        self._members: list[list[int]] = []
        self._folder_ordinals: dict[str, int] = {}
        self._by_name = _Postings()
        self._by_folded_name = _Postings()
        self._folders_by_key = _Postings()
        self._close: _CloseNames | None = None
        for media_id, path in media:
            self.add(media_id, path)

    def add(self, media_id: uuid.UUID, path: str) -> None:
        """Index one media row. Adding after the close-name index was built is refused."""
        if self._close is not None:
            msg = "the close-name index is already built"
            raise RuntimeError(msg)
        folder, name = media_folder(path), _nfc(PurePosixPath(path).name)
        if (folder_ordinal := self._folder_ordinals.get(folder)) is None:
            folder_ordinal = self._folder_ordinals[folder] = len(self._folders)
            self._folders.append(folder)
            self._members.append([])
            self._folders_by_key.add(folder_name_key(folder), folder_ordinal)
        ordinal = len(self._ids)
        self._ids.append(media_id)
        self._names.append(name)
        self._folder_of.append(folder_ordinal)
        self._members[folder_ordinal].append(ordinal)
        self._by_name.add(name, ordinal)
        self._by_folded_name.add(name.casefold(), ordinal)

    def __len__(self) -> int:
        return len(self._ids)

    def media_id(self, ordinal: int) -> uuid.UUID:
        return self._ids[ordinal]

    def in_folder(self, folder: str) -> list[tuple[str, uuid.UUID]]:
        """``(NFC name, id)`` of the media directly in ``folder`` (a :func:`media_folder`).

        The chain's answer to "does this folder hold media"; the stamp grouping and the junk-review
        detector get the same answer from the same rows through
        :func:`phaze.services.companion_content.media_in_folders`.
        """
        folder_ordinal = self._folder_ordinals.get(folder)
        if folder_ordinal is None:
            return []
        return [(self._names[ordinal], self._ids[ordinal]) for ordinal in self._members[folder_ordinal]]

    def named(self, name: str) -> list[int]:
        """Ordinals of the media anywhere on the agent with this exact basename, else this basename case-insensitively."""
        return self._by_name.get(name) or self._by_folded_name.get(name.casefold())

    def folder_of(self, ordinal: int) -> str:
        return self._folders[self._folder_of[ordinal]]

    def twin_folders(self, folder: str) -> list[str]:
        """The OTHER media folders on the agent with the same release-folder name as ``folder``."""
        return [self._folders[other] for other in self._folders_by_key.get(folder_name_key(folder)) if self._folders[other] != folder]

    def close_names(self) -> _CloseNames:
        """The close-name token index, built on first use."""
        if self._close is None:
            self._close = _CloseNames(self)
        return self._close


class _Postings:
    """``key -> ordinals`` where a key held by one ordinal costs one ``int`` (most basenames are unique)."""

    def __init__(self) -> None:
        self._single: dict[str, int] = {}
        self._many: dict[str, list[int]] = {}

    def add(self, key: str, ordinal: int) -> None:
        if (held := self._many.get(key)) is not None:
            held.append(ordinal)
        elif (first := self._single.pop(key, None)) is not None:
            self._many[key] = [first, ordinal]
        else:
            self._single[key] = ordinal

    def get(self, key: str) -> list[int]:
        if (first := self._single.get(key)) is not None:
            return [first]
        return self._many.get(key, [])


class _CloseNames:
    """Tag-stripped tokens and full dates of every media stem and folder name, plus token postings (step 6)."""

    def __init__(self, index: AgentMediaIndex) -> None:
        interned: dict[str, str] = {}
        folder_tokens = [tuple(interned.setdefault(token, token) for token in name_tokens(PurePosixPath(folder).name)) for folder in index._folders]
        folder_dates = [frozenset(full_dates(PurePosixPath(folder).name)) for folder in index._folders]
        self.stem_tokens: list[tuple[str, ...]] = []
        self.dates: dict[int, frozenset[FullDate]] = {}
        self._folder_tokens = folder_tokens
        self._folder_of = index._folder_of
        postings: dict[str, array[int]] = {}
        for ordinal, name in enumerate(index._names):
            stem = _stem(name)
            tokens = tuple(interned.setdefault(token, token) for token in name_tokens(stem))
            self.stem_tokens.append(tokens)
            folder_ordinal = index._folder_of[ordinal]
            if dates := full_dates(stem) | folder_dates[folder_ordinal]:
                self.dates[ordinal] = frozenset(dates)
            for token in set(tokens).union(folder_tokens[folder_ordinal]):
                postings.setdefault(token, array("i")).append(ordinal)
        self._postings = postings

    def folder_tokens(self, ordinal: int) -> tuple[str, ...]:
        return self._folder_tokens[self._folder_of[ordinal]]

    def tokens(self, ordinal: int) -> set[str]:
        return set(self.stem_tokens[ordinal]).union(self.folder_tokens(ordinal))

    def postings(self, token: str) -> Sequence[int]:
        return self._postings.get(token, ())


# --- the steps ----------------------------------------------------------------------------------------


def _ends_in_media(name: str) -> bool:
    return PurePosixPath(name).suffix.lower() in MEDIA_EXTENSIONS


def _own_folder_hits(name: str, source: str, media: Sequence[tuple[str, uuid.UUID]]) -> list[uuid.UUID]:
    """One reference against the companion's own folder: exact, case-insensitive, token tail, other extension."""
    if hits := [media_id for media_name, media_id in media if media_name == name]:
        return hits
    folded = name.casefold()
    if hits := [media_id for media_name, media_id in media if media_name.casefold() == folded]:
        return hits
    if source == "text_token" and (
        hits := [media_id for media_name, media_id in media if len(media_name) >= TEXT_TOKEN_MIN_NAME and name.endswith(media_name)]
    ):
        return hits
    stem = _stem(name).casefold()
    return [media_id for media_name, media_id in media if _stem(media_name).casefold() == stem]


def resolve_references(companion: LinkInput, index: AgentMediaIndex) -> list[uuid.UUID]:
    """Step 2: the media the companion's stored references name, own folder first, then the whole agent."""
    folder = media_folder(companion.path)
    media = index.in_folder(folder)
    own = [media_id for name, source in companion.references for media_id in _own_folder_hits(_nfc(name), source, media)]
    if own:
        return list(dict.fromkeys(own))
    release = folder_name_key(folder)
    found: list[uuid.UUID] = []
    for name, _source in companion.references:
        if not _ends_in_media(name):
            continue
        holders = index.named(_nfc(name))
        if len(holders) != 1:
            holders = [ordinal for ordinal in holders if folder_name_key(index.folder_of(ordinal)) == release]
        if len(holders) == 1:
            found.append(index.media_id(holders[0]))
    return list(dict.fromkeys(found))


def own_folder_stem(companion: LinkInput, index: AgentMediaIndex) -> list[uuid.UUID]:
    """Step 3: own-folder media whose shipped match key equals the companion's own stem key."""
    key = companion_match_key(_stem(companion.path))
    if not key:
        return []
    return [media_id for name, media_id in index.in_folder(media_folder(companion.path)) if companion_match_key(_stem(name)) == key]


def release_folder_twin(companion: LinkInput, index: AgentMediaIndex) -> list[uuid.UUID]:
    """Step 5: a media-less folder's single same-named twin folder, all of its media."""
    folder = media_folder(companion.path)
    if index.in_folder(folder) or len(folder_name_key(folder)) < TWIN_MIN_KEY:
        return []
    twins = index.twin_folders(folder)
    return [media_id for _name, media_id in index.in_folder(twins[0])] if len(twins) == 1 else []


def close_name(companion: LinkInput, index: AgentMediaIndex) -> list[uuid.UUID]:
    """Step 6: the unique best whole-agent close-name match at :data:`CLOSE_NAME_THRESHOLD`, date-guarded.

    Spike §2, method (dw): candidates share a token held by fewer than :data:`CLOSE_NAME_MAX_POSTINGS`
    media names; the best :data:`CLOSE_NAME_SHORTLIST` by token Jaccard are scored by
    :func:`name_similarity` of the companion's stem and folder name against each media stem and
    folder name; the date guard drops candidates naming another date; the best must reach the
    threshold and beat every rival in ANOTHER folder by :data:`CLOSE_NAME_MARGIN`. Ties sort by
    folder then ordinal, so the outcome never depends on set iteration order.
    """
    path = PurePosixPath(companion.path)
    ours = (name_tokens(path.stem), name_tokens(path.parent.name))
    query = set(ours[0]).union(ours[1])
    our_dates = full_dates(path.stem) | full_dates(path.parent.name)
    names = index.close_names()
    candidates: set[int] = set()
    for token in query:
        held = names.postings(token)
        if len(held) < CLOSE_NAME_MAX_POSTINGS:
            candidates.update(held)

    def overlap(ordinal: int) -> tuple[float, str, int]:
        theirs = names.tokens(ordinal)
        return (-len(query & theirs) / len(query | theirs), index.folder_of(ordinal), ordinal)

    scored = sorted(
        (
            max(name_similarity(mine, theirs) for mine in ours for theirs in (names.stem_tokens[ordinal], names.folder_tokens(ordinal))),
            index.folder_of(ordinal),
            ordinal,
        )
        for ordinal in sorted(candidates, key=overlap)[:CLOSE_NAME_SHORTLIST]
        if dates_agree(our_dates, names.dates.get(ordinal, ()))
    )
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    if not scored or scored[0][0] < CLOSE_NAME_THRESHOLD:
        return []
    best_score, best_folder, best = scored[0]
    if any(best_score - score < CLOSE_NAME_MARGIN and folder != best_folder for score, folder, _ordinal in scored[1:]):
        return []
    return [index.media_id(best)]


def link_companion(companion: LinkInput, index: AgentMediaIndex) -> LinkDecision:
    """Run the chain for one companion: the first step that returns anything decides (module docstring)."""
    own_media = index.in_folder(media_folder(companion.path))
    vetoed = never_link_reason(companion.junk_class, folder_has_media=bool(own_media), identical_copy_linked=companion.identical_copy_linked)
    if vetoed is not None:
        return LinkDecision("duplicate" if vetoed == "duplicate" else "junk")
    if found := resolve_references(companion, index):
        return LinkDecision("reference", tuple(found))
    if found := own_folder_stem(companion, index):
        return LinkDecision("stem", tuple(found))
    if companion.is_tracklist and is_collection_folder(name for name, _media_id in own_media):
        return LinkDecision("unlinked")
    if own_media:
        return LinkDecision("folder", tuple(media_id for _name, media_id in own_media))
    if found := release_folder_twin(companion, index):
        return LinkDecision("twin", tuple(found))
    if found := close_name(companion, index):
        return LinkDecision("close_name", tuple(found))
    return LinkDecision("unlinked")
