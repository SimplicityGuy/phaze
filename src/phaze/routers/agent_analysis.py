"""PUT /api/internal/agent/analysis/{file_id} -- idempotent audio-analysis upsert (Phase 26 D-26).

Mirrors `agent_metadata.py` exactly: `pg_insert` + `on_conflict_do_update` with
`exclude_unset` semantics (Phase 25 CR-01 fix). Natural key:
`AnalysisResult.file_id` (unique=True per models/analysis.py:18).

Storage representation: `AnalysisResult.mood` and `.style` are `String(50)`
columns (Phase 5 schema). D-26's wire format is `dict[str, float]`. The handler
converts each incoming dict to a "key1=score1,key2=score2,key3=score3" summary
string bounded at 50 chars (top-3 highest-score keys, deterministic alphabetical
tiebreak on equal scores). This avoids an Alembic migration in Phase 26 while
preserving enough information for downstream displays. A future migration to
JSONB columns is deferred.

Overflow funnel: D-26's wire schema also includes `danceability` and `energy`
fields that have no dedicated column on `AnalysisResult` yet. The handler
funnels any wire field without a backing column into the existing `features`
JSONB column, so the wire contract is honored end-to-end without a migration
this phase. Plan 11's process_file rewrite produces the same wire shape; a
future migration can promote these to dedicated columns.

PK NOTE: `AnalysisResult.id` has a Python-only `default=uuid.uuid4`, which fires
only through ORM `session.add()`, NOT through `pg_insert(...).values()`. We
therefore stamp `payload["id"] = uuid.uuid4()` explicitly so a fresh INSERT
doesn't raise `NotNullViolationError`. `ON CONFLICT DO UPDATE` preserves the
existing row's id (`excluded.id` is not in the SET clause).
"""

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.database import get_session
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult, AnalysisWindow
from phaze.routers.agent_auth import get_authenticated_agent
from phaze.schemas.agent_analysis import AnalysisWritePayload, AnalysisWriteResponse


router = APIRouter(prefix="/api/internal/agent/analysis", tags=["agent-internal"])

# Columns that physically exist on the `analysis` table. Wire-format fields
# accepted by AnalysisWritePayload but absent here (e.g. `danceability`,
# `energy`) are bundled into the `features` JSONB column instead -- the model
# stays unchanged this phase (no Alembic migration), while D-26's wire contract
# is fully honored end-to-end. Plan 11's process_file rewrite produces the same
# wire shape; a future migration can promote these to dedicated columns.
_ANALYSIS_COLUMN_FIELDS: frozenset[str] = frozenset({"bpm", "musical_key", "mood", "style", "fingerprint", "features"})


def _summarize_dict_to_string(value: dict[str, float]) -> str:
    """Convert ``dict[str, float]`` to ``"k=v,k=v,k=v"`` summary, top-3 by score, max 50 chars.

    Sort order: primary by ``-score`` (descending), secondary by ``key``
    (ascending alphabetical) for deterministic tiebreak when scores are equal
    (W6 invariant -- verified by `test_summarize_dict_to_string`). Hard 50-char
    cap matches the existing `AnalysisResult.mood/style` `String(50)` columns.
    """
    items = sorted(value.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    summary = ",".join(f"{k}={v:.2f}" for k, v in items)
    return summary[:50]


@router.put("/{file_id}", status_code=status.HTTP_200_OK, response_model=AnalysisWriteResponse)
async def put_analysis(
    file_id: uuid.UUID,
    body: AnalysisWritePayload,
    agent: Annotated[Agent, Depends(get_authenticated_agent)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AnalysisWriteResponse:
    """Idempotently upsert AnalysisResult for a file. Natural key: ``analysis.file_id`` (UQ).

    Field-level last-write-wins per D-14 / Phase 25 CR-01: only fields the
    client *explicitly set* (via Pydantic's ``exclude_unset`` dump semantics)
    land in the UPDATE SET clause. Unset Optional fields preserve whatever was
    already on the row. `agent_id` comes from the auth dep, NEVER from body
    (AUTH-01); `extra='forbid'` on the payload returns 422 on attempted forgery.

    Storage conversion: incoming ``mood`` and ``style`` dicts are reduced to a
    bounded summary string before storage (see `_summarize_dict_to_string`),
    because the existing columns are ``String(50)``. Future migration to JSONB
    will lift this constraint without changing the wire contract.

    Empty-body PUT (``{}``) is a no-op against an existing row: the INSERT path
    falls back to ``ON CONFLICT DO NOTHING`` (Postgres rejects an empty SET
    clause). New rows still get an INSERT (with all NULL fields) on first call.
    """
    # CR-01 fix: only fields the client explicitly set participate in the UPDATE.
    dumped = body.model_dump(exclude_unset=True)

    # `windows` is the per-window child time-series, NOT an aggregate column. Pop it
    # out of the aggregate dump BEFORE the overflow funnel so it never lands in the
    # `features` JSONB. Child rows are replaced separately, after the aggregate upsert,
    # guarded on `body.windows is not None` (partial-PUT). The bool of an empty list is
    # falsy, so read the field off `body` directly to distinguish [] from None.
    dumped.pop("windows", None)

    # Storage conversion at the boundary: AnalysisResult.mood/.style are String(50).
    # Wire format from essentia is dict[str, float]; we serialize to a "k=v,k=v"
    # summary bounded at 50 chars. The conversion stays inside ``dumped`` so the
    # rest of the upsert pipeline is identical to agent_metadata.py.
    for field in ("mood", "style"):
        raw = dumped.get(field)
        if isinstance(raw, dict):
            dumped[field] = _summarize_dict_to_string(raw)

    # Funnel any wire-format fields without a dedicated column into `features` JSONB.
    # D-26's wire schema includes `danceability`/`energy` (and future-proofs additions);
    # the model currently only has columns for bpm/musical_key/mood/style. The funnel
    # keeps the wire contract intact without requiring a migration this phase.
    overflow = {k: dumped.pop(k) for k in list(dumped) if k not in _ANALYSIS_COLUMN_FIELDS}
    if overflow:
        # Merge into any features the caller also set explicitly (avoid clobbering).
        existing_features = dumped.get("features")
        merged_features: dict[str, object] = dict(existing_features) if isinstance(existing_features, dict) else {}
        merged_features.update(overflow)
        dumped["features"] = merged_features

    # Stamp PK explicitly because AnalysisResult.id has a Python-only default,
    # which pg_insert bypasses.
    payload = {**dumped, "file_id": file_id, "id": uuid.uuid4()}
    stmt = pg_insert(AnalysisResult).values([payload])
    if dumped:
        # `set_` covers ONLY the user-provided fields (D-14 field-level LWW);
        # excludes file_id AND id from the SET clause (both are conflict-target /
        # immutable PK -- existing row keeps its existing id).
        stmt = stmt.on_conflict_do_update(
            index_elements=["file_id"],
            set_={k: stmt.excluded[k] for k in dumped},
        )
    else:
        # Empty body -- no-op for existing rows; INSERT still happens for fresh ones.
        # Avoids Postgres "SET clause empty" syntax error (matches agent_metadata.py:65-68).
        stmt = stmt.on_conflict_do_nothing(index_elements=["file_id"])
    await session.execute(stmt)

    # Child-row replace (Phase 31, ANL-01): idempotently REPLACE this file's windows
    # in the SAME transaction as the aggregate upsert. Guarded on `is not None` so an
    # aggregate-only PUT (windows omitted) leaves existing windows untouched (partial-PUT).
    # A present-but-empty list explicitly clears all windows. The DELETE predicate and each
    # inserted row's file_id use the PATH `file_id` ONLY -- the body never carries a
    # file/window-owner id (cross-file-deletion mitigation, AUTH-01).
    if body.windows is not None:
        await session.execute(delete(AnalysisWindow).where(AnalysisWindow.file_id == file_id))
        if body.windows:
            # pg_insert bypasses the Python-only `default=uuid.uuid4` PK, so stamp `id`
            # explicitly per row (mirrors the aggregate path above).
            await session.execute(
                pg_insert(AnalysisWindow).values([{"id": uuid.uuid4(), "file_id": file_id, **w.model_dump()} for w in body.windows])
            )

    await session.commit()
    return AnalysisWriteResponse(agent_id=agent.id, file_id=file_id)
