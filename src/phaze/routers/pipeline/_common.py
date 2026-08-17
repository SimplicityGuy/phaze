"""Shared objects for the pipeline router package."""

from __future__ import annotations

# `asyncio` survives only in the `_background_tasks` annotation, so ruff offers to demote it.
# Kept at runtime for the same reason as `uuid` below: this package's imports are deliberately not
# TYPE_CHECKING-gated, and `_background_tasks` is handed to `asyncio.create_task` callers in every
# sibling module.
import asyncio  # noqa: TC003
from pathlib import Path
from typing import Any

# The suppression below is deliberate (runtime import, NOT type-only): this module carries
# `from __future__ import annotations`, so ruff offers to move `uuid` into the TYPE_CHECKING block.
# FastAPI resolves route annotations at RUNTIME via get_type_hints, so a `file_id: uuid.UUID` path
# param would raise NameError on import. (Before phaze-0jpe this import also had a plain runtime
# use -- `uuid.uuid4()` for the scan_live_set nonce -- which masked the rule; the annotation
# requirement is the real reason it must stay here.)
import uuid  # noqa: TC003

from fastapi import APIRouter
from fastapi.templating import Jinja2Templates
import structlog

from phaze.web.static import static_asset_url


# phaze-oau1o: the logger name is PINNED to the old module path rather than taken from ``__name__``.
# Splitting `routers/pipeline.py` into a package would otherwise rename every record this package
# emits to `phaze.routers.pipeline._common`, and the point of the split is that nothing observable
# changes. One shared logger also keeps all eleven submodules on the single name the tests filter on
# (`caplog.at_level(..., logger="phaze.routers.pipeline")`) and that operational log queries use.
logger = structlog.get_logger("phaze.routers.pipeline")

_NO_ACTIVE_AGENT_MESSAGE = "No active agent available — start an agent worker and retry"


# phaze-oau1o: THREE ``.parent`` hops, not two. This file moved one directory deeper
# (`routers/pipeline.py` -> `routers/pipeline/_common.py`), so reaching `src/phaze/templates` now
# costs an extra hop: `_common.py` -> `pipeline/` -> `routers/` -> `phaze/`. Getting this wrong does
# not raise at import time -- Jinja2Templates accepts a non-existent directory and every render then
# fails with TemplateNotFound at request time. `tests/shared/routers/test_pipeline_package_facade.py`
# asserts the resolved directory actually contains the templates this package renders.
TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# phaze-315t: fingerprinted, cache-forever static asset URLs (app.css link + favicon set), used by
# any template rendered through this env that pulls in `base.html`/`shell.html` chrome.
templates.env.globals["static_url"] = static_asset_url
router = APIRouter(tags=["pipeline"])

# Hold references to background enqueue tasks to prevent GC (same pattern as scan.py). Typed
# `Task[Any]` (not `Task[None]`) because `_enqueue_analysis_jobs` returns `list[uuid.UUID]`
# (phaze-4ter) while every other producer here returns `None`.
_background_tasks: set[asyncio.Task[Any]] = set()


# The record pane enrich stage labels (the stage loop in record_body.html) — informational text inside the
# pill's aria-label only. Enrich-only, mirroring STAGE_TO_FUNCTION, because non-enrich stages are
# rejected 422 before this is ever reached (D-10).
_ENRICH_STAGE_LABELS = {"metadata": "Meta", "analyze": "Analyze"}


def _stage_pill_oob(file_id: uuid.UUID, stage: str, bucket: str, *, id_prefix: str = "stage-pill") -> str:
    """Render the shared five-bucket pill as an ``hx-swap-oob`` fragment addressed to ONE (file, stage).

    phaze-5p43: ``_force_skip_dialog.html``'s header contract promises the pill flips to ``⊘ skipped``
    "on the NEXT poll tick", but the dialog ships ONLY inside ``record_body.html``, which is a
    deliberate SNAPSHOT (D-02: renders once, no ``hx-trigger="every"``) — so no tick ever comes and the
    pill contradicts the operator's just-taken action for the life of the open record. Adding a
    full-body poll is forbidden (it would clobber in-progress inline edits in the pending-approval
    ``_diff_row`` islands), so the WRITER — which already knows the new bucket — pushes the single pill
    it invalidated, and nothing else.

    ID UNIQUENESS (load-bearing — this repo has a history of duplicate-id OOB bugs: phaze-gzrd,
    phaze-op6f, phaze-7j50): the default ``stage-pill-{stage}-{file_id}`` id is emitted from exactly
    ONE place, ``record_body.html``'s stage loop, once per (stage, file) — six ids for the one open
    record, and ``record_host.html`` hosts at most one record at a time. The id lives on a WRAPPER
    span, not on the pill itself, so ``_stage_pill.html`` stays a pure, id-free token.

    phaze-bgz26: the Files matrix (``files_table_view.html``) needs the SAME shape for its own
    per-row pill, but MUST NOT reuse the ``stage-pill-*`` id space — that id is reserved for the
    (at-most-one) open record pane, and the matrix can render many rows of the same (stage, file)
    simultaneously with the record pane. ``id_prefix`` namespaces the two: callers targeting the
    Files matrix pass ``id_prefix="files-stage-pill"`` (matching the wrapper span
    ``files_table_view.html`` renders around each cell's ``_stage_pill.html`` include), so a
    per-file retry can safely OOB-push into both surfaces from one response with no id collision.

    ``bucket`` is a derived enum value and ``stage`` is allowlisted; the pill template autoescapes.
    """
    pill = templates.get_template("pipeline/partials/_stage_pill.html").render(
        stage_label=_ENRICH_STAGE_LABELS.get(stage, stage),
        bucket=bucket,
    )
    return f'<span id="{id_prefix}-{stage}-{file_id}" class="inline-flex" hx-swap-oob="true">{pill}</span>'
