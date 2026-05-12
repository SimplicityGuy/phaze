"""Unit tests for phaze.schemas.agent_identity (Phase 26 Plan 03 — D-15)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from phaze.schemas.agent_identity import AgentIdentity


def test_agent_identity_accepts_minimum_valid_payload() -> None:
    """AgentIdentity must accept the full /whoami response shape."""
    identity = AgentIdentity(
        agent_id="agent-a",
        name="phaze-server",
        scan_roots=["/music", "/concerts"],
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )

    assert identity.agent_id == "agent-a"
    assert identity.name == "phaze-server"
    assert identity.scan_roots == ["/music", "/concerts"]
    assert identity.created_at.year == 2026


def test_agent_identity_accepts_empty_scan_roots() -> None:
    """Newly-registered agents have no scan_roots; empty list must be valid."""
    identity = AgentIdentity(
        agent_id="x",
        name="x",
        scan_roots=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )

    assert identity.scan_roots == []


def test_agent_identity_parses_iso8601_created_at_string() -> None:
    """Pydantic should coerce an ISO-8601 string to datetime (server JSON wire format)."""
    identity = AgentIdentity.model_validate(
        {
            "agent_id": "a",
            "name": "n",
            "scan_roots": ["/r"],
            "created_at": "2026-01-01T00:00:00Z",
        },
    )

    assert isinstance(identity.created_at, datetime)
    assert identity.created_at.year == 2026


def test_agent_identity_is_response_only_no_extra_forbid() -> None:
    """Response schemas stay loose so the server can add fields non-breakingly (D-15)."""
    # Adding an unknown key MUST NOT raise — response schemas allow forward-compat.
    identity = AgentIdentity.model_validate(
        {
            "agent_id": "a",
            "name": "n",
            "scan_roots": [],
            "created_at": "2026-01-01T00:00:00Z",
            "future_field": "ignored",
        },
    )
    assert identity.agent_id == "a"


def test_agent_identity_requires_agent_id() -> None:
    """agent_id is required."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        AgentIdentity.model_validate(
            {
                "name": "n",
                "scan_roots": [],
                "created_at": "2026-01-01T00:00:00Z",
            },
        )
