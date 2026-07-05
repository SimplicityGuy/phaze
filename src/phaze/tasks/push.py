"""SAQ task: push_file -- rsync-over-SSH push of a media file to the compute scratch dir (Phase 50).

The file-server agent (which owns the media mount) rsyncs a cloud-routed long file to the compute
agent's scratch directory over SSH-over-Tailscale, then reports success through a control-side
callback (the agent is Postgres-free, so the push -> process_file handoff goes via HTTP, NOT a
direct enqueue -- RESEARCH §Critical Finding 1). The file-server initiates; the compute agent only
receives (CLOUDPIPE-02 directional invariant).

This module MUST NOT import phaze.database, phaze.models.*, or sqlalchemy. Enforced by
tests/test_task_split.py (D-25 import boundary). It carries ONLY stdlib (asyncio/subprocess/
pathlib/tempfile), phaze.config (AgentSettings narrowing), phaze.schemas (PushFilePayload), and
references PhazeAgentClient via ctx["api_client"] at runtime.

Transport invariants (RESEARCH §"rsync-over-SSH from asyncio", D-06/D-07):
- argv is a Python list spawned via ``asyncio.create_subprocess_exec`` -- NEVER a shell
  (no shell-injection surface; the remote path is the server UUID, never the untrusted filename).
- the SSH host key is PINNED: ``StrictHostKeyChecking=yes`` + a fixed ``UserKnownHostsFile``;
  ``BatchMode=yes`` makes ssh fail fast instead of hanging a worker slot on an auth prompt.
- atomicity comes for free from rsync's default temp-file-then-rename behavior -- we DO NOT use
  ``--inplace`` (which would let a reader see a half-written file). ``--partial-dir`` keeps a
  resumable partial out of the final-name space; ``--timeout`` bounds an I/O stall.
- ``-z``/``-c``/``-a`` are omitted: audio is already compressed, the app-level sha256 verify
  (Plan 50-04) covers integrity, and perms/owner preservation is irrelevant for ephemeral scratch.
- a missing rsync/ssh binary is a clear TERMINAL error -- the task NEVER falls back to local
  analysis (CLOUDROUTE-02 invariant).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any

from phaze.config import AgentSettings, get_settings
from phaze.schemas.agent_tasks import PushFilePayload


if TYPE_CHECKING:
    from phaze.services.agent_client import PhazeAgentClient


# Bound the rsync stderr snippet that crosses into an error message so a runaway rsync cannot ship
# a multi-megabyte string into the SAQ job error / logs (T-50-secret-leak DoS bound).
_STDERR_SNIPPET_MAX = 500

# The outer asyncio.wait_for bound sits ABOVE the rsync --timeout so rsync's own I/O-stall kill
# (exit 30) fires first on a stall; this outer layer is the belt-and-suspenders cap for the rare
# case rsync itself wedges without honoring --timeout (mirrors the process_file inner<outer pattern).
_OUTER_TIMEOUT_BUFFER_SEC = 30

# WR-03: the SAQ job-net timeout a producer MUST stamp on a push_file enqueue. It has to sit
# strictly ABOVE the asyncio outer guard (push_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC) so SAQ never
# cancels the coroutine before that guard fires -- a SAQ timeout cancels via CancelledError (NOT
# TimeoutError), and only the asyncio guard reaps the rsync child. With the default push_timeout_sec
# of 600 the layering is rsync --timeout=600 < asyncio outer=630 < SAQ net=690, so the kill is
# deterministic and the child is always reaped before the secret-shredding finally. Producers live
# on the CONTROL plane (which does not see the agent's AgentSettings.push_timeout_sec), so this is
# derived from the documented default + a 30s margin above the outer guard. An operator who raises
# PHAZE_PUSH_TIMEOUT_SEC on the agent must raise this margin too (both live in the same deployment).
_SAQ_JOB_TIMEOUT_MARGIN_SEC = 30
PUSH_FILE_SAQ_TIMEOUT_SEC = 600 + _OUTER_TIMEOUT_BUFFER_SEC + _SAQ_JOB_TIMEOUT_MARGIN_SEC


def _agent_settings() -> AgentSettings:
    """Return the AgentSettings for this worker process (mirrors functions._agent_settings).

    ``push_file`` is registered ONLY on the agent worker (``PHAZE_ROLE=agent``), so
    ``get_settings()`` returns an :class:`AgentSettings`. The module-level ``settings`` singleton is
    ``ControlSettings``-typed and intentionally lacks the agent-only push_* fields, so we MUST
    resolve via ``get_settings()`` and narrow.
    """
    cfg = get_settings()
    if not isinstance(cfg, AgentSettings):  # pragma: no cover - defensive; worker always agent-role
        msg = f"push_file requires PHAZE_ROLE=agent; get_settings() returned {type(cfg).__name__}"
        raise RuntimeError(msg)
    return cfg


def _build_rsync_argv(
    cfg: AgentSettings,
    payload: PushFilePayload,
    *,
    key_path: str,
    known_hosts_path: str,
) -> list[str]:
    """Build the shell-free rsync argv for one push transfer (pure -- unit-testable, no I/O).

    The ``-e "ssh …"`` is a SINGLE argv element that rsync parses internally (it is NOT handed to a
    shell). The remote destination is ``<scratch_dir>/<file_id>.<file_type>`` -- the server-generated
    UUID, never the untrusted original filename (eliminates path-traversal / shell-metachar risk and
    makes the cleanup/janitor target deterministically computable from file_id).

    D-04 (MCOMP-03): the host + scratch dir + user come from the payload (``dest_host`` /
    ``dest_scratch_dir`` / ``dest_ssh_user``) so N compute agents each receive files at their OWN
    destination, resolved per file. The fileserver's single-global remote-target env
    (``cfg.push_ssh_host`` + ``cfg.cloud_scratch_dir``) is retired here -- it is no longer read.
    ``dest_ssh_user=None`` falls back to ``cfg.push_ssh_user`` (preserves ≤1-compute behavior
    byte-identical).
    """
    ssh_cmd = (
        f"ssh -i {key_path} -o StrictHostKeyChecking=yes "
        f"-o UserKnownHostsFile={known_hosts_path} -o BatchMode=yes "
        f"-o ConnectTimeout={cfg.push_connect_timeout_sec}"
    )
    ssh_user = payload.dest_ssh_user or cfg.push_ssh_user
    remote_dest = f"{ssh_user}@{payload.dest_host}:{payload.dest_scratch_dir}/{payload.file_id}.{payload.file_type}"
    return [
        "rsync",
        "--partial-dir=.rsync-partial",  # resumable partial kept OUT of the final-name space
        f"--timeout={cfg.push_timeout_sec}",  # I/O-stall timeout -> rsync exit 30
        "-e",
        ssh_cmd,
        "--",  # argv terminator: no operand below can smuggle an rsync flag (#sec argv-injection)
        payload.original_path,  # media-mount source (read by the fileserver)
        remote_dest,
    ]


def _require_push_config(cfg: AgentSettings) -> None:
    """Fail fast (clear terminal error) if the operator-provisioned push config is incomplete.

    D-04: the remote target (``push_ssh_host`` + ``cloud_scratch_dir``) is now carried per file on the
    payload (``dest_host`` / ``dest_scratch_dir``), so it is NO LONGER part of the required set -- the
    fileserver's single-global remote-target read is retired. What stays required is the SSH secret
    material (``push_ssh_key`` + ``push_known_hosts``, D-03) plus ``push_ssh_user`` (the
    ``dest_ssh_user=None`` fallback source). Note: ``cloud_scratch_dir`` is dropped only from THIS
    (fileserver) required set -- the AgentSettings field itself survives because the compute agent's
    OWN local janitor (agent_worker.py) still reads it (Landmine 2).
    """
    missing = [name for name in ("push_ssh_user", "push_ssh_key", "push_known_hosts") if getattr(cfg, name) is None]
    if missing:
        msg = f"push_file missing required push config: {', '.join(missing)} (operator-provisioned in Phase 51)"
        raise RuntimeError(msg)
    # WR-03: the timeout layering MUST stay inner(rsync) < outer(asyncio) < SAQ-net, otherwise a SAQ
    # CancelledError reaps the rsync child before the asyncio guard's secret-shredding finally runs.
    # PUSH_FILE_SAQ_TIMEOUT_SEC is a control-side module constant derived from the DEFAULT
    # push_timeout_sec; the control plane cannot see this agent's AgentSettings.push_timeout_sec. So
    # an operator who raises PHAZE_PUSH_TIMEOUT_SEC on the agent without bumping the control-side net
    # would silently invert the layering (SAQ cancels healthy long transfers minutes early). Turn that
    # silent footgun into a loud fail-fast at the agent's first push.
    outer_guard = cfg.push_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC
    if outer_guard >= PUSH_FILE_SAQ_TIMEOUT_SEC:
        msg = (
            f"push_file timeout layering inverted: rsync+asyncio outer guard ({cfg.push_timeout_sec}+"
            f"{_OUTER_TIMEOUT_BUFFER_SEC}={outer_guard}s) must be STRICTLY BELOW the control-side SAQ "
            f"net timeout PUSH_FILE_SAQ_TIMEOUT_SEC ({PUSH_FILE_SAQ_TIMEOUT_SEC}s). You raised "
            f"PHAZE_PUSH_TIMEOUT_SEC on the agent without raising the control-side margin — SAQ would "
            f"cancel healthy long transfers before the rsync child is reaped (WR-03)."
        )
        raise RuntimeError(msg)


async def push_file(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Rsync a cloud-routed file to the compute scratch dir, then report success via HTTP.

    rc==0 -> ``api.report_pushed(file_id)`` (control flips the file to PUSHED and enqueues
    ``process_file`` against the scratch copy). rc!=0 -> RuntimeError (SAQ retry; ``--partial``
    resumes). Missing rsync/ssh binary -> clear terminal RuntimeError, NO callback, NO local
    fallback. The SSH key + known_hosts SecretStr contents are materialized to private temp files
    (0600) for the duration of the transfer and shredded in ``finally`` -- their paths/contents are
    never logged (T-50-secret-leak).
    """
    payload = PushFilePayload.model_validate(kwargs)
    api: PhazeAgentClient = ctx["api_client"]
    cfg = _agent_settings()
    _require_push_config(cfg)

    # Materialize the file-mounted secrets (SecretStr CONTENTS) into a private temp dir so ssh -i /
    # UserKnownHostsFile have real paths to read. The dir is 0700 and the key file 0600 (ssh refuses
    # a world-readable identity); both are removed in finally.
    tmp_dir = Path(tempfile.mkdtemp(prefix="phaze-push-"))
    key_path = tmp_dir / "id_key"
    known_hosts_path = tmp_dir / "known_hosts"
    try:
        key_path.write_text(cfg.push_ssh_key.get_secret_value())  # type: ignore[union-attr]  # _require_push_config asserts not None
        key_path.chmod(0o600)
        known_hosts_path.write_text(cfg.push_known_hosts.get_secret_value())  # type: ignore[union-attr]
        known_hosts_path.chmod(0o600)

        argv = _build_rsync_argv(cfg, payload, key_path=str(key_path), known_hosts_path=str(known_hosts_path))

        try:
            # Fixed list argv, no shell; remote path is the server UUID (T-50-injection): neither
            # ruff S603 nor bandit B603 flags create_subprocess_exec with a list argv.
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            # rsync (or ssh) is not installed/on PATH. TERMINAL -- surface clearly and NEVER fall
            # back to local analysis (T-50-no-fallback / CLOUDROUTE-02). Provisioned in Phase 51.
            missing = exc.filename or "rsync/ssh"
            msg = f"push_file: required binary {missing!r} not found; cannot push (no local fallback)"
            raise RuntimeError(msg) from exc

        try:
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=cfg.push_timeout_sec + _OUTER_TIMEOUT_BUFFER_SEC)
        except (TimeoutError, asyncio.CancelledError):
            # Outer-layer kill (rsync wedged past its own --timeout) OR a SAQ job-net cancellation
            # (CancelledError, NOT TimeoutError -- WR-03). Either way reap the child BEFORE the
            # ``finally`` shreds id_key/known_hosts, so no live ``ssh -i`` keeps reading secret files
            # we are about to delete and no rsync child is orphaned. ``--partial`` resumes on retry.
            proc.kill()
            await proc.wait()
            raise

        if proc.returncode != 0:
            snippet = err.decode(errors="replace")[:_STDERR_SNIPPET_MAX]
            msg = f"push_file: rsync exit {proc.returncode} for file_id={payload.file_id}: {snippet}"
            raise RuntimeError(msg)
    finally:
        # Shred the materialized secrets regardless of outcome.
        for secret_file in (key_path, known_hosts_path):
            secret_file.unlink(missing_ok=True)
        tmp_dir.rmdir()

    # rc==0: the push landed atomically (rsync temp-then-rename). Hand off to control (D-08).
    await api.report_pushed(payload.file_id)
    return {"file_id": str(payload.file_id), "status": "pushed"}
