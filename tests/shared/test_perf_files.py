"""Safety boundaries for the developer-only Files profiling probe."""

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlunsplit

from fastapi.responses import HTMLResponse
from httpx import ASGITransport, AsyncClient
import pytest

from phaze.database import get_session
from phaze.main import create_app
from phaze.services.pipeline.files import get_files_page
from scripts.perf_files import _DATABASE_SESSION_LOCK_KEY, healthy_files_reads, profile, validate_targets
from tests.db_guard import TEST_DB_SESSION_LOCK_KEY


_SEAT = "postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_profile_test"
# Parser-only fixtures: no Redis client connects to these component-built URLs.
_CACHE = urlunsplit(("redis", "localhost:6380", "/12", "", ""))


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://phaze:phaze@localhost:5432/phaze_profile_test",
        "postgresql://phaze:phaze@host-prod:5433/phaze_profile_test",
        "postgresql://phaze:phaze@localhost:5433/phaze_test",
        "postgresql://phaze:phaze@localhost:5433/phaze",
        "postgresql://phaze:phaze@localhost:5433/not-a-seat",
        "postgresql://phaze:phaze@localhost:5433/phaze_profile_test?host=host-prod",
    ],
)
def test_profile_rejects_targets_outside_an_isolated_local_seat(dsn: str) -> None:
    with pytest.raises(ValueError, match="dedicated local"):
        validate_targets(dsn, _CACHE)


@pytest.mark.parametrize(
    "cache",
    [
        urlunsplit(("redis", "localhost:6380", "/0", "", "")),
        urlunsplit(("redis", "localhost:6379", "/12", "", "")),
        urlunsplit(("redis", "host-prod:6380", "/12", "", "")),
        urlunsplit(("redis", "localhost:6380", "/", "", "")),
        urlunsplit(("redis", "localhost:6380", "/12", "db=0", "")),
    ],
)
def test_profile_rejects_shared_or_remote_cache(cache: str) -> None:
    with pytest.raises(ValueError, match="dedicated local"):
        validate_targets(_SEAT, cache)


def test_profile_uses_the_existing_database_session_lock_protocol() -> None:
    assert _DATABASE_SESSION_LOCK_KEY == TEST_DB_SESSION_LOCK_KEY
    validate_targets(_SEAT, _CACHE)


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [None, (False,)])
async def test_busy_database_never_reaches_profile_setup(row: tuple[bool] | None) -> None:
    connection = AsyncMock()
    connection.execute.return_value.fetchone.return_value = row
    manager = AsyncMock()
    manager.__aenter__.return_value = connection
    with (
        patch("scripts.perf_files.AsyncConnection.connect", AsyncMock(return_value=manager)),
        patch("scripts.perf_files._profile_owned", AsyncMock()) as setup,
        pytest.raises(RuntimeError, match="owned by a pytest session"),
    ):
        await profile(_SEAT, _CACHE, 1)
    setup.assert_not_awaited()
    manager.__aexit__.assert_awaited_once()


def _empty_session() -> MagicMock:
    session = MagicMock()
    session.begin_nested.return_value.__aenter__ = AsyncMock()
    session.begin_nested.return_value.__aexit__ = AsyncMock(return_value=False)
    result = MagicMock()
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_python_statement_failure_is_not_a_fast_empty_profile() -> None:
    session = _empty_session()
    with patch("phaze.services.pipeline.files._files_page_stmt", side_effect=RuntimeError("synthetic statement failure")):
        # This is the actual product downgrade that used to pass len(rows)<=50 and HTTP200.
        assert (await get_files_page(session)).rows == []
        with pytest.raises(RuntimeError, match="Refusing degraded Files profile"), healthy_files_reads([]):
            await get_files_page(session)
        # The observer is scoped; ordinary product callers retain their existing fallback.
        assert (await get_files_page(session)).rows == []
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [1, 100])
async def test_genuine_empty_and_past_end_reads_remain_valid_profiles(page: int) -> None:
    session = _empty_session()
    with healthy_files_reads([]):
        result = await get_files_page(session, page=page)
    assert result.rows == []
    assert result.has_next is False
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers", [{}, {"HX-Request": "true", "HX-Target": "stage-workspace"}, {"HX-Request": "true", "HX-Target": "files-table-view"}]
)
async def test_route_cannot_hide_degradation_behind_a_caught_http200(headers: dict[str, str], session) -> None:
    """Drive real document/rail/table routes with a handler that deliberately swallows the error."""
    app = create_app()

    async def session_override():
        yield session

    async def caught_fallback(_request, _error):
        return HTMLResponse("Synthetic caught fallback", status_code=200)

    app.dependency_overrides[get_session] = session_override
    app.add_exception_handler(RuntimeError, caught_fallback)
    failures: list[str] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://synthetic") as client:
        with (
            patch("phaze.services.pipeline.files._files_page_stmt", side_effect=RuntimeError("synthetic statement failure")),
            pytest.raises(RuntimeError, match="Refusing degraded Files profile"),
            healthy_files_reads(failures),
        ):
            response = await client.get("/s/files", headers=headers)
            assert response.status_code == 200
            assert response.text == "Synthetic caught fallback"
        assert failures == ["files_page_degraded"]
