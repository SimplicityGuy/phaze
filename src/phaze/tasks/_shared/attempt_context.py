"""Task-local control enqueue context; no database imports on the agent path."""

from contextvars import ContextVar
from typing import Any


ATTEMPT_META_KEY = "phaze_attempt_enqueued_at"
ledger_enqueue_session: ContextVar[Any] = ContextVar("ledger_enqueue_session", default=None)
