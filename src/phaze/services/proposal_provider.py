"""Provider orchestration and rate limiting for filename proposals."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from phaze.services.proposal_parsing import BatchProposalResponse


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class ProposalCompletionProvider(Protocol):
    """Narrow port for a provider capable of producing one structured completion."""

    async def __call__(self, **kwargs: Any) -> Any:
        """Return a provider response for the supplied completion request."""
        ...


class CompletionParser(Protocol):
    """Port from provider wire output to the stable proposal response contract."""

    def __call__(self, content: str | None, *, batch_size: int, finish_reason: str | None) -> BatchProposalResponse:
        """Parse one completion or raise the stable malformed-completion error."""
        ...


async def generate_batch(
    *,
    model: str,
    prompt_template: str,
    files_context: list[dict[str, Any]],
    completion_provider: ProposalCompletionProvider,
    completion_parser: CompletionParser,
    date_guidance: Callable[[list[dict[str, Any]]], str],
) -> BatchProposalResponse:
    """Render a prompt, call the configured provider, and parse its first choice."""
    prompt = prompt_template.replace("{date_convention_guidance}\n", date_guidance(files_context))
    prompt = prompt.replace("{files_json}", json.dumps(files_context, indent=2))
    response = await completion_provider(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format=BatchProposalResponse,
    )
    choice = response.choices[0]
    return completion_parser(
        choice.message.content,
        batch_size=len(files_context),
        finish_reason=getattr(choice, "finish_reason", None),
    )


_RATE_LIMIT_WINDOW_SEC = 60
_RATE_LIMIT_LUA = """
local count = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) == -1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


async def check_rate_limit(redis_pool: Any, max_rpm: int, *, sleep: Callable[[float], Awaitable[Any]]) -> None:
    """Block until an atomic rolling-window request slot is available."""
    key = "phaze:llm:rpm"
    while True:
        count = int(await redis_pool.eval(_RATE_LIMIT_LUA, 1, key, _RATE_LIMIT_WINDOW_SEC))
        if count <= max_rpm:
            return
        await redis_pool.decr(key)
        await sleep(2.0)
