"""The LEDGER REPLAY-SAFETY INVARIANT and its guards (phaze-71nz).

**A ``scheduling_ledger`` payload must be replayable at an arbitrary FUTURE time.**

That invariant is what ``recover_orphaned_work`` silently assumed and nothing enforced. On
2026-07-31 one operator Recover replayed 430 orphaned ``s3_upload`` rows whose payloads carried
presigned S3 PUT URLs signed at the ORIGINAL enqueue; 428 ran their retries out to terminal
``failed`` at the object store (122x HTTP 403, 257x HTTP 400) and ZERO succeeded, while the same
run's 2,512 ``process_file`` rows replayed fine.

These tests hold the GENERAL form of the fix, deliberately not a pin on ``s3_upload``:

1. the classification (:data:`LEDGER_REPLAY_TIME_INVARIANT` / :data:`LEDGER_REPLAY_REGENERATED`) is
   TOTAL and DISJOINT over the keyed-producer universe, so a NEW producer cannot slip through by
   omission the way ``s3_upload`` did;
2. every declared-regenerated function has a real regenerator, so "do not replay verbatim" can never
   quietly mean "never recover this stage";
3. the CONTENT detector recognises the shape of expiring material -- verified against REAL presigned
   URLs minted by a wire-compatible object store, in both the SigV4 form boto3 emits today and the
   SigV2 ``AWSAccessKeyId``/``Expires``/``Signature`` form the live incident actually produced -- and
   does NOT fire on any of the time-invariant producers' real payload shapes;
4. the write-side chokepoint (``apply_deterministic_key``) LOGS the violation without ever blocking
   an enqueue.

The replay-side refusal and the ``s3_upload`` regeneration itself live in
``tests/analyze/tasks/test_recovery_replay_safety.py``, which drives them through
``recover_orphaned_work`` against a real object store.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
import uuid

import boto3
from moto.server import ThreadedMotoServer
import pytest
from saq.job import Job

from phaze.tasks._shared.deterministic_key import _KEY_BUILDERS, apply_deterministic_key
from phaze.tasks._shared.replay_safety import (
    LEDGER_REPLAY_REGENERATED,
    LEDGER_REPLAY_TIME_INVARIANT,
    find_time_limited_paths,
)
from phaze.tasks.reenqueue import _REPLAY_REGENERATORS
from tests._queue_fakes import FakeRedis


if TYPE_CHECKING:
    from collections.abc import Iterator


_BUCKET = "phaze-replay-safety"
_CREDS = {"aws_access_key_id": "testing", "aws_secret_access_key": "testing"}


# ---------------------------------------------------------------------------
# 1. The classification is TOTAL and DISJOINT -- the anti-omission guard
# ---------------------------------------------------------------------------


def test_every_keyed_producer_is_classified() -> None:
    """Every keyed producer declares whether its ledger payload survives time. No silent third state.

    THE ROOT CAUSE, stated as a test. ``s3_upload`` did not "break" replay -- it was added as a keyed
    producer, its payload happened to embed presigned URLs, and no layer ever asked whether that was
    replayable. This assertion makes the question unavoidable: a new entry in ``_KEY_BUILDERS`` fails
    the build until its author says which side of the invariant it falls on.
    """
    classified = LEDGER_REPLAY_TIME_INVARIANT | LEDGER_REPLAY_REGENERATED
    keyed = set(_KEY_BUILDERS)
    assert keyed - classified == set(), (
        f"unclassified keyed producer(s) {sorted(keyed - classified)}: declare each in "
        "replay_safety.LEDGER_REPLAY_TIME_INVARIANT (payload is safe to replay verbatim, forever) or "
        "LEDGER_REPLAY_REGENERATED (payload carries time-limited material -- add a regenerator)"
    )
    assert classified - keyed == set(), f"stale classification entr(ies) {sorted(classified - keyed)} -- no such keyed producer"


def test_classification_is_disjoint() -> None:
    """A producer is EITHER replayed verbatim OR regenerated -- never ambiguous about its own payload."""
    assert frozenset() == LEDGER_REPLAY_TIME_INVARIANT & LEDGER_REPLAY_REGENERATED


def test_every_regenerated_producer_has_a_regenerator() -> None:
    """Declaring "never replay verbatim" without a regenerator would drop the stage from recovery.

    That failure mode is quieter than the bug being fixed but the same shape: the operator presses
    Recover, the stage reports zero re-enqueued forever, and nothing says why. ``reenqueue`` also
    fails LOUD at import on this mismatch; this test names the contract at the layer that owns it.
    """
    assert set(_REPLAY_REGENERATORS) == set(LEDGER_REPLAY_REGENERATED)


def test_s3_upload_is_the_declared_time_limited_producer() -> None:
    """The concrete, measured instance: ``s3_upload`` is regenerated, ``process_file`` is not.

    Locks the two halves of the 2026-07-31 contrast so a future refactor cannot quietly flip either:
    430 ``s3_upload`` rows replayed into guaranteed-403 jobs; 2,512 ``process_file`` rows replayed
    correctly, because that payload carries nothing that expires.
    """
    assert "s3_upload" in LEDGER_REPLAY_REGENERATED
    assert "process_file" in LEDGER_REPLAY_TIME_INVARIANT


# ---------------------------------------------------------------------------
# 2. The content detector, against REAL presigned URLs
# ---------------------------------------------------------------------------


@pytest.fixture
def moto_s3_server() -> Iterator[str]:
    """Start a wire-compatible moto S3 server on a free port; yield its endpoint URL.

    A REAL object store, not a mock (acceptance 5). The whole defect is an object-store auth
    response, so the URLs the detector is tested against must be signed by something that would
    actually reject them once expired -- a hand-written string could drift from the real query-param
    layout without anyone noticing.
    """
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


def _presign_part_url(endpoint: str, *, signature_version: str) -> str:
    """Presign a REAL multipart upload-part PUT URL against the moto server."""
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        config=Config(signature_version=signature_version, s3={"addressing_style": "path"}),
        **_CREDS,
    )
    client.create_bucket(Bucket=_BUCKET)
    upload_id = client.create_multipart_upload(Bucket=_BUCKET, Key="phaze-staging/obj")["UploadId"]
    return str(
        client.generate_presigned_url(
            "upload_part",
            Params={"Bucket": _BUCKET, "Key": "phaze-staging/obj", "UploadId": upload_id, "PartNumber": 1},
            ExpiresIn=60,
        )
    )


def test_detector_flags_a_real_sigv4_presigned_part_url(moto_s3_server: str) -> None:
    """A genuine SigV4 presigned PUT URL -- what boto3 mints today -- is caught, at its exact path."""
    url = _presign_part_url(moto_s3_server, signature_version="s3v4")
    assert "X-Amz-Signature" in url  # sanity: the harness really signed it
    payload = {"file_id": str(uuid.uuid4()), "original_path": "/music/set.flac", "part_urls": [url], "part_size_bytes": 5242880}
    assert find_time_limited_paths(payload) == ["part_urls[0]"]


def test_detector_flags_a_real_sigv2_presigned_part_url(moto_s3_server: str) -> None:
    """The SigV2 ``AWSAccessKeyId`` / ``Expires`` / ``Signature`` form -- the shape the INCIDENT produced.

    The live deployment's object store emitted V2-style presigned URLs, so a detector that only knew
    the modern ``X-Amz-*`` layout would have missed the exact 430 rows this bead exists for.
    """
    url = _presign_part_url(moto_s3_server, signature_version="s3")
    assert "AWSAccessKeyId" in url
    assert find_time_limited_paths({"part_urls": [url]}) == ["part_urls[0]"]


def test_detector_reports_every_offending_index(moto_s3_server: str) -> None:
    """A multipart payload carries one URL PER PART -- the report must name them all, not just the first."""
    url = _presign_part_url(moto_s3_server, signature_version="s3v4")
    assert find_time_limited_paths({"part_urls": [url, url, url]}) == ["part_urls[0]", "part_urls[1]", "part_urls[2]"]


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        # The real shapes of the nine time-invariant producers' payloads. Every one of these is
        # durable identity + paths + tuning knobs; none of it is minted against a clock, which is
        # exactly why 2,512 process_file rows replayed correctly in the same incident run.
        ("process_file", {"file_id": "f1", "original_path": "/music/set.flac", "file_type": "flac", "agent_id": "nox", "models_path": "/models"}),
        ("extract_file_metadata", {"file_id": "f1", "original_path": "/music/set.flac", "agent_id": "nox"}),
        ("search_tracklist", {"file_id": "f1"}),
        ("scrape_and_store_tracklist", {"tracklist_id": "t1"}),
        ("match_tracklist_to_discogs", {"tracklist_id": "t1"}),
        ("generate_proposals", {"file_ids": ["f1", "f2"], "batch_index": 0}),
        ("push_file", {"file_id": "f1", "original_path": "/music/set.flac", "agent_id": "nox", "destination": "host-store:/archive"}),
        ("submit_cloud_job", {"file_id": "f1"}),
        (
            "write_file_tags",
            {"log_id": "l1", "file_id": "f1", "agent_id": "nox", "file_path": "/music/set.flac", "tags": {"artist": "A", "title": "T"}},
        ),
    ],
)
def test_detector_is_quiet_on_time_invariant_payloads(name: str, payload: dict[str, Any]) -> None:
    """No false positives on the payloads that are genuinely replayable.

    A detector that cried wolf here would be worse than none: every refusal in ``_replay_row`` is a
    stage NOT recovered, so an over-eager rule would turn the incident tool into a no-op.
    """
    assert find_time_limited_paths(payload) == [], f"{name} payload wrongly flagged as time-limited"


@pytest.mark.parametrize(
    "payload",
    [
        {"download_url": "https://s3.test/bucket/key?X-Amz-Signature=abc&X-Amz-Expires=900"},
        {"nested": {"urls": ["https://s3.test/k?AWSAccessKeyId=AK&Expires=1&Signature=s"]}},
        {"auth_token": "eyJhbGciOi"},
        {"credentials": {"access_token": "tok"}},
        {"expires_at": "2026-07-31T00:00:00Z"},
        {"blob": {"sas_token": "sv=2021&sig=abc"}},
    ],
)
def test_detector_generalises_beyond_s3_upload(payload: dict[str, Any]) -> None:
    """The guard is about EXPIRING MATERIAL, not about one task name (acceptance 2).

    ``s3_upload`` is merely the first producer to trip the invariant. A future producer that stores a
    bearer token, an Azure SAS, a GCS signed URL, or an explicit deadline must be caught by the same
    rule -- otherwise this fix buys one incident's worth of safety and no more.
    """
    assert find_time_limited_paths(payload) != []


def test_detector_ignores_a_plain_url_with_no_credentials() -> None:
    """An ordinary addressable URL is not time-limited -- only a CREDENTIAL-bearing query string is."""
    assert find_time_limited_paths({"endpoint": "https://s3.test/bucket/key?versionId=42"}) == []


def test_detector_is_total_on_odd_payloads() -> None:
    """Never raises: an empty/None payload, exotic scalars, and deep nesting all just do not match.

    The detector runs on the enqueue hot path and inside recovery's per-row loop. A crash there would
    convert a bookkeeping opinion into a lost enqueue or an aborted recovery run -- strictly worse
    than the bug it guards.
    """
    assert find_time_limited_paths(None) == []
    assert find_time_limited_paths({}) == []
    assert find_time_limited_paths({"n": 1, "f": 1.5, "b": True, "none": None, "u": uuid.uuid4()}) == []
    # Nested well past the walk's depth ceiling: it stops, it does not blow the stack.
    deep: Any = "x"
    for _ in range(50):
        deep = {"nest": deep}
    assert find_time_limited_paths(deep) == []


# ---------------------------------------------------------------------------
# 3. The write-side chokepoint: DETECTS loudly, never blocks
# ---------------------------------------------------------------------------


class _FakeSession:
    """Minimal async-context-manager session double the ledger write hook opens via ``sm()``."""

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _CapturingLogger:
    """A structlog stand-in that records the STRUCTURED kwargs of every ``error`` call.

    The assertions here are about the fields (``function`` / ``payload_paths``) and about what is
    NOT in them (the signed URL), so the bound kwargs have to be inspectable -- a text-level
    ``caplog`` match could pass while the credential leaked in some other field.
    """

    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []

    def error(self, _event: str, **kwargs: Any) -> None:
        self.errors.append(kwargs)

    def warning(self, _event: str, **_kwargs: Any) -> None:
        return None

    def exception(self, _event: str, **_kwargs: Any) -> None:
        return None


async def test_write_hook_logs_when_a_replay_safe_producer_writes_expiring_material(
    monkeypatch: pytest.MonkeyPatch,
    moto_s3_server: str,
) -> None:
    """A producer declared replay-safe that writes a presigned URL is reported AT THE WRITE.

    The incident's most expensive property was silence: nothing in the response, the logs, or the
    ledger distinguished the 430 doomed rows from the 2,512 healthy ones. This is the earliest point
    the violation is knowable -- the payload is about to become durable -- so it is where it is
    cheapest to see. Only PATHS are logged; the values are live credentials.
    """
    log = _CapturingLogger()
    monkeypatch.setattr("phaze.tasks._shared.deterministic_key.logger", log)
    monkeypatch.setattr("phaze.services.scheduling_ledger.upsert_ledger_entry", _noop_upsert)

    url = _presign_part_url(moto_s3_server, signature_version="s3v4")
    job = Job(function="process_file", kwargs={"file_id": str(uuid.uuid4()), "sneaky_url": url})
    job.queue = SimpleNamespace(cache_redis=FakeRedis(), ledger_sessionmaker=_FakeSession)  # type: ignore[assignment]

    await apply_deterministic_key(job)

    assert len(log.errors) == 1
    assert log.errors[0]["function"] == "process_file"
    assert log.errors[0]["payload_paths"] == ["sneaky_url"]
    # PATHS only -- the signed URL itself must never reach a log sink.
    assert not any(url in str(value) for value in log.errors[0].values())


async def test_write_hook_does_not_flag_the_declared_regenerated_producer(
    monkeypatch: pytest.MonkeyPatch,
    moto_s3_server: str,
) -> None:
    """``s3_upload`` legitimately carries presigned URLs -- it is DECLARED, so it is not an anomaly.

    The ledger still stores them (the live ``/failed`` re-drive loop reads that row), and recovery
    regenerates rather than replays. Logging an error on every staging enqueue would drown the signal
    this hook exists to raise.
    """
    log = _CapturingLogger()
    monkeypatch.setattr("phaze.tasks._shared.deterministic_key.logger", log)
    monkeypatch.setattr("phaze.services.scheduling_ledger.upsert_ledger_entry", _noop_upsert)

    url = _presign_part_url(moto_s3_server, signature_version="s3v4")
    job = Job(function="s3_upload", kwargs={"file_id": str(uuid.uuid4()), "part_urls": [url]})
    job.queue = SimpleNamespace(cache_redis=FakeRedis(), ledger_sessionmaker=_FakeSession)  # type: ignore[assignment]

    await apply_deterministic_key(job)

    assert log.errors == []


async def test_write_hook_still_writes_the_ledger_row_on_a_violation(
    monkeypatch: pytest.MonkeyPatch,
    moto_s3_server: str,
) -> None:
    """The check DETECTS; it must never block the enqueue or drop the ledger row (T-45-03).

    A bookkeeping opinion that could fail an enqueue would be a far larger outage than the one it
    prevents, and dropping the row would strand the work outside recovery's view entirely. The hard
    refusal belongs at REPLAY time, where the only cost of being wrong is one deliberate skip.
    """
    captured: list[dict[str, Any]] = []

    async def _capture(
        session: object, *, key: str, function: str, kwargs: dict[str, Any], timeout: int | None = None, retries: int | None = None
    ) -> None:
        captured.append({"key": key, "function": function, "kwargs": kwargs})

    monkeypatch.setattr("phaze.services.scheduling_ledger.upsert_ledger_entry", _capture)

    url = _presign_part_url(moto_s3_server, signature_version="s3v4")
    fid = uuid.uuid4()
    job = Job(function="process_file", kwargs={"file_id": str(fid), "sneaky_url": url})
    job.queue = SimpleNamespace(cache_redis=FakeRedis(), ledger_sessionmaker=_FakeSession)  # type: ignore[assignment]

    await apply_deterministic_key(job)

    assert job.key == f"process_file:{fid}"
    assert len(captured) == 1
    assert captured[0]["kwargs"]["sneaky_url"] == url


async def _noop_upsert(*_a: object, **_k: object) -> None:
    """Stand-in for the ledger upsert -- these tests assert on the hook, not on Postgres."""
    return None
