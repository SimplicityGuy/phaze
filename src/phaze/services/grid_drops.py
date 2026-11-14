"""The one place a grid is dropped instead of failing an analysis (phaze-7hcgn.5).

Sub-window mood and the beat grid are secondary display data. A failure on their path degrades to "no grids for this
window or file": the grid is left out, the drop is logged here and counted on ``phaze.analysis.grids.dropped``, and the
core analysis -- windows, bpm, key, mood -- is untouched. The rule, the stage table and its general form are in
docs/design/0026-subwindow-mood-and-beat-grid.md, section 10.

Every call site keeps its own narrow ``try`` around the grid work alone; this module is only the shared report, so the
log line and the counter cannot drift between the analysis child, the two payload builders and the agent client.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import logging
from typing import TYPE_CHECKING, Final, Literal

from phaze.telemetry import instruments as telemetry


if TYPE_CHECKING:
    from collections.abc import Iterator


log = logging.getLogger(__name__)

GridStage = Literal["fine_ticks", "coarse_pooling", "assembly", "payload", "server_capability"]

#: Where a grid can be dropped, in pipeline order -- the ``grid_stage`` label's whole domain.
GRID_STAGES: Final[tuple[GridStage, ...]] = ("fine_ticks", "coarse_pooling", "assembly", "payload", "server_capability")

_TALLY: ContextVar[Counter[str] | None] = ContextVar("grid_drop_tally", default=None)


def record_grid_drop(stage: GridStage, what: str, *, count: int = 1, exc_info: bool = False) -> None:
    """Log and count ``count`` grids dropped at ``stage``. ``what`` names the window or file part that lost its grid."""
    log.warning("grid dropped at %s: %s; the core analysis is unaffected", stage, what, exc_info=exc_info)
    telemetry.add("phaze.analysis.grids.dropped", count, grid_stage=stage)
    tally = _TALLY.get()
    if tally is not None:
        tally[stage] += count


@contextmanager
def tally_grid_drops(subject: str) -> Iterator[Counter[str]]:
    """Collect the drops recorded inside the block, and log ONE summary WARNING with the run's count if there were any.

    The per-drop lines name each window; this line is the per-run answer to "how much grid did this file lose". It is
    written only when the block completes: a run that raises has failed for its own reason, and saying its core analysis
    is complete would be false.
    """
    tally: Counter[str] = Counter()
    token = _TALLY.set(tally)
    try:
        yield tally
    finally:
        _TALLY.reset(token)
    if tally:
        stages = ", ".join(f"{stage}={tally[stage]}" for stage in GRID_STAGES if tally[stage])
        log.warning("%s: %d grid(s) dropped this run (%s); the core analysis is complete", subject, sum(tally.values()), stages)
