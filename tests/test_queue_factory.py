"""Unit proof for the single PostgresQueue construction seam ``build_pipeline_queue``.

The factory (``phaze.tasks._shared.queue_factory``) is the ONE place a ``PostgresQueue``
is constructed in Phase 36 — every call site adopts it in Plan 02. These tests prove the
four construction-time contracts with NO live DB and NO live Redis (construction only,
``open=False`` pool):

1. returns a ``saq.queue.postgres.PostgresQueue``;
2. registers BOTH project before-enqueue hooks (job defaults + deterministic key);
3. attaches a decoupled ``cache_redis`` handle the backend-agnostic counter hooks read
   off ``getattr(job.queue, "cache_redis", None)``;
4. opens NO connection at construction (the pool is created with ``open=False``) — proven
   by an AST-walk of the factory body (no ``open``/``connect`` call) plus a runtime
   ``pool.closed`` assertion.
"""

from __future__ import annotations

import ast
from pathlib import Path

import redis.asyncio as aioredis
from saq.queue.postgres import PostgresQueue

from phaze.tasks._shared.deterministic_key import apply_deterministic_key
from phaze.tasks._shared.queue_defaults import apply_project_job_defaults
from phaze.tasks._shared.queue_factory import build_pipeline_queue


_PG_URL = "postgresql://u:p@h:5432/d"
_REDIS_URL = "redis://cache:6379/0"


def test_returns_postgres_queue() -> None:
    """Test 1: the factory returns a PostgresQueue (NOT a redis-backed Queue)."""
    q = build_pipeline_queue("controller", _PG_URL, cache_redis_url=_REDIS_URL)
    assert isinstance(q, PostgresQueue)
    assert q.name == "controller"


def test_both_before_enqueue_hooks_registered() -> None:
    """Test 2: both Phase 27 + Phase 35 before-enqueue hooks carry over onto the queue."""
    q = build_pipeline_queue("controller", _PG_URL, cache_redis_url=_REDIS_URL)
    registered = set(q._before_enqueues.values())
    assert apply_project_job_defaults in registered
    assert apply_deterministic_key in registered


def test_attaches_cache_redis_handle() -> None:
    """Test 3: a dedicated ``cache_redis`` client is attached so the counter hooks can read
    ``getattr(job.queue, "cache_redis", None)`` (the before_enqueue hook only sees ``job``)."""
    q = build_pipeline_queue("controller", _PG_URL, cache_redis_url=_REDIS_URL)
    assert isinstance(q.cache_redis, aioredis.Redis)


def test_construction_opens_no_connection() -> None:
    """Test 4: constructing the factory opens NO connection.

    Two independent proofs: (a) AST-walk the factory body and assert it never calls
    ``open``/``connect`` (the ``open=False`` pool flag lives in SAQ's own __init__, not
    here); (b) at runtime the freshly-built pool reports ``closed`` — no socket opened."""
    source = Path(build_pipeline_queue.__code__.co_filename).read_text(encoding="utf-8")
    func = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == "build_pipeline_queue")
    called: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                called.add(target.id)
            elif isinstance(target, ast.Attribute):
                called.add(target.attr)
    assert "open" not in called
    assert "connect" not in called

    q = build_pipeline_queue("controller", _PG_URL, cache_redis_url=_REDIS_URL)
    assert q.pool.closed is True
