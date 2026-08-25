"""Seam E1 (phaze-9nz1g): SAQ enqueue payloads survive a REAL Postgres broker round trip.

WHAT WAS MISSING
----------------

Every background lane in phaze rides one artifact: ``queue.enqueue(task, **payload)`` ->
``saq_jobs.job`` (a JSON blob in a ``BYTEA`` column) -> the worker's ``deserialize`` -> the task
function's ``**kwargs`` -> ``<Payload>.model_validate(kwargs)``. Until this module, the TYPE
fidelity of that hop was asserted nowhere:

* ``tests/_queue_fakes.py::FakeQueue.enqueue`` recorded the kwargs dict IN-PROCESS and
  UNSERIALIZED, so it is the producer's own echo -- ADR-0012 rule 3's exact shape. (phaze-9nz1g
  also fixed that: the fake now round-trips through the real ``PostgresQueue`` serializer. This
  module is the other half -- the fake's serializer is the RIGHT one, but a fake broker is still
  not a broker.)
* the three real-broker modules (``test_pg_dedup``, ``test_pg_queue_priority``,
  ``test_pg_active_reap``) exercise broker SEMANTICS -- dedup, priority, reaping -- and every one
  of their enqueues carries **no payload kwargs at all**.

So a ``UUID`` / ``datetime`` / ``Path`` / ``enum`` kwarg -- which ``json.dumps`` refuses outright --
and a ``tuple`` / int-keyed ``dict`` -- which survives but arrives as a DIFFERENT TYPE -- were both
invisible. That is a worker-side failure, in the place failures are least visible.

WHAT THIS PINS
--------------

:func:`test_production_payload_survives_the_broker_round_trip` is parametrized over ALL NINE
task payloads that cross this seam, and for each one it:

1. builds the payload the way the PRODUCER does (``<Payload>(...).model_dump(mode="json")``);
2. enqueues it on a real ``PostgresQueue`` against the ephemeral 5433 harness -- so the blob is
   really serialized by ``PostgresQueue.serialize`` and really written to ``saq_jobs``;
3. reads it back with ``queue.job(key)``, which SELECTs the ``BYTEA`` and runs the real
   ``deserialize``; and
4. hands ``job.kwargs`` to **the actual consumer** -- the same ``<Payload>.model_validate(kwargs)``
   call the task function makes (``tasks/functions.py:573``, ``tasks/push.py:227``,
   ``tasks/scan.py:375``, ``tasks/metadata_extraction.py:62``, ``tasks/tag_write.py:64``,
   ``tasks/cue_write.py:56``, ``tasks/companion_read.py:72``, ``tasks/s3_upload.py:196``,
   ``tasks/execution.py:1299``) -- and asserts the reconstructed model equals the producer's.

Every one of those models declares at least one ``uuid.UUID`` field, so step 1 is load-bearing:
:func:`test_a_raw_uuid_kwarg_is_refused_by_the_real_broker` shows what happens without it.

Runs against the ephemeral integration-test Postgres broker (``just test-db``, host port 5433);
the DSN is derived via ``tests.db_guard.integration_dsns`` (phaze-yxklc) and the module skips
cleanly when the broker is down, mirroring ``test_pg_dedup``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING
import uuid

import pytest
import pytest_asyncio
from saq.queue.postgres import PostgresQueue

from phaze.schemas.agent_s3 import UploadFileS3Payload
from phaze.schemas.agent_tasks import (
    CompanionReadItem,
    ExecuteApprovedBatchPayload,
    ExecuteBatchProposalItem,
    ExtractMetadataPayload,
    ProcessFilePayload,
    PushFilePayload,
    ReadCompanionFilesPayload,
    ScanDirectoryPayload,
    WriteCueSheetPayload,
    WriteFileTagsPayload,
)
from tests.db_guard import integration_dsns


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pydantic import BaseModel


# Raw libpq broker DSN (NOT the +asyncpg dialect form psycopg3 cannot parse).
BROKER_DSN, _ = integration_dsns()


def _representative_payloads() -> list[tuple[str, BaseModel]]:
    """One production-shaped payload per task that crosses the enqueue seam.

    The nine entries are exactly the nine ``<Payload>.model_validate(kwargs)`` consumers in
    ``src/phaze/tasks/`` -- :func:`test_every_model_validate_consumer_is_covered` fails the build
    if a tenth appears and is not added here, so this list cannot silently fall behind the code.

    Values are representative rather than minimal: every optional field is populated (a ``None``
    would pass a JSON round trip trivially and prove nothing about the field's real type), and the
    two list-of-model payloads carry two items so nested-model serialization is exercised.
    """
    return [
        (
            "process_file",
            ProcessFilePayload(
                file_id=uuid.uuid4(),
                original_path="/archive/<track-01>.mp3",
                file_type="audio",
                agent_id="itest-agent",
                models_path="/models",
                expected_sha256="a" * 64,
                scratch_path="/scratch/<track-01>.mp3",
            ),
        ),
        (
            "push_file",
            PushFilePayload(
                file_id=uuid.uuid4(),
                original_path="/archive/<track-02>.mp3",
                file_type="audio",
                agent_id="itest-fileserver",
                dest_host="host-store",
                dest_scratch_dir="/scratch",
                dest_ssh_user="phaze",
            ),
        ),
        (
            "extract_file_metadata",
            ExtractMetadataPayload(
                file_id=uuid.uuid4(),
                original_path="/archive/<track-03>.m4a",
                file_type="audio",
                agent_id="itest-agent",
            ),
        ),
        (
            "scan_directory",
            ScanDirectoryPayload(scan_path="/archive", batch_id=uuid.uuid4(), agent_id="itest-agent"),
        ),
        (
            "write_file_tags",
            WriteFileTagsPayload(
                log_id=uuid.uuid4(),
                file_id=uuid.uuid4(),
                agent_id="itest-agent",
                file_path="/archive/<track-04>.mp3",
                tags={"title": "Title", "tracknumber": 3, "genre": ["Live", "Set"], "comment": None},
            ),
        ),
        (
            "write_cue_sheet",
            WriteCueSheetPayload(
                file_id=uuid.uuid4(),
                tracklist_id=uuid.uuid4(),
                agent_id="itest-agent",
                audio_path="/archive/<set-01>.mp3",
                content='FILE "<set-01>.mp3" MP3\n  TRACK 01 AUDIO\n',
            ),
        ),
        (
            "read_companion_files",
            ReadCompanionFilesPayload(
                agent_id="itest-agent",
                companions=[
                    CompanionReadItem(filename="<set-01>.txt", path="/archive/<set-01>.txt"),
                    CompanionReadItem(filename="<set-01>.nfo", path="/archive/<set-01>.nfo"),
                ],
                max_chars=4096,
            ),
        ),
        (
            "s3_upload",
            UploadFileS3Payload(
                file_id=uuid.uuid4(),
                original_path="/archive/<set-02>.mp3",
                part_urls=["https://example.invalid/part-1", "https://example.invalid/part-2"],
                part_size_bytes=8 * 1024 * 1024,
                agent_id="itest-agent",
            ),
        ),
        (
            "execute_approved_batch",
            ExecuteApprovedBatchPayload(
                batch_id=uuid.uuid4(),
                agent_id="itest-agent",
                proposals=[
                    ExecuteBatchProposalItem(
                        proposal_id=uuid.uuid4(),
                        file_id=uuid.uuid4(),
                        original_path="/archive/<track-05>.mp3",
                        proposed_path="/archive/Artist/Album",
                        proposed_filename="Artist - Event - Title (2024).mp3",
                        sha256_hash="b" * 64,
                    ),
                    ExecuteBatchProposalItem(
                        proposal_id=uuid.uuid4(),
                        file_id=uuid.uuid4(),
                        original_path="/archive/<track-06>.mp3",
                        proposed_path="/archive/Artist/Album",
                        proposed_filename="Artist - Event - Other (2024).mp3",
                        sha256_hash=None,
                    ),
                ],
                sub_batch_index=1,
            ),
        ),
    ]


@pytest_asyncio.fixture
async def pg_queue() -> AsyncGenerator[PostgresQueue]:
    """Yield a connected real ``PostgresQueue`` with a per-test-unique name.

    Same shape as ``test_pg_dedup``'s fixture: probe first and ``pytest.skip`` when the broker is
    down (so a bare ``uv run pytest`` with no ``just test-db`` skips rather than errors),
    ``connect()`` opens the psycopg3 pool and runs ``init_db()``, teardown deletes this queue's
    rows. The unique name keeps concurrent seats off each other's rows in the shared ``saq_jobs``
    table.
    """
    import psycopg

    try:
        probe = await psycopg.AsyncConnection.connect(BROKER_DSN)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres broker unavailable: {exc}")
    else:
        await probe.close()

    queue = PostgresQueue.from_url(BROKER_DSN, name=f"itest-payload-{uuid.uuid4().hex[:8]}")
    await queue.connect()
    try:
        yield queue
    finally:
        with contextlib.suppress(Exception):
            async with queue.pool.connection() as conn:
                # Parameterized query -- queue.name is bound, never interpolated.
                await conn.execute("DELETE FROM saq_jobs WHERE queue = %s", (queue.name,))
        await queue.disconnect()


@pytest.mark.integration
@pytest.mark.parametrize(("task_name", "payload"), _representative_payloads(), ids=[name for name, _ in _representative_payloads()])
async def test_production_payload_survives_the_broker_round_trip(pg_queue: PostgresQueue, task_name: str, payload: BaseModel) -> None:
    """Producer -> real broker -> the task's own ``model_validate`` reconstructs an EQUAL payload.

    The consumer is named explicitly and is the real one: ``type(payload).model_validate``, the same
    call the task function makes on its ``**kwargs``. Asserting equality of the MODELS (not of the
    dicts) is what makes this a type-fidelity test -- ``file_id`` leaves as a ``UUID``, crosses the
    broker as a string, and must arrive back as the SAME ``UUID``, not as its text.
    """
    key = f"{task_name}:{uuid.uuid4()}"
    dumped = payload.model_dump(mode="json")

    job = await pg_queue.enqueue(task_name, key=key, **dumped)
    assert job is not None  # a fresh key always lands

    # Read back through the broker: SELECT the BYTEA blob, run the real deserialize.
    stored = await pg_queue.job(key)
    assert stored is not None
    assert stored.function == task_name

    # THE CONSUMER. extra="forbid" on every one of these models, so an added/renamed key fails here.
    reconstructed = type(payload).model_validate(stored.kwargs)
    assert reconstructed == payload


@pytest.mark.integration
async def test_a_raw_uuid_kwarg_is_refused_by_the_real_broker(pg_queue: PostgresQueue) -> None:
    """``queue.enqueue(task, file_id=<UUID>)`` raises ``TypeError`` -- the exposure is real, not theoretical.

    This is the failure ``FakeQueue`` could not exhibit before phaze-9nz1g: an unserialized fake
    accepted the ``UUID`` and echoed it back, so a producer that forgot ``model_dump(mode="json")``
    read as green. Every payload model above carries a ``UUID`` field, so this is one omitted
    ``mode="json"`` away at nine call sites.
    """
    with pytest.raises(TypeError, match="UUID is not JSON serializable"):
        await pg_queue.enqueue("process_file", key=f"process_file:{uuid.uuid4()}", file_id=uuid.uuid4())


@pytest.mark.integration
async def test_container_types_drift_across_the_real_broker(pg_queue: PostgresQueue) -> None:
    """A ``tuple`` arrives as a ``list`` and an int-keyed ``dict`` arrives string-keyed.

    The quieter half of the seam: unlike the ``UUID`` above these SURVIVE the round trip, so nothing
    raises -- the worker simply receives a different type than the producer sent. Pinned here so the
    drift is a documented broker property with a named test, rather than something a future consumer
    discovers by indexing a ``dict`` with an ``int`` that is now a ``str``.
    """
    key = f"process_file:{uuid.uuid4()}"
    await pg_queue.enqueue("process_file", key=key, seq=(1, 2, 3), by_index={1: "a"})

    stored = await pg_queue.job(key)
    assert stored is not None
    assert stored.kwargs == {"seq": [1, 2, 3], "by_index": {"1": "a"}}
    assert isinstance(stored.kwargs["seq"], list)
    assert list(stored.kwargs["by_index"]) == ["1"]


def test_every_model_validate_consumer_is_covered() -> None:
    """Every ``<Payload>.model_validate(kwargs)`` in ``src/phaze/tasks/`` has a row above.

    Source scan, deliberately: the point is to catch a NEW task payload that nobody added to
    :func:`_representative_payloads`, and a runtime check could only see the ones already listed.
    Not marked ``integration`` -- it needs no broker, so it runs in every bucket that collects this
    module.
    """
    from pathlib import Path
    import re

    tasks_dir = Path(__file__).resolve().parents[2] / "src" / "phaze" / "tasks"
    found = set()
    for source in sorted(tasks_dir.rglob("*.py")):
        found.update(re.findall(r"(\w+Payload)\.model_validate\(kwargs\)", source.read_text(encoding="utf-8")))

    covered = {type(payload).__name__ for _, payload in _representative_payloads()}
    assert found - covered == set(), f"task payload(s) with no broker round-trip row: {sorted(found - covered)}"


# --- phaze-ot3os: the OMITTED conversion, against live broker bytes ---------------------------------
#
# The tests above prove the CORRECT producer call (``model_dump(mode="json")``) survives the hop.
# These prove the claim phaze-ot3os actually makes -- that with ``WirePayload`` carrying the
# discipline in the TYPE, the OMISSION survives it too, so there is no longer anything for a producer
# to forget. That claim is about bytes on a broker, so it is adjudicated by a broker, not by a
# same-process assertion about a dict: ADR-0012 rule 3.
#
# The general form of the lesson, since ADR-0012 rule 5 asks for it: a mechanism that makes a class of
# input SAFE is verified by driving that exact input through the REAL consumer, together with a
# negative control proving the consumer still rejects the input the mechanism does NOT cover.
# Without the control, a green run is equally consistent with "the mechanism works" and "the consumer
# accepts everything". Here the control is a plain ``BaseModel`` payload on the SAME live queue.


@pytest.mark.integration
@pytest.mark.parametrize(("task_name", "payload"), _representative_payloads(), ids=[name for name, _ in _representative_payloads()])
async def test_the_omitted_conversion_is_now_harmless_across_the_real_broker(pg_queue: PostgresQueue, task_name: str, payload: BaseModel) -> None:
    """``model_dump()`` WITHOUT ``mode="json"`` -- the omission -- survives the real broker intact.

    Note the single difference from
    :func:`test_production_payload_survives_the_broker_round_trip` above: that one dumps the way
    every producer is currently WRITTEN, this one dumps the way a producer might be written TOMORROW
    if its author forgets. With ``WirePayload`` forcing JSON mode it lands, and the real consumer --
    ``type(payload).model_validate``, the same call the task function makes at
    ``tasks/functions.py:573`` and its eight siblings -- reconstructs an EQUAL model, so a ``UUID``
    field comes back a ``UUID`` rather than its text.

    **EIGHT of the nine rows are load-bearing; ``read_companion_files`` is not, and saying so is the
    point.** phaze-9nz1g's inventory and this bead both state that ``uuid.UUID`` "is a field on ALL
    NINE task-payload models". Re-measured here: it is a field on eight.
    ``ReadCompanionFilesPayload`` declares **no** ``UUID`` -- neither does its nested
    ``CompanionReadItem`` -- so that lane's payload was already JSON-native and its omission was
    already harmless before this bead. Its row still belongs in the parametrization (it guards
    against a UUID being ADDED to that payload later, which is exactly the change that would make it
    load-bearing) but it must not be counted as evidence that the mechanism does anything: it would
    have passed unchanged. Do not restore the "all nine" phrasing without re-running
    ``model_fields`` -- an inherited count nobody re-measured is how it got here.

    Paired with :func:`test_the_omitted_conversion_is_still_refused_for_a_plain_basemodel`, which
    shows the same omission STILL failing on this same queue for a model that did not inherit the
    base. Read the two together: separately, this one cannot distinguish "the mechanism works" from
    "the broker accepts anything".
    """
    key = f"{task_name}:{uuid.uuid4()}"

    dumped = payload.model_dump()  # THE OMISSION -- no mode= at all.

    job = await pg_queue.enqueue(task_name, key=key, **dumped)
    assert job is not None

    stored = await pg_queue.job(key)
    assert stored is not None
    assert stored.function == task_name

    reconstructed = type(payload).model_validate(stored.kwargs)
    assert reconstructed == payload


@pytest.mark.integration
async def test_the_omitted_conversion_is_still_refused_for_a_plain_basemodel(pg_queue: PostgresQueue) -> None:
    """THE NEGATIVE CONTROL: on the same live queue, a non-``WirePayload`` payload still fails.

    This is what makes the parametrized test above evidence rather than decoration. The model here is
    field-for-field the shape of a real task payload and differs in exactly one respect -- it
    inherits ``BaseModel`` instead of ``WirePayload`` -- and the live broker refuses it with the same
    ``TypeError`` every one of the nine used to raise.

    It is also the failure mode ``tests/shared/schemas/test_wire_payload_contract.py`` exists to
    prevent at review time: a tenth payload model wired up without the base.
    """
    # Runtime import: the module-level ``BaseModel`` lives in the TYPE_CHECKING block (it is only an
    # annotation there), and this test needs the real class to subclass.
    from pydantic import BaseModel as RuntimeBaseModel, ConfigDict

    class _NotWired(RuntimeBaseModel):
        model_config = ConfigDict(extra="forbid")

        file_id: uuid.UUID
        agent_id: str

    payload = _NotWired(file_id=uuid.uuid4(), agent_id="itest-agent")

    with pytest.raises(TypeError, match="UUID is not JSON serializable"):
        await pg_queue.enqueue("process_file", key=f"process_file:{uuid.uuid4()}", **payload.model_dump())


@pytest.mark.integration
async def test_a_refused_enqueue_leaves_no_saq_jobs_row(pg_queue: PostgresQueue) -> None:
    """The refusal is PRODUCER-side and pre-INSERT -- phaze-ot3os's correction to the bead's premise, measured.

    phaze-9nz1g and phaze-ot3os both describe an un-converted payload as failing "in a worker rather
    than in a request, which is where failures are least visible". That is wrong.
    ``saq.Queue.enqueue`` runs ``_before_enqueue(job)`` then ``_enqueue(job)`` ->
    ``PostgresQueue.serialize`` -> ``json.dumps``, all synchronously inside the producer's own
    ``await queue.enqueue(...)`` and BEFORE the ``saq_jobs`` INSERT (``saq/queue/base.py:314-357``,
    ``:219``). So the row never exists and no worker ever sees it.

    Pinned as a test rather than left as prose because it is the fact the whole mechanism choice
    rested on: the gap phaze-ot3os closes was never "invisible until a worker sees it" -- the failure
    was always loud and producer-side. The gap was that it happened at RUNTIME IN PRODUCTION rather
    than by construction.
    """
    key = f"process_file:{uuid.uuid4()}"

    with pytest.raises(TypeError, match="UUID is not JSON serializable"):
        await pg_queue.enqueue("process_file", key=key, file_id=uuid.uuid4())

    async with pg_queue.pool.connection() as conn:
        # Parameterized -- queue.name and key are bound, never interpolated.
        cursor = await conn.execute("SELECT count(*) FROM saq_jobs WHERE queue = %s AND key = %s", (pg_queue.name, key))
        row = await cursor.fetchone()

    assert row is not None
    assert row[0] == 0, "a refused enqueue wrote a saq_jobs row -- the failure is NOT pre-INSERT after all"
