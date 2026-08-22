"""Controller-side tests for `pipeline_scans.trigger_scan`'s path + auth phases (split from
test_pipeline_scans.py, phaze-1i0h6.6).

POST /pipeline/scans's ordered validation layers (T-27-03): NFC-normalize + reject NUL/invalid
Unicode (phaze-jpji) + reject literal ``..`` traversal (WR-01) + canonicalize (phaze-0wme), THEN
look up the agent (reject missing/revoked) + enforce literal `scan_root` membership (WR-05) + D-06
prefix containment -- `_normalize_and_validate_scan_path` and `_authorize_scan_root` in the
production router. Also covers the 422-vs-200 envelope boundary (a genuinely unintelligible
envelope, e.g. a missing form field, stays FastAPI's own 422; every well-formed-but-rejected
input is a swappable 200 alert per response_shape rule 3, phaze-u1gf).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.shared.routers.pipeline_scans._shared import (
    RENDERABLE_ALERT_STATUS,
    Agent,
    ASGITransport,
    AsyncClient,
    _assert_swappable_alert,
    _count_batches,
    _make_smoke_app,
    pytest,
    unicodedata,
)


if TYPE_CHECKING:
    from unittest.mock import AsyncMock

    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_post_scans_subpath_rejects_dotdot(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """T-27-03: subpath containing ``..`` rejects with a swappable error card; NO batch created.

    Status is 200 per response_shape rule 3 (phaze-u1gf) -- see
    ``test_post_scans_failure_branches_are_swappable_alerts`` for the contract rationale.
    """
    ac, mock_router = smoke
    pre_count = await _count_batches(session)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "../../etc"},
    )
    assert response.status_code == RENDERABLE_ALERT_STATUS
    assert 'role="alert"' in response.text
    # Jinja autoescapes `'` to `&#39;`, so check on a substring that survives escaping.
    assert "Subpath must not contain" in response.text
    assert "path traversal" in response.text

    # Atomicity: NO ScanBatch row created on rejection.
    post_count = await _count_batches(session)
    assert post_count == pre_count
    # And NO enqueue.
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_subpath_rejects_nul_byte(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """phaze-jpji: a NUL byte in subpath rejects with a swappable error card; NO 500, NO batch.

    A NUL survives NFC normalization, is not a ``..`` component, and does not break the
    scan_root membership/prefix checks -- so before the fix it sailed through every other
    validation layer and reached ``session.commit()``, where asyncpg raises
    ``CharacterNotInRepertoireError`` (PostgreSQL cannot store NUL in a UTF8 text column). That
    exception escaped the handler as a raw 500, violating the documented contract that EVERY
    failure branch renders ``scan_submit_error.html`` with ``RENDERABLE_ALERT_STATUS``.
    """
    ac, mock_router = smoke
    pre_count = await _count_batches(session)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "music\x00evil"},
    )
    assert response.status_code == RENDERABLE_ALERT_STATUS
    assert 'role="alert"' in response.text
    assert "Subpath must not contain" in response.text

    # Atomicity: NO ScanBatch row created on rejection (and no orphan row from an aborted txn).
    post_count = await _count_batches(session)
    assert post_count == pre_count
    # And NO enqueue.
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_subpath_rejects_nul_via_direct_handler_invocation(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phaze-jpji: the reject-branch fires even when the NUL only appears AFTER NFC normalization.

    httpx form-encodes ``data=`` through :func:`urllib.parse.urlencode`, which round-trips a raw
    NUL just fine (unlike a lone surrogate, which cannot be UTF-8 encoded at all -- an invalid
    surrogate could only reach the handler via a raw malformed byte stream, not a well-formed HTTP
    request, so it is covered at the unit level in ``tests/shared/test_pg_text.py`` instead).
    This test additionally proves the guard checks the POST-normalization ``joined`` path (not
    just the raw subpath), by monkeypatching ``unicodedata.normalize`` to inject a NUL that was
    not present in the original request -- mirroring the existing prefix-mismatch test's technique
    for exercising a normalization edge case.
    """
    ac, mock_router = smoke
    pre_count = await _count_batches(session)

    from phaze.routers import scan as ps_mod

    original_normalize = ps_mod.unicodedata.normalize

    def _normalize_injecting_nul(form: str, text: str) -> str:
        if text.startswith("/data/music/"):
            return text.replace("2026", "2026\x00")
        return original_normalize(form, text)

    monkeypatch.setattr(ps_mod.unicodedata, "normalize", _normalize_injecting_nul)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    assert response.status_code == RENDERABLE_ALERT_STATUS, response.text
    assert 'role="alert"' in response.text
    assert "Subpath must not contain" in response.text

    post_count = await _count_batches(session)
    assert post_count == pre_count
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_agent_id_rejects_nul_byte_as_422(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """phaze-oldp: a NUL byte in ``agent_id`` used to bypass the ``joined``-only NUL guard
    entirely and reach ``session.get(Agent, form.agent_id)`` unfiltered, where PostgreSQL cannot
    bind a NUL in a UTF8 text comparison at all -- asyncpg raised CharacterNotInRepertoireError
    and the request escaped as a raw 500, violating the handler's documented contract that EVERY
    failure branch renders ``scan_submit_error.html``.

    The fix mirrors the sibling ``agent_roots_swap`` endpoint's ``pattern=`` bound (itself the
    wire mirror of ``Agent.id``'s DB ``CheckConstraint``): an id that can never denote a real
    agent is a genuinely unintelligible envelope per this handler's own carve-out, so the correct
    outcome is FastAPI's own 422 at the boundary -- never a DB round trip at all.
    """
    ac, mock_router = smoke
    pre_count = await _count_batches(session)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "\x00abc", "scan_root": "/data/music", "subpath": ""},
    )
    assert response.status_code == 422

    post_count = await _count_batches(session)
    assert post_count == pre_count
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_subpath_allows_triple_dot_filename(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """WR-01 regression: subpath containing literal ``..`` as a non-component substring is allowed.

    The traversal guard rejects ``..`` *path components* only; legitimate
    filenames/directories containing the substring ``..`` (e.g.,
    ``...thinking.mp3`` for triple-dot, ``Album...Live`` for torrent-archive
    naming) must NOT 400. Previously the simple ``".." in joined`` substring
    check rejected these false-positives.
    """
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "...thinking.mp3"},
    )
    # Should succeed (200 RUNNING) -- the triple-dot filename is a legitimate
    # path component and must not trip the traversal guard.
    assert response.status_code == 200, response.text
    assert "Scan in progress" in response.text
    mock_router.enqueue_for_agent.assert_awaited_once()
    call = mock_router.enqueue_for_agent.await_args
    assert call.kwargs["payload"].scan_path == "/data/music/...thinking.mp3"


@pytest.mark.asyncio
async def test_post_scans_nfd_scan_root_is_scannable(session: AsyncSession) -> None:
    """phaze-g0if regression: an agent's non-NFC (NFD) ``scan_roots`` entry must be scannable.

    Before the fix, ``joined`` was NFC-normalized but ``agent.scan_roots`` was compared raw: the
    WR-05 membership check (``form.scan_root not in agent.scan_roots``) is raw-vs-raw and passes,
    but the D-06 prefix check compared the NFC ``joined`` against the un-normalized root and failed
    -- even for the bare root with NO subpath (``joined == NFC(scan_root) != scan_root`` when the
    root itself is not already NFC). NFD is the norm for paths sourced from an HFS+/macOS agent.
    """
    nfd_root = unicodedata.normalize("NFD", "/data/Café Del Mar")
    assert nfd_root != unicodedata.normalize("NFC", nfd_root), "fixture root must be genuinely non-NFC"

    agent = Agent(id="test-agent-nfd", name="Test Agent NFD", token_hash=None, scan_roots=[nfd_root])
    session.add(agent)
    await session.commit()

    app, mock_router = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(
            "/pipeline/scans",
            data={"agent_id": "test-agent-nfd", "scan_root": nfd_root, "subpath": ""},
        )

    assert response.status_code == 200, response.text
    assert "Scan in progress" in response.text
    mock_router.enqueue_for_agent.assert_awaited_once()
    payload = mock_router.enqueue_for_agent.await_args.kwargs["payload"]
    assert payload.scan_path == unicodedata.normalize("NFC", nfd_root)


@pytest.mark.asyncio
async def test_post_scans_nfd_scan_root_with_subpath_is_scannable(session: AsyncSession) -> None:
    """phaze-g0if regression: the prefix gate must also pass for a non-NFC root WITH a subpath."""
    nfd_root = unicodedata.normalize("NFD", "/data/Café Del Mar")

    agent = Agent(id="test-agent-nfd-sub", name="Test Agent NFD Sub", token_hash=None, scan_roots=[nfd_root])
    session.add(agent)
    await session.commit()

    app, mock_router = _make_smoke_app(session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(
            "/pipeline/scans",
            data={"agent_id": "test-agent-nfd-sub", "scan_root": nfd_root, "subpath": "2026/set1.flac"},
        )

    assert response.status_code == 200, response.text
    mock_router.enqueue_for_agent.assert_awaited_once()
    payload = mock_router.enqueue_for_agent.await_args.kwargs["payload"]
    assert payload.scan_path == unicodedata.normalize("NFC", f"{nfd_root}/2026/set1.flac")


@pytest.mark.asyncio
async def test_post_scans_path_outside_scan_root(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """T-27-03: scan_root not in agent.scan_roots rejects with a swappable error card (200)."""
    ac, mock_router = smoke

    # /data/photos is NOT in the seeded agent's scan_roots (which are
    # /data/music + /data/videos). The literal-membership check fails.
    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/photos", "subpath": "vacation/"},
    )
    assert response.status_code == RENDERABLE_ALERT_STATUS
    # WR-05: scan_root membership check fires before the prefix check.
    assert "Selected scan root is not configured for this agent." in response.text
    assert await _count_batches(session) == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_unknown_agent_renders_alert(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """Unknown agent_id rejects with a swappable (200) 'Unknown or revoked agent.' card."""
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "nonexistent-agent", "scan_root": "/data/music", "subpath": ""},
    )
    assert response.status_code == RENDERABLE_ALERT_STATUS
    assert "Unknown or revoked agent." in response.text
    assert await _count_batches(session) == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_scan_root_not_in_agent_roots(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """WR-05: scan_root NOT literally in agent.scan_roots rejects with a swappable card (200)."""
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        # /etc is not in seeded agent's scan_roots.
        data={"agent_id": "test-agent", "scan_root": "/etc", "subpath": ""},
    )
    assert response.status_code == RENDERABLE_ALERT_STATUS
    # WR-05: literal-membership check fires before the prefix check.
    assert "Selected scan root is not configured for this agent." in response.text
    mock_router.enqueue_for_agent.assert_not_awaited()


async def test_post_scans_prefix_mismatch_via_direct_handler_invocation(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive prefix check (pipeline_scans.py:207-212).

    The prefix-mismatch branch is structurally defensive: the literal-membership
    check on line 195 dominates the normal failure mode, and well-formed
    subpaths always join to a path that starts with the scan_root. To reach
    the prefix-fail branch we monkeypatch ``unicodedata.normalize`` so the
    NFC pass rewrites the joined path out from under the prefix predicate
    (simulating a hypothetical normalization edge case that today's inputs
    cannot produce). Pins the swappable-alert envelope (200 + alert body, per
    response_shape rule 3) so a real future normalization quirk surfaces as a
    card the operator can actually see, not a 500 or a silent enqueue.
    """
    ac, mock_router = smoke

    from phaze.routers import scan as ps_mod

    original_normalize = ps_mod.unicodedata.normalize

    def _normalize_rewriting_joined(form: str, text: str) -> str:
        # Only rewrite the joined path; leave the agent-side normalize
        # passes alone so the literal-membership check still passes.
        if text.startswith("/data/music/"):
            return "/elsewhere/x"  # force prefix mismatch on the joined path
        return original_normalize(form, text)

    monkeypatch.setattr(ps_mod.unicodedata, "normalize", _normalize_rewriting_joined)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    assert response.status_code == RENDERABLE_ALERT_STATUS, response.text
    assert "Resolved path is outside the selected scan root." in response.text
    assert await _count_batches(session) == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_rejects_partial_scan_root_prefix(
    smoke: tuple[AsyncClient, AsyncMock],
    session: AsyncSession,
) -> None:
    """WR-05 regression: scan_root="/data" + subpath="music/foo" must reject.

    The agent's scan_roots are ["/data/music", "/data/videos"]; "/data" alone
    is a parent path that was never authorized. Previously the joined-path
    prefix check passed because ``"/data/music/foo".startswith("/data/music/")``
    is True, so the audit log would have recorded ``scan_root="/data"`` for a
    scan against ``/data/music/foo`` -- a surprising mode where unconfigured
    scan_roots can authorize sub-trees that happen to fall inside a configured
    one. Tighten the validator to require literal membership.
    """
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        # /data is the *parent* of a real scan_root but is not configured itself.
        data={"agent_id": "test-agent", "scan_root": "/data", "subpath": "music/foo"},
    )
    assert response.status_code == RENDERABLE_ALERT_STATUS
    assert "Selected scan root is not configured for this agent." in response.text
    assert await _count_batches(session) == 0
    mock_router.enqueue_for_agent.assert_not_awaited()


# ---------------------------------------------------------------------------
# phaze-u1gf: EVERY trigger_scan failure branch must be a SWAPPABLE alert.
#
# The Trigger Scan form (trigger_scan_card.html) posts with
# ``hx-target="#scan-submit-result" hx-swap="innerHTML"``. htmx 2.x's default
# ``responseHandling`` maps ``[45]..`` to ``{swap: false, error: true}``, and the only
# global non-2xx opt-in in this repo (shell.html's ``htmx:beforeSwap``) fires solely for
# ``status === 404 && target.id === 'record-body'``. So while these branches returned
# 400/503, ``scan_submit_error.html`` -- ``role="alert"`` and all -- was fetched and then
# DISCARDED: spinner flash, empty ``#scan-submit-result``, operator none the wiser.
#
# response_shape.py rule 3 owns this defect class: a renderable error is a 200 whose body
# carries the error. Rule 4's boundary test -- "is there a swap target waiting to display
# this answer?" -- is YES for all six branches, so none of them is a
# request_guards rule 1 (422) malformed envelope. A genuinely unintelligible envelope
# (missing form field) remains FastAPI's own 422; see
# ``test_post_scans_missing_form_field_is_still_a_422_envelope_rejection``.
#
# Asserting the status alone would not have caught the bug's real cost, so each case below
# also asserts the ALERT MARKUP the operator would actually see.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_scans_dotdot_traversal_is_a_swappable_alert(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """Branch 1/6 -- ``..`` traversal.

    Argued explicitly because it is the branch most tempting to call a protocol-level
    rejection: it is a *security* refusal, and security refusals feel like they want a 4xx.
    They do not, here. phaze UNDERSTOOD this request perfectly -- ``subpath`` is a
    well-formed string, it parsed, it joined, it NFC-normalized, and only then did a
    *policy* check refuse it. Rule 4's test is about whether a swap target is waiting, not
    about how severe the failure is, and ``#scan-submit-result`` is waiting. Answering 422
    would hide the refusal from the very operator who must correct it -- strictly worse
    security UX, since a silently-dropped rejection is indistinguishable from a started
    scan. The refusal itself is unchanged and still server-authoritative: no batch, no
    enqueue.
    """
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "../../etc"},
    )
    _assert_swappable_alert(response, "path traversal")
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_unknown_agent_is_a_swappable_alert(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """Branch 2/6 -- unknown/revoked agent. Well-formed id, no such row: bad news, not gibberish."""
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "nonexistent-agent", "scan_root": "/data/music", "subpath": ""},
    )
    _assert_swappable_alert(response, "Unknown or revoked agent.")
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_unconfigured_scan_root_is_a_swappable_alert(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """Branch 3/6 -- scan_root not in agent.scan_roots. A directly actionable operator mistake."""
    ac, mock_router = smoke

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/photos", "subpath": "vacation/"},
    )
    _assert_swappable_alert(response, "Selected scan root is not configured for this agent.")
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_path_outside_root_is_a_swappable_alert(
    smoke: tuple[AsyncClient, AsyncMock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch 4/6 -- resolved path outside the scan root (the defensive prefix check).

    Reached the same way the existing coverage-gap test reaches it: monkeypatch
    ``unicodedata.normalize`` so the NFC pass rewrites the joined path out from under the
    prefix predicate, since the literal-membership check dominates every input today.
    """
    ac, mock_router = smoke

    from phaze.routers import scan as ps_mod

    original_normalize = ps_mod.unicodedata.normalize

    def _normalize_rewriting_joined(form: str, text: str) -> str:
        if text.startswith("/data/music/"):
            return "/elsewhere/x"
        return original_normalize(form, text)

    monkeypatch.setattr(ps_mod.unicodedata, "normalize", _normalize_rewriting_joined)

    response = await ac.post(
        "/pipeline/scans",
        data={"agent_id": "test-agent", "scan_root": "/data/music", "subpath": "2026/"},
    )
    _assert_swappable_alert(response, "Resolved path is outside the selected scan root.")
    mock_router.enqueue_for_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_scans_missing_form_field_is_still_a_422_envelope_rejection(
    smoke: tuple[AsyncClient, AsyncMock],
) -> None:
    """The rule-4 boundary, pinned: 200-with-alert did NOT become "every error is 200".

    A POST missing the required ``scan_root`` field is an envelope phaze cannot understand.
    There is no meaningful answer to render into ``#scan-submit-result``, so FastAPI's own
    422 (``request_guards`` rule 1) is correct and must stay. This test fails loudly if a
    future change mechanically converts the whole handler to 200.
    """
    ac, mock_router = smoke

    response = await ac.post("/pipeline/scans", data={"agent_id": "test-agent"})
    assert response.status_code == 422, response.text
    assert 'role="alert"' not in response.text
    mock_router.enqueue_for_agent.assert_not_awaited()
