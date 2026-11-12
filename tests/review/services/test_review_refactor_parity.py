"""phaze-b4u3p parity evidence: the ``services/review.py`` decomposition is behavior-preserving.

Loads the PRE-refactor ``services/review.py`` straight out of git (the merge-base commit this
bead branched from) as a second, independent module object, and runs every one of its public
read functions against the SAME seeded session state the NEW (decomposed) module reads -- then
asserts the two results are equal. The old module's own imports (``from phaze.routers.cue import
_build_cue_tracks, ...`` / ``from phaze.routers.tags import (...)``) resolve through the CURRENT,
edited ``routers/cue.py`` / ``routers/tags.py`` at exec time -- which still re-export every one of
those names (see those modules' docstrings) -- so this is a genuine apples-to-apples comparison,
not a frozen snapshot compared against itself.

Parity tests below assert the RENDERED ROW DATA is byte-for-byte identical old vs new.

phaze-zimli: the CUE parity and query-count tests are retired. ADR-0027 (retire legacy timestamp
qualification) deliberately changed legacy cue behaviour, so history is no longer a valid oracle
for it, and the batched cue-track path they measured is gone. The retired names the historical blob
still imports are stood in for its exec only (``_retired_cue_names``).
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib
import importlib.util
from pathlib import Path
import subprocess
import sys
from typing import TYPE_CHECKING, NoReturn
import uuid

import pytest

from phaze.models.proposal import ProposalStatus
from phaze.models.tag_write_log import TagWriteLog, TagWriteStatus
from phaze.services import review as new_review
from tests.review.capabilities.tag_write.test_degrade import _seed_applied_tagwrite_file


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
    with _retired_cue_names():
        exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102 -- trusted, our own git history
    return module


# phaze-zimli (ADR-0027 (retire legacy timestamp qualification)) removed the legacy CUE "eligible"
# set and per-tracklist track building. The historical blobs still import those names at exec time,
# so they are injected for the exec alone and removed straight after: the old module keeps its own
# bindings, the shipped modules gain nothing. Each stand-in raises if CALLED -- the old cue
# composition treats raw legacy timestamps as eligible, which ADR-0027 (retire legacy timestamp qualification) deliberately changed, so no
# parity test compares cue behaviour against history any more.
_RETIRED_CUE_NAMES = {
    "phaze.routers.cue": ("_build_cue_tracks", "_eligible_tracklist_stmt", "_get_eligible_tracklist_query"),
    "phaze.services.cue_review": ("build_cue_tracks_for_versions", "eligible_tracklist_stmt", "get_eligible_tracklist_query"),
}


def _retired(name: str) -> Callable[..., NoReturn]:
    def _call(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError(f"{name} was retired by ADR-0027 (retire legacy timestamp qualification); a parity test reached the historical cue path")

    return _call


@contextmanager
def _retired_cue_names() -> Iterator[None]:
    injected: list[tuple[ModuleType, str]] = []
    try:
        for module_name, names in _RETIRED_CUE_NAMES.items():
            target = importlib.import_module(module_name)
            for name in names:
                if not hasattr(target, name):
                    setattr(target, name, _retired(name))
                    injected.append((target, name))
        yield
    finally:
        for target, name in injected:
            delattr(target, name)


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


# Deliberate key renames since the pinned pre-refactor SHA (phaze-n8o9p)
#
# These row dicts are compared with a raw ``==``, which is what gives this module its teeth: any
# divergence at all, in any key or value, fails. The cost is that it also cannot tell a REGRESSION
# from a rename the repo made on purpose, and phaze-n8o9p made one -- ``original_path`` ->
# ``current_path`` in ``_build_changes_review_row``. The VALUE is untouched on both sides
# (``proposal.file.current_path``); only the key's spelling moved, because the old name asserted
# the ingest path while carrying the current one. The Propose-only copy was removed by phaze-ogcrr
# and is handled by ``_REMOVED_SINCE_PRE_REFACTOR`` below.
#
# So the old module's rows are re-keyed through exactly this mapping before the comparison, rather
# than weakening the assertion to "same values, any keys". Every other difference -- a changed
# value, a dropped key, an added one -- still fails loudly, which is the property worth keeping.
# Add to this dict ONLY for a rename that is deliberate, value-preserving, and recorded on a bead.
_RENAMED_SINCE_PRE_REFACTOR = {"original_path": "current_path"}

# phaze-ogcrr: the Propose row's `current_path` key was the former tooltip reader's only
# consumer. The live template now uses `filename`, so remove that one obsolete key from the OLD
# side before comparing the still-byte-identical row shape. This is deliberately separate from the
# rename map: a dropped key is a named contract change, not a spelling change.
_REMOVED_SINCE_PRE_REFACTOR = {"current_path"}

# phaze-x1qr3.9: a KEY the pre-refactor snapshot structurally cannot carry -- it predates the
# ``SetProfile`` model and the row builders' ``set_profile`` field entirely, so no rename mapping
# could ever make the two sides agree on it. Handled the same way as ``_RENAMED_SINCE_PRE_REFACTOR``
# above and for the same reason: a named, auditable exception that keeps the raw ``==`` teeth on
# every OTHER key, rather than weakening the assertion generally. Both call sites below assert the
# NEW side actually carries this key (with the value the seeded fixtures make correct -- ``None``,
# since neither seeds a ``SetProfile`` row) before stripping it for the equality comparison, so an
# accidental drop of the field still fails loudly. Add to this set ONLY for a field that is new,
# additive, and recorded on a bead -- never to paper over a real divergence.
# phaze-vssr1 separates generated confidence warnings from other reasons for live slider copy.
_ADDED_SINCE_PRE_REFACTOR = {"set_profile", "other_warnings"}


def _rekey(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """The pre-refactor rows, with deliberately-renamed keys spelled the way the new module spells them."""
    return [{_RENAMED_SINCE_PRE_REFACTOR.get(key, key): value for key, value in row.items()} for row in rows]


def _strip_removed(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """The old module's rows with the explicitly-removed Propose-only key omitted."""
    return [{key: value for key, value in row.items() if key not in _REMOVED_SINCE_PRE_REFACTOR} for row in rows]


def _strip_added(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """The new module's rows, with post-refactor-added keys removed for the parity comparison."""
    return [{key: value for key, value in row.items() if key not in _ADDED_SINCE_PRE_REFACTOR} for row in rows]


# Changes Review / Propose / Rename-Move parity


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
    # phaze-x1qr3.9: the new side carries `set_profile` -- none of these seeded proposals' files
    # have one, so it must be None on every row, never silently dropped.
    assert all(row["set_profile"] is None for row in new_page.rows)
    assert all(row["other_warnings"] == ["No destination change; the file will be renamed in its current directory."] for row in new_page.rows)
    assert _rekey(old_page.rows) == _strip_added(new_page.rows)


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

    assert _strip_removed(_rekey(old_result.rows)) == new_result.rows
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
    assert _strip_removed(_rekey(old_page.rows)) == new_page.rows


def test_proposal_row_parity_still_rejects_changed_values() -> None:
    """Removing one named key must not weaken the raw value-equality parity guard."""
    old_rows = [{"id": 1, "original_path": "/old/path", "filename": "same.mp3"}]
    new_rows = [{"id": 1, "filename": "same.mp3"}]

    assert _strip_removed(_rekey(old_rows)) == new_rows

    mutated = [{**new_rows[0], "filename": "changed.mp3"}]
    with pytest.raises(AssertionError):
        assert _strip_removed(_rekey(old_rows)) == mutated


# Tag-write review parity


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
    # phaze-x1qr3.9: the new side carries `set_profile` -- neither seeded file has one, so it must
    # be None on every row, never silently dropped.
    assert all(row["set_profile"] is None for row in new_page.rows)
    assert old_by_id == {file_id: {k: v for k, v in row.items() if k not in _ADDED_SINCE_PRE_REFACTOR} for file_id, row in new_by_id.items()}


# Dedupe parity


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
    # phaze-6ak0q: ``strictly_better`` is a KEY the pinned snapshot cannot carry (the badge's tie-vs-win
    # state is new, additive, and recorded on that bead). Assert the new side carries it, then strip it
    # so the raw ``==`` keeps its teeth on every other key.
    assert all("strictly_better" in group for group in new_groups)
    new_groups = [{k: v for k, v in group.items() if k != "strictly_better"} for group in new_groups]

    assert old_groups == new_groups
