"""Browser-origin refusal at real companion mutation boundaries, with valid reviewed pins."""

from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from phaze.models.discogs_link import DiscogsLink
from phaze.models.provider_source import ProviderRecordingCandidate, ProviderSelectionEvent, ProviderSourceObservation
from phaze.models.tracklist import Tracklist
from phaze.routers.request_guards import require_same_origin_form
from phaze.services.local_source_import import get_selected_recording_source
from phaze.services.selected_cue import build_selected_cue_artifact
from tests._queue_fakes import install_fake_queues
from tests.integration.test_local_source_import import decision
from tests.integration.test_selected_discogs_consumers import selected_inventory


HOSTILE = [
    {},
    {"Origin": "http://evil.invalid"},
    {"Origin": "null", "Sec-Fetch-Site": "same-origin"},
    {"Origin": "http://test", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://test", "Sec-Fetch-Site": "same-site"},
    {"Origin": "http://test:81"},
    {"Origin": "https://test"},
    {"Origin": "http://test.evil.invalid"},
    {"Origin": "http://user@test"},
    {"Origin": "http://test:"},
    {"Origin": "http://test?"},
    {"Referer": "http://evil.invalid/review", "HX-Request": "true"},
    {"Origin": "http://evil.invalid", "Forwarded": "host=evil.invalid;proto=http", "X-Forwarded-Host": "evil.invalid"},
]


@pytest.mark.parametrize("headers", HOSTILE)
async def test_hostile_valid_pins_have_no_mutation_or_enqueue(client, session, headers):
    media, companion, selected = await selected_inventory(session)
    link = DiscogsLink(source_observation_id=selected.observation_id, source_track_position=1, discogs_release_id="synthetic", confidence=90)
    session.add(link)
    await session.commit()
    artifact = await build_selected_cue_artifact(session, media.id)
    assert artifact is not None
    # The actual raw capture is retained as the parent of the immutable interpretation.
    observation = await session.get(ProviderSourceObservation, selected.observation_id)
    raw_id = observation.parent_id
    queue, router = install_fake_queues(client)

    async def counts():
        return tuple(
            [
                await session.scalar(select(func.count(model.id)))
                for model in (ProviderSourceObservation, ProviderRecordingCandidate, ProviderSelectionEvent)
            ]
        )

    before = await counts()
    pin = {"observation_id": str(selected.observation_id), "selected_token": str(selected.selection_token)}
    review = decision(media, companion, selected.observation_id, token=selected.selection_token, ordinal=1)
    commands = [
        (f"/recording-discogs/{media.id}/match", pin),
        (f"/recording-discogs/{media.id}/links/{link.id}", {**pin, "action": "accept"}),
        (f"/cue/files/{media.id}/generate", {"binding": artifact.binding.model_dump_json(), "audio_path": media.current_path}),
        (f"/files/{media.id}/companions/{companion.id}/refresh", {}),
        ("/api/local-sources/import", {"media_id": str(media.id), "raw_observation_id": str(raw_id)}),
        ("/api/local-sources/reimport", {"media_id": str(media.id), "raw_observation_id": str(raw_id)}),
        (
            "/api/local-sources/decision",
            {key: str(value) if value is not None else "" for key, value in review.model_dump().items()},
        ),
    ]
    for path, body in commands:
        response = await client.post(path, data=body, headers=headers)
        assert response.status_code == 403, (path, response.text)
    assert not queue.captured and not router.captures
    assert await counts() == before
    assert (await session.get(DiscogsLink, link.id, populate_existing=True)).status == "candidate"
    retained = await get_selected_recording_source(session, media.id)
    assert retained.observation_id == selected.observation_id and retained.selection_token == selected.selection_token
    # The same complete decision really is actionable when delivered by the owning origin.
    allowed = await client.post(commands[-1][0], data=commands[-1][1], headers={"Origin": "http://test"})
    assert allowed.status_code == 200 and allowed.json()["observation_id"] == str(selected.observation_id)
    assert (await counts())[-1] == before[-1] + 1


@pytest.mark.parametrize(
    "headers", [{"Origin": "https://PUBLIC.example:443"}, {"Referer": "https://public.example/review?tab=cue"}, {"Sec-Fetch-Site": "same-origin"}]
)
async def test_trusted_asgi_proxy_origin_and_native_htmx_compatibility(headers):
    app = FastAPI()

    @app.post("/mutate", dependencies=[Depends(require_same_origin_form)])
    async def mutate(request: Request):
        return {"value": (await request.form())["value"]}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://public.example") as http:
        for extra in ({}, {"HX-Request": "true"}):
            response = await http.post("/mutate", data={"value": "reviewed"}, headers={**headers, **extra})
            assert response.status_code == 200 and response.json() == {"value": "reviewed"}
        forged = await http.post(
            "/mutate",
            data={"value": "hostile"},
            headers={"Origin": "https://evil.invalid", "X-Forwarded-Host": "evil.invalid", "X-Forwarded-Proto": "https"},
        )
        assert forged.status_code == 403


@pytest.mark.parametrize(
    "headers",
    [
        [("origin", "http://test"), ("origin", "http://evil.invalid")],
        [("referer", "http://test/page"), ("referer", "http://evil.invalid/page")],
        [("sec-fetch-site", "same-origin"), ("sec-fetch-site", "cross-site")],
        [("origin", "http://test/extra")],
        [("origin", "http://test/#fragment")],
        [("origin", "http://test:65536")],
        [("origin", "http://[broken")],
        [("origin", "http://test\\evil")],
        [("origin", " http://test")],
        [("origin", "http://test\x7f")],
        [("origin", "http://" + "a" * 4097)],
        [("sec-fetch-site", "none")],
        [("sec-fetch-site", "unknown")],
    ],
)
async def test_malformed_or_ambiguous_headers_fail_closed(headers):
    app = FastAPI()

    @app.post("/mutate", dependencies=[Depends(require_same_origin_form)])
    async def mutate():
        pytest.fail("A refused origin must never reach the mutation")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.post("/mutate", data={"value": "hostile"}, headers=headers)).status_code == 403


async def test_only_trusted_asgi_proxy_handling_changes_effective_scheme():
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app = FastAPI()

    @app.post("/mutate", dependencies=[Depends(require_same_origin_form)])
    async def mutate():
        return {"accepted": True}

    proxied = ProxyHeadersMiddleware(app, trusted_hosts=["127.0.0.1"])
    headers = {"Origin": "https://public.example", "X-Forwarded-Proto": "https"}
    for address, expected in (("127.0.0.1", 200), ("198.51.100.1", 403)):
        async with AsyncClient(transport=ASGITransport(app=proxied, client=(address, 12345)), base_url="http://public.example") as http:
            assert (await http.post("/mutate", data={"value": "reviewed"}, headers=headers)).status_code == expected


async def test_explicit_json_api_retains_contract_without_simple_type_downgrade(client, session):
    media, _companion, selected = await selected_inventory(session)
    observation = await session.get(ProviderSourceObservation, selected.observation_id)
    body = {"media_id": str(media.id), "raw_observation_id": str(observation.parent_id)}
    response = await client.post("/api/local-sources/reimport", json=body)
    assert response.status_code == 200 and response.json()["raw_observation_id"] == str(observation.parent_id)
    hostile = await client.post("/api/local-sources/reimport", json=body, headers={"Origin": "http://evil.invalid"})
    assert hostile.status_code == 403
    for content_type in ("text/plain; application/json", "application/x-www-form-urlencoded; note=application/json"):
        assert (await client.post("/api/local-sources/reimport", content=b"{}", headers={"Content-Type": content_type})).status_code == 403


def test_operator_index_compiles_to_unchanged_partial_predicate():
    index = next(item for item in Tracklist.__table__.indexes if item.name == "ix_tracklists_external_id")
    sql = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert (
        sql
        == "CREATE UNIQUE INDEX ix_tracklists_external_id ON tracklists (external_id) WHERE propagated_from_set_key IS NULL AND provider_object_id IS NULL"
    )
