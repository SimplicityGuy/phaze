"""The REAL agent worker, plus one gated job, for the live-retune end-to-end test (phaze-mvq8z.11).

Run as its own OS process by SAQ's own CLI -- ``python -m saq tests.integration._retune_agent_harness.settings``,
the same ``saq <module>.settings`` entry point every compose file runs -- never imported by the test.
``settings`` IS ``phaze.tasks.agent_worker.settings`` with one function appended: the production
startup hook (runtime-config store, SIGHUP handler, directory watch, heartbeat + config poll, the
resizable analysis limiter, and ``install_live_concurrency`` adopting the plain ``saq.Worker`` the CLI
built), the production queue, hooks and lane selection all run unmodified.

Two things differ from a deployment, both test cadence rather than mechanism:

* :func:`retune_probe`, the gated job. It takes ``ctx["analysis_semaphore"]`` -- the SAME limiter
  ``process_file`` holds around every analysis child -- and then parks until the test drops a release
  file, so the test decides when each job ends. It records ``start`` (a SAQ job loop is running it),
  ``hold`` (it is inside the analysis pool) and ``done`` to an append-only events file, one line
  each; ``done`` is written in the same loop step that releases the limiter, so no other job can be
  admitted between the two.
* The heartbeat cadence (``AGENT_HEARTBEAT_INTERVAL_SECONDS``, 30 s in production, and so also the
  admin-override poll's cadence) comes from the environment, so an override reaches the agent in
  well under a second instead of up to half a minute. What the poll does on each beat is unchanged.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from phaze.tasks import agent_worker, heartbeat


PROBE_DIR_ENV = "PHAZE_RETUNE_PROBE_DIR"
HEARTBEAT_INTERVAL_ENV = "PHAZE_RETUNE_HEARTBEAT_INTERVAL_SEC"
EVENTS_FILE = "events.log"
#: How often a parked job looks for its release file. A poll cadence, not a margin.
_RELEASE_POLL_SEC = 0.02

heartbeat.AGENT_HEARTBEAT_INTERVAL_SECONDS = float(os.environ[HEARTBEAT_INTERVAL_ENV])


def release_path(probe_dir: Path, name: str) -> Path:
    return probe_dir / f"{name}.release"


def _record(probe_dir: Path, kind: str, name: str) -> None:
    with (probe_dir / EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(f"{kind} {name}\n")


async def retune_probe(ctx: dict[str, Any], *, name: str) -> str:
    probe_dir = Path(os.environ[PROBE_DIR_ENV])
    _record(probe_dir, "start", name)
    async with ctx["analysis_semaphore"]:
        _record(probe_dir, "hold", name)
        while not release_path(probe_dir, name).exists():
            await asyncio.sleep(_RELEASE_POLL_SEC)
    _record(probe_dir, "done", name)
    return name


settings = {**agent_worker.settings, "functions": [*agent_worker.settings["functions"], retune_probe]}
