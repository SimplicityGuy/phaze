"""SAQ task: write_cue_sheet -- CUE file write on the agent's media mount (phaze-6bkk).

``POST /cue/{tracklist_id}/generate`` used to call ``write_cue_file`` inside the api process. Under
DIST-01 the api container has no media mount, so ``path.open("w", ...)`` raised
``FileNotFoundError`` on a parent directory that does not exist there -- for every CUE sheet,
always -- behind a toast that told the operator to check filesystem permissions. The api now
renders the CUE text (pure string work over rows it already holds) and enqueues the bytes here,
onto the OWNING agent's ``meta`` lane, where the archive really is.

FIRE-AND-FORGET by design, unlike ``write_file_tags``: a CUE sheet's only durable artifact is the
file on disk -- there is no control-plane row whose state a callback would advance. A failure
therefore surfaces the way every other unacknowledged agent job does (a raise -> SAQ retry ->
a recorded failed job), rather than through a bespoke callback endpoint that would exist purely to
be logged.

Requires the media mount to be READ-WRITE on the meta lane (docker-compose.agent.yml) -- see
``phaze.tasks.tag_write`` for the phaze-mwvt / phaze-au0r cross-reference.

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy.
Enforced by tests/shared/core/test_task_split.py (Plan 10 / D-25).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import structlog

from phaze.config import AgentSettings, get_settings
from phaze.schemas.agent_tasks import WriteCueSheetPayload
from phaze.services.containment import resolve_and_check_containment
from phaze.services.cue_generator import retarget_cue_file_line, write_cue_file


logger = structlog.get_logger(__name__)


def _write_sync(audio_path: Path, content: str, scan_roots: list[str]) -> tuple[Path, int]:
    """Containment-check the audio path, then pick + write the next CUE version beside it.

    The containment check runs HERE, against the agent's own configured scan roots, rather than on
    the control plane: ``audio_path`` originates as agent-reported ``FileRecord.current_path`` and
    can be re-pointed by ``PATCH /agent/proposals``, and only the agent's filesystem can resolve
    symlinks honestly (``Path.resolve()`` against a path that does not exist in the api container
    normalizes lexically and proves nothing). Same symlink-safe resolve-then-compare the executor
    uses for its move destinations (T-26-11-S1).

    phaze-pqib3 (seam C5): that resolve is also what makes the RETARGET below necessary, which is
    why the two sit together. ``routers/cue.py::generate_cue`` renders ``FILE "<basename>"`` from
    the UNRESOLVED ``current_path`` -- the only name the api can know -- while the line above then
    writes the sheet beside the RESOLVED file. For a symlink into a differently-named target the
    sheet therefore landed in the target's directory naming the symlink, and ``FILE`` is a bare
    name that every consumer resolves relative to the ``.cue``'s OWN directory. This function is
    the first and only place that knows both halves, so it reconciles them: the sheet names the
    file it is actually placed beside. Nothing is retargeted for a path that is its own realpath,
    which is every non-symlinked file in the archive.
    """
    resolved, _owning_root = resolve_and_check_containment(str(audio_path), scan_roots)
    return write_cue_file(retarget_cue_file_line(content, resolved.name), resolved)


async def write_cue_sheet(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:  # noqa: ARG001  -- ctx is SAQ's required first arg; this task needs nothing from it
    """Write one pre-rendered CUE sheet next to its audio file on the agent."""
    payload = WriteCueSheetPayload.model_validate(kwargs)
    cfg = get_settings()
    scan_roots = list(cfg.scan_roots) if isinstance(cfg, AgentSettings) else []

    logger.info("cue write started", file_id=str(payload.file_id), tracklist_id=str(payload.tracklist_id))

    # phaze-9dwb: the whole blocking sequence (exists() + iterdir() version scan, create, write)
    # is ONE thread offload -- the same discipline the api used to lack. The agent's event loop
    # runs the Phase-46 liveness heartbeat, so a hung mount must not block it.
    written_path, version = await asyncio.to_thread(_write_sync, Path(payload.audio_path), payload.content, scan_roots)

    logger.info(
        "cue write completed",
        file_id=str(payload.file_id),
        tracklist_id=str(payload.tracklist_id),
        cue_version=version,
    )
    return {"file_id": str(payload.file_id), "cue_filename": written_path.name, "cue_version": version}
