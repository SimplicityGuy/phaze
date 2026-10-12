"""Core-held remote host profiles. A provider declares remote transport; core declares where it may read.

Only the parts the registry needs to mint and authorize references live here. The transport effect owns
host, routes, pacing, budget, User-Agent, credentials and stop conditions.
"""

import re
from typing import Annotated, Self

from pydantic import Field, model_validator

from phaze.tracklist_providers.domain import MAX_SEARCH_PAGE, DomainModel, ProviderId, SearchField, Text


class RemoteSearchRoute(DomainModel):
    fields: Annotated[frozenset[SearchField], Field(min_length=1)]
    format: Text
    max_page: int = Field(default=1, gt=0, le=MAX_SEARCH_PAGE)


class RemoteFetchRoute(DomainModel):
    native_id_pattern: Annotated[str, Field(min_length=1, max_length=1024)]
    format: Text

    @model_validator(mode="after")
    def compile_pattern(self) -> Self:
        try:
            re.compile(self.native_id_pattern)
        except re.error as exc:
            raise ValueError(f"Invalid native ID pattern: {exc}") from exc
        return self

    def accepts(self, native_id: str) -> bool:
        return re.fullmatch(self.native_id_pattern, native_id) is not None


class RemoteHostProfile(DomainModel):
    provider_id: ProviderId
    search: RemoteSearchRoute | None = None
    fetch: RemoteFetchRoute | None = None

    @model_validator(mode="after")
    def require_route(self) -> Self:
        if self.search is None and self.fetch is None:
            raise ValueError("A host profile declares at least one route")
        return self
