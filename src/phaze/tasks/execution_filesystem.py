"""Filesystem engine for approved rename proposals.

This module owns the destructive half of execution: containment-safe destination
resolution, no-clobber claims, same- and cross-filesystem movement, bounded
streaming copies, hashes, commit markers, and crash/replay recovery.  It has no
SAQ, HTTP-client, reporting, or application-persistence dependency.  The
``phaze.tasks.execution`` adapter owns those concerns and injects its compatibility
facade as ``FilesystemPrimitives`` so historical patch points continue to resolve.

The crash model is intentionally asymmetric.  A same-filesystem move is claimed
with a hard link before the source is removed.  A cross-filesystem move is copied
to a unique sibling, fsynced, published with the same no-clobber claim, marked for
the proposal, and only then removes the source.  Content identity alone never
authorizes deleting a source: replay also requires proposal-specific corroboration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import hashlib
import os
import shutil
from typing import TYPE_CHECKING, Literal, Protocol
import uuid

from phaze.services.containment import resolve_and_check_containment


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from phaze.schemas.agent_tasks import ExecuteBatchProposalItem


FailedAtStep = Literal["copy", "verify", "delete"]

COPY_CHUNK_BYTES = 16 * 1024 * 1024
COPY_TMP_SUFFIX = ".phaze-tmp"
COMMIT_MARKER_SUFFIX = ".phaze-committed"

_LINK_UNSUPPORTED_ERRNOS = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EMLINK, errno.EXDEV})


@dataclass
class MoveStep:
    """Mutable failure-attribution state shared with the orchestration adapter."""

    current: FailedAtStep = "copy"


@dataclass(frozen=True)
class FilesystemMoveRequest:
    """Everything the filesystem engine needs for one proposal move."""

    item: ExecuteBatchProposalItem
    scan_roots: list[str]
    replay_corroborated: bool = False


@dataclass(frozen=True)
class FilesystemMoveResult:
    """A resolved destination plus whether this call committed the move."""

    proposed: Path
    committed_now: bool


class FilesystemPrimitives(Protocol):
    """Replaceable local-I/O primitives used by the engine."""

    def resolve_containment(self, candidate: str, roots: list[str]) -> tuple[Path, Path]: ...

    def resolve_destination(
        self,
        item: ExecuteBatchProposalItem,
        original: Path,
        owning_root: Path,
        scan_roots: list[str],
    ) -> Path: ...

    def committed_copy_marker_path(self, proposed: Path, proposal_id: uuid.UUID) -> Path: ...

    def same_filesystem(self, src: Path, dst_dir: Path) -> bool: ...

    def is_same_file(self, a: Path, b: Path) -> bool: ...

    def is_case_only_same_entry(self, original: Path, proposed: Path) -> bool: ...

    def destination_is_committed_copy(self, original: Path, proposed: Path, expected_hash: str | None) -> bool: ...

    async def verify_hash_or_raise(self, path: Path, expected_hash: str, *, label: str, suffix: str = "") -> None: ...

    def atomic_cross_fs_copy(self, src: Path, dst: Path) -> None: ...

    def atomic_same_fs_move(self, src: Path, dst: Path) -> None: ...

    @property
    def hash_file_for_offload(self) -> Callable[[Path], str]: ...

    @property
    def cross_fs_copy_for_offload(self) -> Callable[[Path, Path], None]: ...


class ExecutionFilesystemEngine(Protocol):
    """Consumer-facing capability for executing one approved filesystem move."""

    async def move(self, request: FilesystemMoveRequest, step: MoveStep) -> FilesystemMoveResult: ...


class LocalFilesystemPrimitives:
    """Concrete local-filesystem adapter used by ``LocalExecutionFilesystemEngine``."""

    def resolve_containment(self, candidate: str, roots: list[str]) -> tuple[Path, Path]:
        return resolve_and_check_containment(candidate, roots)

    def resolve_destination(
        self,
        item: ExecuteBatchProposalItem,
        original: Path,
        owning_root: Path,
        scan_roots: list[str],
    ) -> Path:
        """Build and containment-check the absolute destination."""
        dest_dir = (owning_root / item.proposed_path) if item.proposed_path else original.parent
        resolved, _ = self.resolve_containment(str(dest_dir / item.proposed_filename), scan_roots)
        return resolved

    def sha256_of_file(self, path: Path) -> str:
        """Hash a file without loading it into memory."""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def committed_copy_marker_path(self, proposed: Path, proposal_id: uuid.UUID) -> Path:
        """Return the per-proposal marker for a committed cross-filesystem copy."""
        return proposed.with_name(f"{proposed.name}{COMMIT_MARKER_SUFFIX}.{proposal_id}")

    def unique_tmp_path(self, dst: Path) -> Path:
        """Return a destination-sibling staging path unique to this attempt."""
        return dst.with_name(f"{dst.name}{COPY_TMP_SUFFIX}.{os.getpid()}.{uuid.uuid4().hex}")

    def same_filesystem(self, src: Path, dst_dir: Path) -> bool:
        """Return whether source and destination directory share a device."""
        return src.stat().st_dev == dst_dir.stat().st_dev

    def is_same_file(self, a: Path, b: Path) -> bool:
        """Return whether two paths identify the same device and inode."""
        try:
            return os.path.samestat(a.stat(), b.stat())
        except OSError:
            return False

    def is_case_only_same_entry(self, original: Path, proposed: Path) -> bool:
        """Return whether paths differ only by case in the same directory."""
        return original.parent == proposed.parent and original.name != proposed.name and original.name.casefold() == proposed.name.casefold()

    def destination_is_committed_copy(self, original: Path, proposed: Path, expected_hash: str | None) -> bool:
        """Return whether the destination is byte-identical to the source."""
        proposed_hash = self.sha256_of_file(proposed)
        if expected_hash is not None:
            return proposed_hash == expected_hash
        return proposed_hash == self.sha256_of_file(original)

    def streamed_copy(self, src: Path, dst: Path) -> None:
        """Copy in bounded chunks to an exclusively created, fsynced destination."""
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with src.open("rb") as source, os.fdopen(fd, "wb") as destination:
            shutil.copyfileobj(source, destination, length=COPY_CHUNK_BYTES)
            destination.flush()
            os.fsync(destination.fileno())
        shutil.copystat(src, dst)

    def claim_destination_by_link(self, src: Path, dst: Path) -> bool:
        """Atomically claim ``dst``; return false only when links are unsupported."""
        try:
            os.link(src, dst)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno in _LINK_UNSUPPORTED_ERRNOS:
                return False
            raise
        return True

    def atomic_cross_fs_copy(self, src: Path, dst: Path) -> None:
        """Publish a bounded cross-filesystem copy atomically without clobbering."""
        tmp = self.unique_tmp_path(dst)
        try:
            self.streamed_copy(src, tmp)
            if self.claim_destination_by_link(tmp, dst):
                tmp.unlink()
            else:
                tmp.replace(dst)
        finally:
            tmp.unlink(missing_ok=True)

    def atomic_same_fs_move(self, src: Path, dst: Path) -> None:
        """Move within a filesystem through an atomic no-clobber claim."""
        if self.claim_destination_by_link(src, dst):
            src.unlink()
        else:
            src.replace(dst)

    @property
    def hash_file_for_offload(self) -> Callable[[Path], str]:
        return self.sha256_of_file

    @property
    def cross_fs_copy_for_offload(self) -> Callable[[Path, Path], None]:
        return self.atomic_cross_fs_copy

    async def verify_hash_or_raise(self, path: Path, expected_hash: str, *, label: str, suffix: str = "") -> None:
        """Verify a streaming hash off-loop and retain the established error copy."""
        actual = await asyncio.to_thread(self.hash_file_for_offload, path)
        if actual != expected_hash:
            msg = f"sha256 mismatch for {label}: expected {expected_hash}, got {actual}{suffix}"
            raise ValueError(msg)


class LocalExecutionFilesystemEngine:
    """Local implementation of the filesystem-move capability."""

    def __init__(self, primitives: FilesystemPrimitives | None = None) -> None:
        self._primitives = primitives or LocalFilesystemPrimitives()

    async def move(self, request: FilesystemMoveRequest, step: MoveStep) -> FilesystemMoveResult:
        """Resolve, verify, and execute one move while preserving replay semantics."""
        item = request.item
        original, owning_root = self._primitives.resolve_containment(item.source_path, request.scan_roots)
        proposed = self._primitives.resolve_destination(item, original, owning_root, request.scan_roots)

        already_moved = self._check_replay_corroborated(original, proposed, item, request.replay_corroborated)
        if already_moved and item.sha256_hash is not None:
            step.current = "verify"
            await self._primitives.verify_hash_or_raise(
                proposed,
                item.sha256_hash,
                label=item.source_path,
                suffix=f" (already-moved replay check against {proposed})",
            )

        if already_moved:
            self._primitives.committed_copy_marker_path(proposed, item.proposal_id).unlink(missing_ok=True)
            return FilesystemMoveResult(proposed=proposed, committed_now=False)

        if item.sha256_hash is not None:
            step.current = "verify"
            await self._primitives.verify_hash_or_raise(original, item.sha256_hash, label=item.source_path)

        await self._apply_file_move(original, proposed, item, step)
        return FilesystemMoveResult(proposed=proposed, committed_now=True)

    def _check_replay_corroborated(
        self,
        original: Path,
        proposed: Path,
        item: ExecuteBatchProposalItem,
        moved_flag_present: bool,
    ) -> bool:
        if original.exists() or not proposed.exists():
            return False
        marker = self._primitives.committed_copy_marker_path(proposed, item.proposal_id)
        return moved_flag_present or marker.exists()

    async def _reclaim_or_refuse_existing_destination(
        self,
        original: Path,
        proposed: Path,
        item: ExecuteBatchProposalItem,
        step: MoveStep,
        *,
        same_fs: bool,
    ) -> None:
        own_marker = self._primitives.committed_copy_marker_path(proposed, item.proposal_id)
        if (
            not same_fs
            and own_marker.exists()
            and await asyncio.to_thread(self._primitives.destination_is_committed_copy, original, proposed, item.sha256_hash)
        ):
            step.current = "delete"
            original.unlink()
            own_marker.unlink(missing_ok=True)
            return
        msg = f"destination already exists, refusing to overwrite: {proposed}"
        raise FileExistsError(msg)

    def _move_same_fs_entry(self, original: Path, proposed: Path, step: MoveStep) -> None:
        if original != proposed and not self._primitives.is_case_only_same_entry(original, proposed) and original.stat().st_nlink > 1:
            step.current = "delete"
            original.unlink()
        else:
            original.replace(proposed)

    async def _move_across_filesystem(
        self,
        original: Path,
        proposed: Path,
        item: ExecuteBatchProposalItem,
        step: MoveStep,
    ) -> None:
        await asyncio.to_thread(self._primitives.cross_fs_copy_for_offload, original, proposed)
        if item.sha256_hash is not None:
            step.current = "verify"
            await self._primitives.verify_hash_or_raise(
                proposed,
                item.sha256_hash,
                label=f"published destination {proposed}",
                suffix=" (post-publish re-verify)",
            )
        marker = self._primitives.committed_copy_marker_path(proposed, item.proposal_id)
        marker.write_text(str(item.proposal_id))
        step.current = "delete"
        original.unlink()
        marker.unlink(missing_ok=True)

    async def _apply_file_move(
        self,
        original: Path,
        proposed: Path,
        item: ExecuteBatchProposalItem,
        step: MoveStep,
    ) -> None:
        step.current = "copy"
        proposed.parent.mkdir(parents=True, exist_ok=True)
        same_fs = self._primitives.same_filesystem(original, proposed.parent)
        if proposed.exists() and not self._primitives.is_same_file(original, proposed):
            await self._reclaim_or_refuse_existing_destination(original, proposed, item, step, same_fs=same_fs)
        elif same_fs:
            if self._primitives.is_same_file(original, proposed):
                self._move_same_fs_entry(original, proposed, step)
            else:
                self._primitives.atomic_same_fs_move(original, proposed)
        else:
            await self._move_across_filesystem(original, proposed, item, step)
