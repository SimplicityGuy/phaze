"""Independent provider substitution and executable boundary invariants."""

import asyncio
import contextlib
from datetime import UTC, datetime
from fractions import Fraction
import subprocess
import sys

from pydantic import ValidationError
import pytest

from phaze.tracklist_providers import LocalProvider, ProviderContractError, ProviderRegistry, local_registry
from phaze.tracklist_providers.domain import (
    Capability,
    Completeness,
    DiscoveryBudget,
    DiscoveryOutcome,
    LoadBudget,
    LoadOutcome,
    OpaqueCursor,
    OutcomeStatus,
    ProviderCandidate,
    ProviderDescriptor,
    ProviderTrack,
    RationalOffset,
    RecordingContext,
    ReleaseFact,
    Snapshot,
    SourceIdentity,
    SourceRead,
    SourceReference,
    Timestamp,
)
from phaze.tracklist_providers.registry import context_key


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def identity(provider: str = "example_catalog", native: str = "set-01") -> SourceIdentity:
    return SourceIdentity(provider_id=provider, native_id=native)


def snapshot(**overrides: object) -> Snapshot:
    values = {
        "identity": identity(),
        "retrieved_at": NOW,
        "source_format": "example",
        "parser_version": "1",
        "completeness": Completeness(state="complete", reason="exhausted", evidence=("Explicit empty-track source",)),
    }
    values.update(overrides)
    return Snapshot.model_validate(values)


class Effects:
    def __init__(self, **overrides: object) -> None:
        self.calls = 0
        self.values: dict[str, object] = {
            "status": "found",
            "code": "read",
            "scope": "set-01",
            "text": "01. Example Artist - Example Title",
            "bytes_read": 34,
            "retrieved_at": NOW,
        }
        self.values.update(overrides)

    async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
        del source, budget
        self.calls += 1
        return SourceRead.model_validate(self.values)


class ExampleProvider:
    descriptor = ProviderDescriptor(
        id="example_catalog", display_name="Fictional catalog", capabilities=frozenset({Capability.LOAD, Capability.DISCOVERY})
    )

    async def discover(self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: Effects) -> DiscoveryOutcome:
        del budget, cursor, effects
        return DiscoveryOutcome(
            status="found",
            code="exhausted",
            scope=context.recording_id,
            candidates=(ProviderCandidate(identity=identity()),),
            completeness=Completeness(state="complete", reason="exhausted", evidence=("Explicit empty-track source",)),
        )

    async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: Effects) -> LoadOutcome:
        del budget, effects
        return LoadOutcome(
            status="found",
            code="loaded",
            scope=candidate.identity.native_id,
            snapshot=snapshot(tracks=(ProviderTrack(position=1), ProviderTrack(position=3, title="Example Title"))),
        )


@pytest.mark.asyncio
async def test_independent_provider_substitution() -> None:
    registry = ProviderRegistry((ExampleProvider(), LocalProvider()))
    effects = Effects()
    discovery = await registry.discover("example_catalog", RecordingContext(recording_id="recording-01"), DiscoveryBudget(), effects)
    result = await registry.load(discovery.candidates[0], LoadBudget(), effects)
    assert result.status == "found"
    assert result.snapshot is not None
    assert result.snapshot.tracks[0].title is None
    assert result.snapshot.tracks[0].artist is None
    assert result.snapshot.tracks[1].position == 3
    assert effects.calls == 0
    assert [d.id for d in registry.descriptors()] == ["example_catalog", "local"]
    assert identity("local") != identity()


@pytest.mark.parametrize("status", list(OutcomeStatus))
def test_typed_outcomes_roundtrip(status: OutcomeStatus) -> None:
    result = LoadOutcome(status=status, code="evidence", scope="native-object", snapshot=snapshot() if status == "found" else None)
    assert LoadOutcome.model_validate_json(result.model_dump_json()) == result


def test_timestamp_precision_origin_and_nullable_tracks() -> None:
    timestamp = Timestamp(
        original="00:03:10",
        kind="offset",
        precision="cue_frame_75",
        origin="file-01",
        offset=RationalOffset(numerator=235, denominator=75),
        offset_usability="qualified",
        evidence=("CUE INDEX 01 is file-relative",),
    )
    saved = Snapshot.model_validate_json(snapshot(tracks=(ProviderTrack(position=1, timestamp=timestamp),)).model_dump_json())
    assert saved.tracks[0].timestamp.offset.as_fraction() == Fraction(235, 75)
    for changes in (
        {"kind": "clock"},
        {"origin": None},
        {"evidence": ()},
        {"precision": "minute"},
        {"offset": RationalOffset(numerator=1, denominator=7)},
    ):
        with pytest.raises(ValidationError):
            Timestamp.model_validate(timestamp.model_dump() | changes)
    assert Timestamp(original="12:35", kind="unknown").offset is None


def test_malformed_tracks_and_aggregate_bounds() -> None:
    with pytest.raises(ValidationError, match="unique and ordered"):
        snapshot(tracks=(ProviderTrack(position=1), ProviderTrack(position=1)))
    with pytest.raises(ValidationError, match="4MiB"):
        snapshot(tracks=tuple(ProviderTrack(position=i + 1, title="x" * 4096, artist="x" * 4096) for i in range(600)))
    with pytest.raises(ValidationError):
        LoadOutcome(status="found", code="fake", scope="set-01")
    with pytest.raises(ValidationError):
        LoadBudget(max_tracks=0)
    with pytest.raises(ValidationError):
        Snapshot.model_validate(snapshot().model_dump() | {"retrieved_at": datetime(2026, 1, 1)})


def test_monotonicity_validated_separately_for_every_origin() -> None:
    def cue(position: int, origin: str, seconds: int) -> ProviderTrack:
        return ProviderTrack(
            position=position,
            timestamp=Timestamp(
                original=str(seconds),
                kind="offset",
                precision="second",
                origin=origin,
                offset=RationalOffset(numerator=seconds, denominator=1),
                offset_usability="qualified",
                evidence=("origin confirmed",),
            ),
        )

    with pytest.raises(ValidationError, match="each origin"):
        snapshot(tracks=(cue(1, "part-01", 4), cue(2, "part-02", 0), cue(3, "part-01", 3)))
    snapshot(tracks=(cue(1, "part-01", 4), cue(2, "part-02", 0), cue(3, "part-01", 5)))


def test_normalization_and_release_fact_evidence() -> None:
    fact = ReleaseFact(
        field="duration",
        value="1 h",
        original_value="Duration: 1 h",
        normalized_value="3600",
        unit="seconds",
        certainty="known",
        source_line=2,
        evidence=("explicit source label",),
    )
    first = snapshot(release_facts=(fact,), text="Duration: 1 h")
    second = first.model_copy(update={"retrieved_at": datetime(2026, 2, 1, tzinfo=UTC), "url": "https://example.invalid/source"})
    assert first.normalized_payload() == second.normalized_payload()
    assert first.normalized_payload()["release_facts"][0]["source_line"] == 2


def test_duplicate_version_and_import_time_no_io() -> None:
    with pytest.raises(ProviderContractError, match="Duplicate"):
        ProviderRegistry((ExampleProvider(), ExampleProvider()))
    provider = ExampleProvider()
    provider.descriptor = provider.descriptor.model_copy(update={"contract_version": (1, 1)})
    with pytest.raises(ProviderContractError, match="version"):
        ProviderRegistry((provider,))
    script = """
from unittest.mock import patch
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
import asyncio
with patch('builtins.open', side_effect=AssertionError('import I/O')), patch('socket.socket', side_effect=AssertionError('network I/O')):
    from phaze.tracklist_providers import local_registry
    assert local_registry().descriptors()[0].id == 'local'
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_unknown_disabled_explicit_only_and_cursor_rejection() -> None:
    effects = Effects()
    context = RecordingContext(recording_id="recording-01")
    registry = ProviderRegistry((ExampleProvider(),), disabled=frozenset({"example_catalog"}))
    assert (await registry.discover("missing", context, DiscoveryBudget(), effects)).code == "unknown_provider"
    assert (await registry.load(ProviderCandidate(identity=identity()), LoadBudget(), effects)).code == "disabled_provider"
    registry = ProviderRegistry((ExampleProvider(),))
    bad = OpaqueCursor(provider_id="example_catalog", context_key="different", token=str(0))
    assert (await registry.discover("example_catalog", context, DiscoveryBudget(), effects, bad)).code == "cursor_mismatch"
    provider = ExampleProvider()
    provider.descriptor = ProviderDescriptor(id="example_catalog", display_name="Explicit only")
    assert (await ProviderRegistry((provider,)).discover("example_catalog", context, DiscoveryBudget(), effects)).code == "discovery_unsupported"
    assert effects.calls == 0


def local_context(count: int = 1) -> RecordingContext:
    return RecordingContext(
        recording_id="recording-01",
        sources=tuple(SourceReference(identity=identity("local", f"companion:{i}"), channel="companion", format="txt") for i in range(count)),
    )


@pytest.mark.asyncio
async def test_local_pagination_and_recognition_only_text() -> None:
    effects = Effects(encoding="utf-8", revision="r1", revision_scope="full")
    registry = local_registry()
    context = local_context(3)
    first = await registry.discover("local", context, DiscoveryBudget(max_candidates=2), effects)
    assert first.status == "incomplete" and first.cursor is not None
    second = await registry.discover("local", context, DiscoveryBudget(max_candidates=2), effects, first.cursor)
    assert second.status == "found" and len(second.candidates) == 1
    assert effects.calls == 0
    result = await registry.load(first.candidates[0], LoadBudget(), effects)
    assert result.status == "incomplete" and result.code == "recognition_only"
    assert result.snapshot is not None and result.snapshot.text == effects.values["text"]
    assert result.snapshot.tracks == ()
    assert context_key(context) != context_key(local_context(2))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,code",
    [
        ({"truncated": True}, "read_truncated"),
        ({"status": "unavailable", "text": None, "code": "permission_denied"}, "permission_denied"),
        ({"text": "x" * 40}, "input_cap"),
    ],
)
async def test_read_failures_and_budgets(changes: dict[str, object], code: str) -> None:
    candidate = ProviderCandidate(identity=local_context().sources[0].identity, source=local_context().sources[0])
    result = await local_registry().load(candidate, LoadBudget(max_characters=35), Effects(**changes))
    assert result.code == code
    assert result.status != "found"


@pytest.mark.asyncio
async def test_changed_revision_and_foreign_snapshot_fail_closed() -> None:
    source = local_context().sources[0]
    candidate = ProviderCandidate(identity=source.identity, source=source, observed_revision="old")
    assert (await local_registry().load(candidate, LoadBudget(), Effects(revision="new"))).status == "retry"
    candidate = ProviderCandidate(identity=identity(native="other"))
    result = await ProviderRegistry((ExampleProvider(),)).load(candidate, LoadBudget(), Effects())
    assert result.status == "contract_error"


@pytest.mark.asyncio
async def test_deadline_and_cancellation() -> None:
    class Slow(ExampleProvider):
        async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: Effects) -> LoadOutcome:
            await asyncio.sleep(10)
            return await super().load(candidate, budget, effects)

    registry = ProviderRegistry((Slow(),))
    result = await registry.load(ProviderCandidate(identity=identity()), LoadBudget(deadline_seconds=0.001), Effects())
    assert result.code == "deadline"
    task = asyncio.create_task(registry.load(ProviderCandidate(identity=identity()), LoadBudget(), Effects()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_adapter_contract_errors_and_limits() -> None:
    class Output(ExampleProvider):
        def __init__(self, result: LoadOutcome) -> None:
            self.result = result

        async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: Effects) -> LoadOutcome:
            del candidate, budget, effects
            return self.result

    def registry(value: Snapshot) -> ProviderRegistry:
        return ProviderRegistry((Output(LoadOutcome(status="found", code="loaded", scope="set-01", snapshot=value)),))

    candidate = ProviderCandidate(identity=identity())
    result = await registry(snapshot(text="x" * 10)).load(candidate, LoadBudget(max_characters=9), Effects())
    assert result.code == "output_cap"
    result = await registry(snapshot(tracks=(ProviderTrack(position=1), ProviderTrack(position=2)))).load(
        candidate, LoadBudget(max_tracks=1), Effects()
    )
    assert result.code == "track_cap" and result.snapshot.completeness.state == "incomplete"
    result = await registry(snapshot(revision="new")).load(candidate.model_copy(update={"observed_revision": "old"}), LoadBudget(), Effects())
    assert result.code == "revision_changed"
    assert (await local_registry().load(ProviderCandidate(identity=identity("local")), LoadBudget(), Effects())).code == "missing_source_reference"
    cursor = OpaqueCursor(provider_id="local", context_key=context_key(local_context()), token="invalid")  # noqa: S106
    assert (await local_registry().discover("local", local_context(), DiscoveryBudget(), Effects(), cursor)).code == "invalid_cursor"
    cursor = cursor.model_copy(update={"token": "-1"})
    assert (await local_registry().discover("local", local_context(), DiscoveryBudget(), Effects(), cursor)).code == "invalid_cursor"


@pytest.mark.asyncio
async def test_effect_authorization_read_count_and_io_error() -> None:
    class Reads(LocalProvider):
        async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: Effects) -> LoadOutcome:
            await effects.read(candidate.source, budget)
            await effects.read(candidate.source, budget)
            return await super().load(candidate, budget, effects)

    source = local_context().sources[0]
    candidate = ProviderCandidate(identity=source.identity, source=source)
    assert (await ProviderRegistry((Reads(),)).load(candidate, LoadBudget(max_reads=1), Effects())).code == "read_cap"

    class Unauthorized(LocalProvider):
        async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: Effects) -> LoadOutcome:
            await effects.read(source.model_copy(update={"revision": "foreign"}), budget)
            return await super().load(candidate, budget, effects)

    assert (await ProviderRegistry((Unauthorized(),)).load(candidate, LoadBudget(), Effects())).status == "contract_error"

    class IOErrorEffects(Effects):
        async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
            del source, budget
            raise PermissionError("refused")

    assert (await local_registry().load(candidate, LoadBudget(), IOErrorEffects())).code == "read_error"


@pytest.mark.asyncio
async def test_local_injected_interpreter() -> None:
    class Interpreter:
        def interpret(self, candidate: ProviderCandidate, content: SourceRead, budget: LoadBudget) -> LoadOutcome:
            del budget
            return LoadOutcome(status="found", code="parsed", scope=content.scope, snapshot=snapshot(identity=candidate.identity, text=content.text))

    source = local_context().sources[0]
    result = await local_registry(Interpreter()).load(ProviderCandidate(identity=source.identity, source=source), LoadBudget(), Effects())
    assert result.status == "found"


@pytest.mark.asyncio
async def test_discovery_rejects_foreign_outputs_and_preserves_partial_batch() -> None:
    class Batch(ExampleProvider):
        def __init__(self, foreign: bool = False) -> None:
            self.foreign = foreign

        async def discover(
            self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: Effects
        ) -> DiscoveryOutcome:
            del budget, cursor, effects
            return DiscoveryOutcome(
                status="found",
                code="exhausted",
                scope=context.recording_id,
                completeness=Completeness(state="complete", reason="enumerated"),
                candidates=tuple(ProviderCandidate(identity=identity("local" if self.foreign else "example_catalog", str(i))) for i in range(2)),
            )

    context = RecordingContext(recording_id="recording-01")
    assert (await ProviderRegistry((Batch(True),)).discover("example_catalog", context, DiscoveryBudget(), Effects())).status == "contract_error"
    result = await ProviderRegistry((Batch(),)).discover("example_catalog", context, DiscoveryBudget(max_candidates=1), Effects())
    assert result.status == "incomplete" and len(result.candidates) == 1


def test_contract_validation_of_metadata_and_identity() -> None:
    from phaze.tracklist_providers.domain import Fact

    with pytest.raises(ValidationError):
        ProviderDescriptor(id="bad-id", display_name="Invalid")
    with pytest.raises(ValidationError):
        ProviderDescriptor(id="valid", display_name="Invalid", capabilities=frozenset())
    with pytest.raises(ValidationError):
        Fact(certainty="known")
    with pytest.raises(ValidationError):
        snapshot(date=Fact(certainty="known", value="2026-02-31"))
    with pytest.raises(ValidationError):
        ProviderCandidate(identity=identity(), source=local_context().sources[0])
    with pytest.raises(ValidationError):
        DiscoveryOutcome(status="found", code="fabricated", scope="recording-01")


def test_contradictory_outcomes_and_timestamp_granularity() -> None:
    partial = snapshot(completeness=Completeness(state="incomplete", reason="partial"))
    for status, content in (
        ("absent", snapshot()),
        ("incomplete", snapshot()),
        ("found", snapshot(completeness=Completeness(state="complete", reason="unknown empty source"))),
    ):
        with pytest.raises(ValidationError):
            LoadOutcome(status=status, code="contradiction", scope="set-01", snapshot=content)
    assert LoadOutcome(status="incomplete", code="partial", scope="set-01", snapshot=partial).snapshot is not None
    with pytest.raises(ValidationError):
        DiscoveryOutcome(status="incomplete", code="missing_reason", scope="set-01")
    for precision, numerator, denominator in (("second", 1, 75), ("minute", 1, 2)):
        with pytest.raises(ValidationError):
            Timestamp(original="original", kind="offset", precision=precision, offset=RationalOffset(numerator=numerator, denominator=denominator))


@pytest.mark.asyncio
async def test_sticky_read_violation_and_remaining_allowance() -> None:
    class Tolerant(LocalProvider):
        async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: Effects) -> LoadOutcome:
            with contextlib.suppress(ValueError):
                await effects.read(candidate.source, budget.model_copy(update={"max_bytes": budget.max_bytes + 1}))
            return LoadOutcome(status="found", code="ignored", scope="set-01", snapshot=snapshot(identity=candidate.identity))

    source = local_context().sources[0]
    candidate = ProviderCandidate(identity=source.identity, source=source)
    effects = Effects()
    result = await ProviderRegistry((Tolerant(),)).load(candidate, LoadBudget(), effects)
    assert result.status == "contract_error" and result.code == "budget_changed" and effects.calls == 0

    class TwoReads(LocalProvider):
        async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: Effects) -> LoadOutcome:
            await effects.read(candidate.source, budget)
            await effects.read(candidate.source, budget)
            return LoadOutcome(status="found", code="two", scope="set-01", snapshot=snapshot(identity=candidate.identity))

    class Capture(Effects):
        def __init__(self) -> None:
            super().__init__(text="xx", bytes_read=2)
            self.budgets: list[DiscoveryBudget] = []

        async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
            self.budgets.append(budget)
            return await super().read(source, budget)

    effects = Capture()
    assert (await ProviderRegistry((TwoReads(),)).load(candidate, LoadBudget(max_bytes=4), effects)).status == "found"
    assert [b.max_bytes for b in effects.budgets] == [4, 2]
    effects = Capture()
    assert (await ProviderRegistry((TwoReads(),)).load(candidate, LoadBudget(max_bytes=2), effects)).code == "input_cap"
    assert len(effects.budgets) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception,status",
    [(RuntimeError("adapter failure"), "contract_error"), (PermissionError("read failure"), "unavailable"), (TimeoutError(), "incomplete")],
)
async def test_discovery_typed_exception_boundary(exception: Exception, status: str) -> None:
    class Broken(ExampleProvider):
        async def discover(
            self, context: RecordingContext, budget: DiscoveryBudget, cursor: OpaqueCursor | None, effects: Effects
        ) -> DiscoveryOutcome:
            del context, budget, cursor, effects
            raise exception

    assert (
        await ProviderRegistry((Broken(),)).discover("example_catalog", RecordingContext(recording_id="recording-01"), DiscoveryBudget(), Effects())
    ).status == status


@pytest.mark.asyncio
async def test_swallowed_malformed_effect_output_cannot_become_found() -> None:
    class InvalidEffects(Effects):
        async def read(self, source: SourceReference, budget: DiscoveryBudget) -> SourceRead:
            del source, budget
            return SourceRead.model_construct(status="found", code="invalid", scope="set-01", bytes_read=-1, retrieved_at=NOW)

    class Swallow(LocalProvider):
        async def load(self, candidate: ProviderCandidate, budget: LoadBudget, effects: Effects) -> LoadOutcome:
            with contextlib.suppress(ValidationError):
                await effects.read(candidate.source, budget)
            return LoadOutcome(status="found", code="swallowed", scope="set-01", snapshot=snapshot(identity=candidate.identity))

    source = local_context().sources[0]
    result = await ProviderRegistry((Swallow(),)).load(ProviderCandidate(identity=source.identity, source=source), LoadBudget(), InvalidEffects())
    assert result.status == "contract_error" and result.code == "invalid_read"
