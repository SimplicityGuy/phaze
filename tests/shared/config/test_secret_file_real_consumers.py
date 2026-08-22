"""The Docker-secrets path checked against each secret's REAL consumer (phaze-bk9el.18).

WHY THIS FILE EXISTS. ``test_secret_file_resolution.py`` proves the ``<VAR>_FILE`` convention
reads the right bytes off disk; ``test_redis_password_encoding.py`` proves ``redis_url`` comes
out percent-encoded. Both then assert with ``urlparse``/``unquote`` -- the settings layer's own
idiom, not the library that actually has to swallow the value in production. CLAUDE.md rule 3
is explicit that this is not enough: *"verify with the artifact's real consumer, not with the
tool that produced it"*, and ``phaze-3ea41`` is the incident where an artifact validated at its
producer's seam shipped a whole-archive regression.

phaze-bk9el.18 moved the secret-file group and the redis-password group out of
``BaseSettings`` into ``phaze.config_secrets`` / ``phaze.config_redis``. That split has
exactly one interesting failure mode -- a secret that still *resolves* but no longer *parses*
downstream -- and no existing test would have seen it, because every existing test stops at
the settings object. So each test below carries a resolved secret all the way into the library
that consumes it in production:

===========================  ==============================================================
secret (via ``<VAR>_FILE``)  real consumer exercised here
===========================  ==============================================================
``database_url``             ``sqlalchemy.ext.asyncio.create_async_engine`` (``phaze.database``)
``queue_url``                ``psycopg.conninfo`` -- libpq, what SAQ's ``PostgresQueue`` pool parses
``redis_url``/``password``   ``redis.asyncio.Redis.from_url`` -- the parser that percent-DECODES
``agent_token``              ``phaze.routers.agent_auth.hash_token``
``push_ssh_key``             ``ssh-keygen`` -- OpenSSH's own key parser (WR-01's actual judge)
===========================  ==============================================================

The ``push_ssh_key`` case is the one that needed a real binary rather than a Python library:
``cryptography.hazmat.primitives.serialization.load_ssh_private_key`` accepts a key whose
final armour line has no trailing newline, and OpenSSH does not (``invalid format``). Checking
WR-01 with ``cryptography`` would therefore have been another producer-seam verification --
green, and blind to the exact byte that broke every provisioned push.

No DB, no Redis, no network: every consumer below parses a DSN or a key, none of them connects.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import psycopg.conninfo
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import create_async_engine

from phaze.config import AgentSettings, ControlSettings
from phaze.routers.agent_auth import hash_token


if TYPE_CHECKING:
    from pathlib import Path


# Deliberately unroutable hosts: nothing here may reach a real service even by accident.
_DB_HOST = "db.realconsumer.invalid"
_QUEUE_HOST = "queue.realconsumer.invalid"
_REDIS_HOST = "redis.realconsumer.invalid"

# Every RFC-3986 byte that has bitten this code path: `/` and `?` truncated compose's netloc,
# `%` decoded to the wrong bytes, `@` and `:` split the userinfo (phaze-1g89i).
_HOSTILE_PASSWORD = "p@ss/w?rd#1%2f:x"

_AGENT_MIN_ENV = {
    "PHAZE_AGENT_API_URL": "http://app.realconsumer.invalid:8000",
    "PHAZE_AGENT_SCAN_ROOTS": "/data/music,/data/videos",
    "PHAZE_QUEUE_URL": f"postgresql://phaze:phaze@{_QUEUE_HOST}:5432/phaze",
}

# Names whose presence in the ambient environment would pre-empt the `_FILE` sibling under
# test (direct env always wins). The seat's own `PHAZE_REDIS_URL`/`DATABASE_URL` exports are
# deliberately left in place by `tests/conftest.py`, so clearing them here is not optional.
_PREEMPTING_ENV = (
    "PHAZE_DATABASE_URL",
    "DATABASE_URL",
    "database_url",
    "PHAZE_REDIS_URL",
    "REDIS_URL",
    "redis_url",
    "REDIS_PASSWORD",
    "PHAZE_QUEUE_URL",
    "queue_url",
    "PHAZE_AGENT_TOKEN",
    "PHAZE_PUSH_SSH_KEY",
)


@pytest.fixture(autouse=True)
def _clear_preempting_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PREEMPTING_ENV:
        monkeypatch.delenv(name, raising=False)


def _write(path: Path, content: str) -> str:
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_database_url_from_secret_file_is_parsed_by_sqlalchemy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A Docker-secret ``database_url`` survives into SQLAlchemy's own URL parser.

    ``create_async_engine`` is the real consumer (``phaze.database``): it is what actually
    rejects a malformed DSN, and it is what URL-decodes the password back to the bytes asyncpg
    sends. Constructing the engine opens no connection.
    """
    dsn = f"postgresql+asyncpg://phaze_user:p%40ss%2Fword@{_DB_HOST}:5432/phaze_prod"
    monkeypatch.setenv("PHAZE_DATABASE_URL_FILE", _write(tmp_path / "database_url", dsn + "\n"))

    engine = create_async_engine(ControlSettings().database_url)
    try:
        assert engine.url.host == _DB_HOST
        assert engine.url.username == "phaze_user"
        # SQLAlchemy percent-decodes: what asyncpg is handed is the raw password, not the DSN text.
        assert engine.url.password == "p@ss/word"
        assert engine.url.database == "phaze_prod"
        assert engine.url.get_backend_name() == "postgresql"
    finally:
        engine.sync_engine.dispose()


def test_queue_url_from_secret_file_is_a_libpq_dsn_psycopg_accepts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A Docker-secret ``queue_url`` written in SQLAlchemy dialect form reaches psycopg parseable.

    The real consumer is libpq via psycopg3's ``AsyncConnectionPool`` (SAQ's ``PostgresQueue``),
    which cannot parse a ``+driver`` suffix at all. The negative half is what makes this a real
    check rather than a tautology: the bytes as the operator wrote them are asserted to be
    *rejected*, so the test fails if ``_strip_sqlalchemy_driver`` stops running.
    """
    written = f"postgresql+asyncpg://phaze:phaze@{_QUEUE_HOST}:5432/phaze"
    monkeypatch.setenv("PHAZE_QUEUE_URL_FILE", _write(tmp_path / "queue_url", written + "\n"))

    resolved = ControlSettings().queue_url

    parsed = psycopg.conninfo.conninfo_to_dict(resolved)
    assert parsed["host"] == _QUEUE_HOST
    assert parsed["user"] == "phaze"
    assert parsed["dbname"] == "phaze"

    with pytest.raises(psycopg.ProgrammingError):
        psycopg.conninfo.conninfo_to_dict(written)


def test_redis_password_and_url_from_secrets_decode_back_in_redis_from_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Both extracted groups, end to end, judged by the parser that actually AUTHs.

    The secret-file group (``phaze.config_secrets``) supplies ``redis_url`` from a mounted file;
    the redis group (``phaze.config_redis``) percent-encodes ``redis_password`` into it. The
    only thing that settles whether that encoding is correct is
    ``redis.asyncio.Redis.from_url``, which RFC-3986 parses the DSN and percent-DECODES the
    userinfo -- so the assertion is on the bytes it will put on the wire, not on the DSN text.
    ``from_url`` builds a connection pool lazily and connects to nothing here.
    """
    monkeypatch.setenv("PHAZE_REDIS_URL_FILE", _write(tmp_path / "redis_url", f"redis://{_REDIS_HOST}:6379/3\n"))
    monkeypatch.setenv("REDIS_PASSWORD", _HOSTILE_PASSWORD)

    client = Redis.from_url(ControlSettings().redis_url)
    try:
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["password"] == _HOSTILE_PASSWORD
        assert kwargs["username"] == "default"
        assert kwargs["host"] == _REDIS_HOST
        assert kwargs["port"] == 6379
        assert kwargs["db"] == 3
    finally:
        client.connection_pool.reset()


def test_agent_token_from_secret_file_hashes_to_what_the_router_compares(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The mounted token hashes identically to the same token typed into an env var.

    ``hash_token`` (``phaze.routers.agent_auth``) hashes the WHOLE wire string, which is why the
    ``.strip()`` in the secret-file reader is load-bearing rather than cosmetic: a heredoc's
    trailing newline would otherwise hash to a value no agent could ever present.
    """
    for name, value in _AGENT_MIN_ENV.items():
        monkeypatch.setenv(name, value)
    token = "phaze_agent_real-consumer-token-0001"
    monkeypatch.setenv("PHAZE_AGENT_TOKEN_FILE", _write(tmp_path / "agent_token", token + "\n"))

    from_file = AgentSettings().agent_token
    assert from_file is not None
    assert hash_token(from_file.get_secret_value()) == hash_token(token)


def test_push_ssh_key_from_secret_file_is_accepted_by_openssh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """WR-01 judged by OpenSSH itself: the mounted key still loads, stripped it does not.

    ``phaze.tasks.push`` materializes ``push_ssh_key`` to a 0600 temp file and hands the path to
    ``ssh``, so ``ssh-keygen -y -f`` parses the exact artifact production produces. The second
    half is the control: the same key minus its final newline is rejected by ssh-keygen,
    which is the failure WR-01 exists to prevent and the reason
    ``SECRET_FILE_PRESERVE_WHITESPACE`` must keep listing this field after the split.
    """
    for name, value in _AGENT_MIN_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_real-consumer-token-0002")

    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode("ascii")
    monkeypatch.setenv("PHAZE_PUSH_SSH_KEY_FILE", _write(tmp_path / "id_ed25519", pem))

    resolved = AgentSettings().push_ssh_key
    assert resolved is not None
    materialized = tmp_path / "materialized"
    materialized.write_text(resolved.get_secret_value(), encoding="utf-8")
    materialized.chmod(0o600)

    loaded = subprocess.run(["ssh-keygen", "-y", "-f", str(materialized)], capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert loaded.returncode == 0, loaded.stderr
    assert loaded.stdout.startswith("ssh-ed25519 ")

    stripped = tmp_path / "stripped"
    stripped.write_text(resolved.get_secret_value().rstrip("\n"), encoding="utf-8")
    stripped.chmod(0o600)
    rejected = subprocess.run(["ssh-keygen", "-y", "-f", str(stripped)], capture_output=True, text=True, check=False)  # noqa: S603, S607
    assert rejected.returncode != 0
    # The REJECTION is the contract; the wording is not. OpenSSH's message depends on the
    # libcrypto it was built against -- macOS/LibreSSL says "invalid format", the GitHub
    # runner's OpenSSL build says "error in libcrypto". Asserting one spelling pinned the
    # build rather than the behaviour and failed CI while passing locally. Match either,
    # and keep the returncode assertion above as the real check.
    assert "invalid format" in rejected.stderr or "error in libcrypto" in rejected.stderr, rejected.stderr
