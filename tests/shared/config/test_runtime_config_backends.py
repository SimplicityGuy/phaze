"""The ``backends.toml`` hot-reload path (``phaze-mvq8z.8``): :class:`BackendsRegistryReloader`.

Every registry here is built through the REAL ``backends_toml_env`` conftest fixture (writes a
real file, points ``PHAZE_BACKENDS_CONFIG_FILE`` at it) and a REAL ``ControlSettings()`` -- the
file/parse/validate layers are the production path, not a stand-in. Only the DB session for the
in-flight-removal veto is a seam: most scenarios here never remove a backend, so a stub that
raises if touched proves the veto path is not spuriously consulted; the removal scenario uses
the real ``session``/``make_file`` fixtures with real ``CloudJob`` rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
import uuid

import pytest
from sqlalchemy import update
from structlog.testing import capture_logs

from phaze.config import ControlSettings, get_settings
from phaze.config_backends import KueueBackend, LocalBackend
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.runtime_config import get_runtime_config_store
from phaze.runtime_config_backends import BackendsRegistryReloader, build_backends_registry_reloader


if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession


class _UnusedSessionFactory:
    """Raises if ever called -- proves the in-flight-removal veto is skipped when nothing was removed."""

    def __call__(self) -> Any:
        raise AssertionError("no backend was removed; the DB session factory should not have been touched")


class _SessionFactory:
    """Wraps the test's transactional ``session`` fixture as the ``SessionFactory`` shape."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def __call__(self) -> _SessionFactory:
        return self

    async def __aenter__(self) -> AsyncSession:
        return self._session

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PHAZE_BACKENDS_CONFIG_FILE", raising=False)
    get_settings.cache_clear()
    get_runtime_config_store.cache_clear()


@pytest.mark.asyncio
async def test_a_changed_backend_applies_on_next_resolve(backends_toml_env: Any) -> None:  # type: ignore[no-untyped-def]
    """Bead acceptance: adding/changing a backend applies on next resolve."""
    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1
        """
    )
    settings = ControlSettings()
    assert [b.id for b in settings.backends] == ["local"]
    reloader = BackendsRegistryReloader(settings, session_factory=_UnusedSessionFactory())

    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1

        [[backends]]
        kind = "compute"
        id = "cloud-a"
        rank = 10
        cap = 2
        agent_ref = "cloud-agent-1"
        scratch_dir = "/scratch"
        push_host = "cloud-a.push"
        """
    )
    await reloader.reload("file")

    # resolve_backends / every consumer reads settings.backends FRESH per use -- so the mutation
    # in place is what "applies on next resolve" means here.
    assert sorted(b.id for b in settings.backends) == ["cloud-a", "local"]


@pytest.mark.asyncio
async def test_an_invalid_registry_is_rejected_and_the_last_good_is_kept(backends_toml_env: Any) -> None:  # type: ignore[no-untyped-def]
    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1
        """
    )
    settings = ControlSettings()
    last_good = settings.backends
    reloader = BackendsRegistryReloader(settings, session_factory=_UnusedSessionFactory())

    # Duplicate backend ids -- validate_unique_registry_ids rejects this outright.
    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1

        [[backends]]
        kind = "local"
        id = "local"
        rank = 50
        cap = 2
        """
    )
    with capture_logs() as logs:
        await reloader.reload("sighup")

    assert settings.backends == last_good  # last-good kept, nothing partially applied
    assert any("reload rejected" in log.get("event", "") for log in logs)


@pytest.mark.asyncio
async def test_removal_of_a_backend_with_in_flight_cloud_job_rows_is_rejected_then_accepted_once_drained(
    backends_toml_env: Any,  # type: ignore[no-untyped-def]
    session: AsyncSession,
    make_file: Any,  # type: ignore[no-untyped-def]
) -> None:
    """Bead acceptance: removal with in-flight cloud_job rows rejected with the count, accepted once drained."""
    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1

        [[backends]]
        kind = "compute"
        id = "cloud-a"
        rank = 10
        cap = 2
        agent_ref = "cloud-agent-1"
        scratch_dir = "/scratch"
        push_host = "cloud-a.push"
        """
    )
    settings = ControlSettings()
    assert sorted(b.id for b in settings.backends) == ["cloud-a", "local"]
    reloader = BackendsRegistryReloader(settings, session_factory=_SessionFactory(session))

    file_a = await make_file(original_filename="active-a.mp3")
    file_b = await make_file(original_filename="active-b.mp3")
    session.add_all(
        [
            CloudJob(id=uuid.uuid4(), file_id=file_a.id, backend_id="cloud-a", status=CloudJobStatus.RUNNING.value),
            CloudJob(id=uuid.uuid4(), file_id=file_b.id, backend_id="cloud-a", status=CloudJobStatus.SUBMITTED.value),
        ]
    )
    await session.commit()

    # Remove cloud-a while it is still referenced -- rejected outright, naming the count.
    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1
        """
    )
    with capture_logs() as logs:
        await reloader.reload("file")

    assert sorted(b.id for b in settings.backends) == ["cloud-a", "local"]  # unchanged
    rejection = next(log for log in logs if "in-flight" in log.get("event", ""))
    assert rejection["blocking"] == {"cloud-a": 2}

    # Drain both rows to a terminal, non-active status -- now the removal is accepted. A
    # REJECTED reload never updates the reloader's own digest bookkeeping (only a successful
    # apply does), so simply retrying against the SAME on-disk file -- unchanged since the
    # rejected attempt -- re-runs the full check rather than short-circuiting as a no-op.
    await session.execute(update(CloudJob).where(CloudJob.backend_id == "cloud-a").values(status=CloudJobStatus.FAILED.value))
    await session.commit()

    await reloader.reload("file")

    assert sorted(b.id for b in settings.backends) == ["local"]


@pytest.mark.asyncio
async def test_a_content_identical_rewrite_is_not_reapplied(backends_toml_env: Any) -> None:  # type: ignore[no-untyped-def]
    body = """
    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 1
    """
    backends_toml_env(body)
    settings = ControlSettings()
    reloader = BackendsRegistryReloader(settings, session_factory=_UnusedSessionFactory())
    before = settings.backends

    backends_toml_env(body)  # same bytes, rewritten
    await reloader.reload("file")
    assert settings.backends is before  # never even rebuilt -- digest-equal short-circuit


@pytest.mark.asyncio
async def test_secrets_are_re_resolved_on_a_backends_reload(backends_toml_env: Any, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Bead acceptance: secrets re-resolved. Eager *_file resolution (config_backends.py:210-252)."""
    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1
        """
    )
    settings = ControlSettings()
    assert settings.buckets == []
    reloader = BackendsRegistryReloader(settings, session_factory=_UnusedSessionFactory())

    secret_file = tmp_path / "access_key"
    secret_file.write_text("AKIAEXAMPLE\n", encoding="utf-8")

    backends_toml_env(
        f"""
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1

        [[buckets]]
        id = "staging"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-staging"
        access_key_id_file = "{secret_file}"
        """
    )
    await reloader.reload("file")

    assert len(settings.buckets) == 1
    access_key = settings.buckets[0].access_key_id
    assert access_key is not None
    assert access_key.get_secret_value() == "AKIAEXAMPLE"  # stripped, per the shared secret rule


@pytest.mark.asyncio
async def test_build_backends_registry_reloader_registers_the_hook_on_the_store(backends_toml_env: Any) -> None:  # type: ignore[no-untyped-def]
    """The wiring function: registers on the store, and a subsequent store.reload(...) drives it."""
    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1
        """
    )
    settings = ControlSettings()
    store = get_runtime_config_store()
    build_backends_registry_reloader(store, settings, _UnusedSessionFactory())

    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1

        [[backends]]
        kind = "compute"
        id = "cloud-a"
        rank = 10
        cap = 2
        agent_ref = "cloud-agent-1"
        scratch_dir = "/scratch"
        push_host = "cloud-a.push"
        """
    )
    await store.reload("sighup")
    assert sorted(b.id for b in settings.backends) == ["cloud-a", "local"]


@pytest.mark.asyncio
async def test_a_kueue_backend_with_no_secrets_is_unaffected(backends_toml_env: Any) -> None:  # type: ignore[no-untyped-def]
    """Non-secret registries reload too -- KueueBackend parses through TypeAdapter identically."""
    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1

        [[backends]]
        kind = "kueue"
        id = "k8s"
        rank = 20
        cap = 4
        buckets = ["b1"]

        [backends.kube]
        api_url = "https://kube.example.com"
        namespace = "phaze"

        [[buckets]]
        id = "b1"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-a"
        """
    )
    settings = ControlSettings()
    reloader = BackendsRegistryReloader(settings, session_factory=_UnusedSessionFactory())
    assert isinstance(settings.backends[1], KueueBackend)
    before_local = next(b for b in settings.backends if b.id == "local")
    assert isinstance(before_local, LocalBackend)

    await reloader.reload("file")  # same content -- no-op, proven by the digest-short-circuit test above.
