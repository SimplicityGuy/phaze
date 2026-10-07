"""Store companion content features and decide known stamps over them (phaze-osy6j) -- the control-side half.

The agent reads each companion and reports its features (``services/companion_features.py``); this
module persists them and decides the one class no single file can decide about itself.

KNOWN STAMPS -- THE METHOD (decided on write, never at read time). A known stamp is a CONTENT, not a filename: one fingerprint that the
agent holds in at least three differently named folders which hold DIFFERENT media -- the media sets
of the folders that hold any have an empty intersection -- or, when none of its folders holds media,
in at least three differently named folders. That is the rule
``docs/spikes/phaze-lm73u-companion-content-survey.md`` (§4, classifier rule 2; §4.4) measured over
production: it selects exactly 27 contents covering 4,203 rows, with 52/52 sampled precision across
every junk label. The stamps are therefore identified from the archive's own evidence each time
features land, never from a hand-typed list of names, digests or text. Two things fall out of it:

- **A stamp with real text appended is not a stamp.** Its bytes differ, so its fingerprint is not
  the stamp's (3 of the survey's 3,895 copies of one release-site stamp carry a real tracklist or
  release block after the advert; a filename rule would have deleted them).
- **A duplicated release is not a stamp.** The same release downloaded three times leaves three
  identical NFOs beside the SAME media, and usually in folders of the same name: the survey's 251
  such rows (``.m3u`` 128, ``.nfo`` 123) are a dedup matter and stay out of junk.

The verdict is a property of the GROUP, so it is recomputed for the whole group whenever its
membership changes: when features land (:func:`store_companion_features` -- the third copy flips all
three) and when a copy leaves (a scan or file deletion, or a content change: ``services/
scan_deletion.py`` calls :func:`stamp_groups_in` before and :func:`refresh_stamp_groups` after), so no
copy keeps a stale flag. Readers take ``junk_class`` as stored.

Folder names compare alphanumeric-only and case-folded, as the survey did. Folder media are what the
agent listed beside the companion when it read it; a folder listed before its media arrived reads as
holding none, which can only make a stamp verdict less likely, never more.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.constants import INGESTIBLE_COMPANION_EXTENSIONS
from phaze.models.companion_content import CompanionContentFeatures
from phaze.models.file import FileRecord
from phaze.services.bulk_insert import chunk_rows
from phaze.services.companion_features import EXTRACTOR_VERSION


if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Sequence
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.schemas.agent_companion_features import CompanionFeaturesRecord


STAMP_MIN_FOLDERS = 3
"""Distinct folder names (and, beside media, distinct media sets) a content needs to be a stamp (survey §4 rule 2)."""

COMPANION_FILE_TYPES: frozenset[str] = frozenset(ext.lstrip(".") for ext in INGESTIBLE_COMPANION_EXTENSIONS)

_BYTES_JUNK: tuple[str, ...] = ("empty", "all_nul")
"""Per-file classes that outrank a stamp verdict: empty bytes are junk whatever else holds them."""

_FINGERPRINT_PAGE = 1_000
"""Fingerprints per stamp-refresh query: one bind each, far under asyncpg's 32,767 cap."""


@dataclass(frozen=True)
class StoreOutcome:
    """What one reported chunk did: rows stored, and records naming no companion row of the agent."""

    stored: int
    unknown: int


def folder_name_key(directory: str) -> str:
    """A folder's name, alphanumeric-only and case-folded -- how the survey told folders apart."""
    return "".join(char for char in PurePosixPath(directory).name.casefold() if char.isalnum())


def is_known_stamp(members: Iterable[tuple[str, Collection[str]]]) -> bool:
    """Decide whether one content is a stamp, from ``(folder, media names in that folder)`` per copy."""
    names: set[str] = set()
    media_sets: list[frozenset[str]] = []
    for directory, media in members:
        names.add(folder_name_key(directory))
        if media:
            media_sets.append(frozenset(media))
    if len(names) < STAMP_MIN_FOLDERS:
        return False
    if len(set(media_sets)) >= STAMP_MIN_FOLDERS:
        return not frozenset.intersection(*media_sets)
    return not media_sets


def _effective_junk_class(content_junk_class: Any, stamp: Any) -> Any:
    """SQL CASE: bytes-junk first, then a stamp verdict, then the agent's per-file class (site_ad or NULL)."""
    return case(
        (content_junk_class.in_(_BYTES_JUNK), content_junk_class),
        (stamp, "known_stamp"),
        else_=content_junk_class,
    )


async def refresh_known_stamps(session: AsyncSession, agent_id: str, fingerprints: Collection[str]) -> set[str]:
    """Re-decide the stamp verdict for ``fingerprints`` on ``agent_id`` and rewrite ``junk_class``; returns the stamps.

    Reads every stored copy of each fingerprint (one indexed read per page of fingerprints) and
    updates them all, so a verdict that changes -- a third folder arrives, a copy is replaced --
    lands on every copy at once. Does NOT commit.
    """
    stamps: set[str] = set()
    ordered = sorted(fingerprints)
    for start in range(0, len(ordered), _FINGERPRINT_PAGE):
        page = ordered[start : start + _FINGERPRINT_PAGE]
        rows = await session.execute(
            select(CompanionContentFeatures.fingerprint, FileRecord.original_path, CompanionContentFeatures.folder_media)
            .join(FileRecord, FileRecord.id == CompanionContentFeatures.file_id)
            .where(CompanionContentFeatures.agent_id == agent_id, CompanionContentFeatures.fingerprint.in_(page))
        )
        groups: dict[str, list[tuple[str, list[str]]]] = defaultdict(list)
        for fingerprint, original_path, media in rows.all():
            groups[fingerprint].append((str(PurePosixPath(original_path).parent), media))
        page_stamps = {fingerprint for fingerprint, members in groups.items() if is_known_stamp(members)}
        stamps |= page_stamps
        await session.execute(
            update(CompanionContentFeatures)
            .where(CompanionContentFeatures.agent_id == agent_id, CompanionContentFeatures.fingerprint.in_(page))
            .values(
                junk_class=_effective_junk_class(CompanionContentFeatures.content_junk_class, CompanionContentFeatures.fingerprint.in_(page_stamps)),
                updated_at=func.now(),
            )
        )
    return stamps


async def stamp_groups_in(session: AsyncSession, file_scope: Any) -> dict[str, set[str]]:
    """``agent_id -> fingerprints`` of the content groups the files ``file_scope`` selects belong to.

    Read BEFORE a deletion or a content invalidation removes those rows, then handed to
    :func:`refresh_stamp_groups` after it: a stamp verdict is a property of the whole group, so a
    removed copy must re-decide the copies that remain (it can un-stamp them -- two folders left --
    and, when the removed copy was the only one beside media, stamp them).
    """
    rows = await session.execute(
        select(CompanionContentFeatures.agent_id, CompanionContentFeatures.fingerprint)
        .where(CompanionContentFeatures.file_id.in_(select(FileRecord.id).where(file_scope)))
        .distinct()
    )
    groups: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        groups[row.agent_id].add(row.fingerprint)
    return dict(groups)


async def refresh_stamp_groups(session: AsyncSession, groups: dict[str, set[str]]) -> None:
    """Re-decide every group :func:`stamp_groups_in` collected. Does NOT commit."""
    for agent_id, fingerprints in sorted(groups.items()):
        await refresh_known_stamps(session, agent_id, fingerprints)


async def _companion_rows(session: AsyncSession, agent_id: str, paths: Sequence[str]) -> dict[str, uuid.UUID]:
    """``original_path -> files.id`` for the agent's COMPANION rows among ``paths``."""
    result = await session.execute(
        select(FileRecord.original_path, FileRecord.id).where(
            FileRecord.agent_id == agent_id,
            FileRecord.original_path.in_(paths),
            FileRecord.file_type.in_(COMPANION_FILE_TYPES),
        )
    )
    return {row.original_path: row.id for row in result}


async def store_companion_features(session: AsyncSession, agent_id: str, records: Sequence[CompanionFeaturesRecord]) -> StoreOutcome:
    """Upsert the agent's reported features and refresh the stamp verdict of every content they touch.

    A record whose path names no companion row of ``agent_id`` is counted as ``unknown`` and dropped:
    the agent posts features right after the upsert that created the row, so an unknown path is a
    file the control plane does not hold (or a media file), never one to create a row for. Only the
    fingerprints whose group could have changed -- a new row, a changed fingerprint or folder listing
    (old and new fingerprint both) -- are re-decided, so an unchanged re-scan costs no stamp query.
    Does NOT commit.
    """
    by_path = {record.original_path: record for record in records}
    file_ids = await _companion_rows(session, agent_id, list(by_path))
    if not file_ids:
        return StoreOutcome(stored=0, unknown=len(by_path))

    previous = {
        file_id: (fingerprint, media, content_junk)
        for file_id, fingerprint, media, content_junk in (
            await session.execute(
                select(
                    CompanionContentFeatures.file_id,
                    CompanionContentFeatures.fingerprint,
                    CompanionContentFeatures.folder_media,
                    CompanionContentFeatures.content_junk_class,
                ).where(CompanionContentFeatures.file_id.in_(list(file_ids.values())))
            )
        ).all()
    }

    values: list[dict[str, Any]] = []
    touched: set[str] = set()
    for path, file_id in sorted(file_ids.items()):
        record = by_path[path]
        values.append(
            {
                "file_id": file_id,
                "agent_id": agent_id,
                "fingerprint": record.fingerprint,
                "encoding": record.encoding,
                "byte_size": record.byte_size,
                "truncated": record.truncated,
                "media_references": [reference.model_dump() for reference in record.media_references],
                "reference_count": record.reference_count,
                "is_tracklist": record.is_tracklist,
                "content_junk_class": record.content_junk_class,
                "junk_class": record.content_junk_class,
                "folder_media": record.folder_media,
                "folder_media_count": record.folder_media_count,
                "extractor_version": record.extractor_version,
            }
        )
        before = previous.get(file_id)
        if before != (record.fingerprint, record.folder_media, record.content_junk_class):
            touched.add(record.fingerprint)
            if before is not None:
                touched.add(before[0])

    for rows in chunk_rows(values):
        statement = pg_insert(CompanionContentFeatures).values(rows)
        excluded = statement.excluded
        # An unchanged copy keeps a stamp verdict it already carries; every changed copy's group is
        # re-decided below, so this only spares the unchanged re-report a pointless reset.
        kept_stamp = (CompanionContentFeatures.junk_class == "known_stamp") & (CompanionContentFeatures.fingerprint == excluded.fingerprint)
        statement = statement.on_conflict_do_update(
            index_elements=["file_id"],
            set_={
                **{column: excluded[column] for column in values[0] if column not in ("file_id", "junk_class")},
                "junk_class": _effective_junk_class(excluded.content_junk_class, kept_stamp),
                "extracted_at": func.now(),
                "updated_at": func.now(),
            },
        )
        await session.execute(statement)
    await refresh_known_stamps(session, agent_id, touched)
    return StoreOutcome(stored=len(values), unknown=len(by_path) - len(values))


@dataclass(frozen=True)
class BackfillCounts:
    """One agent's companion rows, by what ``phaze backfill companion-features`` would do with them."""

    agent_id: str
    companions: int
    current: int
    missing: int
    stale_content: int
    stale_extractor: int

    @property
    def pending(self) -> int:
        """Rows the backfill would ask the agent to (re-)read."""
        return self.missing + self.stale_content + self.stale_extractor


def _needs_features() -> Any:
    """WHERE clause over ``files LEFT JOIN companion_content_features``: no row, older bytes, or an older extractor."""
    return (
        CompanionContentFeatures.file_id.is_(None)
        | (CompanionContentFeatures.fingerprint != FileRecord.sha256_hash)
        | (CompanionContentFeatures.extractor_version < EXTRACTOR_VERSION)
    )


def _companions_with_features() -> Any:
    """``files LEFT JOIN companion_content_features`` restricted to companion rows."""
    return (
        select()
        .select_from(FileRecord)
        .outerjoin(CompanionContentFeatures, CompanionContentFeatures.file_id == FileRecord.id)
        .where(FileRecord.file_type.in_(COMPANION_FILE_TYPES))
    )


async def count_backfill(session: AsyncSession) -> list[BackfillCounts]:
    """Per agent: companion rows, and how many have current features, none, stale bytes or an older extractor."""
    missing = CompanionContentFeatures.file_id.is_(None)
    stale_content = ~missing & (CompanionContentFeatures.fingerprint != FileRecord.sha256_hash)
    stale_extractor = ~missing & ~stale_content & (CompanionContentFeatures.extractor_version < EXTRACTOR_VERSION)
    result = await session.execute(
        _companions_with_features()
        .add_columns(
            FileRecord.agent_id,
            func.count(),
            func.count().filter(missing),
            func.count().filter(stale_content),
            func.count().filter(stale_extractor),
        )
        .group_by(FileRecord.agent_id)
        .order_by(FileRecord.agent_id)
    )
    return [
        BackfillCounts(
            agent_id=agent_id,
            companions=total,
            current=total - n_missing - n_content - n_extractor,
            missing=n_missing,
            stale_content=n_content,
            stale_extractor=n_extractor,
        )
        for agent_id, total, n_missing, n_content, n_extractor in result.all()
    ]


async def select_backfill_page(
    session: AsyncSession, agent_id: str, *, after: tuple[str, uuid.UUID] | None, limit: int
) -> list[tuple[uuid.UUID, str]]:
    """One keyset page of ``(file_id, original_path)`` the agent should (re-)read, ordered by ``(original_path, id)``."""
    statement = (
        _companions_with_features()
        .add_columns(FileRecord.id, FileRecord.original_path)
        .where(FileRecord.agent_id == agent_id, _needs_features())
        .order_by(FileRecord.original_path, FileRecord.id)
        .limit(limit)
    )
    if after is not None:
        statement = statement.where(tuple_(FileRecord.original_path, FileRecord.id) > after)
    return [(file_id, original_path) for file_id, original_path in (await session.execute(statement)).all()]
