"""Tests for ``phaze.services.cloud_budget.cloud_budget_hold_reason``'s naive/aware ``now`` coercion.

The function accepts a caller-supplied ``now`` that may or may not carry tzinfo (the drain
deliberately coerces ``now`` to the CANDIDATE's awareness, but a ``create_all``-built test schema can
still hand back a naive ``budget_spent_at``), and normalizes whichever side is naive to match the
other's awareness BEFORE subtracting -- a naive-minus-aware subtraction raises ``TypeError``, which
would abort a whole drain tick otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from phaze.services.cloud_budget import CloudBudgetState, cloud_budget_hold_reason


def _cfg(*, max_chains: int = 0, max_node_loss: int = 0, cooldown_days: int = 1) -> SimpleNamespace:
    """Minimal ControlSettings stand-in carrying only the three knobs this function reads."""
    return SimpleNamespace(
        cloud_budget_max_chains=max_chains,
        cloud_budget_max_node_loss=max_node_loss,
        cloud_budget_cooldown_days=cooldown_days,
    )


def _budget(*, spent_at: datetime) -> CloudBudgetState:
    return CloudBudgetState(chains_spent=0, attempts_spent=0, node_loss_spent=0, budget_spent_at=spent_at)


def test_cloud_budget_hold_reason_coerces_a_naive_spent_at_to_the_aware_now() -> None:
    """A naive ``budget_spent_at`` (test-schema artifact) is coerced to ``now``'s tzinfo before subtracting.

    Without the coercion this raises ``TypeError: can't subtract offset-naive and offset-aware
    datetimes``. Picks a ``spent_at`` inside the cooldown window so the coerced comparison is provably
    exercised (not merely "did not crash") via the returned HOLD label.
    """
    now = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
    naive_spent_at = datetime(2026, 8, 10, 11, 0, 0)  # 1 hour ago, no tzinfo -- still inside a 1-day cooldown
    budget = _budget(spent_at=naive_spent_at)

    reason = cloud_budget_hold_reason(budget, now, _cfg(cooldown_days=1))

    assert reason == "cloud_budget_cooldown"


def test_cloud_budget_hold_reason_coerces_a_naive_now_to_the_aware_spent_at() -> None:
    """The reverse mismatch: a naive ``now`` against an aware ``budget_spent_at`` is coerced the same way."""
    aware_spent_at = datetime(2026, 8, 10, 11, 0, 0, tzinfo=UTC)
    naive_now = datetime(2026, 8, 10, 12, 0, 0)  # 1 hour later, no tzinfo
    budget = _budget(spent_at=aware_spent_at)

    reason = cloud_budget_hold_reason(budget, naive_now, _cfg(cooldown_days=1))

    assert reason == "cloud_budget_cooldown"


def test_cloud_budget_hold_reason_naive_pair_clears_once_cooldown_elapses() -> None:
    """Both naive, well past the cooldown: still no crash, and cloud is allowed again."""
    spent_at = datetime(2026, 8, 1, 0, 0, 0)
    now = spent_at + timedelta(days=30)
    budget = _budget(spent_at=spent_at)

    reason = cloud_budget_hold_reason(budget, now, _cfg(cooldown_days=1))

    assert reason == ""
