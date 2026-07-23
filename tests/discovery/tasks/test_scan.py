"""Tests for the HTTP-rewritten scan_live_set SAQ task (Phase 26 Plan 11)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

from pydantic import ValidationError
import pytest

from phaze.services.fingerprint import CombinedMatch


def _make_ctx(api_client: AsyncMock | None = None, orchestrator: AsyncMock | None = None) -> dict[str, Any]:
    """Create a minimal SAQ context dict with api_client + orchestrator mocks."""
    if api_client is None:
        api_client = AsyncMock()
        api_client.create_tracklist = AsyncMock(return_value=MagicMock(tracklist_id=uuid.uuid4(), version=1, track_count=1))
    if orchestrator is None:
        orchestrator = AsyncMock()
    return {"api_client": api_client, "fingerprint_orchestrator": orchestrator}


def _make_payload_kwargs(file_id: uuid.UUID | None = None, scan_run_id: uuid.UUID | None = None) -> dict[str, Any]:
    kwargs = {
        "file_id": str(file_id or uuid.uuid4()),
        "original_path": "/music/liveset.mp3",
        "agent_id": "test-agent",
    }
    # phaze-y07u: omitted (not None-valued) when absent, mirroring a pre-upgrade in-flight job's
    # serialized kwargs -- the payload's default supplies None.
    if scan_run_id is not None:
        kwargs["scan_run_id"] = str(scan_run_id)
    return kwargs


async def test_scan_no_matches_returns_no_matches() -> None:
    """When combined_query returns empty list, scan_live_set short-circuits."""
    from phaze.tasks.scan import scan_live_set

    api = AsyncMock()
    api.create_tracklist = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=[])
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    file_id = uuid.uuid4()

    result = await scan_live_set(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "no_matches"
    assert result["file_id"] == str(file_id)
    api.create_tracklist.assert_not_awaited()
    # Phase 45 (L-02): a no-match COMPLETE must ack exactly once so the ledger row clears
    # (otherwise a legitimate no-match scan re-enqueues on every recovery -- T-45-16).
    api.report_scan_terminal.assert_awaited_once_with(file_id)


async def test_scan_with_matches_posts_tracklist() -> None:
    """When matches exist, scan_live_set POSTs tracklist with stable uuid5 request_id."""
    from phaze.tasks.scan import scan_live_set

    matches = [
        CombinedMatch(track_id="track-1", confidence=85.0, timestamp="00:01:23"),
        CombinedMatch(track_id="track-2", confidence=70.0, timestamp="00:04:56"),
    ]
    api = AsyncMock()
    tracklist_id = uuid.uuid4()
    api.create_tracklist = AsyncMock(return_value=MagicMock(tracklist_id=tracklist_id, version=1, track_count=2))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=matches)
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    file_id = uuid.uuid4()

    result = await scan_live_set(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "scanned"
    assert result["tracklist_id"] == str(tracklist_id)
    assert result["version"] == 1

    api.create_tracklist.assert_awaited_once()
    body = api.create_tracklist.await_args.args[0]
    assert body.file_id == file_id
    assert body.source == "fingerprint"
    assert body.external_id == f"fp-{file_id.hex[:12]}"
    # request_id is a stable uuid5 of (NAMESPACE_URL, "phaze-scan-{file_id}")
    expected_request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"phaze-scan-{file_id}")
    assert body.request_id == expected_request_id
    assert len(body.tracks) == 2
    assert body.tracks[0].position == 1
    assert body.tracks[1].position == 2
    # Artist/title intentionally None per W5 Option (b) -- controller-side enrichment
    assert body.tracks[0].artist is None
    assert body.tracks[0].title is None
    # phaze-nldg: timestamp=match.timestamp is a straight pass-through -- lock it against
    # regressing back to always-None now that combined_query actually populates it.
    assert body.tracks[0].timestamp == "00:01:23"
    assert body.tracks[1].timestamp == "00:04:56"
    # Phase 45 (L-02): the MATCH path clears via create_tracklist -- it must NOT also ack
    # (no double-clear of scan_live_set:<file_id>).
    api.report_scan_terminal.assert_not_awaited()


async def test_scan_match_timestamp_at_varchar20_cap_is_accepted() -> None:
    """phaze-nldg / phaze-btlu: a 20-char timestamp (the column's exact cap) must pass through.

    combined_query now actually emits engine-sourced timestamps (previously always None), so
    the tracklist_tracks.timestamp varchar(20) cap becomes load-bearing on this path for the
    first time. This locks that the wire schema's max_length=20 accommodates the longest value
    this path can realistically produce (e.g. "HH:MM:SS.f" scraper-style strings are far
    shorter; the fingerprint engines themselves emit short plain-seconds strings like "12.3").
    """
    from phaze.tasks.scan import scan_live_set

    twenty_char_timestamp = "1" * 20
    assert len(twenty_char_timestamp) == 20
    matches = [CombinedMatch(track_id="track-1", confidence=85.0, timestamp=twenty_char_timestamp)]
    api = AsyncMock()
    api.create_tracklist = AsyncMock(return_value=MagicMock(tracklist_id=uuid.uuid4(), version=1, track_count=1))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=matches)
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    result = await scan_live_set(ctx, **_make_payload_kwargs())

    assert result["status"] == "scanned"
    body = api.create_tracklist.await_args.args[0]
    assert body.tracks[0].timestamp == twenty_char_timestamp


async def test_scan_match_timestamp_over_varchar20_cap_is_rejected() -> None:
    """A 21-char timestamp is machine-rejected by the wire schema before it ever reaches SQL.

    Guards the ceiling side of the phaze-btlu cap: this path must fail loudly (a validation
    error visible to the caller/operator), not silently truncate or 500 deep in Postgres.
    """
    from phaze.schemas.agent_tracklists import TracklistTrackPayload

    twenty_one_char_timestamp = "1" * 21
    with pytest.raises(ValidationError):
        TracklistTrackPayload(position=1, timestamp=twenty_one_char_timestamp, confidence=85.0)


async def test_scan_request_id_is_stable_across_calls() -> None:
    """Replaying scan_live_set with IDENTICAL kwargs produces the same request_id.

    This is the SAQ-retry shape (a retry reruns the job with the same serialized kwargs --
    phaze-y07u: including the same ``scan_run_id`` when present; here both runs carry none,
    the pre-upgrade legacy shape), so the controller's idempotency cache collapses the replay.
    """
    from phaze.tasks.scan import scan_live_set

    matches = [CombinedMatch(track_id="t1", confidence=80.0)]
    api1 = AsyncMock()
    api1.create_tracklist = AsyncMock(return_value=MagicMock(tracklist_id=uuid.uuid4(), version=1, track_count=1))
    orchestrator1 = AsyncMock()
    orchestrator1.combined_query = AsyncMock(return_value=matches)
    ctx1 = _make_ctx(api_client=api1, orchestrator=orchestrator1)

    api2 = AsyncMock()
    api2.create_tracklist = AsyncMock(return_value=MagicMock(tracklist_id=uuid.uuid4(), version=1, track_count=1))
    orchestrator2 = AsyncMock()
    orchestrator2.combined_query = AsyncMock(return_value=matches)
    ctx2 = _make_ctx(api_client=api2, orchestrator=orchestrator2)

    file_id = uuid.uuid4()
    await scan_live_set(ctx1, **_make_payload_kwargs(file_id=file_id))
    await scan_live_set(ctx2, **_make_payload_kwargs(file_id=file_id))

    req_id_1 = api1.create_tracklist.await_args.args[0].request_id
    req_id_2 = api2.create_tracklist.await_args.args[0].request_id
    assert req_id_1 == req_id_2  # stable -- SAQ retries will hit cached response server-side


def _scan_ctx_with_matches() -> dict[str, Any]:
    """A ctx whose orchestrator returns one match and whose api records create_tracklist calls."""
    api = AsyncMock()
    api.create_tracklist = AsyncMock(return_value=MagicMock(tracklist_id=uuid.uuid4(), version=1, track_count=1))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=[CombinedMatch(track_id="t1", confidence=80.0)])
    return _make_ctx(api_client=api, orchestrator=orchestrator)


async def test_scan_request_id_differs_across_distinct_runs() -> None:
    """THE phaze-y07u regression: two DISTINCT scan runs of the same file get DIFFERENT request_ids.

    Pre-fix the request_id was uuid5 over the file_id alone -- deterministic per FILE forever --
    so a deliberate re-scan within the controller's 1h idempotency window (e.g. after ingesting
    more reference tracks) replayed the CACHED create-tracklist response and the fresh match set
    was silently discarded. Each enqueue now stamps a fresh ``scan_run_id`` nonce, so distinct
    runs must produce distinct request_ids.
    """
    from phaze.tasks.scan import scan_live_set

    file_id = uuid.uuid4()
    ctx1, ctx2 = _scan_ctx_with_matches(), _scan_ctx_with_matches()
    await scan_live_set(ctx1, **_make_payload_kwargs(file_id=file_id, scan_run_id=uuid.uuid4()))
    await scan_live_set(ctx2, **_make_payload_kwargs(file_id=file_id, scan_run_id=uuid.uuid4()))

    req_id_1 = ctx1["api_client"].create_tracklist.await_args.args[0].request_id
    req_id_2 = ctx2["api_client"].create_tracklist.await_args.args[0].request_id
    assert req_id_1 != req_id_2  # a re-scan is a NEW operation -- never the cached response


async def test_scan_request_id_stable_across_retries_of_one_run() -> None:
    """phaze-y07u: retries of ONE run (same serialized kwargs, same scan_run_id) still collapse."""
    from phaze.tasks.scan import scan_live_set

    file_id = uuid.uuid4()
    run_id = uuid.uuid4()
    ctx1, ctx2 = _scan_ctx_with_matches(), _scan_ctx_with_matches()
    await scan_live_set(ctx1, **_make_payload_kwargs(file_id=file_id, scan_run_id=run_id))
    await scan_live_set(ctx2, **_make_payload_kwargs(file_id=file_id, scan_run_id=run_id))

    req_id_1 = ctx1["api_client"].create_tracklist.await_args.args[0].request_id
    req_id_2 = ctx2["api_client"].create_tracklist.await_args.args[0].request_id
    assert req_id_1 == req_id_2  # retry replay -> controller idempotency cache catches it


async def test_scan_request_id_without_run_id_keeps_legacy_key() -> None:
    """phaze-y07u: a pre-upgrade job (no scan_run_id) keeps the legacy per-file request_id.

    An in-flight job enqueued before the upgrade retries with its original kwargs; its retries
    must still dedupe against the POST the original attempt already made under the legacy key.
    """
    from phaze.tasks.scan import scan_live_set

    file_id = uuid.uuid4()
    ctx = _scan_ctx_with_matches()
    await scan_live_set(ctx, **_make_payload_kwargs(file_id=file_id))

    body = ctx["api_client"].create_tracklist.await_args.args[0]
    assert body.request_id == uuid.uuid5(uuid.NAMESPACE_URL, f"phaze-scan-{file_id}")


async def test_orchestrator_error_propagates() -> None:
    """Orchestrator failures propagate (SAQ retries)."""
    from phaze.tasks.scan import scan_live_set

    api = AsyncMock()
    api.create_tracklist = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(side_effect=RuntimeError("audfprint down"))
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    with pytest.raises(RuntimeError, match="audfprint down"):
        await scan_live_set(ctx, **_make_payload_kwargs())
    api.create_tracklist.assert_not_awaited()


async def test_total_engine_outage_is_not_a_terminal_no_match() -> None:
    """phaze-z7yw: a total fingerprint outage must NOT ack report_scan_terminal.

    combined_query raising FingerprintQueryUnavailableError means every engine failed at
    the ENGINE level. scan_live_set must re-raise (SAQ retry/backoff) WITHOUT writing the
    terminal no-match ack, so the scan_live_set:<file_id> ledger row survives and recovery
    re-enqueues the file after the outage -- instead of converting the outage into a
    permanent, success-looking 'no_matches' verdict.
    """
    from phaze.services.fingerprint import FingerprintQueryUnavailableError
    from phaze.tasks.scan import scan_live_set

    api = AsyncMock()
    api.create_tracklist = AsyncMock()
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(side_effect=FingerprintQueryUnavailableError("all fingerprint engines failed"))
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    with pytest.raises(FingerprintQueryUnavailableError):
        await scan_live_set(ctx, **_make_payload_kwargs())

    api.report_scan_terminal.assert_not_awaited()
    api.create_tracklist.assert_not_awaited()


# ---------------------------------------------------------------------------
# Phase 45 (L-02): scan terminal-ack on the create_tracklist failure path
# ---------------------------------------------------------------------------


async def test_scan_match_terminal_failure_acks_then_raises() -> None:
    """A retries-EXHAUSTED create_tracklist failure acks once, then re-raises (T-45-06)."""
    from phaze.tasks.scan import scan_live_set

    matches = [CombinedMatch(track_id="t1", confidence=80.0)]
    api = AsyncMock()
    api.create_tracklist = AsyncMock(side_effect=RuntimeError("controller 5xx after retries"))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=matches)
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    # SAQ job on its terminal (non-retryable) attempt.
    ctx["job"] = MagicMock(retryable=False)
    file_id = uuid.uuid4()

    with pytest.raises(RuntimeError, match="controller 5xx"):
        await scan_live_set(ctx, **_make_payload_kwargs(file_id=file_id))

    api.report_scan_terminal.assert_awaited_once_with(file_id)


async def test_scan_match_retryable_failure_does_not_ack() -> None:
    """A RETRYABLE create_tracklist failure re-raises WITHOUT acking -- the row survives for the retry."""
    from phaze.tasks.scan import scan_live_set

    matches = [CombinedMatch(track_id="t1", confidence=80.0)]
    api = AsyncMock()
    api.create_tracklist = AsyncMock(side_effect=RuntimeError("transient"))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=matches)
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    # SAQ job that still has a retry left.
    ctx["job"] = MagicMock(retryable=True)

    with pytest.raises(RuntimeError, match="transient"):
        await scan_live_set(ctx, **_make_payload_kwargs())

    api.report_scan_terminal.assert_not_awaited()


async def test_scan_match_failure_without_job_in_ctx_does_not_ack() -> None:
    """No job in ctx (pure unit context) -> the terminal guard is skipped; just re-raise."""
    from phaze.tasks.scan import scan_live_set

    matches = [CombinedMatch(track_id="t1", confidence=80.0)]
    api = AsyncMock()
    api.create_tracklist = AsyncMock(side_effect=RuntimeError("boom"))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=matches)
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)  # no "job" key

    with pytest.raises(RuntimeError, match="boom"):
        await scan_live_set(ctx, **_make_payload_kwargs())

    api.report_scan_terminal.assert_not_awaited()


async def test_scan_match_terminal_ack_failure_reraises_original_error() -> None:
    """WR-01: on the TERMINAL attempt, if the ack ALSO raises (E2), the ORIGINAL task error (E1)
    must propagate -- not the ack error. The ack is awaited once and its failure is swallowed."""
    from phaze.services.agent_client import AgentApiServerError
    from phaze.tasks.scan import scan_live_set

    matches = [CombinedMatch(track_id="t1", confidence=80.0)]
    api = AsyncMock()
    api.create_tracklist = AsyncMock(side_effect=RuntimeError("controller 5xx"))
    api.report_scan_terminal = AsyncMock(side_effect=AgentApiServerError("ack boom"))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=matches)
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    ctx["job"] = MagicMock(retryable=False)
    file_id = uuid.uuid4()

    # E1 (the create_tracklist RuntimeError) propagates -- NOT E2 (the AgentApiServerError ack).
    with pytest.raises(RuntimeError, match="controller 5xx"):
        await scan_live_set(ctx, **_make_payload_kwargs(file_id=file_id))

    api.report_scan_terminal.assert_awaited_once_with(file_id)


# ---------------------------------------------------------------------------
# Phase 45 (CR-01 / T-45-16): scan terminal-ack on the NO-MATCH path
# ---------------------------------------------------------------------------


async def test_scan_no_match_terminal_ack_raise_on_terminal_attempt_swallows_and_returns() -> None:
    """No-match + ack raises on the TERMINAL attempt -> swallow + log, still return no_matches.

    The terminal-ack is best-effort on the no-match branch: a clean COMPLETE found no
    tracklist, so a controller hiccup on the retries-exhausted attempt must NOT block the
    no_matches return -- the alternative leaks scan_live_set:<file_id> forever (T-45-16).
    """
    from phaze.services.agent_client import AgentApiServerError
    from phaze.tasks.scan import scan_live_set

    api = AsyncMock()
    api.create_tracklist = AsyncMock()
    api.report_scan_terminal = AsyncMock(side_effect=AgentApiServerError("controller 5xx after retries"))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=[])
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    # SAQ job on its terminal (non-retryable) attempt.
    ctx["job"] = MagicMock(retryable=False)
    file_id = uuid.uuid4()

    result = await scan_live_set(ctx, **_make_payload_kwargs(file_id=file_id))

    assert result["status"] == "no_matches"
    assert result["file_id"] == str(file_id)
    api.report_scan_terminal.assert_awaited_once_with(file_id)
    api.create_tracklist.assert_not_awaited()


async def test_scan_no_match_terminal_ack_raise_on_retryable_attempt_reraises() -> None:
    """No-match + ack raises on a RETRYABLE attempt -> re-raise so SAQ retries (row survives)."""
    from phaze.services.agent_client import AgentApiServerError
    from phaze.tasks.scan import scan_live_set

    api = AsyncMock()
    api.create_tracklist = AsyncMock()
    api.report_scan_terminal = AsyncMock(side_effect=AgentApiServerError("transient"))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=[])
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)
    # SAQ job that still has a retry left.
    ctx["job"] = MagicMock(retryable=True)
    file_id = uuid.uuid4()

    with pytest.raises(AgentApiServerError, match="transient"):
        await scan_live_set(ctx, **_make_payload_kwargs(file_id=file_id))

    api.report_scan_terminal.assert_awaited_once_with(file_id)


async def test_scan_no_match_terminal_ack_raise_without_job_reraises() -> None:
    """No-match + ack raises with job absent from ctx -> treated as NON-terminal -> re-raise.

    Mirrors the match-path guard exactly: a None job is not "terminal", so the conservative
    behavior is to re-raise (let SAQ retry; the row survives for the real retry).
    """
    from phaze.services.agent_client import AgentApiServerError
    from phaze.tasks.scan import scan_live_set

    api = AsyncMock()
    api.create_tracklist = AsyncMock()
    api.report_scan_terminal = AsyncMock(side_effect=AgentApiServerError("boom"))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=[])
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)  # no "job" key
    file_id = uuid.uuid4()

    with pytest.raises(AgentApiServerError, match="boom"):
        await scan_live_set(ctx, **_make_payload_kwargs(file_id=file_id))

    api.report_scan_terminal.assert_awaited_once_with(file_id)


async def test_http_error_propagates() -> None:
    """create_tracklist failures propagate (SAQ retries)."""
    from phaze.tasks.scan import scan_live_set

    matches = [CombinedMatch(track_id="t1", confidence=80.0)]
    api = AsyncMock()
    api.create_tracklist = AsyncMock(side_effect=RuntimeError("server is down"))
    orchestrator = AsyncMock()
    orchestrator.combined_query = AsyncMock(return_value=matches)
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    with pytest.raises(RuntimeError, match="server is down"):
        await scan_live_set(ctx, **_make_payload_kwargs())


async def test_rejects_extra_kwargs() -> None:
    """ScanLiveSetPayload.extra='forbid' rejects unknown fields."""
    from phaze.tasks.scan import scan_live_set

    api = AsyncMock()
    api.create_tracklist = AsyncMock()
    orchestrator = AsyncMock()
    ctx = _make_ctx(api_client=api, orchestrator=orchestrator)

    bad_kwargs = _make_payload_kwargs()
    bad_kwargs["bogus_field"] = "x"
    with pytest.raises(ValidationError):
        await scan_live_set(ctx, **bad_kwargs)
    orchestrator.combined_query.assert_not_awaited()
    api.create_tracklist.assert_not_awaited()
