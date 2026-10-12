"""Core-held remote host profiles. A provider declares remote transport; core declares where it may read.

The registry reads the route kinds, formats, search fields, page bound and native-ID syntax to mint and
authorize references. The transport effect (``phaze.services.remote_transport``) reads the rest: the one
``https`` host, the route paths, pacing, budgets, User-Agent, credential slot, robots handling and stop
conditions. A request is built only from these fields and a minted reference, so a reference never needs
to carry a URL, host, path, method or header. See docs/design/0025-remote-provider-sources.md.

Every default below is the implementer's conservative choice, not an operator figure: the 6 s gap sits
above the 5.1 s gap the MixesDB investigations ran at (docs/spikes/phaze-6ehg6-mixesdb-rest-access.md)
and above that site's robots ``Crawl-delay: 4``; the durable budget is 60 requests in any rolling hour,
a tenth of what the gap alone would allow.
"""

import re
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from phaze.tracklist_providers.domain import MAX_SEARCH_PAGE, DomainModel, ProviderId, SearchField, Text


DEFAULT_MIN_GAP_SECONDS = 6.0
DEFAULT_PER_RUN_BUDGET = 16
DEFAULT_DURABLE_BUDGET = 60
DEFAULT_DURABLE_WINDOW_SECONDS = 3600
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_CLOCK_TOLERANCE_SECONDS = 2.0
# SourceRead.text holds at most 262,144 characters, so a larger body could never be handed over intact.
MAX_RESPONSE_BYTES = 262144
# A 403 or 429 always stops a host; a profile may add statuses but never remove these.
REQUIRED_STOP_STATUSES = frozenset({403, 429})
# Markers of a genuine challenge interstitial in an HTML body. The challenge-platform script that a
# readable page merely embeds (``/cdn-cgi/challenge-platform/scripts/jsd/main.js``) is deliberately not one.
DEFAULT_CHALLENGE_MARKERS = ("window._cf_chl_opt", "<title>Just a moment...</title>", "cf-browser-verification")

_HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SEGMENT = r"[A-Za-z0-9._~!$&'()*+,;=:@-]+"
_PATH = re.compile(rf"(?:/{_SEGMENT})+/?")
_PARAMETER_NAME = re.compile(r"[A-Za-z0-9._~-]{1,64}")
_PRINTABLE = re.compile(r"[\x20-\x7e]+")
_HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")
NATIVE_ID_PLACEHOLDER = "{native_id}"

ParameterName = Annotated[str, Field(pattern=rf"^{_PARAMETER_NAME.pattern}$")]
Parameter = tuple[ParameterName, Annotated[str, Field(min_length=1, max_length=256)]]


def _validate_path(path: str) -> str:
    if _PATH.fullmatch(path) is None or any(segment in {".", ".."} for segment in path.split("/")):
        raise ValueError("A route path is an absolute path of plain segments, without a host, query or dot segment")
    return path


def _unique_names(names: list[str]) -> None:
    if len(set(names)) != len(names):
        raise ValueError("Query parameter names must be unique")


class RemoteSearchRoute(DomainModel):
    fields: Annotated[frozenset[SearchField], Field(min_length=1)]
    format: Text
    max_page: int = Field(default=1, gt=0, le=MAX_SEARCH_PAGE)
    path: Annotated[str, Field(max_length=512)]
    # Each accepted field names the query parameter it is sent as. Fields sharing a parameter are joined
    # with one space in artist, event, date order.
    term_parameters: Annotated[tuple[tuple[SearchField, ParameterName], ...], Field(min_length=1)]
    page_parameter: ParameterName | None = None
    fixed_parameters: Annotated[tuple[Parameter, ...], Field(max_length=8)] = ()
    etag_is_content_validator: bool = False

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        _validate_path(self.path)
        mapped = [field for field, _ in self.term_parameters]
        if len(set(mapped)) != len(mapped) or set(mapped) != self.fields:
            raise ValueError("Every accepted search field maps to exactly one query parameter")
        if self.max_page > 1 and self.page_parameter is None:
            raise ValueError("A search with more than one page needs a page parameter")
        terms = {name for _, name in self.term_parameters}
        page = [self.page_parameter] if self.page_parameter else []
        fixed = [name for name, _ in self.fixed_parameters]
        _unique_names([*page, *fixed])
        if terms & {*page, *fixed}:
            raise ValueError("Query parameter names must be unique")
        return self


class RemoteFetchRoute(DomainModel):
    native_id_pattern: Annotated[str, Field(min_length=1, max_length=1024)]
    format: Text
    # The native ID fills one whole segment, percent-encoded, so it can never add a segment or a query.
    path_template: Annotated[str, Field(max_length=512)]
    fixed_parameters: Annotated[tuple[Parameter, ...], Field(max_length=8)] = ()
    etag_is_content_validator: bool = False

    @model_validator(mode="after")
    def compile_pattern(self) -> Self:
        try:
            re.compile(self.native_id_pattern)
        except re.error as exc:
            raise ValueError(f"Invalid native ID pattern: {exc}") from exc
        segments = self.path_template.split("/")
        if self.path_template.count(NATIVE_ID_PLACEHOLDER) != 1 or NATIVE_ID_PLACEHOLDER not in segments:
            raise ValueError("A fetch path template holds the native ID placeholder once, as a whole segment")
        _validate_path(self.path_template.replace(NATIVE_ID_PLACEHOLDER, "id"))
        _unique_names([name for name, _ in self.fixed_parameters])
        return self

    def accepts(self, native_id: str) -> bool:
        return re.fullmatch(self.native_id_pattern, native_id) is not None


class CredentialSlot(DomainModel):
    """Where a provider's secret goes. The secret itself comes from settings and is never held here."""

    header: Annotated[str, Field(pattern=rf"^{_HEADER_NAME.pattern}$")]
    prefix: Annotated[str, Field(max_length=32, pattern=r"^[A-Za-z0-9 ._-]*$")] = ""


class RemoteHostProfile(DomainModel):
    provider_id: ProviderId
    search: RemoteSearchRoute | None = None
    fetch: RemoteFetchRoute | None = None
    host: Annotated[str, Field(max_length=253)]
    user_agent: Annotated[str, Field(min_length=1, max_length=256, pattern=rf"^{_PRINTABLE.pattern}$")]
    min_gap_seconds: float = Field(default=DEFAULT_MIN_GAP_SECONDS, ge=1, le=3600, allow_inf_nan=False)
    per_run_budget: int = Field(default=DEFAULT_PER_RUN_BUDGET, gt=0, le=256)
    durable_budget: int = Field(default=DEFAULT_DURABLE_BUDGET, gt=0, le=10000)
    durable_window_seconds: int = Field(default=DEFAULT_DURABLE_WINDOW_SECONDS, gt=0, le=7 * 86400)
    request_timeout_seconds: float = Field(default=DEFAULT_REQUEST_TIMEOUT_SECONDS, gt=0, le=120, allow_inf_nan=False)
    max_response_bytes: int = Field(default=MAX_RESPONSE_BYTES, gt=0, le=MAX_RESPONSE_BYTES)
    clock_tolerance_seconds: float = Field(default=DEFAULT_CLOCK_TOLERANCE_SECONDS, gt=0, le=60, allow_inf_nan=False)
    credential: CredentialSlot | None = None
    # "respect": fetch /robots.txt (paced and charged like any request), refuse a disallowed route with a
    # persistent stop, and widen the gap to any Crawl-delay. "not_applicable" is for a documented API host.
    robots: Literal["respect", "not_applicable"] = "respect"
    stop_statuses: frozenset[int] = REQUIRED_STOP_STATUSES
    challenge_markers: Annotated[tuple[Annotated[str, Field(min_length=8, max_length=256)], ...], Field(max_length=16)] = DEFAULT_CHALLENGE_MARKERS

    @model_validator(mode="after")
    def require_route(self) -> Self:
        if self.search is None and self.fetch is None:
            raise ValueError("A host profile declares at least one route")
        if _HOST.fullmatch(self.host) is None:
            raise ValueError("A host is one lowercase DNS name: no scheme, port, user-info, path or IP literal")
        if not self.stop_statuses >= REQUIRED_STOP_STATUSES or any(not 400 <= status <= 599 for status in self.stop_statuses):
            raise ValueError("Stop statuses are 4xx or 5xx and always include 403 and 429")
        return self
