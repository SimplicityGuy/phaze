"""Seams A4 + A5 (phaze-0nq4i): a REAL staged S3 object, fetched through a REALLY-minted presign
URL, handed to the one-shot pod's REAL download -> sha256 -> extract -> essentia chain.

**The gap this closes.** ``docs/spikes/phaze-d2hgv.6-artifact-seam-inventory-2026-08-20.md`` rows
A4 and A5. Each half of this chain was tested, and the halves never met:

* the multipart producer had a real moto round trip, but with the TEST's httpx and one 37-byte
  part (``test_s3_staging.py::test_multipart_round_trip_assembles_object``);
* the presign route ran against a real moto server that never held an object, and no test ever
  fetched the URL it minted (``test_agent_presign_download.py`` asserts ``str(file.id) in url``);
* every ``job_runner`` test respx-mocks the GET, and the closest thing to an end-to-end
  (``test_phase101_e2e.py``) also monkeypatches ``extract_audio_track``.

So nothing could exhibit a multipart assembly whose bytes differ from what the pod hashes and
decodes, a dead or wrongly-signed URL, or wrong bytes behind a good one. That is CLAUDE.md rule 3
(verify with the artifact's real consumer) and ADR
``docs/design/0012-verification-fidelity-and-operator-attribution.md``, one boundary further out
than ``tests/analyze/services/pipeline/test_extraction_analysis_handoff.py``.

Every component below is the production one; only the TRANSPORTS are test-shaped:

    real ffmpeg-generated WAV, big enough for TWO multipart parts at S3's 5 MiB part floor
      -> cloud_staging.stage_file_to_s3            REAL producer: create multipart + presign parts
      -> tasks.s3_upload.upload_file_s3            REAL agent: httpx PUT of each part to moto
      -> PhazeAgentClient.report_upload_complete   REAL client, ASGITransport onto the REAL app
      -> routers/agent_s3 -> process_uploaded      REAL CompleteMultipartUpload + UPLOADED CAS
      -> job_runner.run()                          REAL pod: presign via the REAL route ...
      -> job_runner._download_to                   ... REAL GET of the minted URL (no respx) ...
      -> compute_sha256 / extract_audio_track      ... REAL sha gate, REAL ffprobe ...
      -> run_analysis_subprocess                   ... REAL child process, REAL essentia ...
      -> PhazeAgentClient.put_analysis             ... REAL callback into the REAL route
      -> Postgres                                  the stored AnalysisResult is the assertion

moto's S3 server (``ThreadedMotoServer``) enforces the rules that matter here itself: a part's
ETag must match what was PUT (``InvalidPart``) and every part but the last must be >= 5 MiB
(``EntityTooSmall``). No moto setting is overridden. The endpoint is the only stand-in; the
consumer under test is the pod's code, not AWS.

Two settings objects exist because production has two processes: the control plane
(``ControlSettings``, whose routes run in-process on the ASGI app) and the fileserver agent and
pod (``AgentSettings``). The agent-side modules' ``get_settings`` lookup is pointed at an
``AgentSettings`` built from the same env the pod receives; nothing else is patched.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import boto3
import httpx
from httpx import ASGITransport
from moto.server import ThreadedMotoServer
import pytest
from sqlalchemy import select, update
from sqlalchemy.sql import func

from phaze.config import ControlSettings, get_settings
from phaze.database import get_session
import phaze.job_runner as jr
from phaze.main import create_app
from phaze.models.agent import Agent
from phaze.models.analysis import AnalysisResult
from phaze.models.cloud_job import CloudJob, CloudJobStatus
from phaze.models.file import FileRecord
from phaze.services import cloud_staging, s3_staging
from phaze.services.agent_client import PhazeAgentClient
import phaze.tasks.s3_upload as s3_upload


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    import uuid

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession


_HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

pytestmark = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg/ffprobe not installed on this runner")

_BUCKET = "phaze-test-staging"
_CREDS = {"aws_access_key_id": "testing", "aws_secret_access_key": "testing"}
# S3's own minimum multipart part size, and ControlSettings' floor for the part-size knob.
_PART_SIZE = 5 * 1024 * 1024
# 32 s of 44.1 kHz stereo 16-bit PCM is 5,644,878 bytes: TWO parts at the 5 MiB floor (a full first
# part and a short last one), and exactly ONE natural 30 s fine window at the pod's default windowing.
_FIXTURE_SEC = 32
# A 220 Hz sine is the pitch A3. A decode of anything other than THESE bytes cannot key to A.
_FIXTURE_FREQ_HZ = 220


def _make_wav(dest: Path) -> Path:
    """Generate the fixture with REAL ffmpeg (no committed audio)."""
    argv = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={_FIXTURE_FREQ_HZ}:duration={_FIXTURE_SEC}",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        # Named explicitly so the sentinel test can write a WAV whose path has no extension.
        "-f",
        "wav",
        str(dest),
    ]
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    if proc.returncode != 0:  # pragma: no cover - a broken fixture, not a tested path
        msg = f"fixture ffmpeg failed (exit {proc.returncode}): {proc.stderr.strip()}"
        raise RuntimeError(msg)
    return dest


@pytest.fixture
def moto_s3_server() -> Iterator[str]:
    """A wire-compatible moto S3 server on a free port (real HTTP, real multipart rules)."""
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def seam_env(
    moto_s3_server: str,
    job_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    backends_toml_env: Callable[[str], object],
) -> Iterator[dict[str, Any]]:
    """Both processes' settings: the pod/agent ``AgentSettings`` and the control ``ControlSettings``.

    ``job_env`` sets the env the one-shot pod really receives. Its ``AgentSettings`` is captured
    first, then the process is flipped to ``PHAZE_ROLE=control`` for the in-process routes, and the
    two agent-side modules' ``get_settings`` lookups are pointed at the captured agent object.
    """
    agent_cfg = get_settings()
    assert type(agent_cfg).__name__ == "AgentSettings"

    monkeypatch.setenv("PHAZE_ROLE", "control")
    monkeypatch.setenv("PHAZE_S3_MULTIPART_PART_SIZE_BYTES", str(_PART_SIZE))
    backends_toml_env(
        f"""
        [[backends]]
        kind = "kueue"
        id = "cluster-01"
        rank = 10
        cap = 4
        buckets = ["staging"]

        [backends.kube]
        api_url = "https://kube.test"
        namespace = "phaze"
        local_queue = "phaze-lq"

        [[buckets]]
        id = "staging"
        scope = "shared"
        endpoint_url = "{moto_s3_server}"
        bucket = "{_BUCKET}"
        region = "us-east-1"
        addressing_style = "path"
        access_key_id = "testing"
        secret_access_key = "testing"
        """
    )
    control_cfg = get_settings()
    assert isinstance(control_cfg, ControlSettings)
    assert control_cfg.s3_multipart_part_size_bytes == _PART_SIZE

    monkeypatch.setattr(jr, "get_settings", lambda: agent_cfg)
    monkeypatch.setattr(s3_upload, "get_settings", lambda: agent_cfg)

    s3 = boto3.client("s3", endpoint_url=moto_s3_server, region_name="us-east-1", **_CREDS)
    s3.create_bucket(Bucket=_BUCKET)
    yield {**job_env, "endpoint": moto_s3_server, "s3": s3, "agent_cfg": agent_cfg, "control_cfg": control_cfg}
    get_settings.cache_clear()


class _RecordingQueue:
    """A SAQ queue stand-in that records each enqueue. The QUEUE is not the seam under test."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:
        return None

    async def enqueue(self, function: str, **kwargs: Any) -> object:
        self.enqueued.append((function, kwargs))
        return object()


class _RecordingRouter:
    """``AgentTaskRouter`` stand-in: every agent's queue is one recording queue."""

    def __init__(self) -> None:
        self.queue = _RecordingQueue()

    def queue_for(self, _agent_id: str, _lane: object) -> _RecordingQueue:
        return self.queue


def _control_app(session: AsyncSession, controller_queue: _RecordingQueue) -> FastAPI:
    """The REAL control-plane app, bound to the per-test session.

    ``ASGITransport`` does not run the lifespan, so the controller queue the upload-complete
    callback routes ``submit_cloud_job`` onto is supplied here (recording, not dispatching).
    """
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.state.controller_queue = controller_queue
    return app


def _agent_client(app: FastAPI, token: str) -> PhazeAgentClient:
    """The REAL ``PhazeAgentClient`` on an ``ASGITransport`` onto the REAL app, never respx."""
    inner = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"})
    return PhazeAgentClient(base_url="http://test", token=token, _client=inner)


async def _seed(session: AsyncSession, agent: Agent, source: Path, *, file_type: str, file_id: uuid.UUID) -> FileRecord:
    """A FileRecord whose ``sha256_hash`` is the SOURCE's real digest, owned by a live fileserver."""
    await session.execute(update(Agent).where(Agent.id == agent.id).values(last_seen_at=func.now()))
    data = source.read_bytes()
    file = FileRecord(
        id=file_id,
        sha256_hash=hashlib.sha256(data).hexdigest(),
        original_path=str(source),
        original_filename=source.name,
        current_path=str(source),
        file_type=file_type,
        file_size=len(data),
        agent_id=agent.id,
    )
    session.add(file)
    await session.commit()
    return file


class _Stage:
    """What the producer half left behind, for the pod half and for the assertions."""

    def __init__(self, upload_kwargs: dict[str, Any], app: FastAPI, controller_queue: _RecordingQueue) -> None:
        self.upload_kwargs = upload_kwargs
        self.app = app
        self.controller_queue = controller_queue


async def _stage_and_upload(
    session: AsyncSession,
    file: FileRecord,
    token: str,
    control_cfg: ControlSettings,
    *,
    tamper_source: Callable[[Path], None] | None = None,
) -> _Stage:
    """Run the REAL producer: stage (multipart + presign), agent PUTs every part, control completes.

    ``tamper_source`` runs between staging and the agent's read of the media file, i.e. at the
    moment a real source could change under an in-flight upload.
    """
    bucket = s3_staging.resolve_bucket_config(control_cfg, "staging")
    assert bucket is not None
    router = _RecordingRouter()
    await cloud_staging.stage_file_to_s3(session, file, router, bucket)  # type: ignore[arg-type]

    [(function, enqueue_kwargs)] = router.queue.enqueued
    assert function == "s3_upload"
    upload_kwargs = {k: v for k, v in enqueue_kwargs.items() if k not in {"key", "timeout", "retries"}}

    if tamper_source is not None:
        tamper_source(Path(file.original_path))

    controller_queue = _RecordingQueue()
    app = _control_app(session, controller_queue)
    client = _agent_client(app, token)
    try:
        result = await s3_upload.upload_file_s3({"api_client": client}, **upload_kwargs)
    finally:
        await client.close()
    assert result["status"] == "uploaded"
    return _Stage(upload_kwargs, app, controller_queue)


async def _run_pod(monkeypatch: pytest.MonkeyPatch, app: FastAPI, token: str, file_id: uuid.UUID) -> tuple[int | str | None, dict[str, Any]]:
    """Run the REAL one-shot pod; return its exit code and what its download actually fetched.

    ``construct_agent_client`` is pointed at the same REAL client on the ASGI app (the pod's TLS
    callback hop is a transport, not the consumer). ``_download_to`` is WRAPPED, never replaced:
    the real GET runs, and the wrapper only records the URL it was handed and the digest and path
    of what landed, before ``run``'s ``finally`` deletes the temp file.
    """
    monkeypatch.setenv("PHAZE_JOB_FILE_ID", str(file_id))
    monkeypatch.setattr(jr, "construct_agent_client", lambda _cfg: _agent_client(app, token))

    fetched: dict[str, Any] = {}
    real_download_to = jr._download_to

    async def _recording_download_to(url: str, dest: Path) -> None:
        await real_download_to(url, dest)
        fetched["url"] = url
        fetched["dest"] = dest
        fetched["sha256"] = hashlib.sha256(dest.read_bytes()).hexdigest()
        fetched["size"] = dest.stat().st_size

    monkeypatch.setattr(jr, "_download_to", _recording_download_to)

    with pytest.raises(SystemExit) as exc:
        await jr.run()
    return exc.value.code, fetched


async def _analysis_row(session: AsyncSession, file_id: uuid.UUID) -> AnalysisResult | None:
    session.expire_all()
    return (await session.execute(select(AnalysisResult).where(AnalysisResult.file_id == file_id))).scalar_one_or_none()


async def test_a_multipart_staged_object_fetched_through_the_minted_url_is_what_the_pod_analyzes(
    seam_env: dict[str, Any],
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A4 + A5 end to end: the object the producer ASSEMBLED is the audio the pod ANALYZED.

    The assertions reach past the exit code on purpose. Exit 0 alone would prove only that some
    bytes hashed right; what this seam owes is that the minted URL was the one fetched, that it
    served the assembled object, and that essentia decoded THAT audio (a stored key of A from a
    220 Hz source, and the natural window count its real duration implies).
    """
    agent, token = seed_test_agent
    file_id: uuid.UUID = seam_env["file_id"]
    source = _make_wav(tmp_path / "set-01.wav")
    file = await _seed(session, agent, source, file_type="wav", file_id=file_id)

    stage = await _stage_and_upload(session, file, token, seam_env["control_cfg"])

    # The producer really split the object: two presigned parts, a full 5 MiB one and a short tail.
    assert len(stage.upload_kwargs["part_urls"]) == 2
    assert stage.upload_kwargs["part_size_bytes"] == _PART_SIZE
    cloud_job = (await session.execute(select(CloudJob).where(CloudJob.file_id == file_id))).scalar_one()
    assert cloud_job.status == CloudJobStatus.UPLOADED.value
    assert [name for name, _ in stage.controller_queue.enqueued] == ["submit_cloud_job"]
    staged = seam_env["s3"].get_object(Bucket=_BUCKET, Key=s3_staging.staged_object_key(file_id))["Body"].read()
    assert hashlib.sha256(staged).hexdigest() == file.sha256_hash, "the ASSEMBLED object is not the source file"

    code, fetched = await _run_pod(monkeypatch, stage.app, token, file_id)

    assert code == jr.EXIT_OK
    # A5: the URL the REAL route minted is the one the pod FETCHED, over real HTTP from the S3
    # endpoint, carrying a signature -- and what it served is the assembled object.
    endpoint = urlparse(seam_env["endpoint"])
    got = urlparse(fetched["url"])
    assert (got.hostname, got.port) == (endpoint.hostname, endpoint.port)
    assert got.path.endswith(s3_staging.staged_object_key(file_id))
    assert "Signature" in got.query
    assert fetched["sha256"] == file.sha256_hash
    assert fetched["size"] == file.file_size
    # The server-threaded audio_ext named the temp file, not the `.audio` sentinel.
    assert fetched["dest"].suffix == ".wav"

    row = await _analysis_row(session, file_id)
    assert row is not None, "the pod exited 0 but the control plane stored no analysis"
    assert row.fine_windows_total == 1
    assert row.fine_windows_analyzed == 1
    assert row.musical_key is not None
    assert row.musical_key.split()[0] == "A", f"a {_FIXTURE_FREQ_HZ} Hz source decoded to key {row.musical_key!r}: not these bytes"


async def test_an_assembled_object_that_differs_from_the_recorded_file_is_refused_by_the_pod(
    seam_env: dict[str, Any],
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A4's "cannot exhibit" case: the assembled bytes differ from ``FileRecord.sha256_hash``.

    The source is truncated after staging, so the agent uploads a SHORTER final part and S3
    assembles an object with a truncated tail -- a real, well-formed multipart completion whose
    content is wrong. The pod's sha gate must refuse it (EXIT_INTEGRITY), and nothing is stored.
    """
    agent, token = seed_test_agent
    file_id: uuid.UUID = seam_env["file_id"]
    source = _make_wav(tmp_path / "set-01.wav")
    file = await _seed(session, agent, source, file_type="wav", file_id=file_id)

    def _truncate_tail(path: Path) -> None:
        data = path.read_bytes()
        path.write_bytes(data[: len(data) - 4096])

    stage = await _stage_and_upload(session, file, token, seam_env["control_cfg"], tamper_source=_truncate_tail)

    code, fetched = await _run_pod(monkeypatch, stage.app, token, file_id)

    assert code == jr.EXIT_INTEGRITY
    assert fetched["size"] == file.file_size - 4096, "the pod fetched something other than the assembled object"
    assert await _analysis_row(session, file_id) is None


async def test_the_audio_sentinel_is_never_a_silent_empty_success(
    seam_env: dict[str, Any],
    session: AsyncSession,
    seed_test_agent: tuple[Agent, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A5's sentinel: an empty ``file_type`` sends the pod down the ``.audio`` last-resort suffix.

    ``agent_files.py`` and ``job_runner._temp_suffix`` document the hazard as essentia decoding 0
    duration -> 0 windows -> a silent empty-but-successful analysis. The invariant pinned here is
    the one the bead asks for, and it is platform-independent: whatever the decoder makes of a
    ``.audio`` file, the pod never exits 0 with an empty analysis stored. Either the analysis is
    genuine (windows present, and the right audio), or the pod fails loudly (EXIT_ANALYSIS) and the
    control plane holds no result.

    Measured 2026-09-23 on macOS arm64, essentia-tensorflow 2.1b6.dev1438, ffmpeg 9.0.2: a
    ``.audio``-suffixed WAV and MP3 both decode correctly (ffprobe 32.0 s, 1/1 fine windows), so on
    that platform this test takes the genuine-analysis branch. The deployed linux x86_64 wheel was
    NOT measured; that is why both branches are accepted and the invariant, not the branch, is the
    assertion.
    """
    agent, token = seed_test_agent
    file_id: uuid.UUID = seam_env["file_id"]
    # A real WAV whose stored extension is empty: the ingest schema forbids it
    # (FileUpsertRecord.file_type min_length=1) but the column does not, so this is seeded directly.
    source = _make_wav(tmp_path / "set-01")
    file = await _seed(session, agent, source, file_type="", file_id=file_id)

    stage = await _stage_and_upload(session, file, token, seam_env["control_cfg"])
    code, fetched = await _run_pod(monkeypatch, stage.app, token, file_id)

    assert fetched["dest"].suffix == ".audio", "the sentinel path was not reached, so this test proves nothing"
    row = await _analysis_row(session, file_id)
    if code == jr.EXIT_OK:
        assert row is not None
        assert row.fine_windows_total == 1
        assert row.fine_windows_analyzed == 1, "exit 0 with no analyzed window is the silent empty success"
        assert row.musical_key is not None
        assert row.musical_key.split()[0] == "A"
    else:
        assert code == jr.EXIT_ANALYSIS
        assert row is None or not row.fine_windows_analyzed
