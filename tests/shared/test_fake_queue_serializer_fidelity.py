"""Seam E1 (phaze-9nz1g): ``FakeQueue`` records what the WORKER receives, not what the producer sent.

``tests/_queue_fakes.py::FakeQueue`` is a legitimate and valuable test double -- it makes hundreds of
tests fast and hermetic, and this module is emphatically NOT an argument for deleting it. The defect
it fixes is narrower: the fake used to record the kwargs dict in-process and UNSERIALIZED, so the
type-fidelity property of the ``queue.enqueue`` -> broker -> worker ``**kwargs`` hop -- the
most-used producer-to-consumer artifact in the system -- was asserted nowhere. A dict handed back
unchanged proves the dict was handed back unchanged (ADR-0012 rule 3).

The fake now routes every enqueue through the SAME ``PostgresQueue.serialize`` /
``PostgresQueue.deserialize`` pair the production broker runs, so all ~43 modules that use it
inherit the check for free -- the phaze-bk9el.21 "set once, inherited everywhere" shape.

This module locks the three properties that make that worth having, and the one that makes it safe:

* a payload the real broker would reject raises here too, instead of being echoed back;
* a payload that survives but CHANGES TYPE is recorded as the changed type;
* job-control kwargs (``key`` / ``timeout`` / ``retries``) cross the same round trip; and
* the serializer queue is never connected, so nothing in the hermetic suite opens a socket.

``tests/integration/test_pg_payload_type_fidelity.py`` is the other half: this module proves the
fake uses the right serializer, that one proves the real broker agrees, against nine real payloads
and their nine real ``model_validate`` consumers.
"""

from __future__ import annotations

from pathlib import Path
import uuid

import pytest
from saq.queue.postgres import PostgresQueue

from tests._queue_fakes import (
    DedupFakeQueue,
    FakeQueue,
    NonSerializableEnqueueError,
    _serializer_queue,
    round_trip_enqueue_kwargs,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


async def test_a_uuid_kwarg_raises_instead_of_being_echoed_back() -> None:
    """The failure ``FakeQueue`` structurally could not exhibit before: a raw ``UUID`` payload value.

    ``json.dumps`` -- and therefore the real ``PostgresQueue`` -- refuses it, so a producer that
    forgot ``model_dump(mode="json")`` fails at enqueue in production. Pre-fix the fake accepted it
    and recorded the ``UUID`` object, so the test read green.
    """
    queue = FakeQueue("controller")

    with pytest.raises(NonSerializableEnqueueError) as excinfo:
        await queue.enqueue("process_file", file_id=uuid.uuid4())

    # A TypeError subclass, because that is exactly what the real broker raises for this value.
    assert isinstance(excinfo.value, TypeError)
    assert "process_file" in str(excinfo.value)
    assert queue.captured == []  # a refused enqueue records nothing


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(uuid.uuid4(), id="uuid"),
        pytest.param(Path("/archive/<track-01>.mp3"), id="path"),
        pytest.param({"a", "b"}, id="set"),
    ],
)
async def test_non_json_native_payload_types_are_all_refused(value: object) -> None:
    """The named non-JSON-native classes from the seam inventory, each refused at the fake's enqueue."""
    queue = FakeQueue("controller")

    with pytest.raises(NonSerializableEnqueueError):
        await queue.enqueue("process_file", field=value)


async def test_the_recorded_payload_is_the_deserialized_one_not_the_submitted_one() -> None:
    """A ``tuple`` goes in and a ``list`` is recorded -- the fake reports the worker's view.

    This is the quieter half of the seam. Unlike a ``UUID`` these values SURVIVE the round trip, so
    nothing raises; the worker just receives a different type. The fake now records that difference
    rather than hiding it.
    """
    queue = FakeQueue("controller")

    await queue.enqueue("generate_proposals", file_ids=("a", "b"), by_index={1: "x"})

    task, payload = queue.captured[0]
    assert task == "generate_proposals"
    assert payload == {"file_ids": ["a", "b"], "by_index": {"1": "x"}}
    assert isinstance(payload["file_ids"], list)


async def test_job_control_kwargs_cross_the_same_round_trip() -> None:
    """``key`` / ``timeout`` / ``retries`` are ``saq.Job`` fields, and are serialized with the job.

    ``timeout=0`` is the value that matters most here (``process_file`` disables the SAQ wall clock
    -- see ``services/analysis_enqueue.py``), so pin that it survives as ``0`` and not as ``None``
    or the ``Job`` default.
    """
    queue = FakeQueue("controller")

    await queue.enqueue("process_file", key="process_file:abc", timeout=0, retries=2, file_id=str(uuid.uuid4()))

    assert queue.captured_policy[0] == {"key": "process_file:abc", "timeout": 0, "retries": 2}
    assert list(queue.captured[0][1]) == ["file_id"]  # job-control keys stay out of the payload


async def test_the_dedup_fake_inherits_the_round_trip() -> None:
    """``DedupFakeQueue`` overrides ``enqueue`` for the dedup no-op; it must not bypass the serializer."""
    queue = DedupFakeQueue("phaze-agent-itest-analyze")

    with pytest.raises(NonSerializableEnqueueError):
        await queue.enqueue("process_file", key="process_file:abc", file_id=uuid.uuid4())

    # And the dedup behaviour itself is unchanged by the round trip.
    assert await queue.enqueue("process_file", key="process_file:abc", file_id="x") is not None
    assert await queue.enqueue("process_file", key="process_file:abc", file_id="x") is None


def test_the_round_trip_uses_a_real_postgresqueue_that_is_never_connected() -> None:
    """The serializer is the production class, and constructing it opens no socket.

    Two claims, both load-bearing. That it is a real ``PostgresQueue`` is what makes this the REAL
    serializer rather than a hand-rolled ``json.dumps`` standing in for it -- a stand-in would drift
    silently the day ``build_pipeline_queue`` passes a custom ``dump``/``load``. That it is never
    connected is what keeps the hermetic unit suite hermetic: ``PostgresQueue.__init__`` builds its
    psycopg pool ``open=False``, and ``serialize``/``deserialize`` touch only ``_dump`` / ``_load`` /
    ``name``.
    """
    queue = _serializer_queue("controller")

    assert isinstance(queue, PostgresQueue)
    assert queue.name == "controller"
    assert queue._connected is False
    assert queue.pool.closed is True
    # Cached per name, so the fakes do not build a pool object per enqueue.
    assert _serializer_queue("controller") is queue


def test_the_round_trip_delegates_to_the_production_serialize_and_deserialize(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spy on ``PostgresQueue.serialize`` / ``deserialize`` to prove the production methods are what runs.

    Behavioural assertions above show the OUTCOME matches JSON semantics; this one shows the outcome
    is produced by the same code the broker runs, which is the property that survives a future
    serializer change.
    """
    calls: list[str] = []
    real_serialize = PostgresQueue.serialize
    real_deserialize = PostgresQueue.deserialize

    def spy_serialize(self: PostgresQueue, job: object) -> object:
        calls.append("serialize")
        return real_serialize(self, job)  # type: ignore[arg-type]

    def spy_deserialize(self: PostgresQueue, payload: object, status: object = None) -> object:
        calls.append("deserialize")
        return real_deserialize(self, payload, status)  # type: ignore[arg-type]

    monkeypatch.setattr(PostgresQueue, "serialize", spy_serialize)
    monkeypatch.setattr(PostgresQueue, "deserialize", spy_deserialize)

    payload, policy = round_trip_enqueue_kwargs("controller", "process_file", {"file_id": "x"}, {"retries": 2})

    assert calls == ["serialize", "deserialize"]
    assert payload == {"file_id": "x"}
    assert policy == {"retries": 2}


def test_fake_queue_enqueue_cannot_be_simplified_back_to_an_unserialized_echo() -> None:
    """Source lock: ``FakeQueue.enqueue`` must call ``round_trip_enqueue_kwargs``.

    A behavioural test proves the round trip happens today; this proves it cannot be removed by a
    well-meaning "the fake does not need to serialize" simplification without the build going red.
    The same reasoning as ``tests/shared/test_cluster_wide_catalog_scoping.py``'s source scan: the
    regression would be invisible in any suite that happens not to pass an offending type.
    """
    source = (REPO_ROOT / "tests" / "_queue_fakes.py").read_text(encoding="utf-8")
    body = source.split("    async def enqueue(self, task_name: str, **kwargs: Any) -> MagicMock:", 1)
    assert len(body) == 2, "FakeQueue.enqueue signature changed -- update this guard"
    assert "round_trip_enqueue_kwargs(" in body[1].split("\n    async def ", 1)[0]
