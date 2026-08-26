"""Lifecycle-TTL boot-wiring tests for phaze.tasks.controller (phaze-cws5).

Every comment in the S3 staging pipeline (stage_file_to_s3's phaze-bbwx compensation, the reaper's
post-commit cleanup, report_upload_failed's terminal cleanup) names ``ensure_bucket_lifecycle_ttl``
as "the eventual backstop" for a missed inline abort/delete -- but nothing ever called it in
production (it was vulture-whitelisted as unused). ``controller.startup`` now pushes it once per
configured bucket, best-effort (a transient S3 hiccup at boot must never abort control-plane
startup, mirroring the LocalQueue-reachability probe's D-05 discipline).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.shared.tasks._shared import make_stub_session_factory


def _stub_controller(monkeypatch: pytest.MonkeyPatch, *, buckets: list[Any]) -> MagicMock:
    """Patch controller.startup's heavyweight collaborators + a MagicMock ``get_settings``.

    Mirrors ``test_controller_startup_localqueue.py``'s ``_stub_controller``, minus the kueue
    registry shape (an empty ``backends`` list -- the LocalQueue probe is out of scope here) plus
    an explicit ``buckets`` list, the surface this module's new startup step reads.
    """
    monkeypatch.setattr("phaze.database.create_async_engine", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.async_sessionmaker", lambda *_a, **_kw: make_stub_session_factory())
    monkeypatch.setattr("phaze.tasks.controller.DiscogsographyClient", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.load_prompt_template", lambda: "stub")
    monkeypatch.setattr("phaze.tasks.controller.ProposalService", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.AgentTaskRouter", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr("phaze.tasks.controller.redis_async.Redis.from_url", lambda *_a, **_kw: AsyncMock())

    fake_cfg = MagicMock()
    fake_cfg.redis_url = "redis://localhost:6379/0"
    fake_cfg.database_url = "postgresql+asyncpg://test"
    fake_cfg.queue_url = "postgresql+asyncpg://test"
    fake_cfg.debug = False
    fake_cfg.discogsography_url = "http://test"
    fake_cfg.llm_model = "stub-model"
    fake_cfg.llm_max_rpm = 60
    fake_cfg.log_level = "INFO"
    fake_cfg.log_json = True
    fake_cfg.anthropic_api_key = None
    fake_cfg.openai_api_key = None
    fake_cfg.backends = []  # no kueue backend -> the LocalQueue probe is skipped, out of scope here
    fake_cfg.buckets = buckets
    monkeypatch.setattr("phaze.tasks.controller.get_settings", lambda: fake_cfg)
    return fake_cfg


@pytest.mark.asyncio
async def test_startup_configures_lifecycle_ttl_once_per_configured_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-cws5: startup calls ensure_bucket_lifecycle_ttl exactly once per configured bucket."""
    bucket_a = SimpleNamespace(id="bucket-a")
    bucket_b = SimpleNamespace(id="bucket-b")
    _stub_controller(monkeypatch, buckets=[bucket_a, bucket_b])

    ensure_ttl = AsyncMock()
    monkeypatch.setattr("phaze.tasks.controller.s3_staging.ensure_bucket_lifecycle_ttl", ensure_ttl)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    assert ensure_ttl.await_count == 2
    called_buckets = [call.args[0] for call in ensure_ttl.await_args_list]
    assert called_buckets == [bucket_a, bucket_b]


@pytest.mark.asyncio
async def test_startup_skips_lifecycle_ttl_when_no_buckets_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """An all-local deployment (no configured buckets) never calls the S3 lifecycle backstop."""
    _stub_controller(monkeypatch, buckets=[])

    ensure_ttl = AsyncMock()
    monkeypatch.setattr("phaze.tasks.controller.s3_staging.ensure_bucket_lifecycle_ttl", ensure_ttl)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    await controller.startup(ctx)

    ensure_ttl.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_lifecycle_ttl_failure_on_one_bucket_does_not_abort_boot_or_skip_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-05: a failing lifecycle-TTL push on one bucket must not abort boot, nor block the rest.

    Mirrors the LocalQueue-reachability probe's boot-resilience discipline exactly: a transient S3
    auth/network hiccup at boot degrades to a logged warning, never a controller crash, and the next
    restart retries the same idempotent upsert.
    """
    bucket_bad = SimpleNamespace(id="bucket-bad")
    bucket_good = SimpleNamespace(id="bucket-good")
    _stub_controller(monkeypatch, buckets=[bucket_bad, bucket_good])

    async def _fail_or_succeed(bucket: Any) -> None:
        if bucket.id == "bucket-bad":
            raise RuntimeError("bucket unreachable")

    ensure_ttl = AsyncMock(side_effect=_fail_or_succeed)
    monkeypatch.setattr("phaze.tasks.controller.s3_staging.ensure_bucket_lifecycle_ttl", ensure_ttl)

    from phaze.tasks import controller

    ctx: dict[str, Any] = {}
    # Must NOT raise -- a lifecycle-TTL failure on one bucket can never abort controller boot.
    await controller.startup(ctx)

    # Both buckets were attempted -- the failing first bucket did not short-circuit the second.
    assert ensure_ttl.await_count == 2
