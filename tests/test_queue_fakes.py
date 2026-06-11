"""Regression tests pinning the :class:`tests._queue_fakes.FakeQueue` contract.

These guard the test-double surface the Phase 33 SAQ-monitoring-UI plans build on.
``saq_web`` renders the dashboard by calling ``q.info()`` on every registered queue
(``saq.web.starlette._get_all_info`` => ``[await q.info() for q in QUEUES.values()]``)
and ``q.info(jobs=True)`` for the single-queue route. The Wave 1 ``build_saq_app`` test
and the Wave 2 lifespan test exercise that path against in-memory ``FakeQueue`` instances
with no Redis. If ``FakeQueue.info()`` ever drops one of the six ``QueueInfo`` keys
(``workers``/``name``/``queued``/``active``/``scheduled``/``jobs``) or stops echoing the
queue's own name, ``saq_web`` would fail to render under test — so these assertions fail
loudly here instead of as an opaque dashboard error downstream.
"""

from tests._queue_fakes import FakeQueue


_QUEUE_INFO_KEYS = {"workers", "name", "queued", "active", "scheduled", "jobs"}


async def test_info_returns_full_queueinfo_shape_echoing_name() -> None:
    """info() returns exactly the six QueueInfo keys and echoes self.name."""
    info = await FakeQueue("controller").info()
    assert set(info.keys()) == _QUEUE_INFO_KEYS
    assert info["name"] == "controller"
    assert info["workers"] == {}
    assert info["jobs"] == []
