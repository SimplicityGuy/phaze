"""Characterization of the registry seam under a DUPLICATE ``[[backends]]`` id (phaze-dr9df).

The 2026-08-08 bug hunt's duplicate-``[[backends]]``-id "silent last-wins" defect lived in exactly the
registry logic now isolated in :mod:`phaze.services.backends.registry`. phaze-1sgee closed it, but it
closed it ONE LAYER UP -- in ``ControlSettings``' cross-entry validator -- and the registry functions
themselves were never characterized against a duplicate. That left the shape of the defect undocumented
at the seam it actually lives at, so anyone reading ``resolve_compute_backend`` in isolation cannot tell
whether its ``{backend.id: backend}`` collapse is a guarded invariant or a live bug.

These tests pin BOTH halves of the current answer:

1. **The guard is at config validation and it fails fast** -- a duplicate id can never reach these
   functions through the supported path (``ControlSettings()`` over ``backends.toml``).
2. **The registry functions themselves have NO dedupe of their own**, and they disagree with each
   other about what "the backend named <id>" means when handed a duplicate anyway. That is only safe
   BECAUSE of (1). If (1) is ever relaxed -- a hand-built settings object, a future loader that
   bypasses the validator, a test stub -- the divergence is live again:

   * :func:`resolve_backends` is **positional, not keyed**: it emits one impl per ENTRY, so two
     entries sharing an id produce TWO backends with the same ``id``. Not last-wins -- WORSE than
     last-wins, because both then run and each one's ``in_flight_count``
     (``COUNT(cloud_job WHERE backend_id == self.id)``) counts the OTHER's rows, so a shared id
     double-counts its own cap.
   * :func:`resolve_compute_backend` is **keyed and silently last-wins** -- the literal
     dict-comprehension collapse the 08-08 hunt found.

   phaze-dr9df deliberately does NOT change either behavior: this bead is a pure decomposition and
   the fail-fast above means neither is reachable in production. Follow-up candidate, filed
   separately: make the registry layer defend itself rather than trusting its caller.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from phaze.services.backends.registry import resolve_backends, resolve_compute_backend, resolved_non_local_kind


_DUPLICATE_ID_TOML = """
    [[backends]]
    kind = "compute"
    id = "cloud-1"
    rank = 10
    cap = 2
    agent_ref = "agent-a"
    scratch_dir = "/scratch/a"
    push_host = "a.push"

    [[backends]]
    kind = "compute"
    id = "cloud-1"
    rank = 20
    cap = 7
    agent_ref = "agent-b"
    scratch_dir = "/scratch/b"
    push_host = "b.push"

    [[backends]]
    kind = "local"
    id = "local"
    rank = 99
    cap = 4
"""


def _dupe_settings() -> Any:
    """Build an UNVALIDATED settings stub carrying two ``compute`` entries under one id.

    Deliberately a ``SimpleNamespace`` and not a ``ControlSettings``: the real model refuses to
    construct this (see :func:`test_duplicate_backend_id_fails_fast_at_config_validation`), which is
    the whole point. The registry functions read only ``.backends`` / ``.cloud_enabled``, so a
    namespace whose entries duck-type ``kind``/``id``/``rank``/``cap``/``agent_ref`` is enough to
    observe what they would do if a duplicate ever reached them.
    """
    return SimpleNamespace(
        cloud_enabled=True,
        backends=[
            SimpleNamespace(kind="compute", id="cloud-1", rank=10, cap=2, agent_ref="agent-a", scratch_dir="/scratch/a", push_host="a.push"),
            SimpleNamespace(kind="compute", id="cloud-1", rank=20, cap=7, agent_ref="agent-b", scratch_dir="/scratch/b", push_host="b.push"),
            SimpleNamespace(kind="local", id="local", rank=99, cap=4),
        ],
    )


def test_duplicate_backend_id_fails_fast_at_config_validation(backends_toml_env: Any) -> None:
    """THE GUARD (phaze-1sgee): a duplicate ``[[backends]]`` id refuses to load, kind-agnostic.

    This is the reason the two divergent behaviors characterized below are not a live bug: no
    supported path can hand the registry functions a duplicate id. The error names the offending id
    AND its kinds so an operator can find the copy-paste.
    """
    from phaze.config import ControlSettings

    backends_toml_env(_DUPLICATE_ID_TOML)
    with pytest.raises(ValueError, match="duplicate backend ids in registry") as excinfo:
        ControlSettings()
    message = str(excinfo.value)
    assert "cloud-1" in message
    assert "must be unique" in message


def test_resolve_backends_is_positional_and_does_not_dedupe_by_id() -> None:
    """CHARACTERIZATION: ``resolve_backends`` emits one impl per ENTRY -- duplicates are NOT collapsed.

    Two entries sharing ``id="cloud-1"`` resolve to TWO ``ComputeAgentBackend`` objects, each bound to
    its own config (so they carry the entries' distinct ``cap``/``rank``/``agent_ref``) but sharing one
    ``id``. This is asserted as CURRENT behavior, not as desirable behavior: because every backend's
    cap accounting is ``COUNT(cloud_job WHERE backend_id == self.id)``, two same-id backends would each
    count the other's in-flight rows. The registry layer does not defend against this; ``ControlSettings``
    does (test above).
    """
    resolved = resolve_backends(_dupe_settings())

    assert len(resolved) == 3, "one Backend per registry ENTRY -- no keying, no collapse"
    cloud_ones = [backend for backend in resolved if backend.id == "cloud-1"]
    assert len(cloud_ones) == 2
    # Each impl kept ITS OWN entry's fields -- so the two are genuinely distinct backends wearing one id.
    assert sorted(backend.cap for backend in cloud_ones) == [2, 7]
    assert sorted(backend.rank for backend in cloud_ones) == [10, 20]
    assert sorted(backend._agent_ref() for backend in cloud_ones) == ["agent-a", "agent-b"]


def test_resolve_compute_backend_is_silent_last_wins_on_a_duplicate_id() -> None:
    """CHARACTERIZATION: ``resolve_compute_backend`` silently resolves a duplicate id to the LAST entry.

    The literal shape the 08-08 hunt found: a ``{backend.id: backend}`` dict comprehension over
    ``cfg.backends`` collapses duplicates to whichever entry appears LAST in the TOML list, with no
    warning and no error. Every downstream compute reader (the rsync destination, the ``/pushed`` and
    ``/mismatch`` callbacks) would therefore resolve the second entry's ``scratch_dir`` / ``push_host``
    / ``agent_ref`` for work the FIRST entry dispatched.

    Asserted, not fixed, by phaze-dr9df (pure decomposition; the fail-fast above makes it unreachable).
    It notably does NOT agree with :func:`resolve_backends` above, which keeps both -- and that
    disagreement is the follow-up's actual justification.
    """
    resolved = resolve_compute_backend(_dupe_settings(), "cloud-1")

    assert resolved is not None
    assert resolved.agent_ref == "agent-b", "LAST entry wins, silently"
    assert resolved.cap == 7
    assert resolved.push_host == "b.push"


def test_resolved_non_local_kind_is_unaffected_by_a_duplicate_id() -> None:
    """A duplicate id does not perturb the single-kind reduction -- it asks about KIND, never id.

    Included so the follow-up has the full picture of the seam: of the three registry functions, only
    the two above are id-keyed, so only those two can diverge on a duplicate.
    """
    assert resolved_non_local_kind(_dupe_settings()) == "compute"
