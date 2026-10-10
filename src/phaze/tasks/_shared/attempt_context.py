"""Task-local control enqueue context; no database imports on the agent path."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


ATTEMPT_META_KEY = "phaze_attempt_enqueued_at"
ledger_enqueue_session: ContextVar[Any] = ContextVar("ledger_enqueue_session", default=None)


@dataclass(frozen=True)
class EnqueueAuthority:
    """Proof belongs to this enqueue call/key, never an inherited session."""

    key: str
    serialized: bool


enqueue_authority: ContextVar[EnqueueAuthority | None] = ContextVar("enqueue_authority", default=None)
