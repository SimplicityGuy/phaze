"""Pydantic schemas for the compute-scratch janitor liveness probe (phaze-5cvbz).

``POST /api/internal/agent/scratch/live`` -- the compute agent's startup janitor
(``phaze.tasks.agent_worker._maybe_sweep_scratch``) asks the control plane which of its
age-eligible scratch entries are still claimed by a queued/active job BEFORE deleting them,
instead of relying solely on a fixed ``min_age_sec`` ceiling as a proxy for liveness. That
proxy was proven unsafe (phaze-5cvbz): a durable ``process_file`` job can sit ``QUEUED``
(never dequeued under a concurrency-1 clamp, or parked at the ``SENTINEL`` far-future
timestamp by a stage pause -- ``phaze.tasks._shared.stage_control``) for far longer than any
finite ceiling, with its scratch copy's mtime frozen at push-completion the whole time.

``ScratchLivenessRequest`` is a REQUEST model (``extra="forbid"``, Phase 25 D-16 discipline).
``ScratchLivenessResponse`` is control-TRUSTED and stays loose (``extra="ignore"``) so a
control-plane-first rolling deploy adding an additive field never breaks an older agent image
(mirrors ``PushedResponse`` / ``PushMismatchResponse`` in ``schemas/agent_push.py``).
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field


# Bounds one probe request to a single sweep pass's worth of stale entries. A compute scratch
# dir holds at most a handful of multi-GB pushed files at once (D-14 module docstring); this
# cap is generous headroom while still bounding the request/query size against a pathological
# scratch dir.
_MAX_SCRATCH_LIVENESS_IDS = 500


class ScratchLivenessRequest(BaseModel):
    """Body the compute agent POSTs with the file_ids of its age-eligible scratch FILE entries.

    ``file_ids`` are parsed from scratch filenames (``<file_id>.<file_type>`` -- deterministic
    per ``push.py::_build_rsync_argv``'s destination naming), never trusted identity from
    elsewhere. Empty is valid (the sweep still wants ``push_file_active`` to gate the shared
    ``.rsync-partial`` directory).
    """

    model_config = ConfigDict(extra="forbid")

    file_ids: list[uuid.UUID] = Field(default_factory=list, max_length=_MAX_SCRATCH_LIVENESS_IDS)


class ScratchLivenessResponse(BaseModel):
    """Which of the requested ``file_ids`` still have a live (queued/active) ``process_file`` job.

    ``live_file_ids`` is the exact subset of the request's ``file_ids`` the control plane found
    a ``saq_jobs`` row for with key ``process_file:<file_id>`` and ``status IN ('queued',
    'active')`` -- this INCLUDES a job parked at the pause ``SENTINEL`` (``status`` stays
    ``'queued'`` while parked; ``phaze.tasks._shared.stage_control.repark_if_stage_paused``),
    which is exactly the indefinite-park case a fixed age ceiling cannot cover.

    ``push_file_active`` is coarser: True when ANY ``push_file`` job (any file) is currently
    queued/active, regardless of the request's ``file_ids``. It gates the shared
    ``.rsync-partial`` staging directory (``push.py`` ``--partial-dir``), whose mtime advances
    only on directory entry create/remove and so freezes at transfer START for a single
    long-running transfer -- unrelated to any one ``file_id``.

    RESPONSE-only model the agent TRUSTS from the control plane -- stays loose
    (Phase 25 convention, mirrors ``PushedResponse``).
    """

    model_config = ConfigDict(extra="ignore")

    live_file_ids: list[uuid.UUID] = Field(default_factory=list)
    push_file_active: bool = False
