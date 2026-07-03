"""Unit tests for the Phase 67 backend/bucket registry on ``ControlSettings`` (REG-01/04/05).

The registry integrates the Plan-01 typed submodels (``config_backends``) into ``ControlSettings``
ADDITIVELY: a ``backends``/``buckets`` pair loaded from ``backends.toml`` via the
``PHAZE_BACKENDS_CONFIG_FILE`` env pointer (Idiom B, D-01/D-02), an implicit single ``kind=local``
backend when no file is present (D-03), a container ``model_validator`` enforcing whole-registry
invariants (non-empty, resolvable bucket sets, scope cardinality — REG-04/05, D-08/D-09), a
registry-derived ``cloud_enabled`` gate + transitional ≤1-non-local accessors (D-14/D-15), and a
secret-free startup-log projection (Pitfall 5).

These are pure pydantic-settings tests -- no DB, no Redis. Registry fixtures are supplied via the
shared ``backends_toml_env`` conftest fixture (writes a tmp ``backends.toml`` + points the env var).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from phaze.config import ControlSettings
from phaze.config_backends import BucketConfig, ComputeBackend, KueueBackend, LocalBackend


if TYPE_CHECKING:
    import pytest as _pytest


def _clear_backends_env(monkeypatch: _pytest.MonkeyPatch) -> None:
    """Drop the registry pointer so an ambient operator env cannot leak into these unit assertions."""
    monkeypatch.delenv("PHAZE_BACKENDS_CONFIG_FILE", raising=False)


# --------------------------------------------------------------------------- #
# Task 1: implicit-local default + tomllib env-pointer loader
# --------------------------------------------------------------------------- #
def test_implicit_local_when_no_pointer_and_no_file(monkeypatch: _pytest.MonkeyPatch) -> None:
    """No PHAZE_BACKENDS_CONFIG_FILE + no default file → a single implicit kind=local backend (D-03).

    The live all-local deploy needs zero config edits: the ``default_factory`` synthesizes the
    rank-99 cap-1 local backend when the ``backends`` key is entirely absent.
    """
    _clear_backends_env(monkeypatch)
    settings = ControlSettings()
    assert settings.backends == [LocalBackend(kind="local", id="local", rank=99, cap=1)]
    assert settings.buckets == []


def test_toml_file_parses_backends_and_buckets(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A PHAZE_BACKENDS_CONFIG_FILE pointing at a backends.toml parses [[backends]]/[[buckets]] into typed models."""
    backends_toml_env(
        """
        [[backends]]
        kind = "local"
        id = "local"
        rank = 99
        cap = 1

        [[backends]]
        kind = "compute"
        id = "oci-a1"
        rank = 10
        cap = 2
        agent_ref = "compute-agent-01"
        scratch_dir = "/scratch"

        [[buckets]]
        id = "shared-bucket"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-staging"
        """
    )
    settings = ControlSettings()
    assert len(settings.backends) == 2
    assert isinstance(settings.backends[0], LocalBackend)
    assert isinstance(settings.backends[1], ComputeBackend)
    assert settings.backends[1].agent_ref == "compute-agent-01"
    assert settings.backends[1].scratch_dir == "/scratch"
    assert len(settings.buckets) == 1
    assert isinstance(settings.buckets[0], BucketConfig)
    assert settings.buckets[0].scope == "shared"
    assert settings.buckets[0].bucket == "phaze-staging"


def test_kueue_backend_with_kube_table_parses(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A nested [backends.kube] table parses into a KubeConfig on the KueueBackend (D-13)."""
    backends_toml_env(
        """
        [[backends]]
        kind = "kueue"
        id = "kueue-a"
        rank = 20
        cap = 4
        buckets = ["bucket-a"]

        [backends.kube]
        api_url = "https://kube.example.com"
        namespace = "phaze"
        local_queue = "phaze-lq"

        [[buckets]]
        id = "bucket-a"
        scope = "cluster-specific"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-a"
        """
    )
    settings = ControlSettings()
    kueue = settings.backends[0]
    assert isinstance(kueue, KueueBackend)
    assert kueue.kube is not None
    assert kueue.kube.api_url == "https://kube.example.com"
    assert kueue.buckets == ["bucket-a"]
