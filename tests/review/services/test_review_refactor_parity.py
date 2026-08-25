"""phaze-b4u3p parity evidence: the ``services/review.py`` decomposition is behavior-preserving.

Loads the PRE-refactor ``services/review.py`` straight out of git (the merge-base commit this
bead branched from) as a second, independent module object, and runs every one of its public
read functions against the SAME seeded session state the NEW (decomposed) module reads -- then
asserts the two results are equal. The old module's own imports (``from phaze.routers.cue import
_build_cue_tracks, ...`` / ``from phaze.routers.tags import (...)``) resolve through the CURRENT,
edited ``routers/cue.py`` / ``routers/tags.py`` at exec time -- which still re-export every one of
those names (see those modules' docstrings) -- so this is a genuine apples-to-apples comparison,
not a frozen snapshot compared against itself.

Two claims are kept deliberately separate, per this bead's acceptance criteria:

* Parity tests below assert the RENDERED ROW DATA is byte-for-byte identical old vs new.
* ``test_cue_review_cards_query_count_*`` asserts the QUERY COUNT dropped -- a different claim,
  proven with a counting session, not by diffing output.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
from typing import TYPE_CHECKING
import uuid

import pytest

from phaze.models.proposal import ProposalStatus
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.services import review as new_review
from tests.review.services.test_review_degrade import (
    _CountingSession,
    _seed_applied_tagwrite_file,
    _seed_eligible_cue_tracklist,
)


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from types import ModuleType

    from sqlalchemy.ext.asyncio import AsyncSession

    from phaze.models.file import FileRecord
    from phaze.models.proposal import RenameProposal


# The commit `phaze-b4u3p` branched from (this worktree's merge-base with main at claim time) --
# the LAST commit where `src/phaze/services/review.py` held its pre-decomposition shape. A fixed
# historical SHA, not a moving ref: `git show <sha>:<path>` reads it regardless of what branch or
# working tree state this test later runs from.
_PRE_REFACTOR_SHA = "306bceeaa5fe2c8b6b7e1ef7593550b989708f51"


def _load_old_review_module(sha: str = _PRE_REFACTOR_SHA) -> ModuleType:
    """Exec the pre-refactor ``services/review.py`` from git history as an independent module.

    Its own ``from phaze.routers.cue import ...`` / ``from phaze.routers.tags import ...``
    statements run as ordinary imports at exec time, resolving against whatever is CURRENTLY
    installed -- i.e. the edited routers, which still re-export every name the old module reaches
    for EXCEPT the two batch tag-comparison lookups (phaze-b4u3p deliberately does not re-export
    those from ``routers/tags.py``: unlike the others, ``tags.py``'s OWN routes never called them,
    so re-exporting them there would be an unused-and-only-for-this-test import in production
    source, not a real compatibility need). Patched onto the ``routers.tags`` module object below
    instead of onto the shipped source -- this is a test-only shim for a HISTORICAL snapshot's
    import shape, not a real caller.
    """
    repo_root = Path(__file__).resolve()
    completed = subprocess.run(  # noqa: S603 -- fixed argv, no shell, trusted git binary
        ["git", "show", f"{sha}:src/phaze/services/review.py"],  # noqa: S607
        cwd=repo_root.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        # phaze-b4u3p review finding: this reads a HISTORICAL blob, so it needs real git history.
        # `actions/checkout` defaults to `fetch-depth: 1`, under which the object simply is not in
        # the clone and this failed for every test in the module with a bare CalledProcessError.
        # tests.yml now sets `fetch-depth: 0` for the test job; this raises a diagnosable error if
        # some future context is shallow again. It must NOT skip -- a parity suite that silently
        # stops running is the fail-open shape phaze-7l8jh was filed to remove.
        raise RuntimeError(
            f"cannot read {sha}:src/phaze/services/review.py from git history "
            f"(exit {completed.returncode}: {completed.stderr.strip()}). "
            "A shallow clone cannot run the parity suite -- the test job needs `fetch-depth: 0`."
        )
    source = completed.stdout
    module_name = f"phaze_services_review_pre_b4u3p_{sha[:12]}"
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__file__ = f"<git show {sha}:src/phaze/services/review.py>"
    sys.modules[module_name] = module
    exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102 -- trusted, our own git history
    return module


@pytest.fixture(scope="module")
def old_review() -> Iterator[ModuleType]:
    """Load the pre-refactor module, with the historical import shim torn down afterwards.

    phaze-b4u3p review finding: the shim below used to be applied inside
    ``_load_old_review_module`` and never removed, so ``phaze.routers.tags`` kept the two batch
    helpers injected for the REST of the pytest session. A later test asserting the new layering
    seam -- that the batch forms are NOT reachable through ``routers.tags`` -- would then pass
    against the shim rather than the shipped source. Applied and reverted here instead.
    """
    import phaze.routers.tags as tags_module
    from phaze.services import tag_comparison

    injected = [name for name in ("_get_accepted_discogs_links_for_files", "_get_tracklists_for_files") if not hasattr(tags_module, name)]
    for name in injected:
        setattr(tags_module, name, getattr(tag_comparison, name))
    try:
        yield _load_old_review_module()
    finally:
        for name in injected:
            delattr(tags_module, name)


# ---------------------------------------------------------------------------
# Deliberate key renames since the pinned pre-refactor SHA (phaze-n8o9p)
# ---------------------------------------------------------------------------
#
# These row dicts are compared with a raw ``==``, which is what gives this module its teeth: any
# divergence at all, in any key or value, fails. The cost is that it also cannot tell a REGRESSION
# from a rename the repo made on purpose, and phaze-n8o9p made one -- ``original_path`` ->
# ``current_path`` in ``_build_changes_review_row`` and ``_proposal_row_base``. The VALUE is
# untouched on both sides (``proposal.file.current_path``); only the key's spelling moved, because
# the old name asserted the ingest path while carrying the current one.
#
# So the old module's rows are re-keyed through exactly this mapping before the comparison, rather
# than weakening the assertion to "same values, any keys". Every other difference -- a changed
# value, a dropped key, an added one -- still fails loudly, which is the property worth keeping.
# Add to this dict ONLY for a rename that is deliberate, value-preserving, and recorded on a bead.
_RENAMED_SINCE_PRE_REFACTOR = {"original_path": "current_path"}


def _rekey(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """The pre-refactor rows, with deliberately-renamed keys spelled the way the new module spells them."""
    return [{_RENAMED_SINCE_PRE_REFACTOR.get(key, key): value for key, value in row.items()} for row in rows]


# ---------------------------------------------------------------------------
# Changes Review / Propose / Rename-Move parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changes_review_page_parity(
    session: AsyncSession,
    old_review: ModuleType,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    statuses = [ProposalStatus.PENDING, ProposalStatus.APPROVED, ProposalStatus.EXECUTED, ProposalStatus.FAILED, ProposalStatus.REJECTED]
    for index, status in enumerate(statuses):
        proposal = await seed_pending_proposal(0.91 - index * 0.05, original_filename=f"parity-changes-{index}.mp3")
        proposal.status = status.value
    await session.commit()

    old_page = await old_review.get_changes_review_page(session, status="all", page=1, page_size=25)
    new_page = await new_review.get_changes_review_page(session, status="all", page=1, page_size=25)

    assert old_page.stats == new_page.stats
    assert old_page.pagination == new_page.pagination
    assert _rekey(old_page.rows) == new_page.rows


@pytest.mark.asyncio
async def test_pending_proposal_rows_parity(
    session: AsyncSession,
    old_review: ModuleType,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    await seed_pending_proposal(0.95, original_filename="parity-pending-high.mp3")
    await seed_pending_proposal(0.5, original_filename="parity-pending-low.mp3")
    await seed_pending_proposal(None, original_filename="parity-pending-null.mp3")

    old_result = await old_review.get_pending_proposal_rows(session)
    new_result = await new_review.get_pending_proposal_rows(session)

    assert _rekey(old_result.rows) == new_result.rows
    assert old_result.total_pending == new_result.total_pending
    assert old_result.high_confidence_pending == new_result.high_confidence_pending


@pytest.mark.asyncio
async def test_proposal_workspace_page_parity(
    session: AsyncSession,
    old_review: ModuleType,
    seed_pending_proposal: Callable[..., Awaitable[RenameProposal]],
) -> None:
    await seed_pending_proposal(0.95, original_filename="parity-workspace-a.mp3")
    await seed_pending_proposal(0.2, original_filename="parity-workspace-b.mp3")

    old_page = await old_review.get_proposal_workspace_page(session, status="all", search="", page=1, page_size=25)
    new_page = await new_review.get_proposal_workspace_page(session, status="all", search="", page=1, page_size=25)

    assert old_page.stats == new_page.stats
    assert old_page.pagination == new_page.pagination
    assert _rekey(old_page.rows) == new_page.rows


# ---------------------------------------------------------------------------
# Tag-write review parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tagwrite_review_page_parity(session: AsyncSession, old_review: ModuleType) -> None:
    fresh_id = await _seed_applied_tagwrite_file(session)
    discrepancy_id = await _seed_applied_tagwrite_file(session)
    session.add(
        TagWriteLog(
            id=uuid.uuid4(),
            file_id=discrepancy_id,
            before_tags={"title": None},
            after_tags={"title": "Some Title"},
            source="review",
            status=TagWriteStatus.DISCREPANCY.value,
        )
    )
    await session.commit()

    old_page = await old_review.get_tagwrite_review_page(session)
    new_page = await new_review.get_tagwrite_review_page(session)

    assert old_page.partial == new_page.partial
    old_by_id = {row["file_id"]: row for row in old_page.rows}
    new_by_id = {row["file_id"]: row for row in new_page.rows}
    assert old_by_id.keys() == new_by_id.keys() == {fresh_id, discrepancy_id}
    assert old_by_id == new_by_id


# ---------------------------------------------------------------------------
# Dedupe parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedupe_groups_parity(
    session: AsyncSession,
    old_review: ModuleType,
    seed_duplicate_group: Callable[..., Awaitable[list[FileRecord]]],
) -> None:
    await seed_duplicate_group(count=3)
    await seed_duplicate_group(count=2)

    old_groups = await old_review.get_dedupe_groups(session)
    new_groups = await new_review.get_dedupe_groups(session)

    assert old_groups == new_groups


# ---------------------------------------------------------------------------
# Cue review parity -- the N+1 fix's own correctness proof, separate from its query-count proof
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cue_review_cards_parity_across_multiple_eligible_and_gated_sets(
    session: AsyncSession,
    old_review: ModuleType,
    seed_cue_set: Callable[..., Awaitable[tuple[FileRecord, object, object]]],
) -> None:
    """Card-level parity for the eligible/gated split -- NOT for the _build_cue_tracks rewrite.

    phaze-b4u3p review finding, recorded so this test is not read as covering more than it does:
    the "old" module's own ``from phaze.routers.cue import _build_cue_tracks`` resolves at exec
    time against the CURRENT router, whose ``_build_cue_tracks`` this change rewrote (the
    ``TracklistVersion`` + ``selectinload`` existence lookup became a direct ``TracklistTrack``
    query). So both sides of the assertion below run the NEW track-building code, and that hunk is
    compared against itself.

    The two forms are in fact equivalent -- ``tracklist_tracks.version_id`` is NOT NULL with an FK
    to ``tracklist_versions``, so a version that cannot be found can have no tracks and both forms
    return ``[]`` -- but that equivalence is argued from the schema, not demonstrated here. What
    this test does cover is the eligible/gated partition and the card payloads built around it.
    """
    for i in range(4):
        await seed_cue_set(eligible=True, original_filename=f"parity-cue-elig-{i}.mp3")
    for i in range(3):
        await seed_cue_set(eligible=False, original_filename=f"parity-cue-gated-{i}.mp3")

    old_cards = await old_review.get_cue_review_cards(session)
    new_cards = await new_review.get_cue_review_cards(session)

    old_by_id = {card["tracklist_id"]: card for card in old_cards}
    new_by_id = {card["tracklist_id"]: card for card in new_cards}
    assert len(old_by_id) == len(new_by_id) == 7
    assert old_by_id == new_by_id
    assert sum(1 for c in new_by_id.values() if c["eligible"]) == 4
    assert sum(1 for c in new_by_id.values() if not c["eligible"]) == 3


@pytest.mark.asyncio
async def test_count_cue_review_candidates_parity(
    session: AsyncSession,
    old_review: ModuleType,
    seed_cue_set: Callable[..., Awaitable[tuple[FileRecord, object, object]]],
) -> None:
    for i in range(2):
        await seed_cue_set(eligible=True, original_filename=f"parity-count-elig-{i}.mp3")
    await seed_cue_set(eligible=False, original_filename="parity-count-gated.mp3")

    old_count = await old_review.count_cue_review_candidates(session)
    new_count = await new_review.count_cue_review_candidates(session)

    assert old_count == new_count == 3


# ---------------------------------------------------------------------------
# The N+1 fix's QUERY COUNT proof -- a different claim from parity above, kept in its own test.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cue_review_cards_query_count_is_not_linear_in_eligible_tracklists(
    session: AsyncSession,
    old_review: ModuleType,
) -> None:
    """The cross-function N+1 acceptance criterion, measured directly: OLD scales with N, NEW does not.

    Five eligible tracklists: OLD calls ``_build_cue_tracks`` once per card inside the render loop
    (a query pair per tracklist, on top of the eligible-set query itself) -- NEW fetches the whole
    eligible half's cue tracks in ONE batched round-trip pair before building any card.
    """
    n = 5
    for i in range(n):
        await _seed_eligible_cue_tracklist(session, artist=f"Query Count Artist {i}")

    counting_old = _CountingSession(session)
    old_cards = await old_review.get_cue_review_cards(counting_old)  # type: ignore[arg-type]

    counting_new = _CountingSession(session)
    new_cards = await new_review.get_cue_review_cards(counting_new)  # type: ignore[arg-type]

    assert len([c for c in old_cards if c["eligible"]]) == n
    assert len([c for c in new_cards if c["eligible"]]) == n

    # OLD: linear in n (a query pair per card, plus the eligible + gated set queries).
    # Measured: n=5 -> old=12, new=4; n=20 -> old=42, new=4 -- constant vs linear, exactly the
    # shape the cross-function N+1 acceptance criterion asks to prove.
    assert counting_old.executes >= 1 + 2 * n
    # NEW: constant in n (eligible query + ONE batched track/discogs pair + gated query).
    assert counting_new.executes <= 4
    # The headline assertion: fixing the N+1 measurably drops the query count, not just moves it.
    assert counting_new.executes < counting_old.executes
