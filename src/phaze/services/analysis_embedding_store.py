"""Persist per-window EffNet embeddings into ``analysis_embedding`` (phaze-g79ea).

The wire form is :func:`phaze.services.analysis_embeddings.embeddings_payload`: base64 of little-endian
float16 per coarse window. Storage is those exact bytes, so :func:`replace_embeddings` base64-decodes and
inserts, and a read returns the same float16 values the agent computed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from phaze.models.analysis import AnalysisEmbedding
from phaze.services.analysis_embeddings import EMBEDDING_DIM, EMBEDDING_WIRE_DTYPE, decode_embedding


if TYPE_CHECKING:
    import uuid

    import numpy as np
    from sqlalchemy.ext.asyncio import AsyncSession


async def replace_embeddings(session: AsyncSession, file_id: uuid.UUID, payload: dict[str, Any]) -> int:
    """Replace this file's embeddings for ``payload["model"]`` wholesale, in the caller's transaction.

    DELETE then INSERT on the CALLER's session, so a re-analysis replaces rather than appends and a
    half-written set is never committed (the contract of ``_replace_subwindows``). Only the payload's own
    model is replaced: another model's rows for the file stay. An empty ``windows`` list clears the model's
    rows -- a recorded "no embeddings now", never stale ones. Raises ``ValueError`` (before any write) on a
    window whose vector does not decode to 1280 float16 values, or a repeated window index. Returns the
    number of rows written. Does NOT commit.
    """
    model = str(payload["model"])
    dim = int(payload.get("dim", EMBEDDING_DIM))
    if dim != EMBEDDING_DIM:
        msg = f"embedding dim must be {EMBEDDING_DIM}, got {dim}"
        raise ValueError(msg)
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for window in payload["windows"]:
        index = int(window["window_index"])
        if index in seen:
            msg = f"duplicate embedding for window {index}"
            raise ValueError(msg)
        seen.add(index)
        rows.append({"file_id": file_id, "model": model, "window_index": index, "dim": dim, "vector": decode_embedding(window["vector"]).tobytes()})
    await session.execute(delete(AnalysisEmbedding).where(AnalysisEmbedding.file_id == file_id, AnalysisEmbedding.model == model))
    if rows:
        await session.execute(pg_insert(AnalysisEmbedding).values(rows))
    return len(rows)


async def load_embeddings(session: AsyncSession, file_id: uuid.UUID, model: str) -> dict[int, np.ndarray]:
    """The file's stored embeddings for ``model`` as ``{window_index: float16 (dim,) array}``."""
    import numpy as np  # noqa: PLC0415 -- runtime use only; the annotation import above is TYPE_CHECKING

    result = await session.execute(
        select(AnalysisEmbedding.window_index, AnalysisEmbedding.vector).where(AnalysisEmbedding.file_id == file_id, AnalysisEmbedding.model == model)
    )
    return {index: np.frombuffer(bytes(vector), dtype=EMBEDDING_WIRE_DTYPE).copy() for index, vector in result.all()}
