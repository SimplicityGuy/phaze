"""Admin API/UI for the DB-override layer of hot-reloadable config (phaze-mvq8z.6, ADR-0019 (runtime config hot-reload) §3/§10).

Thin POST/DELETE endpoints over ``runtime_config_override``, mirroring the ``route_control`` /
``pipeline_stages`` thin-endpoint discipline: load-or-create, mutate, commit in one transaction,
return the re-rendered partial. GET serves the same partial standalone (the shell pane's own
context builder, :func:`build_runtime_config_pane_context`, is shared with it -- see that
function's docstring).

Server-side validation runs through the SAME core :meth:`~phaze.runtime_config.RuntimeConfigStore.preview`
a real reload would use (phaze-mvq8z.4) BEFORE anything reaches the DB, so an invalid override is
rejected with nothing stored (this bead's acceptance) rather than written and then rolled back.

Security (threat model, ADR-0019 (runtime config hot-reload) §10): NO ``get_authenticated_agent`` dependency -- these routes
sit behind the same reverse-proxy private-LAN trust boundary as ``route_control`` / ``admin_agents``
/ every other operator-facing admin surface in this repo. The compensating control is audit
logging on EVERY attempt: a successful set/clear triggers an immediate in-process
``store.reload("api")``, which ``phaze.runtime_config._emit`` logs with the changed key(s), old and
new values, and outcome; a REJECTED attempt (unknown/restart-only key, or a value the core's
validation refuses) is logged here instead, since it never reaches ``reload()`` at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Path as PathParam, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import structlog

from phaze.database import get_session
from phaze.runtime_config import RELOADABLE_KEYS, RESTART_ONLY_KEYS, ReloadRejectedError, RuntimeConfig, get_runtime_config_store
from phaze.services.runtime_config_overrides import clear_runtime_config_override, get_runtime_config_overrides, set_runtime_config_override
from phaze.web.template_globals import register_set_glyph_globals


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


logger = structlog.get_logger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
register_set_glyph_globals(templates.env)

router = APIRouter(prefix="/admin/runtime-config", tags=["admin"])

#: ``log_level``'s operator-facing options. ``RuntimeConfig._known_level`` (phaze.runtime_config)
#: actually accepts any name ``logging.getLevelNamesMapping()`` carries -- this is the practical
#: subset a <select> offers, not the full validation boundary.
_LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

#: wire_bounds.py rule 1 -- matches ``RuntimeConfigOverride.key``'s ``String(64)`` column width
#: exactly (the path segment lands there via ``set_runtime_config_override``/``clear_runtime_config_override``).
_KEY_MAX_LENGTH = 64
#: wire_bounds.py rule 7 -- a DoS-reason bound, NOT a column width: the override VALUE lands in a
#: JSONB column with no fixed width, but every real RELOADABLE_KEYS value is a short int or a
#: log-level name (longest: "CRITICAL", 8 chars), so an operator form submission has no legitimate
#: reason to be longer than this.
_VALUE_MAX_LENGTH = 64


def _coerce(key: str, raw: str) -> Any:
    """Coerce a raw HTML-form string to the type ``RuntimeConfig`` (``strict=True``) expects.

    An HTML form field is always text; the ``file`` (TOML) layer supplies real ints/strings
    natively, so this is where the override layer reconstructs the same typing. Every
    ``RELOADABLE_KEYS`` field is either ``int`` or ``str`` (``log_level``) -- nothing else.
    """
    if RuntimeConfig.model_fields[key].annotation is int:
        try:
            return int(raw.strip())
        except ValueError as exc:
            detail = f"{key}: expected an integer, got {raw!r}"
            logger.warning("phaze.runtime_config_admin override rejected", key=key, attempted=raw, error=detail)
            raise HTTPException(status_code=400, detail=detail) from exc
    return raw


async def build_runtime_config_pane_context(session: AsyncSession) -> dict[str, Any]:
    """Build the Runtime Config utility pane's context (UTILITY_PANES, not a DAG pipeline stage).

    Shared by BOTH producers that can render this pane -- the shell's ``/s/runtime-config`` (via
    ``routers.shell.stage_context``) and this module's own ``GET /admin/runtime-config/_table`` --
    so the two cannot independently drift (mirrors ``admin_agents.build_agents_pane_context``,
    phaze-uvmcr.4).

    Reads the STORE's live snapshot (already resolved through every layer -- override, file, env,
    default -- via ``RuntimeConfigStore.snapshot()``), not the DB directly: the snapshot IS the
    effective value + source layer this pane exists to show (ADR-0019 (runtime config hot-reload) §15), and re-deriving it
    from a fresh DB read here would risk disagreeing with what every OTHER reader of the store
    (appliers, telemetry) currently sees. ``session`` is accepted (and unused) only so this
    function's signature matches every other ``_STAGE_CONTEXT_BUILDERS`` entry's shape.
    """
    del session
    snapshot = get_runtime_config_store().snapshot()
    reloadable = [
        {"key": key, "value": getattr(snapshot.config, key), "source": snapshot.sources.get(key, "default")} for key in sorted(RELOADABLE_KEYS)
    ]
    return {
        "reloadable_keys": reloadable,
        "restart_only_keys": sorted(RESTART_ONLY_KEYS),
        "log_levels": _LOG_LEVELS,
    }


async def _render_table(request: Request, session: AsyncSession) -> HTMLResponse:
    context = await build_runtime_config_pane_context(session)
    context["request"] = request
    return templates.TemplateResponse(request=request, name="admin/partials/_runtime_config_table.html", context=context)


def _reject_unless_reloadable(key: str, *, attempted: Any) -> None:
    """Raise 400 with a structured audit line for a key this endpoint will not even preview."""
    if key in RELOADABLE_KEYS:
        return
    detail = "requires restart" if key in RESTART_ONLY_KEYS else "unknown key"
    logger.warning("phaze.runtime_config_admin override rejected", key=key, attempted=attempted, error=detail)
    raise HTTPException(status_code=400, detail=detail)


@router.get("/_table", response_class=HTMLResponse)
async def table(request: Request, session: AsyncSession = Depends(get_session)) -> HTMLResponse:
    """GET the reloadable-key table fragment standalone (also reachable at ``/s/runtime-config``)."""
    return await _render_table(request, session)


@router.post("/{key}", response_class=HTMLResponse)
async def set_override(
    request: Request,
    key: Annotated[str, PathParam(max_length=_KEY_MAX_LENGTH)],
    value: Annotated[str, Form(max_length=_VALUE_MAX_LENGTH)],
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Set ``key``'s DB override to ``value``.

    Validates via ``RuntimeConfigStore.preview()`` -- the SAME candidate-build/validate path a
    real reload uses -- BEFORE writing anything, so an invalid value is rejected (400, the error
    from the core) with NOTHING stored. On success, persists the override (which ``NOTIFY``s
    every other listening control-plane process, ``phaze.runtime_config_notify``) and triggers an
    immediate in-process ``reload("api")`` so this same request's write is already live by the
    time it returns, rather than waiting on this process's own LISTEN round-trip.
    """
    _reject_unless_reloadable(key, attempted=value)
    coerced = _coerce(key, value)
    store = get_runtime_config_store()
    current_overrides = await get_runtime_config_overrides(session)
    candidate = {**current_overrides, key: coerced}
    try:
        await store.preview(candidate)
    except ReloadRejectedError as exc:
        logger.warning("phaze.runtime_config_admin override rejected", key=key, attempted=coerced, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await set_runtime_config_override(session, key, coerced)
    await store.reload("api")
    return await _render_table(request, session)


@router.delete("/{key}", response_class=HTMLResponse)
async def clear_override(
    request: Request, key: Annotated[str, PathParam(max_length=_KEY_MAX_LENGTH)], session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    """Clear ``key``'s DB override: it falls back to the next layer (``file`` / ``env`` / ``default``).

    Clearing is always a valid operation (there is no way to "clear" into an invalid state -- the
    layer underneath was already in force before this override existed), so there is no preview
    gate here, unlike :func:`set_override`.
    """
    _reject_unless_reloadable(key, attempted=None)
    await clear_runtime_config_override(session, key)
    await get_runtime_config_store().reload("api")
    return await _render_table(request, session)
