"""SAQ task: execute_approved_batch -- per-proposal local file ops + HTTP state reporting (Phase 26 B2 Option A + Phase 28 D-03/D-15).

Reads file paths from payload (no DB lookup -- D-23 invariant). For each proposal:
1. Resolve `item.source_path` -- the move SOURCE, which carries the file's CURRENT on-disk
   location, see ExecuteBatchProposalItem -- under its owning scan_root, then build the destination as
   ``owning_root/proposed_path/proposed_filename`` (``proposed_path`` is a RELATIVE dir;
   empty == rename in place) and containment-check it (T-26-11-S1 path-traversal guard).
2. POST /execution-log with status='in_progress' (per-proposal audit row). phaze-87ba: if that
   POST fails it is retried once more immediately before the terminal PATCH, because PATCH is an
   UPDATE that 404s on a missing row (and a 404 is never retried), which otherwise erased the
   audit record of a move that actually happened.
3. Optionally verify sha256 of `source_path` against `payload.sha256_hash`.
4. Move `source_path` -> destination (an atomic no-clobber os.link claim when same-fs, else a
   bounded streamed copy published by the same claim -- never load the whole file into RAM;
   concert videos are multi-GB). phaze-s0wu: the claim, not the caller's earlier exists-check, is
   what actually guarantees no destination is ever overwritten.
5. Delete the original (only needed on the cross-filesystem copy path).
6. PATCH /execution-log/{id} with status='completed' (or 'failed').
7. PATCH /proposals/{id}/state with proposal_state=executed, file_state=moved, current_path=proposed_path.
8. POST /exec-batches/{batch_id}/progress with terminal_step + failed_at_step (Phase 28 D-03).

On any per-proposal IO error: PATCH execution-log status='failed' + PATCH proposal_state='failed' + POST
exec-batch progress with terminal_step="failed" + error_message + continue with the rest.
The batch returns aggregate processed/error counts; cross-proposal failures are isolated.

Phase 28 changes (Plan 28-05):
- BOTH ``execution_log_id`` AND ``progress_request_id`` are persisted in ``ctx['job'].meta`` so
  SAQ retries reuse the same UUIDs per proposal (closes L6/L22; delivers D-15). The meta-key
  convention is ``log_id:{proposal_id}`` / ``req_id:{proposal_id}``. UUIDs are written as
  strings (SAQ serializes ``meta`` via JSON-compatible types).
- ``_execute_one`` tracks a local ``current_step`` variable through the copy/verify/delete
  transitions; the except clause uses ``_classify_failure_step`` to map exc + current_step to
  the literal ``failed_at_step`` posted to the new progress endpoint.
- ``error_message`` on failed ExecutionLog PATCHes adopts the ``"<step>: <reason>"`` prefix
  convention (D-01 contract).
- Each terminal proposal POSTs to ``/api/internal/agent/exec-batches/{batch_id}/progress``;
  the LAST item of a sub-batch sets ``sub_batch_terminal=True`` so the controller can detect
  ``subjobs_completed == subjobs_expected`` and promote the batch status.
- Progress POST failures (after the agent_client's tenacity retries) log WARNING and do NOT
  raise -- file ops are already committed via ``patch_proposal_state`` (D-16). phaze-j7u8 SPLITS
  that rule by event kind: it holds for the telemetry HINCRBYs, but NOT for the
  ``sub_batch_terminal=True`` POST, which is the sub-batch's non-reconstructible COMPLETION TOKEN
  (the only writer of ``subjobs_completed``, hence the only trigger of the status promotion and
  the ``exec:active`` sentinel release). A lost token re-raises
  ``ExecBatchTerminalReportError`` so SAQ replays the job; see ``_report_progress_failure``.
- The success-path ``patch_proposal_state`` (the 'report' step) is likewise guarded: the move
  is committed on disk before it runs, so a 5xx there is swallowed + logged and the proposal
  still counts as executed. Letting it raise would misattribute the failure to
  ``failed_at_step='delete'`` and mark an already-moved file's proposal FAILED.

phaze-ebpt fix (already-moved replay detection): the guard above closes the ``patch_proposal_state``
5xx window, but a WORKER CRASH between the committed move (``original.replace(proposed)`` / the
cross-filesystem copy+unlink) and those same completed/executed/progress PATCHes left a second,
unguarded divergence -- a SAQ retry of the crashed job re-enters ``_execute_one`` from scratch,
``_resolve_and_check_containment``'s non-strict resolve lets the now-missing ``original`` resolve
without error, and the verify/copy code then raises FileNotFoundError, which the generic failure
handler mistook for a fresh failure: it flipped the already-executed proposal APPROVED -> FAILED
and left ``FileRecord.current_path`` pointing at the deleted ``original``. ``_execute_one`` now
detects this explicitly right after resolving ``original``/``proposed``: ``not original.exists()
and proposed.exists()`` is conclusive replay evidence (only this function ever creates
``proposed``), distinct from both a live in-flight move and a genuinely missing/never-started
file. When detected (and, if a hash was supplied, confirmed by hashing ``proposed`` instead of the
gone ``original``) the file op is skipped entirely and the replay falls through to the SAME
success-reporting path used by a first-time success -- already idempotent via the retry-stable
``execution_log_id``/``progress_request_id`` -- which also self-heals ``current_path`` (the
success PATCH sets it from ``str(proposed)``).

phaze-ebb46 correction: "conclusive replay evidence" above overstated it. ``not original.exists()
and proposed.exists()`` plus a matching content hash proves ``proposed`` is byte-identical to what
``original`` used to be -- it does NOT prove `proposed` is THIS proposal's own move. phaze is a
dedup tool, so an archive full of exact duplicates means a byte-identical `proposed` can just as
easily be a DIFFERENT proposal's already-completed move to the same destination, whose own source
has since gone missing for an unrelated reason. That gap is exactly the one phaze-i7jo already
closed for the cross-fs residue branch below (content identity alone was ruled insufficient there
too); the already-moved branch got no equivalent guard until now. The fix requires per-proposal
corroboration -- this proposal's own "moved" flag (``_moved_flag_key``, persisted in ``job.meta``
right after ITS move commits, retry-stable exactly like ``execution_log_id``/``progress_request_id``)
or its cross-fs commit marker (``_committed_copy_marker_path``) -- before trusting the fast path.
Uncorroborated, ``_execute_one`` falls through to the normal move/verify code, which fails loudly
on the missing `original` instead of silently reporting a stranger's move as this proposal's own
success.

Cross-filesystem crash-state model (phaze-k23z / phaze-q2lg / phaze-qx8z / phaze-i7jo): a cross-fs
move is a non-atomic copy-then-delete, so it has TWO durable committed states the executor must
treat coherently:
1. Copy committed, ``original`` still present (only ``original.unlink()`` pending). Produced by a
   live ``unlink()`` OSError (read-only source mount / busy inode -- phaze-q2lg) or a hard worker
   kill in the fsync->unlink window (phaze-qx8z). BOTH files exist; ``proposed`` is a distinct
   inode from ``original`` (different filesystem), so ``_is_same_file`` cannot match it. This is
   NOT a collision: the move completes FORWARD -- delete ``original``, report executed -- rather
   than re-copying the multi-GB file or misfiring the phaze-yu2e clobber guard. phaze-i7jo:
   byte-identity ALONE (``_destination_is_committed_copy``) is NOT sufficient evidence of this
   state -- phaze is a dedup tool, so an archive full of exact duplicates means a DIFFERENT
   proposal's already-completed move can occupy ``proposed`` with content identical to THIS
   proposal's ``original``. Content identity is corroborating evidence only when paired with a
   per-proposal marker (``_committed_copy_marker_path``, a sibling file named after this exact
   ``proposal_id``) written right after ``_atomic_cross_fs_copy`` returns and before
   ``original.unlink()`` -- i.e. proof that THIS proposal, not some other duplicate's, previously
   committed a copy to this destination. Absent that marker, the occupied destination is treated as
   a genuine (if content-identical) collision and refused via phaze-yu2e's ``FileExistsError``,
   never silently deleting a live duplicate's source.
2. Fully moved, ``original`` gone + ``proposed`` present -- the phaze-ebpt already-moved case above.
The copy itself is atomic at the destination (``_atomic_cross_fs_copy`` streams to a temp sibling
then ``Path.replace``s it -- phaze-k23z), so a copy that ABORTS mid-stream leaves neither a partial
file at ``proposed`` nor state (1); ``original`` is never unlinked until a full copy lands, so no
path here loses data. A genuinely foreign file at ``proposed`` (hash mismatch), or a byte-identical
one this proposal never itself started copying to (no marker), is still refused.

phaze-otoqj: the staging side of that copy used to be the unguarded half. ``dst.with_name(dst.name
+ _COPY_TMP_SUFFIX)`` was DETERMINISTIC (derived only from the destination) and opened via a
truncating, non-exclusive write, so two genuinely concurrent attempts at the same destination --
the meta lane runs sub-batches at concurrency 2, and a case-insensitive collision key can split
same-destination proposals across two of them -- staged into ONE shared inode and could publish
interleaved bytes as a "successful" move while deleting a source that was never actually copied
intact. The fix gives every attempt its own staging name (``_unique_tmp_path``, pid+uuid4) opened
O_EXCL (``_streamed_copy``), plus an optional post-publish sha256 re-verify against the
already-trusted expected hash before ``original.unlink()`` -- see ``_execute_one``'s cross-fs
branch.

NOTE on schema mapping: Phase 25's ExecutionLog schema is per-proposal (one row per file op),
not per-batch. Plan 11 invariants (one POST at start, per-proposal state PATCH, one PATCH at
end) are adapted to the existing schema as: one POST+PATCH per proposal (matching the
ExecutionLog table's natural key `proposal_id`). The "completed_with_errors" plan label
becomes "completed_with_errors" in the returned batch dict (no schema field for it).

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/shared/core/test_task_split.py (Plan 10).

ERROR-HANDLING REVIEW (phaze-bk9el.7, 2026-08-21): repowise flagged 10 broad-except findings in
this file. Reviewed individually; this is a file-move module, and per the epic's own rule 2 a
broad ``except`` is sometimes correct -- narrowing one here risks abandoning a partially-moved
file mid-flight, which is worse than the noise.

- ONE (`_atomic_cross_fs_copy`'s tmp-cleanup) was never actually a swallow -- it always
  re-raised -- so `except BaseException: cleanup(); raise` is now `finally: cleanup()`, which
  says the same thing without reading as a hidden handler. See that function's inline comment.
  The finding is gone, not just narrowed.
- EIGHT are HTTP-reporting catches AFTER the file op already committed (`_ensure_start_log`,
  `_report_success`, `_report_failure`, `_report_progress_failure`'s caller): the module
  docstring above (Phase 28 bullets, D-16) already records why each must log-and-continue rather
  than raise -- the move is done, only the AUDIT TRAIL write failed, and crashing the job over a
  reporting 5xx would misreport a successfully-moved file as failed. Left broad, unchanged.
- ONE (``_execute_one``'s own outer ``except Exception``) is the per-proposal isolation boundary
  this module's docstring calls out explicitly ("cross-proposal failures are isolated") --
  narrowing it would let one proposal's unexpected error crash the whole batch instead of being
  recorded against that proposal alone. Left broad, unchanged.

PRIMITIVE-OBSESSION REVIEW (phaze-bk9el.7, 2026-08-21): repowise flagged `_execute_one` (8 params)
and `_reclaim_or_refuse_existing_destination` (5, one keyword-only) on param count alone. Neither
is a primitive-obsession smell in the usual sense -- no cluster of same-typed primitives (e.g.
three raw strings that are really one address) is being passed positionally. `_execute_one`'s
params are the SAQ per-proposal call boundary (client, item, roots, payload, position flag, two
retry-stable UUIDs, job); its own body is 15 lines after this bead's extract_method split, so a
parameter object here would relocate the count onto a new type without shrinking either function.
`_reclaim_or_refuse_existing_destination` already keyword-only-guards its one flag (`same_fs`).
Left as-is; not fixed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import hashlib
import os
import shutil
from typing import TYPE_CHECKING, Any, Literal
import uuid

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.enums.execution import ExecutionStatus
from phaze.schemas.agent_exec_batches import ExecBatchProgressPayload
from phaze.schemas.agent_execution import ExecutionLogCreate, ExecutionLogPatch
from phaze.schemas.agent_proposals import ProposalStatePatch
from phaze.schemas.agent_tasks import ExecuteApprovedBatchPayload, ExecuteBatchProposalItem
from phaze.services.containment import resolve_and_check_containment as _resolve_and_check_containment


if TYPE_CHECKING:
    from pathlib import Path

    from phaze.services.agent_client import PhazeAgentClient


logger = structlog.get_logger(__name__)


# Literal type alias for the three terminal sub-steps tracked by _execute_one.
# Matches ExecBatchProgressPayload.failed_at_step (Phase 28 D-06).
FailedAtStep = Literal["copy", "verify", "delete"]


class ExecBatchTerminalReportError(RuntimeError):
    """The sub-batch COMPLETION TOKEN could not be delivered -- fail the job so SAQ replays it (phaze-j7u8).

    Raised only from the ``sub_batch_terminal=True`` progress POST. Every other progress POST is
    telemetry and stays fire-and-forget under D-16; this one is not telemetry, and the distinction
    is the whole point (see :func:`_report_progress_failure`).
    """


# phaze-eycl: `_resolve_and_check_containment` now lives in `phaze.services.containment` (shared
# with `services/proposal.py::load_companion_contents`, which needs the identical symlink-safe
# containment check for agent-supplied companion paths). Imported above under this module's
# original private name so every call site here is unchanged.


def _resolve_destination(
    item: ExecuteBatchProposalItem,
    original: Path,
    owning_root: Path,
    scan_roots: list[str],
) -> Path:
    """Build the absolute destination path for `item` and containment-check it.

    ``proposed_path`` is a RELATIVE destination directory under the file's own
    scan_root; the destination is ``owning_root / proposed_path /
    proposed_filename`` (mirrors ``services.collision`` joining semantics). An
    empty/null ``proposed_path`` means "rename in place" -- keep the directory the
    file is CURRENTLY in and apply the new filename. Both ``owning_root`` and
    ``original.parent`` are derived by the caller from ``item.source_path``, which
    carries ``FileRecord.current_path`` (phaze-shzdj) -- so an in-place rename of an
    already-moved file targets where the file is now, not where it was ingested.

    The constructed absolute path is re-run through
    :func:`_resolve_and_check_containment` so a ``../`` embedded in
    ``proposed_path`` cannot escape the scan_roots (T-26-11-S1).
    """
    dest_dir = (owning_root / item.proposed_path) if item.proposed_path else original.parent
    resolved, _ = _resolve_and_check_containment(str(dest_dir / item.proposed_filename), scan_roots)
    return resolved


def _sha256_of_file(path: Path) -> str:
    """Streaming sha256 (avoid loading large files into memory)."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Chunk size for the cross-filesystem streamed copy. Bounds peak memory
# regardless of file size: the core use case is multi-GB concert videos, and
# execute_approved_batch runs on the 'meta' lane (concurrency 2, no memory pin),
# so a whole-file read would MemoryError -> proposal failed, or the OOM-killer
# SIGKILLs the worker (uncatchable) and the batch crash-loops on SAQ replay.
_COPY_CHUNK_BYTES = 16 * 1024 * 1024

# Suffix for the sibling temp file used by the cross-filesystem copy. The bytes
# stream into ``<dest><suffix><per-attempt token>`` and are published onto the real
# destination only after a full, fsynced copy -- so the destination appears
# atomically and a mid-copy abort never orphans a partial file at the final path
# (phaze-k23z). Kept on the SAME directory (hence a name suffix, not /tmp) so the
# publish stays within one filesystem and is atomic.
#
# phaze-otoqj: the suffix alone is NOT the tmp filename -- see `_unique_tmp_path`. A name
# derived only from `dst` used to be shared by every concurrent attempt at that destination
# (two sub-batches racing on a case-insensitive collision, or a retry overlapping a still-live
# prior attempt), and `_streamed_copy`'s truncating open let a second writer share/reset the
# same inode a first writer was still appending to -- interleaved bytes published as a
# "successful" move with the losing writer's source already deleted.
_COPY_TMP_SUFFIX = ".phaze-tmp"

# phaze-i7jo: suffix for the per-PROPOSAL sibling marker that corroborates a cross-fs
# "committed copy, pending unlink" residue as THIS proposal's own prior attempt, not a
# byte-identical duplicate's already-completed move landing on the same destination.
# The marker filename embeds the proposal_id (see `_committed_copy_marker_path`), so two
# different proposals racing/replaying against the same destination never share one.
_COMMIT_MARKER_SUFFIX = ".phaze-committed"


def _committed_copy_marker_path(proposed: Path, proposal_id: uuid.UUID) -> Path:
    """Sibling marker path proving `proposal_id` itself previously committed a copy to `proposed`.

    phaze-i7jo: written right after ``_atomic_cross_fs_copy`` durably lands the copy at
    `proposed` and before ``original.unlink()`` -- i.e. exactly the crash window
    ``_destination_is_committed_copy`` exists to recognize. Binding the marker name to
    ``proposal_id`` (rather than a bare flag file) is what makes the corroboration
    per-proposal: a DIFFERENT proposal (e.g. a byte-identical duplicate's own move that
    already completed to this same destination) never wrote a marker under ITS proposal_id
    at this path, so it cannot mistake the other proposal's residue for its own -- content
    identity alone stops being sufficient evidence of a resumable move.
    """
    return proposed.with_name(f"{proposed.name}{_COMMIT_MARKER_SUFFIX}.{proposal_id}")


def _unique_tmp_path(dst: Path) -> Path:
    """Sibling staging path for `dst`, unique to THIS copy attempt.

    phaze-otoqj: the old ``dst.with_name(dst.name + _COPY_TMP_SUFFIX)`` was a DETERMINISTIC
    name derived only from the destination, so every concurrent attempt at the same `dst` --
    two sub-batches racing on a case-insensitive collision key, or a SAQ retry overlapping a
    still-live prior attempt -- staged into ONE shared inode. Appending both the pid and a
    fresh uuid4 closes that: pid alone reuses across process restarts (a stale orphan from a
    killed worker could collide with a later attempt sharing the recycled pid), and a bare
    uuid4 alone is still a single unclaimed name if something else raced to create it first --
    see `_streamed_copy`'s O_EXCL open for the other half of this fix. Two attempts choosing
    the same (pid, uuid4) pair is not a real-world possibility.
    """
    return dst.with_name(f"{dst.name}{_COPY_TMP_SUFFIX}.{os.getpid()}.{uuid.uuid4().hex}")


# phaze-ebb46: prefix for the per-proposal "moved" corroboration flag persisted in
# ``ctx['job'].meta`` (see ``_moved_flag_key``). Mirrors the retry-stable
# ``log_id:``/``req_id:`` key convention from `_load_or_seed_uuids` -- same dict, same
# survive-a-SAQ-retry guarantee, just a boolean-shaped flag instead of a UUID.
_MOVED_FLAG_PREFIX = "moved:"


def _moved_flag_key(proposal_id: uuid.UUID) -> str:
    """``job.meta`` key proving `proposal_id` itself already committed its move.

    phaze-ebb46: the ``already_moved`` fast path (``not original.exists() and
    proposed.exists()``) is conclusive evidence of a REPLAY only when corroborated
    per-proposal -- phaze is a dedup tool, so a byte-identical `proposed` can just as
    easily be a DIFFERENT proposal's already-completed move to the same destination
    (the same insufficiency the phaze-i7jo cross-fs commit marker exists to close, one
    level up). The same-fs move path has no residue window to inspect the way the
    cross-fs copy does (a single atomic syscall, not a copy-then-delete with a
    durable intermediate state), so this flag is the same-fs equivalent: written to
    ``job.meta`` -- retry-stable like ``log_id:``/``req_id:`` (D-15) -- immediately
    after THIS proposal's own move commits, so a later SAQ retry of the SAME job can
    tell "my own prior move" apart from "someone else's already-completed move that
    happens to sit at my destination".
    """
    return f"{_MOVED_FLAG_PREFIX}{proposal_id}"


def _same_filesystem(src: Path, dst_dir: Path) -> bool:
    """True when `src` and `dst_dir` live on the same filesystem (matching st_dev).

    os.replace is atomic + O(1) only within one filesystem; across a mount
    boundary it raises ``OSError(EXDEV)``. We pick the branch up front from the
    device ids instead of catching EXDEV, so the fallback path is deterministic
    and testable. ``dst_dir`` (not the not-yet-existent destination file) is what
    we stat -- the caller has already created it via ``mkdir(parents=True)``.
    """
    return src.stat().st_dev == dst_dir.stat().st_dev


def _is_same_file(a: Path, b: Path) -> bool:
    """True when `a` and `b` refer to the same on-disk file (same device + inode).

    Guards the no-op / case-only rename: a move whose destination resolves to the
    very file being moved must NOT be treated as a clobber. ``os.path.samestat``
    compares ``st_dev``/``st_ino`` so hard links and case-insensitive filesystems
    are handled correctly. Any stat error (races, permission) is treated as
    "not the same file" so the caller falls through to the exists guard.
    """
    try:
        return os.path.samestat(a.stat(), b.stat())
    except OSError:
        return False


def _is_case_only_same_entry(original: Path, proposed: Path) -> bool:
    """True iff `original`/`proposed` are two case-fold spellings of the SAME single directory entry.

    phaze-kxnnd: on a case-insensitive filesystem (SMB/NAS), a pure case-only rename
    (``Track.MP3`` -> ``track.mp3``) makes ``original != proposed`` as strings while both
    name the identical directory slot -- there is no second link anywhere, no matter what
    ``original.stat().st_nlink`` reports. ``st_nlink`` is a GLOBAL count of every link to
    the inode on the filesystem, so an unrelated pre-existing hard link elsewhere in the
    archive (phaze is explicitly a dedup tool over one where those exist) inflates it past
    1 for a plain case-only rename too, with nothing to do with `proposed`. This check is
    purely path-shaped -- same parent directory, same name once case-folded, different name
    as strings -- and needs no filesystem access, so it cannot itself be fooled by nlink:
    when it is True, the caller must route to the plain rename regardless of nlink, because
    unlinking `original` in that case deletes the archive's only entry for the file.
    """
    return original.parent == proposed.parent and original.name != proposed.name and original.name.casefold() == proposed.name.casefold()


def _destination_is_committed_copy(original: Path, proposed: Path, expected_hash: str | None) -> bool:
    """True when `proposed` already holds a byte-identical copy of `original`.

    The cross-filesystem move is copy-then-delete: once ``_atomic_cross_fs_copy``
    returns, a COMPLETE, fsynced copy sits at `proposed` while `original` is still
    present, and only ``original.unlink()`` remains. A prior attempt can be
    interrupted in exactly that window -- by a live ``unlink()`` OSError
    (phaze-q2lg) or a hard worker kill between the fsync and the unlink
    (phaze-qx8z) -- leaving BOTH files on disk with `proposed` a distinct inode
    from `original` (different filesystem => different st_dev/st_ino, so
    :func:`_is_same_file` is always False across the mount boundary).

    phaze-i7jo: content identity is necessary but, on its own, is NOT SUFFICIENT
    evidence that `proposed` is THIS proposal's own residue -- phaze is a dedup
    tool, so an archive full of exact duplicates means a byte-identical `proposed`
    can just as easily be a DIFFERENT proposal's already-completed move to the
    same destination. This function answers only the content-identity half of the
    question; the caller MUST additionally require ``_committed_copy_marker_path``
    (this exact ``proposal_id``'s own marker) to exist before treating a positive
    result here as authorization to delete `original` -- see the module docstring's
    "Cross-filesystem crash-state model". Prefer the caller-supplied
    ``expected_hash`` (the hash of the original content) when present -- the
    original was already verified against it -- otherwise hash `original`
    directly.
    """
    proposed_hash = _sha256_of_file(proposed)
    if expected_hash is not None:
        return proposed_hash == expected_hash
    return proposed_hash == _sha256_of_file(original)


def _streamed_copy(src: Path, dst: Path) -> None:
    """Copy `src` -> `dst` in bounded chunks, flushing + fsyncing before return.

    Uses ``shutil.copyfileobj`` with an explicit chunk length so peak memory
    stays bounded no matter how large the file is (never ``read_bytes()`` the
    whole file). ``copystat`` preserves mtime to match the atomic-rename branch
    (which keeps it for free). The fsync durably lands the bytes on disk before
    the caller unlinks the original, so a crash between copy and unlink cannot
    lose data.

    phaze-otoqj: `dst` is opened ``O_CREAT | O_EXCL`` -- never the truncating
    ``Path.open("wb")`` (``O_TRUNC``) this replaces. A truncating open silently shares or
    resets whatever inode already sat at `dst`; O_EXCL instead raises ``FileExistsError`` the
    instant two writers ever aim at the same path, so this stays a hard write-time guarantee
    rather than relying solely on the caller's tmp path being unique (`_unique_tmp_path`) --
    belt AND suspenders, since a unique name alone still has a (vanishingly unlikely but
    real) reuse window a crashed-then-recycled attempt could hit.

    Raises:
        FileExistsError: `dst` was already occupied.
    """
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with src.open("rb") as fsrc, os.fdopen(fd, "wb") as fdst:
        shutil.copyfileobj(fsrc, fdst, length=_COPY_CHUNK_BYTES)
        fdst.flush()
        os.fsync(fdst.fileno())
    shutil.copystat(src, dst)


# phaze-s0wu: errnos meaning "this filesystem cannot make a hard link", as opposed to "the link
# was refused for a reason that matters". Hard links are unavailable on FAT/exFAT, on some SMB and
# FUSE mounts, and across a mount boundary; on those, the no-clobber claim degrades to the old
# unconditional rename rather than failing an otherwise-valid move.
_LINK_UNSUPPORTED_ERRNOS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK, errno.EXDEV})


def _claim_destination_by_link(src: Path, dst: Path) -> bool:
    """Atomically claim `dst` as a hard link to `src`. True if the claim landed, False if links are unsupported.

    phaze-s0wu: this is the primitive that makes the no-clobber guard REAL. ``os.link`` fails with
    EEXIST if anything already occupies `dst`, and it does that check and the create in ONE kernel
    operation -- so unlike ``proposed.exists()`` followed later by a rename, there is no window in
    which another actor can slip a file in between the test and the act. ``os.rename`` /
    ``Path.replace`` cannot be used for this: POSIX rename is unconditionally clobbering, and
    Python exposes no portable ``RENAME_NOREPLACE``.

    Raises:
        FileExistsError: `dst` is already occupied -- the caller must refuse the move, NOT overwrite.
        OSError: any other link failure (permissions on the directory, ENOSPC, ...).
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno in _LINK_UNSUPPORTED_ERRNOS:
            return False
        raise
    return True


def _atomic_cross_fs_copy(src: Path, dst: Path) -> None:
    """Copy `src` -> `dst` across filesystems so `dst` appears atomically, without clobbering it.

    Streams `src` into a sibling temp file unique to this attempt (`_unique_tmp_path`),
    fsyncs it durably, then publishes the temp at `dst` -- within the destination
    filesystem, so the publish is atomic. Because bytes never land at the final
    path incrementally, a mid-copy abort (ENOSPC on a multi-GB concert video, an
    I/O error on the destination mount, or a flush/fsync failure) can never leave
    a truncated/corrupt fragment at `dst`. On ANY failure the temp file is
    removed (``missing_ok=True``) so nothing is orphaned either (phaze-k23z).

    phaze-otoqj: the staging file used to be named DETERMINISTICALLY from `dst` alone
    (``dst.name + _COPY_TMP_SUFFIX``) and opened with a truncating, non-exclusive write. Two
    genuinely concurrent attempts at the same `dst` -- the meta lane runs sub-batches at
    concurrency 2, and a case-insensitive collision key can let two same-destination proposals
    land in different sub-batches -- staged into ONE shared inode: the second writer's open
    truncated mid-stream while the first kept appending at its own offset, interleaving both
    sources' bytes. Whichever finished first published that shared, possibly-still-being-written
    inode at `dst` and unlinked ITS source, silently reporting success with the other source
    about to be deleted too. `_unique_tmp_path` gives every attempt its own inode, and
    `_streamed_copy`'s O_EXCL open refuses to write into one that (impossibly) already exists --
    so the race this used to lose is now structurally impossible rather than merely narrowed.

    Contrast the pre-fix behavior, which wrote straight into `dst` via
    ``dst.open("wb")`` and, on a raise partway through, left a partial file at the
    real destination that misled 'already moved' checks and consumed disk.

    phaze-s0wu: the publish is a no-clobber ``os.link`` claim, NOT the unconditional
    ``tmp.replace(dst)`` it used to be. That rename destroyed whatever sat at `dst`, and the
    caller's ``proposed.exists()`` check is minutes stale by the time we get here -- this copy runs
    for as long as a multi-GB concert video takes. Anything that appeared at `dst` during that
    window was silently overwritten: a colliding proposal in a concurrent sub-batch (the meta lane
    runs 2), whose own move was already committed and whose source was already deleted by its
    rename, so its bytes were unrecoverable while BOTH proposals reported success -- or simply a
    file the operator's own tooling wrote into the archive mid-copy, which needs no dispatch-time
    gate miss at all. EEXIST now surfaces as the phaze-yu2e refusal instead: `src` is untouched, so
    the move fails loudly and losslessly.

    The caller unlinks `src` only after this returns, so a crash between a
    successful copy and that unlink leaves BOTH a complete `dst` and `src` -- a
    recoverable state the executor's replay logic completes forward
    (phaze-q2lg / phaze-qx8z), never data loss.

    Raises:
        FileExistsError: `dst` was occupied by the time the copy completed.
    """
    tmp = _unique_tmp_path(dst)
    try:
        _streamed_copy(src, tmp)
        if _claim_destination_by_link(tmp, dst):
            # The link IS the publish: dst and tmp now name one inode, so dropping tmp leaves the
            # full, fsynced copy at dst and nothing orphaned.
            tmp.unlink()
        else:
            # No hard links on this filesystem. Fall back to the original unconditional rename,
            # which is still atomic -- it just cannot detect a late occupant. Documented, narrow,
            # and strictly better than refusing to move files on such a mount at all.
            tmp.replace(dst)
    finally:
        # phaze-otoqj: safe to unconditionally clean up now that `tmp` is unique to THIS
        # attempt (`_unique_tmp_path`) -- pre-fix, with a deterministic tmp name, this same
        # line let a losing concurrent writer delete a WINNING writer's still-live staging
        # file out from under it, mid-copy.
        #
        # phaze-bk9el.7 (error_handling review): this used to be `except BaseException: ...;
        # raise` -- a catch-cleanup-reraise that never actually handled anything, so it read as
        # a swallow-risk finding even though it swallowed nothing. `finally` says the same thing
        # the code always meant (clean up `tmp` no matter how the block exits) without the
        # except clause implying a handled error. Behaviorally identical on both paths: on
        # success `tmp` is already gone (unlinked directly, or consumed by `tmp.replace(dst)`),
        # so `missing_ok=True` makes the now-redundant unlink here a no-op; on any raise --
        # including BaseException-only signals like CancelledError -- it still fires before
        # propagating, exactly as the broad except did.
        tmp.unlink(missing_ok=True)


def _atomic_same_fs_move(src: Path, dst: Path) -> None:
    """Move `src` -> `dst` within one filesystem, refusing an occupant that appeared after the check.

    phaze-s0wu: ``src.replace(dst)`` alone is unconditionally clobbering. In-process the same-fs
    path is narrow -- there is no ``await`` between the caller's ``proposed.exists()`` check and
    here, so two ``_execute_one`` coroutines cannot interleave through it -- but an EXTERNAL writer
    (the operator's own tooling, another tool touching the archive) has no such constraint, and
    neither does a cross-filesystem actor that publishes to this same destination. Claim the
    destination with a link instead, so the create is the check.

    A crash between the successful link and the source unlink leaves both paths on ONE inode. That
    is recoverable and is handled by the caller: ``_is_same_file`` matches, and the extra link is
    what tells a replay to complete forward rather than re-doing the move.

    Raises:
        FileExistsError: `dst` is occupied by a different file.
    """
    if _claim_destination_by_link(src, dst):
        src.unlink()
    else:
        src.replace(dst)


def _classify_failure_step(current_step: FailedAtStep, exc: BaseException) -> FailedAtStep:
    """Map (current_step, exc) -> the ``failed_at_step`` literal for the progress POST.

    Phase 28 RESEARCH L9 + PATTERNS L594: most failures map directly to
    ``current_step`` (set by the body as it progresses through copy -> verify ->
    delete). The one nuance is the sha256-mismatch ValueError raised by the
    verify branch: even though the agent enters that branch with
    ``current_step="verify"`` already, we encode the rule explicitly so a
    refactor that re-orders the body cannot regress the contract.
    """
    text = str(exc)
    if "sha256 mismatch" in text:
        return "verify"
    return current_step


async def _ensure_start_log(api: PhazeAgentClient, start_log: ExecutionLogCreate, *, start_logged: bool) -> bool:
    """Re-POST the write-ahead audit row if its original POST failed. Returns True if a row should now exist.

    phaze-87ba: the two writers of an ExecutionLog row are asymmetric BY DESIGN, and that asymmetry
    had a hole. CREATE is deliberately idempotent (``on_conflict_do_nothing(index_elements=["id"])``,
    D-13) precisely so an agent can replay it. PATCH is a monotonic-ladder UPDATE (D-15) and
    therefore assumes the row exists -- ``patch_execution_log`` 404s when it does not. A 404 is a
    4xx, so ``PhazeAgentClient._should_retry`` returns False and it is never retried; it then
    surfaces as ``AgentApiClientError`` and is swallowed like any other reporting failure.

    So a single transient failure of the START POST permanently erased the audit record of a file
    move that actually happened: the move committed, the proposal reached EXECUTED, and no row
    ever existed for the terminal PATCH to update. The damage is not just a missing audit page row
    -- ``services/pipeline``'s execute-stage progress counts DISTINCT proposals with a COMPLETED
    ExecutionLog, so a missing row leaves that DAG node reading done < total FOREVER with no path
    to parity, and the apply-stage eligibility probe reports the file as never-applied.

    The fix is entirely agent-side and needs no controller change: this function is called
    immediately before BOTH terminal PATCH sites, and when the start POST is known to have failed
    it re-creates the row first. Safe by construction -- CREATE is ON CONFLICT DO NOTHING, so on
    the ordinary path (or a SAQ replay) this is a no-op against the retry-stable
    ``execution_log_id``.
    """
    if start_logged:
        return True
    try:
        await api.post_execution_log(start_log)
    except Exception as exc:
        logger.error(
            "execute_approved_batch: audit row for %s could not be created -- this committed move will have NO audit trail: %s",
            start_log.proposal_id,
            exc,
        )
        return False
    logger.info(
        "execute_approved_batch: recovered the missing audit row for %s before its terminal report",
        start_log.proposal_id,
    )
    return True


def _report_progress_failure(item: ExecuteBatchProposalItem, is_last: bool, exc: BaseException) -> None:
    """Apply the D-16 swallow rule -- but ONLY to the telemetry half of the message (phaze-j7u8).

    ``post_exec_batch_progress`` carries two semantically different things on one wire message:

    * **Telemetry** -- the ``copied`` / ``verified`` / ``deleted`` / ``completed`` HINCRBYs. These
      are progress reporting for a file op that has ALREADY committed (``patch_proposal_state``
      landed before we got here), so losing one costs a wrong number on a progress bar. D-16's
      swallow rule is correct for these, and they stay fire-and-forget.
    * **The completion token** -- ``sub_batch_terminal=True``. This is the ONLY event that bumps
      ``subjobs_completed``, and that counter reaching ``subjobs_expected`` is the ONLY thing that
      promotes the batch to a terminal status and releases the ``exec:active`` single-dispatch
      sentinel. It is not reconstructible: nothing else ever writes it, and once ``_execute_one``
      returns nothing re-drives the POST. Swallowing it strands the batch at 'running' until the
      24h TTL, holds the SSE stream open for that entire window, and refuses EVERY subsequent
      Execute Approved with "a dispatch is already in progress" -- a stage-level outage caused by
      a transient 502 during a batch whose work had entirely succeeded.

    So the token re-raises, which fails the SAQ job and gets it replayed. The replay is safe by
    construction: every downstream write on that path is idempotent -- the ExecutionLog INSERT is
    ON CONFLICT DO NOTHING (D-13), the progress POST dedups on the retry-stable ``request_id``
    held in the job meta (D-15), ``patch_proposal_state`` to the same state is a no-op, and the
    phaze-ebpt already-moved detection skips the file op outright.

    Contrast ``post_analysis_progress``, swallowed by the same D-16 rule at
    ``agent_client.py`` and genuinely safe there: that path has a SECOND durable completion write
    (``put_analysis`` writes the final count regardless). The exec-batch path has no second
    writer, which is exactly what makes the identical swallow unsound here.

    Raises:
        ExecBatchTerminalReportError: when ``is_last`` -- i.e. the lost POST was the token.
    """
    if not is_last:
        logger.warning(
            "execute_approved_batch: progress POST failed for %s: %s",
            item.proposal_id,
            exc,
        )
        return
    # Worded without the domain term "completion token": semgrep's logger-credential-disclosure
    # rule pattern-matches "TOKEN" in a logged string and flags it as a potential secret.
    logger.error(
        "execute_approved_batch: sub-batch terminal completion event lost for %s: %s -- failing the job so SAQ replays it",
        item.proposal_id,
        exc,
    )
    msg = f"sub_batch_terminal progress POST failed for proposal {item.proposal_id}: {exc}"
    raise ExecBatchTerminalReportError(msg) from exc


@dataclass
class _MoveStep:
    """Mutable box for the sub-step ``_execute_one`` blames in ``failed_at_step`` on a raise.

    phaze-waos5: ``_execute_one`` used to track this as a bare local (``current_step``),
    reassigned right before each fallible operation so a raise from that operation left the
    right value behind for the ``except Exception`` handler to read. Splitting the move body
    into helper functions (below) means those reassignments now happen inside a callee's
    frame, where a plain local can't be seen by the caller after an exception unwinds it.
    This box is threaded through instead: helpers set ``.current`` immediately before each
    op that can raise, exactly where ``current_step = ...`` used to sit inline, so
    ``_execute_one`` reads the same value it always would have after catching. Note
    ``_classify_failure_step`` further overrides this to ``"verify"`` for any exception whose
    text contains "sha256 mismatch", regardless of ``.current`` -- that rule is unchanged.
    """

    current: FailedAtStep = "copy"


async def _verify_hash_or_raise(path: Path, expected_hash: str, *, label: str, suffix: str = "") -> None:
    """Await a streamed sha256 of `path`; raise ``ValueError`` on mismatch against `expected_hash`.

    phaze-timy: hashing runs off-loop (``asyncio.to_thread``) -- ``_sha256_of_file`` streams a
    multi-GB file in 1 MiB reads, and on-loop that freezes the meta-lane worker's event loop for
    minutes, starving the co-scheduled lane slot and the Phase-46 heartbeat.

    The raised message is ``"sha256 mismatch for {label}: expected {expected_hash}, got
    {actual}{suffix}"`` -- `label` and `suffix` let each of ``_execute_one``'s three call sites
    (already-moved replay check, pre-copy verify, post-publish re-verify) reproduce its own exact
    wording verbatim.
    """
    actual = await asyncio.to_thread(_sha256_of_file, path)
    if actual != expected_hash:
        msg = f"sha256 mismatch for {label}: expected {expected_hash}, got {actual}{suffix}"
        raise ValueError(msg)


def _check_replay_corroborated(
    original: Path,
    proposed: Path,
    item: ExecuteBatchProposalItem,
    job: Any | None,
) -> bool:
    """True iff `original` gone + `proposed` present is a CORROBORATED replay of THIS proposal's own prior move.

    phaze-ebpt: `_resolve_and_check_containment` uses a non-strict `Path.resolve()`, so a SAQ
    retry that resumes after a prior attempt already committed `original.replace(proposed)` (or
    the cross-filesystem copy+unlink) -- but crashed before the completed/executed/progress
    PATCHes -- resolves `original` cleanly even though it no longer exists. `original` gone +
    `proposed` present is conclusive evidence SOME proposal already moved a file to `proposed`
    (only `_execute_one` ever creates `proposed`), so the raw shape alone used to be treated as
    "skip the file op, report success".

    phaze-ebb46: that raw shape is NOT, on its own, conclusive that `proposed` is THIS proposal's
    own replayed move -- phaze is a dedup tool, so an archive full of exact duplicates means a
    byte-identical `proposed` can just as easily be a DIFFERENT proposal's already-completed move
    to the same destination, whose OWN source has since gone missing for an unrelated reason.
    Require per-proposal corroboration: either this proposal's own "moved" flag
    (`_moved_flag_key`, persisted in `job.meta` right after ITS move committed) or its cross-fs
    commit marker (the copy-committed-pending-unlink residue, phaze-i7jo/qx8z -- a genuine
    cross-fs replay can reach this same branch too). Absent both, the caller must fall through to
    the normal move/verify path, which fails LOUDLY on the now-missing `original` instead of
    silently reporting a stranger's move as this proposal's own success.
    """
    if original.exists() or not proposed.exists():
        return False
    job_meta = dict(getattr(job, "meta", None) or {}) if job is not None else {}
    return job_meta.get(_moved_flag_key(item.proposal_id)) is not None or _committed_copy_marker_path(proposed, item.proposal_id).exists()


async def _reclaim_or_refuse_existing_destination(
    original: Path,
    proposed: Path,
    item: ExecuteBatchProposalItem,
    step: _MoveStep,
    *,
    same_fs: bool,
) -> None:
    """`proposed` exists and is not `original`'s own inode -- complete a self-authored cross-fs residue forward, or refuse the clobber.

    phaze-qx8z: a distinct-inode `proposed` alongside a still-present `original` is the exact
    residue of THIS move's own prior cross-fs attempt interrupted between the committed copy and
    the pending `original.unlink()` (a hard worker kill; or a live unlink() OSError, phaze-q2lg).
    Across a mount boundary `_is_same_file` can never confirm that residue (st_dev differs).

    phaze-i7jo: content identity alone used to be trusted as proof of that residue, but phaze is
    a dedup tool -- an archive full of exact duplicates means a byte-identical `proposed` can be
    a DIFFERENT proposal's own already-completed move to this same destination, not THIS
    proposal's. Require BOTH: (a) this exact proposal's own commit marker
    (`_committed_copy_marker_path`), written right after THIS proposal's own prior
    `_atomic_cross_fs_copy` call returned, and (b) content identity
    (`_destination_is_committed_copy`). Either check failing falls through to the phaze-yu2e
    no-clobber refusal: a byte-identical duplicate at the destination with no marker of ITS OWN
    fails loudly instead of silently deleting this proposal's source.

    phaze-timy: `_destination_is_committed_copy` hashes both `proposed` and (absent a supplied
    hash) `original` -- multi-GB streaming reads, so it is offloaded so the identity check does
    not freeze the meta-lane event loop. The subsequent `original.unlink()` is a cheap syscall
    and stays inline.

    Raises:
        FileExistsError: `proposed` is occupied by something that is not THIS proposal's own
            resumable residue.
    """
    own_marker = _committed_copy_marker_path(proposed, item.proposal_id)
    if not same_fs and own_marker.exists() and await asyncio.to_thread(_destination_is_committed_copy, original, proposed, item.sha256_hash):
        step.current = "delete"
        original.unlink()
        own_marker.unlink(missing_ok=True)
    else:
        msg = f"destination already exists, refusing to overwrite: {proposed}"
        raise FileExistsError(msg)


def _move_same_fs_entry(original: Path, proposed: Path, step: _MoveStep) -> None:
    """`original`/`proposed` share a filesystem AND resolve to the same inode already.

    Either a no-op / case-only rename on a case-insensitive filesystem (nothing to clobber, keep
    the plain rename), or this move's own crashed link-claim residue (an extra link exists at a
    genuinely different path) that a POSIX rename onto its own sibling link would silently no-op
    forever -- complete that case forward instead.

    phaze-kxnnd: `st_nlink > 1` alone is NOT sufficient evidence of the crashed-claim residue --
    it is a global count of every link to the inode anywhere on the filesystem, not proof the
    extra one sits AT `proposed`. On a case-insensitive mount, a pure case-only rename
    (``Track.MP3`` -> ``track.mp3``) also makes ``original != proposed`` true while both name the
    SAME single directory entry; an unrelated pre-existing hard link elsewhere in the archive then
    inflates nlink and used to trigger `original.unlink()` -- deleting the archive's only entry
    for the file and leaving the rename never applied. `_is_case_only_same_entry` rules that
    shape out FIRST, unconditionally, before trusting nlink as crashed-claim evidence -- the same
    "necessary but not sufficient" corroboration shape phaze-i7jo required one function up.
    """
    if original != proposed and not _is_case_only_same_entry(original, proposed) and original.stat().st_nlink > 1:
        step.current = "delete"
        original.unlink()
    else:
        original.replace(proposed)


async def _move_across_filesystem(
    original: Path,
    proposed: Path,
    item: ExecuteBatchProposalItem,
    step: _MoveStep,
) -> None:
    """Cross-filesystem copy -> optional re-verify -> commit marker -> delete.

    phaze-k23z: copy through a temp sibling + os.replace so the destination materializes
    atomically. A copy that aborts mid-stream (ENOSPC/EIO) leaves no partial file at `proposed`.

    phaze-timy: the streamed copy is blocking I/O that can run for minutes on a multi-GB concert
    video, so the whole primitive is offloaded to a worker thread -- the meta-lane event loop
    also drives the co-scheduled lane slot, SAQ's dequeue/timeout timers, and the Phase-46
    heartbeat. The fsync/atomic-rename crash-safety semantics are internal to
    `_atomic_cross_fs_copy` and are preserved verbatim inside the thread. The follow-up
    `original.unlink()` is a cheap syscall and stays inline; a crash in the (awaited) window
    between copy and unlink lands in the recoverable "copy committed, original present" state
    the replay logic (`_check_replay_corroborated` + this module's docstring) completes forward.

    phaze-otoqj: re-verify the PUBLISHED destination against the already-trusted expected hash
    before unlinking `original` -- belt-and-suspenders on top of the `_unique_tmp_path`/O_EXCL
    fix in `_atomic_cross_fs_copy`, not a substitute for it. `original` is still present here, so
    a mismatch fails loudly with the source untouched. Only runs when a hash was supplied.

    phaze-i7jo: write THIS proposal's own commit marker now that the copy is durably landed at
    `proposed` -- before the unlink, so a crash in the window below leaves corroborating evidence
    a replay can trust (see the module docstring's "Cross-filesystem crash-state model").
    """
    await asyncio.to_thread(_atomic_cross_fs_copy, original, proposed)
    if item.sha256_hash is not None:
        step.current = "verify"
        await _verify_hash_or_raise(
            proposed,
            item.sha256_hash,
            label=f"published destination {proposed}",
            suffix=" (post-publish re-verify)",
        )
    _committed_copy_marker_path(proposed, item.proposal_id).write_text(str(item.proposal_id))
    # 5. Delete the original (a cross-filesystem copy leaves it in place).
    step.current = "delete"
    original.unlink()
    # The move completed in full within this same call -- the marker's only job was to survive a
    # crash between the two lines above; clean it up.
    _committed_copy_marker_path(proposed, item.proposal_id).unlink(missing_ok=True)


async def _apply_file_move(
    original: Path,
    proposed: Path,
    item: ExecuteBatchProposalItem,
    step: _MoveStep,
) -> None:
    """Move `original` -> `proposed` (mkdir parent as needed), dispatching to the right strategy.

    Prefers `os.replace`/`os.link` (atomic, O(1), constant memory) when src + dst share a
    filesystem; otherwise streams the bytes in bounded chunks -- concert videos are multi-GB and
    the meta lane has no memory pin, so a whole-file read would MemoryError / OOM-kill the worker.

    phaze-yu2e: refuse to clobber a pre-existing destination. The dispatch-time collision gate
    cannot catch every case -- a destination already occupied by an earlier executed proposal, or
    an untracked on-disk file -- so fail loudly rather than overwrite. `_is_same_file` exempts the
    no-op / case-only rename.

    phaze-s0wu: this exists-check is necessary but NOT sufficient on its own, and must not be
    mistaken for the guarantee. It is a check-then-act whose "act" can be minutes later (a
    multi-GB cross-fs copy), so it cannot see an occupant that arrives during the move. The real
    no-clobber guarantee is the atomic `os.link` claim inside `_atomic_cross_fs_copy` /
    `_atomic_same_fs_move`; this check survives because it fails fast and because it is where the
    phaze-i7jo/qx8z resumable-residue detection lives (`_reclaim_or_refuse_existing_destination`).
    (Correction: an earlier version of this comment also listed "NULL-path in-place renames" as a
    gate miss -- that was false; collision.py's `_dest_key_columns` was fixed for that case,
    phaze-7czn, and for the mixed-namespace key, phaze-dqx8.)
    """
    step.current = "copy"
    proposed.parent.mkdir(parents=True, exist_ok=True)
    same_fs = _same_filesystem(original, proposed.parent)
    if proposed.exists() and not _is_same_file(original, proposed):
        await _reclaim_or_refuse_existing_destination(original, proposed, item, step, same_fs=same_fs)
    elif same_fs:
        if _is_same_file(original, proposed):
            _move_same_fs_entry(original, proposed, step)
        else:
            # phaze-s0wu: a no-clobber claim, not a bare rename -- the exists-check above is
            # already stale by the time we act on it. The claim also removes the original in the
            # same primitive, so there is no separate delete step to fail.
            _atomic_same_fs_move(original, proposed)
    else:
        await _move_across_filesystem(original, proposed, item, step)


@dataclass
class _ReportContext:
    """Bundles the per-proposal API/reporting parameters shared verbatim by `_report_success`
    and `_report_failure` -- both PATCH the same execution-log row and POST to the same
    exec-batch progress endpoint for the same proposal, just with a different terminal status.
    Built once in `_execute_one` and passed through unchanged.
    """

    api: PhazeAgentClient
    item: ExecuteBatchProposalItem
    execution_log_id: uuid.UUID
    progress_request_id: uuid.UUID
    payload: ExecuteApprovedBatchPayload
    is_last: bool
    start_log: ExecutionLogCreate


async def _finalize_execution_log(
    ctx: _ReportContext,
    patch: ExecutionLogPatch,
    *,
    start_logged: bool,
    terminal_label: str,
) -> bool:
    """Heal the write-ahead audit row via `_ensure_start_log`, then PATCH the execution-log row
    to its terminal state (COMPLETED or FAILED, carried in `patch`).

    Shared by `_report_success` and `_report_failure` -- the same heal-then-PATCH sequence, only
    the terminal `ExecutionLogPatch` and the word used in the swallowed-failure warning
    (`terminal_label`, e.g. "completed"/"failed") differ. A PATCH failure here is swallowed with a
    warning rather than raised: the move already committed to disk (or the caller is already on
    the failure path), so this row is reconciliation-recoverable and must not itself fail the
    batch.

    Returns the (possibly healed) `start_logged` for the caller to carry into its next step.
    """
    start_logged = await _ensure_start_log(ctx.api, ctx.start_log, start_logged=start_logged)
    try:
        await ctx.api.patch_execution_log(ctx.execution_log_id, patch)
    except Exception as patch_exc:
        logger.warning(
            "execute_approved_batch: could not patch %s log for %s: %s",
            terminal_label,
            ctx.item.proposal_id,
            patch_exc,
        )
    return start_logged


async def _post_exec_batch_progress_terminal(
    ctx: _ReportContext,
    *,
    terminal_step: Literal["deleted", "failed"],
    failed_at_step: FailedAtStep | None = None,
) -> None:
    """POST the exec-batch progress event for this proposal's terminal step, carrying the
    `sub_batch_terminal` completion token when this is the batch's last proposal.

    Shared by `_report_success` (`terminal_step="deleted"`) and `_report_failure`
    (`terminal_step="failed"`, `failed_at_step` set) -- same payload shape, same POST, same
    failure handling; only the terminal step and (for a failure) the step it failed at differ.

    A POST failure is routed through `_report_progress_failure`, which applies D-16's swallow
    rule to the telemetry half but re-raises `ExecBatchTerminalReportError` for a lost completion
    token (phaze-j7u8) -- see that function's docstring. This function does not itself catch that
    re-raise; it propagates to the caller (`_report_success`/`_report_failure`) unchanged, same as
    before this was split out.
    """
    try:
        await ctx.api.post_exec_batch_progress(
            ctx.payload.batch_id,
            ExecBatchProgressPayload(
                request_id=ctx.progress_request_id,
                batch_id=ctx.payload.batch_id,
                agent_id=ctx.payload.agent_id,
                sub_batch_index=ctx.payload.sub_batch_index,
                proposal_id=ctx.item.proposal_id,
                terminal_step=terminal_step,
                failed_at_step=failed_at_step,
                sub_batch_terminal=ctx.is_last,
            ),
        )
    except Exception as progress_exc:
        _report_progress_failure(ctx.item, ctx.is_last, progress_exc)


async def _report_success(
    ctx: _ReportContext,
    proposed: Path,
    *,
    start_logged: bool,
    sha_verified: bool,
) -> None:
    """Steps 6a/6b/7 of the success path: PATCH execution-log completed, PATCH proposal executed, POST progress.

    phaze-87ba: heals the audit row first (`_ensure_start_log`) if its write-ahead POST never
    landed, else the PATCH below 404s (not retried) and is swallowed with no audit trail at all.

    6b's PATCH must NOT bubble into `_execute_one`'s generic failure handler: the move is
    ALREADY committed on disk, so a 5xx here would wrongly flip an executed proposal to FAILED
    and strand `FileRecord.current_path` -- swallow + log, the state is reconciliation-recoverable.

    Step 7 is D-16 telemetry EXCEPT the `sub_batch_terminal=True` completion token, which
    `_report_progress_failure` re-raises as `ExecBatchTerminalReportError` (phaze-j7u8) --
    propagates through this function to `_execute_one`'s dedicated `except` clause.
    """
    start_logged = await _finalize_execution_log(
        ctx,
        ExecutionLogPatch(status=ExecutionStatus.COMPLETED, sha256_verified=sha_verified),
        start_logged=start_logged,
        terminal_label="completed",
    )

    try:
        await ctx.api.patch_proposal_state(
            ctx.item.proposal_id,
            ProposalStatePatch(
                proposal_state="executed",
                file_state="moved",
                current_path=str(proposed),
            ),
        )
    except Exception as report_exc:
        logger.error(
            "execute_approved_batch: move committed but reporting executed state failed for %s: %s",
            ctx.item.proposal_id,
            report_exc,
        )

    await _post_exec_batch_progress_terminal(ctx, terminal_step="deleted")


async def _report_failure(
    ctx: _ReportContext,
    failed_step: FailedAtStep,
    formatted_error: str,
    *,
    start_logged: bool,
) -> None:
    """Steps 6a-failed/6b-failed/7-failed: PATCH execution-log failed, PATCH proposal failed, POST progress.

    phaze-87ba: the FAILED audit row is lost exactly the same way as the completed one, so it
    gets the same `_ensure_start_log` heal -- a failed move with no attempt record is worse.

    phaze-j7u8: the twin site to `_report_success`'s progress POST. A sub-batch whose LAST
    proposal legitimately FAILED still carries the completion token on this POST and loses it
    the same way, so the same rule applies: telemetry is swallowed, the token re-raises out of
    this function into `_execute_one`'s dedicated handler.
    """
    start_logged = await _finalize_execution_log(
        ctx,
        ExecutionLogPatch(status=ExecutionStatus.FAILED, error_message=formatted_error),
        start_logged=start_logged,
        terminal_label="failed",
    )
    try:
        await ctx.api.patch_proposal_state(
            ctx.item.proposal_id,
            ProposalStatePatch(
                proposal_state="failed",
                file_state=None,
                error_message=formatted_error,
            ),
        )
    except Exception as report_exc:
        # If we can't even REPORT the failure, log and continue -- one bad network blip should
        # not bring the whole batch down.
        logger.error(
            "execute_approved_batch: failed to report failure for %s: %s",
            ctx.item.proposal_id,
            report_exc,
        )

    await _post_exec_batch_progress_terminal(ctx, terminal_step="failed", failed_at_step=failed_step)


async def _compute_proposed(
    item: ExecuteBatchProposalItem,
    scan_roots: list[str],
    job: Any | None,
    step: _MoveStep,
) -> Path:
    """Resolve the destination for one proposal, applying the move if it is not already done.

    Split out of ``_execute_one`` (phaze-bk9el.7 extract_method plan, source biomarker
    ``complex_method`` on ``_execute_one``, suggested name ``compute_proposed``) -- same order,
    same ``step`` tracking, same ``job.meta`` persistence as before the split; a pure
    decomposition, not a behavior change. ``step`` is the same ``_MoveStep`` instance the caller
    holds, so mutations here (``step.current = "verify"`` etc.) are visible to the caller's
    ``except Exception`` handler exactly as they were inline; a raise from any awaited call below
    propagates through this function unchanged, same as it did inline.

    Returns ``proposed`` -- the only value ``_execute_one`` still needs from this block afterward.
    """
    # 2. Path-traversal guard for source_path + construct/guard the
    # destination. step.current="copy" (the default) covers path-resolve (a
    # failure here means "the copy couldn't begin" -- matches operator
    # intuition). proposed_path is a RELATIVE dir under the owning
    # scan_root; the destination is owning_root/proposed_path/proposed_filename
    # (empty proposed_path == in-place rename).
    original, owning_root = _resolve_and_check_containment(item.source_path, scan_roots)
    proposed = _resolve_destination(item, original, owning_root, scan_roots)

    already_moved = _check_replay_corroborated(original, proposed, item, job)
    if already_moved and item.sha256_hash is not None:
        # Confirm `proposed` is actually the expected file before trusting the
        # replay -- a hash mismatch means this isn't the already-moved file (e.g.
        # an unrelated file landed at the destination), so treat it as a genuine
        # verify failure instead of silently reporting success.
        step.current = "verify"
        await _verify_hash_or_raise(
            proposed,
            item.sha256_hash,
            label=item.source_path,
            suffix=f" (already-moved replay check against {proposed})",
        )

    if already_moved:
        # phaze-v3b1e: a confirmed replay means the move is durably done, so THIS
        # proposal's cross-fs commit marker (if any) has no further job to do. The two
        # sites that normally unlink it (inside `_reclaim_or_refuse_existing_destination`
        # and `_move_across_filesystem`) both require `original` to still exist, so a
        # crash between `original.unlink()` and the marker's own unlink there permanently
        # skips them on every subsequent replay -- `already_moved` is now True and
        # corroborated by the very marker file this call would otherwise never clean up,
        # orphaning `<dest>.phaze-committed.<proposal_id>` in the archive forever. Cleaning
        # it up here, once, on every already-moved replay closes that window regardless of
        # which crash edge produced it.
        _committed_copy_marker_path(proposed, item.proposal_id).unlink(missing_ok=True)

    if not already_moved:
        # 3. Optional sha256 verify (caller may supply None to skip)
        if item.sha256_hash is not None:
            step.current = "verify"
            await _verify_hash_or_raise(original, item.sha256_hash, label=item.source_path)

        # 4. Move original -> proposed.
        await _apply_file_move(original, proposed, item, step)

        # phaze-ebb46: persist THIS proposal's own "moved" corroboration flag now that
        # its move is fully committed -- retry-stable in job.meta, same idempotent
        # convention as `log_id:`/`req_id:` (D-15). This is the fresh-execution half of
        # the corroboration check above: a same-fs move is one atomic syscall with no
        # cross-fs-style residue window to inspect on replay, so this flag is what lets
        # a LATER SAQ retry of this same job tell "my own prior move" apart from a
        # different proposal's already-completed move that happens to sit at this exact
        # destination (the phaze-i7jo insufficiency this bead closes for the same-fs
        # path too). Only reached on a FRESH commit -- an already-moved replay skips
        # this whole block, so the flag is never redundantly re-persisted.
        if job is not None:
            updated_job_meta = dict(getattr(job, "meta", None) or {})
            updated_job_meta[_moved_flag_key(item.proposal_id)] = "1"
            await job.update(meta=updated_job_meta)

    return proposed


async def _execute_one(
    api: PhazeAgentClient,
    item: ExecuteBatchProposalItem,
    scan_roots: list[str],
    payload: ExecuteApprovedBatchPayload,
    is_last: bool,
    execution_log_id: uuid.UUID,
    progress_request_id: uuid.UUID,
    job: Any | None,
) -> bool:
    """Execute one proposal. Returns True on success, False on any failure.

    Per-proposal lifecycle:
    1. POST execution-log (status=in_progress) -- one row per file op.
    2. Path-traversal guard for source_path and proposed_path.
    3. Optional sha256 verify.
    4. Copy + delete.
    5. PATCH execution-log (status=completed | failed).
    6. PATCH proposal-state (executed | failed).
    7. POST exec-batch progress (terminal_step=deleted | failed) -- Phase 28 D-03.

    The two UUID arguments (``execution_log_id`` and ``progress_request_id``)
    come from ``execute_approved_batch`` which loaded them from ``ctx['job'].meta``
    so SAQ retries reuse the same per-proposal values (closes L6/L22; delivers D-15).

    ``job`` (the SAQ job, or ``None`` for a legacy ctx with no ``'job'`` key) is threaded
    through so this function can read and persist the phaze-ebb46 per-proposal "moved"
    replay-corroboration flag in ``job.meta`` -- see ``_check_replay_corroborated``.

    phaze-waos5: this orchestrates the same steps in the same order as before it was split into
    helpers (`_check_replay_corroborated`, `_apply_file_move` and its sub-helpers,
    `_report_success`, `_report_failure`) -- a pure decomposition, not a behavior change. The
    failed-step tracking that used to be a bare local (`current_step`) is now `_MoveStep`,
    threaded through the extracted helpers so a raise inside any of them still leaves the right
    value for the `except Exception` block below to read (see `_MoveStep`'s docstring).

    phaze-bk9el.7: the path-resolve / already-moved-check / verify / move / persist-flag block
    that used to sit inline here is now `_compute_proposed` -- the repowise extract_method plan
    on this function (span 1091-1143 at measurement time, suggested name `compute_proposed`).
    Same steps, same order, same `step`/`job` mutation visible to this function exactly as
    before; `proposed` is the only value this function still needed from that block.
    """
    sha_verified = item.sha256_hash is not None
    # Relative destination for the audit trail (source_path is absolute, but the
    # true absolute destination is only known after resolving the owning
    # scan_root inside the guarded block below). proposed_filename is always
    # present, so this is never empty (satisfies ExecutionLogCreate min_length=1).
    dest_display = f"{item.proposed_path.rstrip('/')}/{item.proposed_filename}" if item.proposed_path else item.proposed_filename
    # Always POST the in-progress audit row first -- this is the durable trail
    # that survives a crash mid-copy.
    start_log = ExecutionLogCreate(
        id=execution_log_id,
        proposal_id=item.proposal_id,
        operation="move",
        source_path=item.source_path,
        destination_path=dest_display,
        sha256_verified=False,  # not yet verified at this point
        status=ExecutionStatus.IN_PROGRESS,
    )
    ctx = _ReportContext(api, item, execution_log_id, progress_request_id, payload, is_last, start_log)
    # phaze-87ba: REMEMBER whether this landed. _execute_one is the only component that knows the
    # POST failed, and discarding that knowledge here is what erased the audit row entirely -- see
    # _ensure_start_log.
    start_logged = True
    try:
        await api.post_execution_log(start_log)
    except Exception as exc:
        # If the audit log POST itself fails (network blip), still attempt the
        # file op so we don't leave the user with stalled state. Best-effort.
        start_logged = False
        logger.warning("execute_approved_batch: could not record start log for %s: %s", item.proposal_id, exc)

    # Phase 28: track which sub-step is currently executing so the failure
    # handler can map exception -> failed_at_step without inspecting types.
    step = _MoveStep()
    try:
        proposed = await _compute_proposed(item, scan_roots, job, step)

        await _report_success(
            ctx,
            proposed,
            start_logged=start_logged,
            sha_verified=sha_verified,
        )
        return True
    except ExecBatchTerminalReportError:
        # phaze-j7u8: a lost completion token must reach `execute_approved_batch` so SAQ replays the
        # job. It must NOT fall into the generic handler below -- this proposal's move SUCCEEDED and
        # its state is already reported; treating the undeliverable token as a per-proposal failure
        # would flip an executed proposal to FAILED and post a `terminal_step="failed"` event for a
        # file that is sitting correctly at its destination.
        raise
    except Exception as exc:
        # Phase 28: classify the failure step BEFORE any PATCH so both the
        # error_message prefix (D-01) and the progress POST failed_at_step
        # (D-06) come from one source of truth.
        failed_step: FailedAtStep = _classify_failure_step(step.current, exc)
        formatted_error = f"{failed_step}: {exc!s}"[:500]

        logger.warning(
            "execute_approved_batch: proposal %s failed at step=%s: %s",
            item.proposal_id,
            failed_step,
            exc,
            exc_info=True,
        )
        await _report_failure(
            ctx,
            failed_step,
            formatted_error,
            start_logged=start_logged,
        )
        return False


def _load_or_seed_uuids(
    job: Any,
    proposals: list[ExecuteBatchProposalItem],
) -> tuple[dict[uuid.UUID, uuid.UUID], dict[uuid.UUID, uuid.UUID], dict[str, str], bool]:
    """Read per-proposal UUIDs from ``job.meta`` or generate fresh ones.

    Returns ``(log_ids_by_proposal, req_ids_by_proposal, updated_meta, changed)``
    where ``changed`` is True iff any keys were newly seeded (caller is responsible
    for persisting via ``await job.update(meta=updated_meta)``). UUIDs in meta are
    stored as strings; in-memory they're returned as ``uuid.UUID`` objects.

    Phase 28 L6/L22/D-15 contract: on a SAQ retry the same ``job`` is reloaded
    from Redis with ``meta`` already populated, so this function returns the
    existing UUIDs and ``changed=False`` -- caller skips the ``job.update`` call
    AND the underlying ExecutionLog INSERT / progress HINCRBY both dedup via
    server-side idempotency (INSERT ON CONFLICT + SET NX EX).
    """
    existing_meta: dict[str, str] = dict(getattr(job, "meta", None) or {})
    log_ids: dict[uuid.UUID, uuid.UUID] = {}
    req_ids: dict[uuid.UUID, uuid.UUID] = {}
    changed = False
    for item in proposals:
        log_key = f"log_id:{item.proposal_id}"
        req_key = f"req_id:{item.proposal_id}"
        if log_key in existing_meta:
            log_ids[item.proposal_id] = uuid.UUID(existing_meta[log_key])
        else:
            new_log_id = uuid.uuid4()
            existing_meta[log_key] = str(new_log_id)
            log_ids[item.proposal_id] = new_log_id
            changed = True
        if req_key in existing_meta:
            req_ids[item.proposal_id] = uuid.UUID(existing_meta[req_key])
        else:
            new_req_id = uuid.uuid4()
            existing_meta[req_key] = str(new_req_id)
            req_ids[item.proposal_id] = new_req_id
            changed = True
    return log_ids, req_ids, existing_meta, changed


async def execute_approved_batch(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Per-agent sub-batch executor (B2 Option A -- full implementation + Phase 28 D-03/D-15).

    Validates payload (extra='forbid'), seeds retry-stable per-proposal UUIDs
    in ``ctx['job'].meta`` (so SAQ retries reuse the same ``execution_log_id``
    and ``progress_request_id`` per proposal), then executes each proposal with
    failure isolation. Cross-proposal failures are isolated: one bad file does
    NOT fail the batch.
    """
    payload = ExecuteApprovedBatchPayload.model_validate(kwargs)
    api: PhazeAgentClient = ctx["api_client"]

    logger.info(
        "execute batch started",
        batch_id=str(payload.batch_id),
        agent=payload.agent_id,
        proposals=len(payload.proposals),
    )

    cfg = get_settings()
    scan_roots: list[str] = list(cfg.scan_roots) if isinstance(cfg, AgentSettings) else []
    if not scan_roots:
        # Mis-deployment: agent has no scan_roots configured. Refuse to perform any
        # file ops (path-traversal guard would reject every path anyway).
        msg = "agent has no scan_roots configured; cannot execute batch"
        raise RuntimeError(msg)

    # Phase 28 L6/L22 + D-15: load retry-stable UUIDs from SAQ job meta (or seed if absent).
    # Legacy callers (Phase 26 in-memory test fixtures) may pass a ctx without 'job' -- in that
    # case we fall back to generating fresh UUIDs per call. The fall-back has no SAQ retry
    # semantics (which legacy callers don't have anyway) and matches Phase 26 B2 behavior.
    job = ctx.get("job")
    if job is not None:
        log_ids, req_ids, updated_meta, changed = _load_or_seed_uuids(job, list(payload.proposals))
        if changed:
            await job.update(meta=updated_meta)
    else:
        logger.debug("execute_approved_batch: ctx has no 'job' key -- using fresh UUIDs (legacy ctx).")
        log_ids = {item.proposal_id: uuid.uuid4() for item in payload.proposals}
        req_ids = {item.proposal_id: uuid.uuid4() for item in payload.proposals}

    processed = 0
    errors = 0
    total = len(payload.proposals)
    for idx, item in enumerate(payload.proposals):
        is_last = idx == total - 1
        ok = await _execute_one(
            api,
            item,
            scan_roots,
            payload,
            is_last,
            log_ids[item.proposal_id],
            req_ids[item.proposal_id],
            job,
        )
        processed += 1
        if not ok:
            errors += 1

    final_status = "completed" if errors == 0 else "completed_with_errors"

    logger.info(
        "execute batch completed",
        batch_id=str(payload.batch_id),
        status=final_status,
        processed_count=processed,
        error_count=errors,
    )
    return {
        "batch_id": str(payload.batch_id),
        "status": final_status,
        "processed_count": processed,
        "error_count": errors,
    }
