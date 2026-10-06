"""Server-side seed for the shell's ``$store.pipeline`` -- real values on the FIRST paint (phaze-s8xtd).

The Alpine store used to boot at literal ``0`` for every count, so each full page load showed
"Agents online 0 · Lanes active 0", an empty rail, "0 files ... 0 not yet enriched" and "No orphaned
enrich work detected" until the first ``/pipeline/stats`` poll replaced them with 145,057 / 91 / ORPHANED
WORK DETECTED. A zero on that screen is indistinguishable from a measured zero, which is the defect.

This module computes the SAME values the poll pushes, with the SAME functions (``get_stage_progress``,
``_derive_stats``, ``_build_dag_context``), and hands them to ``shell.html`` to merge over the store's
defaults. It is deliberately NOT a second description of what the store holds: if it were a hand-rolled
list of keys, it would drift from the poll the first time a key was added.

COST. Only a FULL-document render pays it (a direct load, a bookmark, a history restore); an htmx rail
swap returns a fragment and never calls this -- the poll keeps the store live there. One full-page load
costs one ``get_stage_progress`` (its ~13 concurrent COUNTs, each in its own session under the cap-4
fan-out -- the same read the 5 s poll does every tick, and the slowest of the poll's reads) plus the
remainder of ``_build_dag_context`` (stage controls, metadata selection summary, the SAQ stage-activity
snapshot, three small agent/lane reads, the match-busy count), i.e. roughly the dag half of ONE poll tick
and none of the other twelve reads. The orphan counts are an O(1) in-process cache read. The Analyze
workspace already builds both inputs for its own render, so it is reused there instead of re-read, and
the Summary page pays ``get_stage_progress`` twice (once for its own overview, once here).

DEGRADE. The seed is best-effort by construction: any failure returns ``None`` and the page renders the
em-dash placeholder (``seedKnown`` stays 0), never a fabricated number. Nothing here may 500 a page load.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from phaze.routers.pipeline import _build_dag_context, _derive_stats
from phaze.services.pipeline import get_stage_progress


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = logging.getLogger(__name__)


def _seed_from(stats: dict[str, int], dag: dict[str, int]) -> dict[str, int]:
    """Assemble the store seed from the poll's ``stats`` + ``dag`` shapes (the shapes ``stats_bar.html`` pushes)."""
    return {
        "discovered": int(stats["discovered"]),
        "analyzed": int(stats["analyzed"]),
        "metadataExtracted": int(stats["metadata_extracted"]),
        **{key: int(value) for key, value in dag.items()},
    }


async def build_pipeline_store_seed(app_state: Any, session: AsyncSession, context: dict[str, Any]) -> dict[str, int] | None:
    """Return the ``$store.pipeline`` seed for a full-document render, or ``None`` when it cannot be measured.

    ``context`` is the stage context already built for this render: when it carries the Analyze workspace's
    ``stats`` and ``dag`` (``build_dashboard_context``), they are reused rather than re-read.
    """
    try:
        stats = context.get("stats")
        dag = context.get("dag")
        if not isinstance(stats, dict) or not isinstance(dag, dict):
            stage_progress = await get_stage_progress(session)
            stats = _derive_stats(stage_progress)
            dag = (await _build_dag_context(app_state, session, {}, stage_progress))["dag"]
        return _seed_from(stats, dag)
    except Exception:
        # Broad by design: a first-paint seed must never 500 the page it decorates. The poll still runs.
        logger.warning("shell_store_seed_degraded", exc_info=True)
        return None
