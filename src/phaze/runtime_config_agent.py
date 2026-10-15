"""Authenticated runtime-policy polling shared by agents and one-shot jobs.

Owns config fetch/validation/digest/backoff state only. Importing it never starts a liveness
loop, writes agent last-seen timestamps, constructs database resources or imports worker code.
Each caller owns its context and schedule; rejected sets retain last-good and retry as before.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from phaze.runtime_config import partition_overrides


logger = structlog.get_logger(__name__)
DEFAULT_CONFIG_POLL_TIMEOUT_SECONDS = 5.0
DEFAULT_REJECT_BACKOFF_MAX_TICKS = 20


async def poll_agent_runtime_config(
    ctx: dict[str, Any],
    client: Any,
    *,
    timeout_seconds: float = DEFAULT_CONFIG_POLL_TIMEOUT_SECONDS,
    backoff_max_ticks: int = DEFAULT_REJECT_BACKOFF_MAX_TICKS,
) -> None:
    """Fetch the DB-override layer and reload the agent's LOCAL store only on a real change.

    phaze-mvq8z.9, ADR-0019 (runtime config hot-reload) §14. ``ctx["runtime_config_store"]`` is populated by
    ``phaze.tasks.agent_worker.startup`` (phaze-mvq8z.5/.9) -- its absence (an older agent_worker
    build, or a test ctx that never ran startup) degrades this to a no-op, the same shape every
    other optional-ctx-key read in this module already takes (``ctx["worker"]`` absent ->
    queue_depth defaults to 0, see :func:`_probe_broker_for_queued_jobs`).

    Every failure past that point -- a network blip, an auth error, a malformed response, or (in a
    test with an unconfigured mock client) an attribute that is not really a mapping -- degrades to
    "local config unchanged this tick" and is logged at WARNING. It NEVER prevents, and is never
    gated on, the heartbeat POST that keeps the agent alive: this function runs BEFORE that POST is
    built (so a just-applied override is reflected in the SAME beat's ``effective_config``), but a
    failure here does not stop the POST from being sent -- and neither does a HANG, because the GET
    is bounded by its own ``timeout_seconds``, far inside the beat's (phaze-mvq8z.20).
    """
    store = ctx.get("runtime_config_store")
    if store is None:
        return
    try:
        response = await asyncio.wait_for(client.get_config(), timeout=timeout_seconds)
        overrides = dict(response.overrides)
        digest = str(response.digest)
    except TimeoutError:
        logger.warning("heartbeat: runtime-config poll timed out after %.1fs; local config unchanged", timeout_seconds)
        return
    except Exception:
        logger.warning("heartbeat: runtime-config poll failed; local config unchanged", exc_info=True)
        return
    if digest == ctx.get("_runtime_config_digest"):
        return
    rejection: _RejectedSet | None = ctx.get("_runtime_config_rejected")
    if rejection is not None and rejection.digest == digest and rejection.ticks_to_skip > 0:
        # The SAME set the store already rejected, still inside its backoff: re-running the reload
        # would re-read the file, repeat every warning and metric, and overwrite
        # ``last_reload.at`` with "now", hiding when the rejection began (phaze-dhycx).
        rejection.ticks_to_skip -= 1
        return
    # phaze-mvq8z.22: the endpoint filters by the SERVER's reloadable keys, and a newer control
    # plane can know a key this agent's build does not. Rejecting the whole reload for it would
    # strand every other override (log_level included) until the agent upgrades, so the unknown
    # key is dropped and named, and the rest applies -- the same version-skew tolerance .20 gave
    # the heartbeat POST.
    overrides, ignored = partition_overrides(overrides)
    if ignored:
        logger.warning("heartbeat: ignoring runtime-config override keys this agent does not know", ignored=list(ignored))
    ctx["_runtime_config_overrides"] = overrides

    async def _override_provider() -> dict[str, Any]:
        # Reads ctx fresh on every call (not the `overrides` closed over above) so a LATER
        # trigger on the same store -- SIGHUP, a file-watch tick -- also sees whatever this
        # function last fetched, not a snapshot frozen at registration time.
        return dict(ctx.get("_runtime_config_overrides") or {})

    store.set_override_provider(_override_provider)
    # The digest is recorded only once the set it names is IN FORCE (phaze-mvq8z.22). Recorded
    # before the reload, a rejected set -- a transient validator veto, a failed applier -- was never
    # retried until the operator changed the set again. A rejection FORGETS the digest instead, so
    # the next tick reloads whatever the endpoint then serves: the same set again (a retry), or a
    # withdrawal back to the set already in force, which must still be reloaded -- the provider
    # above holds the rejected set until it is.
    if (await store.reload("poll")).successful:
        ctx["_runtime_config_digest"] = digest
        ctx.pop("_runtime_config_rejected", None)
        return
    ctx.pop("_runtime_config_digest", None)
    # Retry a rejected set with exponential backoff (phaze-dhycx): the first retry is on the very
    # next tick (a transient veto clears at once), then every 1, 2, 4 ... ticks up to the cap. A
    # DIFFERENT digest -- including a withdrawal back to the set in force -- never matches the
    # remembered one, so it reloads immediately.
    attempts = rejection.attempts + 1 if rejection is not None and rejection.digest == digest else 1
    ctx["_runtime_config_rejected"] = _RejectedSet(digest, attempts, rejection_backoff_ticks(attempts, maximum_ticks=backoff_max_ticks))


class _RejectedSet:
    """A poll-delivered override set the store rejected, and how many ticks to leave it alone."""

    __slots__ = ("attempts", "digest", "ticks_to_skip")

    def __init__(self, digest: str, attempts: int, ticks_to_skip: int) -> None:
        self.digest = digest
        self.attempts = attempts
        self.ticks_to_skip = ticks_to_skip


def rejection_backoff_ticks(attempts: int, *, maximum_ticks: int = DEFAULT_REJECT_BACKOFF_MAX_TICKS) -> int:
    """Ticks to skip after the ``attempts``-th consecutive rejection of one set: 0, 1, 2, 4 ... capped."""
    if attempts < 2:
        return 0
    return min(1 << (attempts - 2), maximum_ticks)
