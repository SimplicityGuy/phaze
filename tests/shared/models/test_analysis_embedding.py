"""``AnalysisEmbedding`` and ``replace_embeddings`` against the real Postgres harness (phaze-g79ea)."""

import numpy as np
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models import AnalysisEmbedding, FileRecord
from phaze.services.analysis_embedding_store import load_embeddings, replace_embeddings
from phaze.services.analysis_embeddings import EMBEDDING_DIM, EMBEDDING_MODEL, decode_embedding, embeddings_payload, encode_embedding


def _vector(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(0.0, 3.0, EMBEDDING_DIM).astype(np.float32)


def _payload(seeds: dict[int, int], model: str = EMBEDDING_MODEL) -> dict:
    payload = embeddings_payload({index: encode_embedding(_vector(seed)) for index, seed in seeds.items()})
    payload["model"] = model
    return payload


async def _count(session: AsyncSession, file_id) -> int:  # type: ignore[no-untyped-def]
    return (await session.execute(select(func.count()).select_from(AnalysisEmbedding).where(AnalysisEmbedding.file_id == file_id))).scalar_one()


def test_keyed_by_file_model_and_window_and_cascades_with_files() -> None:
    table = AnalysisEmbedding.__table__
    assert [c.name for c in table.primary_key.columns] == ["file_id", "model", "window_index"]
    (fk,) = table.columns["file_id"].foreign_keys
    assert (fk.column.table.name, fk.ondelete) == ("files", "CASCADE")


@pytest.mark.asyncio
async def test_float16_round_trip_is_exact_through_the_database(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    record = await make_file()
    sent = {0: _vector(1), 3: _vector(2)}
    payload = embeddings_payload({i: encode_embedding(v) for i, v in sent.items()})
    assert await replace_embeddings(session, record.id, payload) == 2
    loaded = await load_embeddings(session, record.id, EMBEDDING_MODEL)
    assert sorted(loaded) == [0, 3]
    for index, vector in sent.items():
        expected = decode_embedding(encode_embedding(vector))
        assert loaded[index].dtype == np.dtype("<f2")
        assert loaded[index].tobytes() == expected.tobytes()  # bit-exact, not approximately equal
        assert np.array_equal(loaded[index], vector.astype(np.float16))
    raw = (await session.execute(select(AnalysisEmbedding.vector).where(AnalysisEmbedding.window_index == 0))).scalar_one()
    assert len(bytes(raw)) == EMBEDDING_DIM * 2


@pytest.mark.asyncio
async def test_reanalysis_replaces_never_appends_and_only_its_own_model(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    record, neighbour = await make_file(), await make_file()
    await replace_embeddings(session, record.id, _payload({0: 1, 1: 2, 2: 3}))
    await replace_embeddings(session, record.id, _payload({0: 9}, model="other-graph"))
    await replace_embeddings(session, neighbour.id, _payload({0: 5}))

    await replace_embeddings(session, record.id, _payload({1: 20, 4: 40}))
    loaded = await load_embeddings(session, record.id, EMBEDDING_MODEL)
    assert sorted(loaded) == [1, 4], "windows 0 and 2 of the first analysis must not survive"
    assert loaded[1].tobytes() == decode_embedding(encode_embedding(_vector(20))).tobytes()
    assert sorted(await load_embeddings(session, record.id, "other-graph")) == [0]
    assert sorted(await load_embeddings(session, neighbour.id, EMBEDDING_MODEL)) == [0]

    assert await replace_embeddings(session, record.id, _payload({})) == 0
    assert await load_embeddings(session, record.id, EMBEDDING_MODEL) == {}


@pytest.mark.asyncio
async def test_a_bad_payload_raises_before_any_write(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    record = await make_file()
    await replace_embeddings(session, record.id, _payload({0: 1}))
    bad = _payload({0: 2})
    bad["windows"].append({"window_index": 1, "vector": "AAAA"})
    with pytest.raises(ValueError, match="2560 bytes"):
        await replace_embeddings(session, record.id, bad)
    duplicate = _payload({0: 2})
    duplicate["windows"].append(duplicate["windows"][0])
    with pytest.raises(ValueError, match="duplicate"):
        await replace_embeddings(session, record.id, duplicate)
    assert (await load_embeddings(session, record.id, EMBEDDING_MODEL))[0].tobytes() == decode_embedding(encode_embedding(_vector(1))).tobytes()


@pytest.mark.asyncio
async def test_deleting_the_file_cascades(session: AsyncSession, make_file) -> None:  # type: ignore[no-untyped-def]
    record = await make_file()
    file_id = record.id
    await replace_embeddings(session, file_id, _payload({0: 1, 1: 2}))
    assert await _count(session, file_id) == 2
    await session.execute(delete(FileRecord).where(FileRecord.id == file_id))
    assert await _count(session, file_id) == 0
