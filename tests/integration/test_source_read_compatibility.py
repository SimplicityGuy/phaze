"""Adding SourceRead.validators leaves every local SourceRead consumer byte-identical.

docs/design/0025-remote-provider-sources.md ("Serialization invariant", "Blast radius"): stored
observations are reused only on an equal payload digest, and the agent's CaptureReport.read reaches
controllers whose SourceRead forbids unknown keys. ``PreChangeSourceRead`` is a frozen copy of the
model as it stood at 49971332, before the remote extension, so these tests compare against the schema
already deployed rather than against the code under test.
"""

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Annotated, Literal
import uuid

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from phaze.models.provider_source import ProviderSourceObservation
from phaze.schemas.agent_companion_capture import CaptureBudget, CaptureReport, CaptureTarget
from phaze.services import provider_persistence as storage
from phaze.services.companion_capture_worker import read_capture
from phaze.tracklist_providers.domain import Evidence, OutcomeStatus, SourceIdentity, SourceRead, SourceValidators, Text


class PreChangeSourceRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    status: OutcomeStatus
    code: Text
    scope: Text
    evidence: Evidence = ()
    text: Annotated[str, Field(max_length=262144)] | None = None
    encoding: Text | None = None
    revision: Annotated[str, Field(max_length=2048)] | None = None
    revision_scope: Literal["full", "bounded", "unknown"] = "unknown"
    truncated: bool = False
    bytes_read: int = Field(default=0, ge=0, le=1048576)
    retrieved_at: AwareDatetime


class PreChangeCaptureReport(BaseModel):
    """The controller's report schema before the change: only ``read`` differs from today's."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: uuid.UUID | None = None
    target: CaptureTarget
    budget: CaptureBudget = CaptureBudget()
    read: PreChangeSourceRead


RAW = b"Artist: Example Artist\r\n01. Example Artist - Opening\n02. Unknown\n"


def capture_target(tmp_path: Path) -> CaptureTarget:
    path = tmp_path / "notes.txt"
    path.write_bytes(RAW)
    return CaptureTarget(file_id=uuid.uuid4(), path=str(path), expected_sha256=hashlib.sha256(RAW).hexdigest(), expected_size=len(RAW))


def worker_stdout(target: CaptureTarget, tmp_path: Path) -> bytes:
    """The agent's real capture worker, run as the subprocess the capture task spawns."""
    request = json.dumps({"target": target.model_dump(mode="json"), "budget": CaptureBudget().model_dump(mode="json"), "scan_roots": [str(tmp_path)]})
    result = subprocess.run(
        [sys.executable, "-m", "phaze.services.companion_capture_worker"], input=request.encode(), capture_output=True, check=True
    )
    return result.stdout


def local_reads(tmp_path: Path) -> list[SourceRead]:
    target = capture_target(tmp_path)
    found = read_capture(target, CaptureBudget(), [str(tmp_path)])
    truncated = read_capture(target, CaptureBudget(max_bytes=8), [str(tmp_path)])
    missing = read_capture(target.model_copy(update={"path": str(tmp_path / "gone.txt")}), CaptureBudget(), [str(tmp_path)])
    assert (found.status, truncated.status, missing.status) == ("found", "incomplete", "absent")
    return [found, truncated, missing]


def test_local_source_read_payload_is_unchanged_without_validators(tmp_path: Path) -> None:
    for read in local_reads(tmp_path):
        before = PreChangeSourceRead.model_validate(read.model_dump())
        assert read.model_dump() == before.model_dump()
        assert read.model_dump(mode="json") == before.model_dump(mode="json")
        assert read.model_dump_json() == before.model_dump_json()
        # The exact bytes store_source_read digests.
        stored = storage._payload_bytes(read.model_dump(mode="json", exclude={"retrieved_at"}))
        assert stored == storage._payload_bytes(before.model_dump(mode="json", exclude={"retrieved_at"}))
    # The agent's real worker subprocess writes the same bytes the deployed schema writes.
    target = capture_target(tmp_path)
    stdout = worker_stdout(target, tmp_path)
    assert "validators" not in json.loads(stdout)
    assert PreChangeSourceRead.model_validate_json(stdout).model_dump_json().encode() == stdout


def test_capture_report_round_trips_against_the_pre_change_schema(tmp_path: Path) -> None:
    target = capture_target(tmp_path)
    for read in (*local_reads(tmp_path), SourceRead.model_validate_json(worker_stdout(target, tmp_path))):
        report_target = target.model_copy(update={"file_id": uuid.UUID(read.scope)})
        report = CaptureReport(target=report_target, read=read)
        # New agent -> controller one deploy behind.
        older = PreChangeCaptureReport.model_validate_json(report.model_dump_json())
        assert older.read.model_dump() == read.model_dump()
        # Agent one deploy behind -> new controller.
        newer = CaptureReport.model_validate_json(older.model_dump_json())
        assert newer == report and newer.read.validators is None
    # The deployed schema really does refuse the key, so omission is what keeps the wire compatible.
    remote = read.model_copy(update={"validators": SourceValidators(etag='"strong"')})
    with pytest.raises(ValidationError, match="validators"):
        PreChangeCaptureReport.model_validate_json(CaptureReport(target=report_target, read=remote).model_dump_json())


async def test_unchanged_local_recapture_reuses_its_observation(session: AsyncSession, tmp_path: Path) -> None:
    read = local_reads(tmp_path)[0]
    identity = SourceIdentity(provider_id="local", native_id=f"companion:{read.scope}")
    before = PreChangeSourceRead.model_validate(read.model_dump())
    # An observation exactly as the pre-change code stored it.
    legacy = await storage._store(
        session,
        identity,
        before.model_dump(mode="json", exclude={"retrieved_at"}),
        status=read.status.value,
        code=read.code,
        revision=read.revision,
        revision_scope=read.revision_scope,
        parser_version="source-read-v1",
        decoded_text=read.text,
        encoding=read.encoding,
        truncated=read.truncated,
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_url=None,
        source_file_id=None,
        channel=None,
        parent_id=None,
        snapshot=False,
    )
    assert not legacy.reused
    recaptured = read.model_copy(update={"retrieved_at": datetime.now(UTC)})
    stored = await storage.store_source_read(session, identity, recaptured, parser_version="source-read-v1")
    assert stored.reused and stored.observation.id == legacy.observation.id
    again = await storage.store_source_read(session, identity, recaptured, parser_version="source-read-v1")
    assert again.reused and again.observation.id == legacy.observation.id
    assert "validators" not in stored.observation.payload
    count = await session.scalar(
        select(func.count()).select_from(ProviderSourceObservation).where(ProviderSourceObservation.object_id == legacy.source.id)
    )
    assert count == 1
