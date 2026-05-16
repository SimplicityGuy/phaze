"""Config-level locality enforcement for audfprint/panako sidecar URLs (Phase 28 D-12 / TASK-04).

Phase 28 D-12 contract: the agent's audfprint+panako sidecars MUST resolve to a
host on the agent's local Docker-compose network. Cross-file-server fingerprint
matching is out of scope for v4.0 (deferred under XAGENT-01), so a misconfigured
`PHAZE_AUDFPRINT_URL` or `PHAZE_PANAKO_URL` pointing at an external host would
silently leak local file paths and audio data to a remote endpoint.

The structural mitigation: `BaseSettings` (which both `ControlSettings` and
`AgentSettings` inherit) carries a `@field_validator("audfprint_url",
"panako_url")` that rejects any URL whose host is not in the allow-list
`{localhost, 127.0.0.1, audfprint, panako}` at construction time. A forged env
var raises `ValidationError` BEFORE the app boots — there is no code path
through which a non-allow-listed URL can reach the sidecar adapters.

Test IDs 28-V-22 (reject external) + 28-V-23 (accept local). These tests are
IMPLEMENTED in Wave 0 (Plan 28-01); the rest of the Phase 28 test suite remains
stubbed.
"""

from __future__ import annotations

import pydantic
import pytest

from phaze.config import ControlSettings


# -----------------------
# Rejection cases (28-V-22)
# -----------------------


def test_audfprint_url_rejects_external_host() -> None:
    """An external host on `audfprint_url` MUST raise ValidationError at construction.

    The error message must reference XAGENT-01 (the deferred cross-fs requirement)
    OR the words "local Compose network" / "Cross-file-server" so operators reading
    the traceback understand WHY their config was rejected.
    """
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ControlSettings(audfprint_url="http://evil.example.com:8001")

    rendered = str(exc_info.value)
    assert "XAGENT-01" in rendered or "local Compose network" in rendered or "Cross-file-server" in rendered, (
        f"Validator message must cite XAGENT-01 / locality contract; got: {rendered}"
    )


def test_panako_url_rejects_external_host() -> None:
    """Symmetric to audfprint: external panako_url also rejected."""
    with pytest.raises(pydantic.ValidationError) as exc_info:
        ControlSettings(panako_url="http://evil.example.com:8002")

    rendered = str(exc_info.value)
    assert "XAGENT-01" in rendered or "local Compose network" in rendered or "Cross-file-server" in rendered, (
        f"Validator message must cite XAGENT-01 / locality contract; got: {rendered}"
    )


# -----------------------
# Acceptance cases (28-V-23)
# -----------------------


def test_audfprint_url_accepts_compose_service_name() -> None:
    """The Docker-compose default `http://audfprint:8001` must be accepted unchanged."""
    cfg = ControlSettings(audfprint_url="http://audfprint:8001")
    assert cfg.audfprint_url == "http://audfprint:8001"


def test_audfprint_url_accepts_localhost() -> None:
    """`http://localhost:8001` is a valid loopback target."""
    cfg = ControlSettings(audfprint_url="http://localhost:8001")
    assert cfg.audfprint_url == "http://localhost:8001"


def test_audfprint_url_accepts_127_0_0_1() -> None:
    """`http://127.0.0.1:8001` is a valid loopback target."""
    cfg = ControlSettings(audfprint_url="http://127.0.0.1:8001")
    assert cfg.audfprint_url == "http://127.0.0.1:8001"


def test_panako_url_accepts_compose_service_name() -> None:
    """The Docker-compose default `http://panako:8002` must be accepted unchanged."""
    cfg = ControlSettings(panako_url="http://panako:8002")
    assert cfg.panako_url == "http://panako:8002"
