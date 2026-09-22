"""Apply learned date-order conventions as a GATED fallback in the proposal path (phaze-5fta.4).

## The precedence rule, stated once

A **self-resolving date always wins.** If the filename's own ``\\d{2}-\\d{2}-\\d{4}`` token can only
be read one way, that reading is used and the convention store is not consulted at all -- not as a
tie-break, not as a cross-check, not even to log a disagreement about this file. The store exists
solely to answer the 35.2% of dates the string genuinely cannot answer. Letting a learned
convention override a self-resolving filename would invert the entire evidence relationship: those
same self-resolving files are what the convention was learned FROM.

## Three gates, all of which must pass

1. **The feature flag** (``convention_date_fallback_enabled``) -- **defaults ON** since 2026-08-04,
   and gates the whole capability rather than just the store query. With it off this module writes
   nothing into the LLM context and nothing into the proposal's ``context_used``, so proposals are
   byte-identical to the pre-phaze-5fta behavior; that fail-closed path is still one env var away
   (``PHAZE_CONVENTION_DATE_FALLBACK_ENABLED=false``). It shipped off and was flipped on by operator
   decision (2026-08-04, see config.py's ROLLOUT HISTORY comment for the dated citation) after
   phaze-5fta.5 validated derived dates against independent published event dates with 0
   contradictions. The permission question that validation deliberately left open was
   answered narrowly: a derived date reaches a rename PROPOSAL, never the filesystem, and the
   approval workflow still gates every move.
2. **The evidence bar** (``convention_date_min_supporting``) -- a group whose convention rests on a
   handful of files is indistinguishable from chance. Validated at 50 against the live corpus,
   where it admits the 10 groups covering 84.9% of the ambiguous files that land in any group.
3. **The purity bar** (``convention_date_min_purity``, compared against the DB-derived
   ``filename_convention.confidence``) -- a group with real contradictions has drift, a shared tag,
   or an extractor bug, and none of those should silently rewrite a date. Raised to 1.0 by
   phaze-5fta.5: below unanimity the bar stops being a rule about evidence quality and becomes a
   rule about group size, since the same single contradicting file clears 0.99 in a large group
   and fails it in a small one.

A group below either bar leaves the date **unresolved**. That is the fail-closed outcome, not a
degraded one: the proposal is generated exactly as it would have been without this feature.

## Provenance travels with the date

Whenever a date is resolved -- from the filename OR from a convention -- the full evidence is
attached under :data:`CONTEXT_KEY` and persisted alongside the proposal, so phaze-5fta.6's approval
UI can render "date inferred from release-group convention -- 1,373 supporting, 0 contradicting"
instead of a bare date presented as fact. The ``source`` field is what distinguishes the two, and
``supporting_count`` / ``contradicting_count`` / ``ambiguous_count`` / ``confidence`` are copied
from the exact convention row that was applied (with its ``id`` and ``computed_at``), so the UI can
show the evidence as it stood when the inference was made rather than as it stands at render time.

## The parent-directory fallback (phaze-soc1q)

Operator decision 2026-09-22 (phaze-soc1q, Q2 as put: *"should the date resolver also read the
parent folder name when the filename has no date?"*, answer *"Yes, fallback to folder
(Recommended)"*): **the filename's own token always wins**; only when the filename carries NO
``\\d{2}-\\d{2}-\\d{4}`` token at all (:attr:`~phaze.services.filename_convention_learner.DateVerdict.NONE_PRESENT`)
is the parent directory name of ``original_path`` read for one, with the identical precedence and
gating rules applied to whatever token is found there. A filename token that exists but is
ambiguous, not-a-date, or conflicting is still the filename's own answer -- unresolved is a
fail-closed outcome, not a licence to keep looking. :data:`DateProvenance.origin` records
which string the applied token actually came from (:data:`ORIGIN_FILENAME` or
:data:`ORIGIN_FOLDER`), independently of ``source`` -- an ambiguous token found in the
folder name can still only be resolved via a release-group convention (the group tag itself is
always read from the filename, per ``release_group.py``'s own scene-tail rules), so ``source`` and
``origin`` vary independently and a reviewer needs both to know what happened.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
import structlog

from phaze.models.filename_convention import FilenameConvention
from phaze.services.filename_convention_learner import (
    CONVENTION_KIND,
    CONVENTION_SCOPE,
    DateOrder,
    DateVerdict,
    read_date_order,
)
from phaze.services.release_group import extract_release_group


if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


__all__ = [
    "CONTEXT_KEY",
    "ORIGIN_FILENAME",
    "ORIGIN_FOLDER",
    "SOURCE_CONVENTION",
    "SOURCE_FILENAME",
    "DateProvenance",
    "annotate_date_conventions",
    "load_release_group_conventions",
    "resolve_date",
]


logger = structlog.get_logger(__name__)


CONTEXT_KEY = "date_convention"
"""The key this module writes into a file's LLM context dict and into the proposal's
``context_used``. ONE name for both so the UI (phaze-5fta.6) has a single place to look, and so the
"flag off => key absent" invariant is checkable with one membership test."""

SOURCE_FILENAME = "filename"
"""The date resolved itself from its token. The convention store was NOT consulted. Orthogonal to
:data:`ORIGIN_FILENAME` / :data:`ORIGIN_FOLDER` -- this says how the order was decided
(read off the string), not which string it was read from."""

SOURCE_CONVENTION = "release_group_convention"
"""The date was ambiguous and was resolved by a learned convention that cleared both gates."""

ORIGIN_FILENAME = "filename"
"""The applied ``\\d{2}-\\d{2}-\\d{4}`` token was read from ``original_filename``."""

ORIGIN_FOLDER = "folder"
"""The applied token was read from the parent directory name of ``original_path`` -- only
attempted when the filename carried no date token at all (phaze-soc1q, operator decision
2026-09-22). The filename's own token always wins when one is present, ambiguous or not."""


@dataclass(frozen=True, slots=True)
class DateProvenance:
    """A resolved date plus everything needed to justify it to a human reviewer.

    Every convention field is ``None`` when ``source`` is :data:`SOURCE_FILENAME` -- a
    self-resolving date has no convention behind it, and leaving the keys present-but-null (rather
    than omitting them) keeps the persisted JSON one fixed shape for the UI to render.
    """

    date: str
    """The resolved date, ISO ``YYYY-MM-DD``."""
    raw: str
    """The date token exactly as it appeared in the source string, e.g. ``04-05-2014``."""
    date_order: str
    """Which reading was applied: a :class:`~phaze.services.filename_convention_learner.DateOrder`."""
    source: str
    """:data:`SOURCE_FILENAME` or :data:`SOURCE_CONVENTION` -- the single field a reviewer needs to
    know whether this date is a fact about the token or an inference about its uploader."""
    origin: str = ORIGIN_FILENAME
    """:data:`ORIGIN_FILENAME` or :data:`ORIGIN_FOLDER` -- which string the applied
    token was actually read from. Always populated, including for a self-resolving filename date,
    so a reviewer never has to infer origin from absence."""
    scope: str | None = None
    scope_value: str | None = None
    convention_kind: str | None = None
    convention_value: str | None = None
    convention_id: str | None = None
    supporting_count: int | None = None
    contradicting_count: int | None = None
    ambiguous_count: int | None = None
    confidence: float | None = None
    computed_at: str | None = None
    """When the learner computed the applied row -- the evidence AS IT STOOD, not as it stands at
    render time. A later refresh must not silently restate the justification for an old proposal."""

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe mapping for the ``context_used`` JSONB column."""
        return asdict(self)


def _clears_gates(convention: FilenameConvention, *, min_supporting: int, min_purity: float) -> bool:
    """True when this convention row is strong enough to resolve an ambiguous date.

    Both bars, never one. ``confidence`` is read straight off the row because it is a Postgres
    ``GENERATED ALWAYS`` column -- recomputing purity here from the counts would be a second
    definition of the same quantity, free to drift from the one the database enforces.
    """
    return convention.supporting_count >= min_supporting and convention.confidence >= min_purity


def resolve_date(
    filename: str,
    *,
    convention: FilenameConvention | None,
    min_supporting: int,
    min_purity: float,
    origin: str = ORIGIN_FILENAME,
) -> DateProvenance | None:
    """Resolve *filename*'s date, or return ``None`` when it cannot be resolved safely.

    Args:
        filename: the string to read -- normally ``original_filename``, but the caller may pass a
            parent directory name instead (phaze-soc1q's folder fallback) when the filename carries
            no date token at all. Only its ``\\d{2}-\\d{2}-\\d{4}`` token is considered.
        convention: the learned ``date_order`` row for this filename's release group, or ``None``
            when the group is unknown or has no row. IGNORED when *filename* resolves itself. The
            group is always the one extracted from the ORIGINAL FILENAME regardless of
            *origin* -- scene release-group tags live in filenames, not directory names.
        min_supporting: the evidence bar (``convention_date_min_supporting``).
        min_purity: the purity bar (``convention_date_min_purity``).
        origin: :data:`ORIGIN_FILENAME` or :data:`ORIGIN_FOLDER` -- which string
            *filename* actually is, recorded verbatim onto the returned provenance so a reviewer
            can tell a folder-derived date from a filename-derived one.

    Returns:
        A :class:`DateProvenance` when a date was resolved, else ``None``. ``None`` covers every
        fail-closed case: no date in *filename*, a token that is no date under either reading, two
        conflicting tokens, no convention for the group, and a convention below either bar.

    This function does NOT check the feature flag -- :func:`annotate_date_conventions` owns that,
    so the flag is enforced at exactly one place and the resolution rule stays independently
    testable.
    """
    reading = read_date_order(filename)

    self_resolved_order = reading.order
    if self_resolved_order is not None:
        # PRECEDENCE: the string answered its own question. The store is not consulted.
        resolved = reading.as_date(self_resolved_order)
        if resolved is None or reading.raw is None:  # pragma: no cover -- a self-resolving reading always has both
            return None
        return DateProvenance(
            date=resolved.isoformat(),
            raw=reading.raw,
            date_order=str(self_resolved_order),
            source=SOURCE_FILENAME,
            origin=origin,
        )

    if reading.verdict is not DateVerdict.AMBIGUOUS or reading.raw is None:
        # No date, no date under either reading, or two conflicting tokens: nothing to fall back on.
        return None
    if convention is None or convention.convention_value is None:
        return None
    if not _clears_gates(convention, min_supporting=min_supporting, min_purity=min_purity):
        return None
    try:
        order = DateOrder(convention.convention_value)
    except ValueError:
        # A row whose convention_value is not a DateOrder is a corrupt/foreign row, not a licence
        # to guess. Fail closed and say so -- silently ignoring it would hide a real store defect.
        logger.warning(
            "ignoring a date_order convention whose value is not a recognized order",
            scope=convention.scope,
            scope_value=convention.scope_value,
            convention_value=convention.convention_value,
        )
        return None

    resolved = reading.as_date(order)
    if resolved is None:  # pragma: no cover -- unreachable: AMBIGUOUS means BOTH readings are legal
        # `read_date_order` decides AMBIGUOUS by constructing the calendar date BOTH ways and
        # finding both legal, so neither order can fail here. This is a type-narrowing guard that
        # fails closed rather than inventing a date, not a live path -- if it ever executes, the
        # verdict and the composer have drifted apart and refusing to resolve is the right answer.
        return None
    return DateProvenance(
        date=resolved.isoformat(),
        raw=reading.raw,
        date_order=str(order),
        source=SOURCE_CONVENTION,
        origin=origin,
        scope=convention.scope,
        scope_value=convention.scope_value,
        convention_kind=convention.convention_kind,
        convention_value=convention.convention_value,
        convention_id=str(convention.id),
        supporting_count=convention.supporting_count,
        contradicting_count=convention.contradicting_count,
        ambiguous_count=convention.ambiguous_count,
        confidence=convention.confidence,
        computed_at=convention.computed_at.isoformat() if convention.computed_at is not None else None,
    )


async def load_release_group_conventions(session: AsyncSession, groups: Collection[str]) -> dict[str, FilenameConvention]:
    """Load the ``date_order`` convention rows for *groups*, keyed by release group.

    One query for the whole batch rather than one per file: ``generate_proposals`` runs over a
    fixed-size batch and the groups within it repeat heavily (that repetition is the entire premise
    of the feature). Returns only the groups that HAVE a row; a missing key is the caller's
    "no convention" case and needs no sentinel.
    """
    if not groups:
        return {}
    rows = (
        await session.execute(
            select(FilenameConvention).where(
                FilenameConvention.scope == CONVENTION_SCOPE,
                FilenameConvention.convention_kind == CONVENTION_KIND,
                FilenameConvention.scope_value.in_(sorted(set(groups))),
            )
        )
    ).scalars()
    return {row.scope_value: row for row in rows}


def _parent_directory_name(original_path: str) -> str:
    """The immediate parent directory's own name in *original_path*, or ``""`` if there is none.

    Pure string handling, no filesystem access -- this module never touches disk, the same
    convention as ``release_group.py``'s ``_basename``. Both POSIX and Windows separators are
    honoured since ``original_path`` may have been recorded from either. A path with fewer than two
    components (a bare filename, or empty) has no parent to read.
    """
    normalized = original_path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 2:
        return ""
    return parts[-2]


async def annotate_date_conventions(
    session: AsyncSession,
    contexts: Sequence[dict[str, Any]],
    *,
    enabled: bool,
    min_supporting: int,
    min_purity: float,
) -> int:
    """Attach :data:`CONTEXT_KEY` provenance to each file context whose date could be resolved.

    Args:
        session: an open read session. Untouched when *enabled* is ``False`` -- the flag-off path
            issues NO query at all.
        contexts: the per-file context dicts from
            :func:`phaze.services.proposal.build_file_context`. Mutated in place; a context whose
            date cannot be resolved is left exactly as it was found.
        enabled: the ``convention_date_fallback_enabled`` flag. **The whole capability's off
            switch**: returning early here is what makes "flag off => byte-identical proposals"
            true by construction rather than by careful downstream handling.
        min_supporting: the evidence bar.
        min_purity: the purity bar.

    Returns:
        How many contexts were annotated.

    The filename's own token always wins (phaze-soc1q, operator decision 2026-09-22): the parent
    directory name of ``original_path`` is consulted only when ``original_filename`` carries no
    ``\\d{2}-\\d{2}-\\d{4}`` token at all (:attr:`~phaze.services.filename_convention_learner.DateVerdict.NONE_PRESENT`).
    An ambiguous, not-a-date, or multi-token filename is still the filename's own answer and is left
    unresolved exactly as before -- it does not fall through to the folder.
    """
    if not enabled:
        return 0

    filenames = [str(context.get("original_filename") or "") for context in contexts]
    original_paths = [str(context.get("original_path") or "") for context in contexts]

    # The string each context's date is actually resolved from (filename, or its parent directory
    # name when the filename has no token at all) plus where that string came from. The release
    # group used for a convention lookup is ALWAYS read from `original_filename` regardless of
    # `origin` -- scene release-group tags live in filenames, per release_group.py.
    resolve_from: list[str] = []
    origins: list[str] = []
    for name, path in zip(filenames, original_paths, strict=True):
        text, origin = name, ORIGIN_FILENAME
        if read_date_order(name).verdict is DateVerdict.NONE_PRESENT:
            folder = _parent_directory_name(path)
            if folder and read_date_order(folder).verdict is not DateVerdict.NONE_PRESENT:
                text, origin = folder, ORIGIN_FOLDER
        resolve_from.append(text)
        origins.append(origin)

    # Only AMBIGUOUS strings need a group looked up -- a self-resolving one never consults the
    # store, so resolving its group would be a query nobody reads.
    wanted: set[str] = set()
    groups: list[str | None] = []
    for name, text in zip(filenames, resolve_from, strict=True):
        if read_date_order(text).verdict is DateVerdict.AMBIGUOUS:
            group = extract_release_group(name)
            groups.append(group)
            if group is not None:
                wanted.add(group)
        else:
            groups.append(None)

    conventions = await load_release_group_conventions(session, wanted)

    annotated = 0
    for context, text, origin, group in zip(contexts, resolve_from, origins, groups, strict=True):
        provenance = resolve_date(
            text,
            convention=conventions.get(group) if group is not None else None,
            min_supporting=min_supporting,
            min_purity=min_purity,
            origin=origin,
        )
        if provenance is None:
            continue
        context[CONTEXT_KEY] = provenance.as_dict()
        annotated += 1
        if provenance.source == SOURCE_CONVENTION:
            logger.info(
                "date resolved from a learned release-group convention",
                scope_value=provenance.scope_value,
                convention_value=provenance.convention_value,
                supporting_count=provenance.supporting_count,
                contradicting_count=provenance.contradicting_count,
                resolved_date=provenance.date,
                origin=provenance.origin,
            )
    return annotated
