"""Characterization pin for scan-path AUTHORISATION -- written BEFORE the phaze-bk9el.17 split.

WHAT THIS IS. ``_normalize_and_validate_scan_path`` and ``_authorize_scan_root`` are the two
halves of a security boundary: together they decide which directory of the operator's archive a
scan is allowed to walk. Everything else ``pipeline_scans.py`` does is a feature; these two are
a gate. The bead's criterion is that authorisation is "provably bit-for-bit unchanged across the
split", and this file is that proof.

WHY A UNIT-LEVEL CORPUS AND NOT MORE ENDPOINT TESTS. ``test_path_and_auth.py`` already exercises
these through ``POST /pipeline/scans`` and those tests stay. But an endpoint test can only reach
inputs the endpoint's own composition can produce, and it observes the outcome through a
rendered HTML body. This file calls both functions DIRECTLY over an exhaustive corpus and
asserts the exact returned value -- the canonical path string on accept, and the exact
``error_message`` and status on reject. That is the granularity at which "unchanged" is
checkable: two different rejections render into the same alert envelope, so a split that swaps
which branch fires is invisible from outside and obvious here.

CHARACTERIZED, NOT ENDORSED. Several rows below record behaviour that is surprising rather than
ideal -- most notably ``test_authorize_scan_root_accepts_a_path_under_a_DIFFERENT_configured_root``.
A characterization test's job is to record what the code does today so a refactor can be checked
against it. Where a row looks like a latent weakness it is called out in its own docstring; none
of them is changed here, and none should be changed as a side effect of the split. If one should
change, that is a bead of its own with its own review.

THE ORDER IS PART OF THE CONTRACT. ``_normalize_and_validate_scan_path``'s docstring pins a
four-step order -- NFC-normalize, reject NUL/surrogates, reject ``..`` as a COMPONENT, THEN
canonicalize -- and says it must not change. Canonicalizing before the ``..`` rejection would
resolve the traversal away and make it invisible, so the order is the mitigation, not a detail.
``test_dotdot_rejection_precedes_canonicalisation`` asserts that directly rather than trusting
the arrangement of the source lines.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

from starlette.requests import Request

from tests.shared.routers.pipeline_scans._shared import (
    RENDERABLE_ALERT_STATUS,
    Agent,
    pytest,
)


if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _request() -> Request:
    """A bare POST Request, sufficient for `_render_scan_alert` to build its TemplateResponse.

    The functions under test take a ``Request`` only so a rejection can be rendered into the
    swappable alert envelope; nothing in either gate reads the request. Constructing one
    directly (rather than routing through the app) is what makes this a unit-level corpus.
    """
    from phaze.main import create_app

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/pipeline/scans",
            "headers": [],
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1),
            "app": create_app(),
        }
    )


def _normalize(scan_root: str, subpath: str) -> tuple[str, Any]:
    """Call `_normalize_and_validate_scan_path` and reduce it to (verdict, detail).

    ``("accept", <canonical path>)`` or ``("reject", <exact operator-facing message>)``.
    """
    from phaze.routers.scan import _normalize_and_validate_scan_path
    from phaze.schemas.pipeline_scans import TriggerScanForm

    result = _normalize_and_validate_scan_path(_request(), TriggerScanForm(agent_id="unused", scan_root=scan_root, subpath=subpath))
    if isinstance(result, str):
        return ("accept", result)
    assert result.status_code == RENDERABLE_ALERT_STATUS, "every rejection is a swappable 200 alert (response_shape rule 3)"
    return ("reject", result.context["error_message"])


async def _authorize(session: AsyncSession, *, scan_roots: list[str] | None, revoked: bool, scan_root: str, joined: str) -> tuple[str, Any]:
    """Seed an agent (or not), call `_authorize_scan_root`, reduce it to (verdict, detail).

    ``("accept", <agent id>)`` or ``("reject", <exact operator-facing message>)``. A fresh
    agent id per call so the corpus rows cannot interfere with each other.
    """
    from phaze.routers.scan import _authorize_scan_root
    from phaze.schemas.pipeline_scans import TriggerScanForm

    agent_id = f"chartest-{uuid.uuid4().hex[:8]}"
    if scan_roots is not None:
        session.add(Agent(id=agent_id, name=agent_id, token_hash=None, scan_roots=scan_roots, revoked_at=datetime.now(UTC) if revoked else None))
        await session.commit()

    result = await _authorize_scan_root(_request(), session, TriggerScanForm(agent_id=agent_id, scan_root=scan_root, subpath=""), joined)
    if isinstance(result, Agent):
        return ("accept", result.id)
    assert result.status_code == RENDERABLE_ALERT_STATUS, "every rejection is a swappable 200 alert (response_shape rule 3)"
    return ("reject", result.context["error_message"])


# The three operator-facing rejection messages, spelled once. They are part of the pinned
# surface: an operator reading "outside the selected scan root" is being told something
# different from "not configured for this agent", and a split that collapses the two branches
# renders the wrong diagnosis for a real rejection.
_MSG_TRAVERSAL = "Subpath must not contain '..' path traversal."
_MSG_INVALID_CHARS = "Subpath must not contain NUL bytes or invalid Unicode."
_MSG_UNKNOWN_AGENT = "Unknown or revoked agent."
_MSG_ROOT_NOT_CONFIGURED = "Selected scan root is not configured for this agent."
_MSG_OUTSIDE_ROOT = "Resolved path is outside the selected scan root."


# (case id, scan_root, subpath, expected verdict, expected detail)
_NORMALISE_CORPUS: list[tuple[str, str, str, str, str]] = [
    # --- accepted: every spelling of the same directory collapses to ONE canonical string.
    # This is not cosmetic. `ScanBatch.scan_path` and the partial-unique index
    # `uq_scan_batches_agent_id_scan_path_running` (migration 044) both key on this value, so
    # if two spellings survive as two strings the duplicate-dispatch guard is bypassable by
    # resubmitting the same directory spelled differently.
    ("root-only", "/data/music", "", "accept", "/data/music"),
    ("root-trailing-slash", "/data/music/", "", "accept", "/data/music"),
    ("plain-subpath", "/data/music", "2026", "accept", "/data/music/2026"),
    ("subpath-leading-slash", "/data/music", "/2026", "accept", "/data/music/2026"),
    ("both-slashes", "/data/music/", "/2026/", "accept", "/data/music/2026"),
    ("double-internal-slash", "/data/music", "2026//sets", "accept", "/data/music/2026/sets"),
    ("dot-component", "/data/music", "./2026", "accept", "/data/music/2026"),
    ("lone-dot", "/data/music", ".", "accept", "/data/music"),
    ("trailing-slash-on-subpath", "/data/music", "a/b/", "accept", "/data/music/a/b"),
    # --- accepted: the WR-01 false positives. The original check was `".." in joined`, which
    # rejected every one of these legitimate filenames. Each is a real archive-shaped name.
    ("leading-ellipsis-filename", "/data/music", "...thinking", "accept", "/data/music/...thinking"),
    ("ellipsis-inside-name", "/data/music", "Album...Live/track", "accept", "/data/music/Album...Live/track"),
    ("dotdot-prefixed-dirname", "/data/music", "..notes/file.mp3", "accept", "/data/music/..notes/file.mp3"),
    ("dotdot-inside-component", "/data/music", "a/..b/c", "accept", "/data/music/a/..b/c"),
    # --- accepted: NFC normalisation. NFD is the norm for paths sourced from a macOS agent, so
    # the decomposed and composed spellings of the same directory MUST collapse together or the
    # agent's own configured root byte-differs from the path derived from it.
    ("nfd-input-normalised", "/data/music", "Café", "accept", "/data/music/Café"),
    ("nfc-input-unchanged", "/data/music", "Café", "accept", "/data/music/Café"),
    # --- accepted: characters that look suspicious but are legal in a POSIX filename. A
    # backslash is not a separator here, and surrounding spaces are not stripped.
    ("backslash-is-not-a-separator", "/data/music", "sub\\path", "accept", "/data/music/sub\\path"),
    ("spaces-preserved", "/data/music", "  spaced  ", "accept", "/data/music/  spaced  "),
    # --- rejected: traversal, as a path COMPONENT.
    ("traversal-leading", "/data/music", "../../etc/passwd", "reject", _MSG_TRAVERSAL),
    ("traversal-embedded", "/data/music", "2026/../../etc", "reject", _MSG_TRAVERSAL),
    ("traversal-bare", "/data/music", "..", "reject", _MSG_TRAVERSAL),
    # --- rejected: bytes PostgreSQL cannot store in a UTF8 text column. Refused outright rather
    # than sanitised: silently stripping the NUL would point the scan at a DIFFERENT filesystem
    # path than the operator typed, which is worse than refusing.
    ("nul-byte", "/data/music", "\x00evil", "reject", _MSG_INVALID_CHARS),
    ("lone-surrogate", "/data/music", "\ud800", "reject", _MSG_INVALID_CHARS),
]


@pytest.mark.parametrize(
    ("scan_root", "subpath", "verdict", "detail"),
    [case[1:] for case in _NORMALISE_CORPUS],
    ids=[case[0] for case in _NORMALISE_CORPUS],
)
def test_normalize_and_validate_scan_path_corpus(scan_root: str, subpath: str, verdict: str, detail: str) -> None:
    """The full accept/reject corpus for path normalisation and validation.

    Both halves matter equally. The rejections are the security boundary; the ACCEPTS are what
    stops a split from "hardening" the gate into rejecting legitimate archive filenames, which
    is a real regression that no security-only corpus would catch.
    """
    assert _normalize(scan_root, subpath) == (verdict, detail)


def test_dotdot_rejection_precedes_canonicalisation() -> None:
    """The ORDER: `..` is rejected as a component BEFORE `PurePosixPath` can resolve it away.

    Asserted behaviourally rather than by reading the source. ``/data/music/2026/../2026``
    canonicalises to ``/data/music/2026`` -- a path that is perfectly legal and inside the root.
    If canonicalisation ran first, this input would be ACCEPTED and the traversal would be
    invisible to every later check. It must reject.
    """
    assert _normalize("/data/music", "2026/../2026") == ("reject", _MSG_TRAVERSAL)


def test_nul_rejection_precedes_dotdot_rejection() -> None:
    """An input carrying BOTH a NUL and a `..` reports the NUL.

    Pins which branch wins when two rejections are both applicable. It reads like a detail, but
    the message is the operator's only diagnostic, and a reordered chain silently changes what
    they are told about an input they will have to fix.
    """
    assert _normalize("/data/music", "\x00/../etc") == ("reject", _MSG_INVALID_CHARS)


# Sentinel: the accept branch returns the seeded agent, and the corpus only needs to know THAT
# an Agent came back rather than which id was generated for the row. Comparing against this
# object keeps every accept row a plain equality assertion.
class _AnyAgentId:
    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and other.startswith("chartest-")

    def __hash__(self) -> int:
        return hash("_AnyAgentId")

    def __repr__(self) -> str:
        return "<the seeded agent>"


_ANY_AGENT = _AnyAgentId()


# (case id, configured scan_roots (None = agent does not exist), revoked, form scan_root, joined
#  path, expected verdict, expected detail)
_AUTHORIZE_CORPUS: list[tuple[str, list[str] | None, bool, str, str, str, Any]] = [
    # --- accepted.
    ("root-itself", ["/data/music"], False, "/data/music", "/data/music", "accept", _ANY_AGENT),
    ("descendant", ["/data/music"], False, "/data/music", "/data/music/2026", "accept", _ANY_AGENT),
    # The stored root and the submitted root are canonicalised the same way, so a trailing
    # slash on either side does not break a legitimate scan.
    ("configured-root-has-trailing-slash", ["/data/music/"], False, "/data/music", "/data/music/2026", "accept", _ANY_AGENT),
    ("submitted-root-has-trailing-slash", ["/data/music"], False, "/data/music/", "/data/music/2026", "accept", _ANY_AGENT),
    # phaze-g0if: `agent.scan_roots` is stored VERBATIM -- nothing upstream normalises it -- so a
    # root configured in NFD (the norm from an HFS+/macOS agent) must still match its own
    # NFC-normalised self.
    ("nfd-configured-root", ["/data/Café"], False, "/data/Café", "/data/Café/x", "accept", _ANY_AGENT),
    ("second-configured-root", ["/data/music", "/data/videos"], False, "/data/videos", "/data/videos/a", "accept", _ANY_AGENT),
    # --- rejected: the agent gate.
    ("agent-does-not-exist", None, False, "/data/music", "/data/music", "reject", _MSG_UNKNOWN_AGENT),
    ("agent-revoked", ["/data/music"], True, "/data/music", "/data/music", "reject", _MSG_UNKNOWN_AGENT),
    # --- rejected: WR-05 literal membership of the SUBMITTED root. Before WR-05 only the joined
    # path was prefix-checked, so `scan_root="/data"` + `subpath="music/foo"` authorised
    # `/data/music/foo` even though `/data` itself was never a configured root.
    ("submitted-root-is-an-ancestor-of-a-configured-one", ["/data/music"], False, "/data", "/data/music/foo", "reject", _MSG_ROOT_NOT_CONFIGURED),
    (
        "submitted-root-is-a-string-prefix-of-a-configured-one",
        ["/data/music"],
        False,
        "/data/mus",
        "/data/music/foo",
        "reject",
        _MSG_ROOT_NOT_CONFIGURED,
    ),
    ("agent-has-no-configured-roots", [], False, "/data/music", "/data/music", "reject", _MSG_ROOT_NOT_CONFIGURED),
    # --- rejected: D-06 prefix containment of the JOINED path.
    ("joined-is-entirely-elsewhere", ["/data/music"], False, "/data/music", "/etc/passwd", "reject", _MSG_OUTSIDE_ROOT),
    # The sibling-prefix attack: `/data/musicXX` starts with the string `/data/music` but is a
    # different directory. The gate compares against `root + "/"`, so it rejects. Dropping that
    # trailing separator during a split turns this row green and opens the hole.
    ("sibling-directory-sharing-a-string-prefix", ["/data/music"], False, "/data/music", "/data/musicXX", "reject", _MSG_OUTSIDE_ROOT),
    ("sibling-file-sharing-a-string-prefix", ["/data/music"], False, "/data/music", "/data/music.bak", "reject", _MSG_OUTSIDE_ROOT),
]


@pytest.mark.parametrize(
    ("scan_roots", "revoked", "scan_root", "joined", "verdict", "detail"),
    [case[1:] for case in _AUTHORIZE_CORPUS],
    ids=[case[0] for case in _AUTHORIZE_CORPUS],
)
@pytest.mark.asyncio
async def test_authorize_scan_root_corpus(
    session: AsyncSession,
    scan_roots: list[str] | None,
    revoked: bool,
    scan_root: str,
    joined: str,
    verdict: str,
    detail: Any,
) -> None:
    """The full accept/reject corpus for agent + scan-root authorisation.

    The three rejection branches fire in a fixed order (agent, then literal root membership,
    then prefix containment) and each has its own message, so this table also pins WHICH gate
    stops a given input -- not merely that something did.
    """
    assert await _authorize(session, scan_roots=scan_roots, revoked=revoked, scan_root=scan_root, joined=joined) == (verdict, detail)


@pytest.mark.asyncio
async def test_authorize_scan_root_accepts_a_path_under_a_DIFFERENT_configured_root(session: AsyncSession) -> None:
    """CHARACTERIZED, NOT ENDORSED: the prefix gate accepts ANY configured root, not the selected one.

    The literal-membership gate checks the SUBMITTED ``scan_root``, but the containment gate
    that follows is ``any(joined == r or joined.startswith(r + "/") for r in scan_roots)`` --
    over every configured root, not over the one the operator selected. So a ``joined`` under
    ``/data/videos`` is authorised even when the submitted root was ``/data/music``.

    Not reachable through ``POST /pipeline/scans`` today, because ``trigger_scan`` always
    derives ``joined`` from the submitted ``scan_root`` -- the two arguments cannot disagree.
    It is recorded here precisely BECAUSE it is unreachable: it is exactly the kind of latent
    coupling a split can turn into a reachable one, and a pin that only covered reachable inputs
    would not notice.

    This test asserts today's behaviour. It is not an endorsement of it, and it is not a
    licence to change it here -- tightening the gate is a behaviour change and belongs in its
    own bead with its own review.
    """
    outcome = await _authorize(session, scan_roots=["/data/music", "/data/videos"], revoked=False, scan_root="/data/music", joined="/data/videos/a")
    assert outcome == ("accept", _ANY_AGENT)
