"""phaze-rmx4e (seam D3): the SQL collision detector vs the executor's ``Path.resolve()`` identity.

``services/collision.py::detect_collisions`` predicts a destination collision with a SQL string
comparison over ``(agent_id, owning_root, proposed_path || '/' || proposed_filename)`` -- no
``lower()``, ``casefold()`` or Unicode normalization anywhere in the module (see its
``_dest_key_columns``). The real consumer of its "no collision" verdict is
``tasks/execution.py::_resolve_destination`` -> ``execution_filesystem.LocalFilesystemPrimitives
.resolve_destination`` -> ``Path.resolve()``, and ultimately the filesystem itself. ``Path.resolve()``
and the OS both collapse symlinks, and on a case-insensitive and/or normalization-insensitive volume
also collapse case and NFC/NFD form -- identities the SQL byte comparison treats as distinct. Pre-fix
proxy: ``tests/discovery/services/test_collision.py`` is pure DB-row assertions and
``tests/review/routers/test_execution_dispatch.py::test_collision_short_circuits_dispatch`` only
exercises the byte-identical case; the executor was never invoked on a pair the detector cleared.

ADR-0012 (verification fidelity and operator attribution) RULE 3 decides the shape of these tests: the artifact under test is the "no collision"
verdict, and its real consumer is the filesystem move, not another read of the same SQL. Each test
below therefore runs the REAL ``detect_collisions`` against a REAL Postgres, builds the REAL dispatch
items via the REAL ``get_approved_proposals_grouped_by_agent`` (the same function
``routers/execution.py::start_execution`` uses), and hands them to the REAL
``tasks/execution.py::execute_approved_batch`` against a REAL filesystem under ``tmp_path`` -- no
mock of either half of the seam.

FILESYSTEM SEMANTICS -- READ BEFORE TRUSTING A GREEN RUN ON THIS MACHINE
=========================================================================
CLAUDE.md's own warning applies directly here: macOS APFS (this development machine's default
volume, confirmed by direct probe: ``os.path.exists()`` finds "track.mp3" after only "Track.mp3" was
created, and finds an NFD-encoded name after only its NFC form was created) is BOTH case-insensitive
AND normalization-insensitive by default. Production is stated (CLAUDE.md) to run on Linux, which is
case- and byte-sensitive for a LOCAL disk -- but the archive itself is measured elsewhere in this
inventory (row D1) to live on SMB / exFAT / CIFS mounts, which are typically case-insensitive. Which
regime a given proposal's destination actually falls in therefore depends on the MOUNT, not the
kernel, and this suite cannot see the production mount from here.

Rather than assume a regime, each test PROBES the real ``tmp_path`` volume for the specific property
(case- or normalization-sensitivity) it needs and asserts the outcome the probed regime predicts:
  * case-insensitive / normalization-insensitive (this machine's APFS default; matches a
    case-insensitive archive mount): the SECOND proposal's destination collides with the FIRST's
    published file at the OS level, so it must be refused -- ``error_count == 1`` and BOTH files'
    content survive uncorrupted (the loser's source is untouched, the winner's destination holds
    exactly one of the two contents, never a mix).
  * case-sensitive / normalization-sensitive (verified separately below to be REACHABLE on this same
    machine via a real "Case-sensitive APFS" volume for the case axis -- see
    ``test_case_only_pair_is_not_even_a_collision_on_a_real_case_sensitive_volume``; genuine byte-sensitive
    Unicode normalization is NOT reachable on any macOS-native volume -- APFS normalizes lookups for
    comparison regardless of its case-sensitivity flag, confirmed by direct probe on a
    "Case-sensitive APFS" test volume, so the NFC/NFD test's case-sensitive branch is exercised only
    when CI actually runs this file on a byte-sensitive filesystem, e.g. Linux ext4): the two
    destination strings are genuinely different directory entries, so BOTH proposals succeed
    independently -- ``error_count == 0`` and each destination holds its own file's content. This is
    not a "bug" in that regime; the two proposals were never colliding physical destinations there.

Either branch satisfies this bead's acceptance criterion ("blocked by the detector, or fails safe in
the executor with no overwrite") -- the detector never blocks either pair (asserted directly below,
against the real SQL), so the branch actually exercised on a run is always the executor's fail-safe
path. What differs by filesystem is only WHETHER that path is needed at all.

THE SYMLINK CASE IS FILESYSTEM-PERSONALITY-INDEPENDENT
========================================================
``Path.resolve()`` collapses symlink components identically on every POSIX platform this suite runs
on (both macOS and Linux). ``test_symlinked_directory_pair_the_detector_clears_the_executor_fails_safe``
therefore needs no probe and no branch: two byte-distinct ``proposed_path`` values that traverse a
symlinked directory to the SAME real directory resolve, in ``resolve_destination``, to one identical
physical path everywhere.

POPULATION (owed by this bead's acceptance criteria; NOT run here -- no production access)
=============================================================================================
This developer seat has no production database access (CLAUDE.md's beadhive dispatch rule). The two
population queries the bead specifies:

    SELECT count(*) FROM (SELECT coalesce(proposed_path,'') || '/' || proposed_filename AS k
      FROM proposals WHERE status IN ('pending','approved') GROUP BY 1 HAVING count(*) > 1) x;
    SELECT count(*) FROM (SELECT lower(normalize(coalesce(proposed_path,'') || '/' || proposed_filename, NFC)) AS k
      FROM proposals WHERE status IN ('pending','approved') GROUP BY 1 HAVING count(*) > 1) x;

were filed as a read-only request on phaze-tkkor rather than run against this seat's local (empty,
synthetic) test database, whose count would not represent the real population the bead asks for.
Symlink-collapsing collisions need an agent-side probe of the real archive mounts, which is likewise
out of reach here; noted on phaze-tkkor for the record.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING
import unicodedata
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from phaze.config import AgentSettings
from phaze.models.agent import Agent
from phaze.models.file import FileRecord
from phaze.models.proposal import ProposalStatus, RenameProposal
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload
from phaze.services.collision import detect_collisions
from phaze.services.execution_dispatch import get_approved_proposals_grouped_by_agent
from phaze.tasks.execution import execute_approved_batch


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ---------------------------------------------------------------------------
# Shared seam-test helpers
# ---------------------------------------------------------------------------


def _make_api_client_mock() -> AsyncMock:
    """A no-op reporting client -- this seam is about the filesystem outcome, not HTTP audit."""
    api = AsyncMock()
    api.post_execution_log = AsyncMock(return_value=MagicMock(execution_log_id=uuid.uuid4()))
    api.patch_execution_log = AsyncMock(return_value=None)
    api.patch_proposal_state = AsyncMock(return_value=None)
    api.post_exec_batch_progress = AsyncMock(return_value=None)
    return api


def _patch_scan_roots(monkeypatch: pytest.MonkeyPatch, roots: list[str]) -> None:
    fake_cfg = MagicMock(spec=AgentSettings)
    fake_cfg.scan_roots = roots
    monkeypatch.setattr("phaze.tasks.execution.get_settings", lambda: fake_cfg)


async def _seed_agent(session: AsyncSession, agent_id: str, scan_root: Path) -> None:
    session.add(Agent(id=agent_id, name=agent_id, kind="fileserver", scan_roots=[str(scan_root)]))
    await session.flush()


async def _seed_file_and_approved_proposal(
    session: AsyncSession,
    *,
    agent_id: str,
    source_path: Path,
    proposed_path: str,
    proposed_filename: str,
) -> None:
    file_id = uuid.uuid4()
    # The executor verifies this against the REAL file's bytes before moving it
    # (`_execute_one` -> `verify_hash_or_raise`), so it must be the file's actual hash, not a
    # fixture placeholder -- a mismatched hash here fails the move at "verify", before the
    # collision-identity logic under test ever runs.
    real_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    session.add(
        FileRecord(
            id=file_id,
            agent_id=agent_id,
            sha256_hash=real_hash,
            original_path=str(source_path),
            original_filename=source_path.name,
            current_path=str(source_path),
            file_type="music",
            file_size=source_path.stat().st_size,
        )
    )
    await session.flush()
    session.add(
        RenameProposal(
            id=uuid.uuid4(),
            file_id=file_id,
            proposed_filename=proposed_filename,
            proposed_path=proposed_path,
            confidence=0.9,
            status=ProposalStatus.APPROVED,
            context_used={},
            reason="phaze-rmx4e seam test",
        )
    )
    await session.commit()


def _fs_is_case_insensitive(directory: Path) -> bool:
    """Probe THIS directory's actual volume, not an assumption about the platform."""
    probe = directory / f"case-probe-{uuid.uuid4().hex}.tmp"
    probe.write_bytes(b"x")
    try:
        return probe.with_name(probe.name.upper()).exists()
    finally:
        probe.unlink(missing_ok=True)


def _fs_is_normalization_insensitive(directory: Path, nfc_name: str, nfd_name: str) -> bool:
    """Probe THIS directory's actual volume for NFC/NFD lookup aliasing."""
    probe = directory / nfc_name
    probe.write_bytes(b"x")
    try:
        return (directory / nfd_name).exists()
    finally:
        probe.unlink(missing_ok=True)


async def _run_batch(session: AsyncSession, agent_id: str, monkeypatch: pytest.MonkeyPatch, scan_root: Path) -> dict[str, object]:
    """Drive the REAL producer->consumer handoff: DB read -> dispatch items -> REAL executor."""
    groups = await get_approved_proposals_grouped_by_agent(session)
    items = groups[agent_id]
    assert len(items) == 2, "fixture bug: expected exactly the two seeded proposals for this agent"

    _patch_scan_roots(monkeypatch, [str(scan_root)])
    payload = ExecuteApprovedBatchPayload(batch_id=uuid.uuid4(), agent_id=agent_id, proposals=items)
    api = _make_api_client_mock()
    return await execute_approved_batch({"api_client": api}, **payload.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Case-only pair
# ---------------------------------------------------------------------------


async def test_case_only_destination_pair_the_detector_clears_the_executor_fails_safe(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two approved proposals whose destinations differ ONLY by case.

    The real SQL comparison is byte-exact (Postgres default collation), so ``detect_collisions``
    does not group ``Music/Track.mp3`` with ``Music/track.mp3`` -- confirmed against a real Postgres
    below, not assumed. The real executor must then not silently destroy either file's content.
    """
    agent_id = "srv-case-seam"
    scan_root = tmp_path / "archive"
    scan_root.mkdir()
    await _seed_agent(session, agent_id, scan_root)

    src_a = scan_root / "incoming" / "a.mp3"
    src_a.parent.mkdir(parents=True)
    src_a.write_bytes(b"CONTENT-A")
    src_b = scan_root / "incoming" / "b.mp3"
    src_b.write_bytes(b"CONTENT-B")

    await _seed_file_and_approved_proposal(session, agent_id=agent_id, source_path=src_a, proposed_path="Music", proposed_filename="Track.mp3")
    await _seed_file_and_approved_proposal(session, agent_id=agent_id, source_path=src_b, proposed_path="Music", proposed_filename="track.mp3")

    # THE REAL DETECTOR: confirm it does NOT block this pair (the proxy this bead names).
    collisions = await detect_collisions(session)
    assert collisions == [], (
        f"the SQL detector unexpectedly flagged a case-only pair as colliding: {collisions!r} "
        "-- if this starts failing, detect_collisions gained case-folding and this seam is closed "
        "from the producer side; update this test's premise rather than skip it"
    )

    result = await _run_batch(session, agent_id, monkeypatch, scan_root)

    dest_dir = scan_root / "Music"
    case_insensitive = _fs_is_case_insensitive(scan_root)

    if case_insensitive:
        # This machine's real APFS volume: "Track.mp3" and "track.mp3" are ONE directory entry.
        assert result["error_count"] == 1
        assert result["status"] == "completed_with_errors"
        surviving = list(dest_dir.iterdir())
        assert len(surviving) == 1, f"expected exactly one physical entry at {dest_dir}, found {surviving!r}"
        winner_content = surviving[0].read_bytes()
        assert winner_content in (b"CONTENT-A", b"CONTENT-B")
        if winner_content == b"CONTENT-A":
            assert not src_a.exists()
            assert src_b.exists()
            assert src_b.read_bytes() == b"CONTENT-B"
        else:
            assert not src_b.exists()
            assert src_a.exists()
            assert src_a.read_bytes() == b"CONTENT-A"
    else:
        # A genuinely case-sensitive volume: these were never colliding physical destinations.
        assert result["error_count"] == 0
        assert (dest_dir / "Track.mp3").read_bytes() == b"CONTENT-A"
        assert (dest_dir / "track.mp3").read_bytes() == b"CONTENT-B"


async def test_case_only_pair_is_not_even_a_collision_on_a_real_case_sensitive_volume(
    session: AsyncSession, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case-sensitive branch above, forced onto a REAL case-sensitive volume, not merely assumed.

    CLAUDE.md: "Don't let a test pass on macOS only because APFS masks the difference." The default
    APFS volume this suite otherwise runs on cannot exercise the case-sensitive branch at all (proven
    case-insensitive by direct probe, see module docstring), so without this test the case-sensitive
    half of the property above would NEVER actually run on this machine -- both a green run and a
    broken assertion would look identical. This test mounts a genuine, freshly-created
    "Case-sensitive APFS" disk image (macOS ``hdiutil``; a real filesystem, not a simulation) so the
    case-sensitive branch is measured, not inferred. Skipped where ``hdiutil`` is unavailable (i.e.
    not macOS) -- there, the platform's OWN native filesystem is typically already case-sensitive
    (ext4), so ``test_case_only_destination_pair_the_detector_clears_the_executor_fails_safe`` above
    already exercises this branch for real via its own probe-and-branch, with no exotic mount needed.
    """
    import shutil
    import subprocess

    if shutil.which("hdiutil") is None:
        pytest.skip("hdiutil not available -- not macOS; the native filesystem here is exercised by the sibling probe-and-branch test")

    image_dir = tmp_path_factory.mktemp("case-sensitive-dmg")
    image_path = image_dir / "phaze-rmx4e-case-sensitive.dmg"
    volume_name = f"phazermx4e{uuid.uuid4().hex[:8]}"
    create = subprocess.run(  # noqa: S603
        ["/usr/bin/hdiutil", "create", "-size", "16m", "-fs", "Case-sensitive APFS", "-volname", volume_name, str(image_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if create.returncode != 0:
        pytest.skip(f"could not create a case-sensitive APFS test volume: {create.stderr.strip()}")

    attach = subprocess.run(  # noqa: S603
        ["/usr/bin/hdiutil", "attach", str(image_path), "-nobrowse"],
        capture_output=True,
        text=True,
        check=False,
    )
    if attach.returncode != 0:
        pytest.skip(f"could not attach the case-sensitive APFS test volume: {attach.stderr.strip()}")

    mount_point = Path(f"/Volumes/{volume_name}")
    try:
        assert not _fs_is_case_insensitive(mount_point), (
            "fixture bug: the freshly-created 'Case-sensitive APFS' volume reports case-insensitive -- "
            "this test's whole premise (a REAL case-sensitive volume) does not hold on this host"
        )

        agent_id = "srv-case-seam-real-cs"
        scan_root = mount_point / "archive"
        scan_root.mkdir()
        await _seed_agent(session, agent_id, scan_root)

        src_a = scan_root / "incoming" / "a.mp3"
        src_a.parent.mkdir(parents=True)
        src_a.write_bytes(b"CONTENT-A")
        src_b = scan_root / "incoming" / "b.mp3"
        src_b.write_bytes(b"CONTENT-B")

        await _seed_file_and_approved_proposal(session, agent_id=agent_id, source_path=src_a, proposed_path="Music", proposed_filename="Track.mp3")
        await _seed_file_and_approved_proposal(session, agent_id=agent_id, source_path=src_b, proposed_path="Music", proposed_filename="track.mp3")

        collisions = await detect_collisions(session)
        assert collisions == []

        result = await _run_batch(session, agent_id, monkeypatch, scan_root)

        dest_dir = scan_root / "Music"
        assert result["error_count"] == 0
        assert result["status"] == "completed"
        assert (dest_dir / "Track.mp3").read_bytes() == b"CONTENT-A"
        assert (dest_dir / "track.mp3").read_bytes() == b"CONTENT-B"
    finally:
        subprocess.run(["/usr/bin/hdiutil", "detach", str(mount_point), "-force"], capture_output=True, check=False)  # noqa: S603


# ---------------------------------------------------------------------------
# NFC / NFD-only pair
# ---------------------------------------------------------------------------

_NFC_NAME = unicodedata.normalize("NFC", "Café Set.mp3")
_NFD_NAME = unicodedata.normalize("NFD", _NFC_NAME)


def _assert_fixture_is_actually_non_nfc() -> None:
    assert _NFD_NAME != _NFC_NAME, "fixture is degenerate: the NFD and NFC forms are byte-identical"
    assert unicodedata.is_normalized("NFC", _NFC_NAME)
    assert not unicodedata.is_normalized("NFC", _NFD_NAME)


async def test_nfc_nfd_only_destination_pair_the_detector_clears_the_executor_fails_safe(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two approved proposals whose destinations differ ONLY by Unicode normalization form.

    D2's fix (phaze-sy8z3) NFC-normalizes ``current_path`` at ingest and at the execute-path writer;
    it does not touch ``proposed_filename``/``proposed_path`` as compared HERE, at proposal time --
    the two bugs are adjacent, not the same one. The real SQL comparison is byte-exact, so an NFC and
    an NFD proposed_filename never collide in ``detect_collisions``, confirmed below.
    """
    _assert_fixture_is_actually_non_nfc()
    agent_id = "srv-nfc-seam"
    scan_root = tmp_path / "archive"
    scan_root.mkdir()
    await _seed_agent(session, agent_id, scan_root)

    src_a = scan_root / "incoming" / "a.mp3"
    src_a.parent.mkdir(parents=True)
    src_a.write_bytes(b"CONTENT-A")
    src_b = scan_root / "incoming" / "b.mp3"
    src_b.write_bytes(b"CONTENT-B")

    await _seed_file_and_approved_proposal(session, agent_id=agent_id, source_path=src_a, proposed_path="Sets", proposed_filename=_NFC_NAME)
    await _seed_file_and_approved_proposal(session, agent_id=agent_id, source_path=src_b, proposed_path="Sets", proposed_filename=_NFD_NAME)

    collisions = await detect_collisions(session)
    assert collisions == [], (
        f"the SQL detector unexpectedly flagged an NFC/NFD-only pair as colliding: {collisions!r} "
        "-- if this starts failing, detect_collisions gained Unicode normalization and this seam is "
        "closed from the producer side; update this test's premise rather than skip it"
    )

    result = await _run_batch(session, agent_id, monkeypatch, scan_root)

    dest_dir = scan_root / "Sets"
    normalization_insensitive = _fs_is_normalization_insensitive(scan_root, _NFC_NAME, _NFD_NAME)

    if normalization_insensitive:
        # This machine's real APFS volume: the NFC and NFD spellings name ONE directory entry.
        # (Measured separately, module docstring: even a case-SENSITIVE APFS volume is still
        # normalization-insensitive -- this axis cannot be forced onto any macOS-native filesystem,
        # unlike the case axis above. This branch is what runs on every macOS developer machine.)
        assert result["error_count"] == 1
        assert result["status"] == "completed_with_errors"
        surviving = list(dest_dir.iterdir())
        assert len(surviving) == 1, f"expected exactly one physical entry at {dest_dir}, found {surviving!r}"
        winner_content = surviving[0].read_bytes()
        assert winner_content in (b"CONTENT-A", b"CONTENT-B")
        if winner_content == b"CONTENT-A":
            assert not src_a.exists()
            assert src_b.exists()
            assert src_b.read_bytes() == b"CONTENT-B"
        else:
            assert not src_b.exists()
            assert src_a.exists()
            assert src_a.read_bytes() == b"CONTENT-A"
    else:
        # A genuinely byte-sensitive filesystem (e.g. Linux ext4 in CI): these were never colliding
        # physical destinations.
        assert result["error_count"] == 0
        assert (dest_dir / _NFC_NAME).read_bytes() == b"CONTENT-A"
        assert (dest_dir / _NFD_NAME).read_bytes() == b"CONTENT-B"


# ---------------------------------------------------------------------------
# Symlinked-directory pair (platform-independent: no probe needed)
# ---------------------------------------------------------------------------


async def test_symlinked_directory_pair_the_detector_clears_the_executor_fails_safe(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two approved proposals whose ``proposed_path`` values are byte-distinct but alias one real dir.

    ``resolve_destination`` -> ``resolve_and_check_containment`` calls ``Path(candidate).resolve()``,
    which collapses symlink components on every POSIX platform (no probe needed, unlike the case and
    normalization axes above). The SQL detector compares the UNRESOLVED strings
    ``"Coachella-2024/set.mp3"`` and ``"Coachella (Copy)/set.mp3"`` and cannot see the alias.
    """
    agent_id = "srv-symlink-seam"
    scan_root = tmp_path / "archive"
    scan_root.mkdir()
    real_dir = scan_root / "Coachella-2024"
    real_dir.mkdir()
    link_dir = scan_root / "Coachella (Copy)"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    await _seed_agent(session, agent_id, scan_root)

    src_a = scan_root / "incoming" / "a.mp3"
    src_a.parent.mkdir(parents=True)
    src_a.write_bytes(b"CONTENT-A")
    src_b = scan_root / "incoming" / "b.mp3"
    src_b.write_bytes(b"CONTENT-B")

    await _seed_file_and_approved_proposal(session, agent_id=agent_id, source_path=src_a, proposed_path="Coachella-2024", proposed_filename="set.mp3")
    await _seed_file_and_approved_proposal(
        session, agent_id=agent_id, source_path=src_b, proposed_path="Coachella (Copy)", proposed_filename="set.mp3"
    )

    collisions = await detect_collisions(session)
    assert collisions == [], (
        f"the SQL detector unexpectedly flagged a symlink-aliased pair as colliding: {collisions!r} "
        "-- the two proposed_path strings are genuinely different bytes, so this would mean "
        "detect_collisions started resolving symlinks, closing this half of the seam from the "
        "producer side; update this test's premise rather than skip it"
    )

    result = await _run_batch(session, agent_id, monkeypatch, scan_root)

    assert result["error_count"] == 1
    assert result["status"] == "completed_with_errors"
    surviving = list(real_dir.glob("set.mp3"))
    assert len(surviving) == 1, f"expected exactly one physical entry under {real_dir}, found {surviving!r}"
    winner_content = surviving[0].read_bytes()
    assert winner_content in (b"CONTENT-A", b"CONTENT-B")
    if winner_content == b"CONTENT-A":
        assert not src_a.exists()
        assert src_b.exists()
        assert src_b.read_bytes() == b"CONTENT-B"
    else:
        assert not src_b.exists()
        assert src_a.exists()
        assert src_a.read_bytes() == b"CONTENT-A"
    # The symlink itself is never disturbed by the move.
    assert link_dir.is_symlink()
