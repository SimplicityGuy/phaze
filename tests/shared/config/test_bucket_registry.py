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

import pytest

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


# --------------------------------------------------------------------------- #
# Task 2: container cross-entry validator (empty / bucket-cardinality / scope)
# --------------------------------------------------------------------------- #
_KUEUE_KUBE = """
[backends.kube]
api_url = "https://kube.example.com"
namespace = "phaze"
local_queue = "phaze-lq"
"""


def test_present_but_empty_registry_fails_fast(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A present-but-empty `backends = []` fails fast rather than silently booting empty (REG-04, Pitfall 2)."""
    backends_toml_env("backends = []\n")
    with pytest.raises(ValueError, match="empty"):
        ControlSettings()


def test_kueue_referencing_unknown_bucket_fails_fast(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A kueue backend referencing an unknown bucket id fails fast, naming the backend id + missing ids (D-08)."""
    backends_toml_env(
        f"""
        [[backends]]
        kind = "kueue"
        id = "kueue-x"
        rank = 10
        cap = 4
        buckets = ["ghost-bucket"]
        {_KUEUE_KUBE}
        """
    )
    with pytest.raises(ValueError, match=r"kueue-x"):
        ControlSettings()


def test_kueue_with_empty_bucket_set_fails_fast(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A kueue backend whose resolved bucket set is empty fails fast (D-08)."""
    backends_toml_env(
        f"""
        [[backends]]
        kind = "kueue"
        id = "kueue-empty"
        rank = 10
        cap = 4
        buckets = []
        {_KUEUE_KUBE}
        """
    )
    with pytest.raises(ValueError, match=r"kueue-empty"):
        ControlSettings()


def test_two_kueue_sharing_cluster_specific_bucket_rejected(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A cluster-specific bucket referenced by two kueue backends fails fast, naming the bucket id (D-09)."""
    backends_toml_env(
        """
        [[backends]]
        kind = "kueue"
        id = "kueue-a"
        rank = 10
        cap = 4
        buckets = ["cs-bucket"]

        [backends.kube]
        api_url = "https://a.example.com"
        namespace = "phaze"
        local_queue = "lq-a"

        [[backends]]
        kind = "kueue"
        id = "kueue-b"
        rank = 20
        cap = 4
        buckets = ["cs-bucket"]

        [backends.kube]
        api_url = "https://b.example.com"
        namespace = "phaze"
        local_queue = "lq-b"

        [[buckets]]
        id = "cs-bucket"
        scope = "cluster-specific"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-cs"
        """
    )
    with pytest.raises(ValueError, match=r"cs-bucket"):
        ControlSettings()


def test_two_kueue_sharing_shared_bucket_accepted(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A shared-scope bucket may be referenced by many kueue backends — constructs cleanly (D-09)."""
    backends_toml_env(
        """
        [[backends]]
        kind = "kueue"
        id = "kueue-a"
        rank = 10
        cap = 4
        buckets = ["shared-bucket"]

        [backends.kube]
        api_url = "https://a.example.com"
        namespace = "phaze"
        local_queue = "lq-a"

        [[backends]]
        kind = "kueue"
        id = "kueue-b"
        rank = 20
        cap = 4
        buckets = ["shared-bucket"]

        [backends.kube]
        api_url = "https://b.example.com"
        namespace = "phaze"
        local_queue = "lq-b"

        [[buckets]]
        id = "shared-bucket"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-shared"
        """
    )
    settings = ControlSettings()
    assert [b.id for b in settings.backends] == ["kueue-a", "kueue-b"]


# --------------------------------------------------------------------------- #
# Task 3: cloud_enabled + transitional accessors + secret-free startup log
# --------------------------------------------------------------------------- #
_ONE_COMPUTE = """
[[backends]]
kind = "compute"
id = "oci-a1"
rank = 10
cap = 3
agent_ref = "compute-agent-01"
scratch_dir = "/scratch/cloud"
"""

_ONE_KUEUE = """
[[backends]]
kind = "kueue"
id = "kueue-a"
rank = 10
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


def test_cloud_enabled_false_for_implicit_local(monkeypatch: _pytest.MonkeyPatch) -> None:
    """The implicit-local registry has no non-local backend → cloud_enabled is False (D-14)."""
    _clear_backends_env(monkeypatch)
    settings = ControlSettings()
    assert settings.cloud_enabled is False
    assert settings.active_compute_scratch_dir is None


def test_single_compute_backend_accessors(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A single compute backend reduces through the retained ≤1-non-local value accessors (D-09/D-15)."""
    backends_toml_env(_ONE_COMPUTE)
    settings = ControlSettings()
    assert settings.cloud_enabled is True
    assert settings.active_compute_scratch_dir == "/scratch/cloud"


def test_single_kueue_backend_accessors(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A single kueue backend exposes its KubeConfig + single resolved bucket via the accessors (D-15)."""
    backends_toml_env(_ONE_KUEUE)
    settings = ControlSettings()
    assert settings.cloud_enabled is True
    assert settings.active_kube is not None
    assert settings.active_kube.api_url == "https://kube.example.com"
    assert settings.active_bucket is not None
    assert settings.active_bucket.id == "bucket-a"


def test_multiple_non_local_backends_accessor_raises(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """>1 non-local backend → the retained value accessors raise (multi-backend dispatch is Phase 69) — never silently pick one.

    The registry itself is VALID (multi-cluster is the milestone goal and D-09 polices bucket sharing
    across multiple kueue backends), so construction succeeds and ``cloud_enabled`` is True; only the
    ≤1-non-local reduction inside ``_single_non_local`` (read by the retained value accessors) refuses
    to pick one until Phase 69 (SCHED). The two dispatch-selector accessors were removed in Phase 68
    (D-07/D-09); the ``>1``-non-local fail-fast now lives in ``resolve_backends`` at boot, with this
    accessor raise kept as defense-in-depth. CR-01 hardened the unguarded readers so this raise
    degrades gracefully instead of crashing boot.
    """
    backends_toml_env(
        """
        [[backends]]
        kind = "compute"
        id = "compute-a"
        rank = 10
        cap = 2
        agent_ref = "agent-a"
        scratch_dir = "/scratch/a"

        [[backends]]
        kind = "compute"
        id = "compute-b"
        rank = 20
        cap = 2
        agent_ref = "agent-b"
        scratch_dir = "/scratch/b"
        """
    )
    settings = ControlSettings()
    assert settings.cloud_enabled is True
    with pytest.raises(ValueError, match=r"Phase 69"):
        _ = settings.active_compute_scratch_dir


def test_multi_bucket_kueue_active_bucket_raises(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """A kueue resolving to >1 bucket → active_bucket raises (per-file bucket selection is Phase 70) — never silently pick one."""
    backends_toml_env(
        """
        [[backends]]
        kind = "kueue"
        id = "kueue-multi"
        rank = 10
        cap = 4
        buckets = ["b1", "b2"]

        [backends.kube]
        api_url = "https://kube.example.com"
        namespace = "phaze"
        local_queue = "phaze-lq"

        [[buckets]]
        id = "b1"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-b1"

        [[buckets]]
        id = "b2"
        scope = "shared"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-b2"
        """
    )
    settings = ControlSettings()
    with pytest.raises(ValueError, match=r"Phase 70"):
        _ = settings.active_bucket


def test_log_effective_registry_is_secret_free_projection(backends_toml_env) -> None:  # type: ignore[no-untyped-def]
    """log_effective_registry emits an id/kind/rank/cap projection only — never secret material (Pitfall 5)."""
    from structlog.testing import capture_logs

    secret_value = "SUPERSECRETSATOKEN"
    backends_toml_env(
        f"""
        [[backends]]
        kind = "kueue"
        id = "kueue-a"
        rank = 10
        cap = 4
        buckets = ["bucket-a"]

        [backends.kube]
        api_url = "https://kube.example.com"
        namespace = "phaze"
        local_queue = "phaze-lq"
        sa_token = "{secret_value}"

        [[buckets]]
        id = "bucket-a"
        scope = "cluster-specific"
        endpoint_url = "https://s3.example.com"
        bucket = "phaze-a"
        """
    )
    settings = ControlSettings()
    # The secret parsed into a SecretStr on the kube config...
    assert settings.active_kube is not None
    assert settings.active_kube.sa_token is not None
    assert settings.active_kube.sa_token.get_secret_value() == secret_value

    with capture_logs() as logs:
        settings.log_effective_registry()
    record = next(r for r in logs if r.get("event") == "phaze.config effective backend registry")
    assert record["backends"] == [{"id": "kueue-a", "kind": "kueue", "rank": 10, "cap": 4}]
    for entry in record["backends"]:
        assert set(entry) == {"id", "kind", "rank", "cap"}
    # No secret value anywhere in the captured records (projection never carries the SecretStr).
    assert secret_value not in repr(logs)
