"""Browser render engine for 1001Tracklists detail pages (phaze-fq9h.1).

WHY A BROWSER AT ALL. The search endpoint is plain HTML and `TracklistScraper` fetches it with
httpx. Detail pages are not: the track listing is injected by JavaScript behind a Cloudflare
Turnstile widget, so an httpx GET returns a shell with zero `.tlpItem` nodes -- indistinguishable,
to a naive parser, from a set that genuinely has no tracklist. Spike phaze-dmvs established the
one client that reaches the content without evasion:

  * HEADFUL Patchright (a CDP-patched Playwright fork). Turnstile issues its own token to a
    browser that presents as real. This is fingerprint MATCHING, not evasion -- no proxies, no
    UA randomization, no CAPTCHA-solving service. That bound is the epic's ethics ceiling.
  * HEADLESS fails. Both vanilla Playwright headless and Patchright headless stall on the
    interstitial, which is why headless workers must run this under Xvfb (`XvfbDisplay` below)
    rather than passing `headless=True`.
  * Turnstile is FLAKY, not deterministic: 6 of 8 sampled pages cleared on the first navigation.
    Without the bounded reload/retry loop here, a quarter of the corpus would be recorded as
    "no tracklist" when the truth is "we never got past the challenge" -- so those two outcomes
    are separate members of `RenderOutcome` and must stay separate all the way to the operator.

THIS MODULE DOES NOT PARSE. It hands back rendered HTML; phaze-fq9h.4 owns the per-track
selectors. Render and parse stay decoupled so a parser change never costs a host request and a
captured fixture can be re-parsed offline forever.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404
import sys
from typing import TYPE_CHECKING, Any, Protocol, Self, cast

import structlog

from phaze.config import get_settings
from phaze.services.tracklist_scraper import (
    DisallowedScrapeHostError,
    TracklistScraper,
    honest_user_agent_token,
    reserve_host_request_slot,
)


if TYPE_CHECKING:
    from collections.abc import Sequence

    from phaze.config import BaseSettings


logger = structlog.get_logger(__name__)


# The detail-page track container. phaze-dmvs re-verified `.tlpItem` against live markup
# (2026-07-24) -- phaze-mk6y had flagged the whole detail-selector block as probably stale after
# the search-page redesign, and for this one selector that fear turned out to be unfounded.
# Deliberately the ONLY selector this module knows: it is a readiness signal ("JS has delivered
# the listing"), not a parse. Per-track selectors belong to phaze-fq9h.4.
TRACK_CONTAINER_SELECTOR = ".tlpItem"

# Substrings that identify Cloudflare's CHALLENGE page, checked case-insensitively.
#
# READ THE PRECEDENCE RULE BEFORE ADDING TO THIS LIST. 1001Tracklists embeds a Turnstile widget on
# detail pages it has ALREADY let us through, so `cf-turnstile` / `challenges.cloudflare.com`
# appear in successful renders too. Presence of a marker therefore never means "blocked" on its
# own -- `_classify` checks for the track container FIRST and only consults these markers when the
# container is absent. Every entry below is specific to the interstitial document itself (the
# challenge-platform script path, the "Just a moment" title, the `cf_chl_opt` bootstrap object),
# never to the widget.
_INTERSTITIAL_MARKERS: tuple[str, ...] = (
    "/cdn-cgi/challenge-platform/",
    "cf_chl_opt",
    "just a moment",
    "verifying you are human",
    "enable javascript and cookies to continue",
    "checking if the site connection is secure",
)


class RenderOutcome(StrEnum):
    """Why a detail-page render ended the way it did.

    The distinction that matters is OK vs NO_TRACKLIST vs INTERSTITIAL_PERSISTED. Collapsing the
    last two -- the shape this engine exists to prevent -- would let a flaky challenge (retryable,
    the set probably IS on 1001Tracklists) be cached as a negative (never re-queried) by the drain
    in phaze-fq9h.7. At the measured ~2/8 first-attempt failure rate that is a large, silent, and
    permanent hole in the corpus.
    """

    OK = "ok"
    """The track container rendered. `RenderResult.html` is worth parsing."""

    NO_TRACKLIST = "no_tracklist"
    """The page rendered fine and simply has no track container -- a real, cacheable negative."""

    INTERSTITIAL_PERSISTED = "interstitial_persisted"
    """Every attempt was served the Turnstile challenge. Retryable later; NOT a negative."""

    TIMEOUT = "timeout"
    """The hard per-page wall clock expired. Retryable; says nothing about the page's content."""

    NAVIGATION_FAILED = "navigation_failed"
    """The browser could not load the URL at all (DNS, TLS, connection reset). Retryable."""


@dataclass(frozen=True, slots=True)
class RenderResult:
    """One detail page's render, as handed to the parser (phaze-fq9h.4)."""

    url: str
    outcome: RenderOutcome
    html: str
    attempts: int
    elapsed_seconds: float
    error: str | None = None
    attempt_outcomes: tuple[RenderOutcome, ...] = ()
    """Per-navigation outcomes, oldest first.

    Kept because the aggregate hides the number that actually governs this engine's cost: how
    often Turnstile fires on the FIRST navigation, and how often a reload clears it. phaze-hu8v
    asks exactly that question, and `outcome` alone cannot answer it -- a page that cleared on
    attempt 3 and one that cleared instantly are both a bare `OK`. Empty for renders that never
    completed a navigation (timeout on launch, navigation failure).
    """

    @property
    def is_usable(self) -> bool:
        """True when `html` is worth handing to the parser."""
        return self.outcome is RenderOutcome.OK

    @property
    def is_retryable(self) -> bool:
        """True when the failure says nothing about whether a tracklist exists.

        The drain (phaze-fq9h.7) must NOT write a negative-cache entry for these -- only
        NO_TRACKLIST is a genuine "this set is not on 1001Tracklists" answer.
        """
        return self.outcome in {
            RenderOutcome.INTERSTITIAL_PERSISTED,
            RenderOutcome.TIMEOUT,
            RenderOutcome.NAVIGATION_FAILED,
        }


class XvfbError(RuntimeError):
    """Raised when a virtual display was required for the headful browser but could not start."""


class RenderPage(Protocol):
    """The subset of a Playwright/Patchright `Page` this engine uses.

    Structural, not nominal, so tests can substitute a fake browser without importing patchright
    and without a live Chrome -- the acceptance criterion "tests mock the browser boundary". This
    is that boundary, stated once.
    """

    async def goto(self, url: str, *, wait_until: str, timeout: float) -> Any: ...

    async def reload(self, *, wait_until: str, timeout: float) -> Any: ...

    async def wait_for_selector(self, selector: str, *, timeout: float, state: str) -> Any: ...

    async def content(self) -> str: ...


class RenderContext(Protocol):
    """The subset of a Playwright/Patchright `BrowserContext` this engine uses."""

    async def new_page(self) -> RenderPage: ...

    async def close(self) -> None: ...


class RenderBrowser(Protocol):
    """The subset of a Playwright/Patchright `Browser` this engine uses."""

    async def new_context(self, **kwargs: Any) -> RenderContext: ...

    async def close(self) -> None: ...


class BrowserLauncher(Protocol):
    """Owns the browser process lifetime, so `TracklistRenderer` never imports patchright.

    `launch()` must be idempotent-friendly (the renderer calls it once per batch and caches the
    handle); `shutdown()` must tolerate being called without a preceding `launch()`.
    """

    async def launch(self) -> RenderBrowser: ...

    async def shutdown(self) -> None: ...

    @property
    def user_agent(self) -> str | None:
        """The UA string contexts should present, or None to leave the browser's own untouched."""


@lru_cache(maxsize=1)
def _browser_error_types() -> tuple[type[BaseException], type[BaseException]]:
    """Return `(TimeoutError, Error)` from patchright, imported lazily.

    Lazy because `patchright.async_api` costs ~0.5s to import and most phaze processes
    (api, agent, every non-render worker) never render a page. Cached because the cost should be
    paid at most once per process.
    """
    from patchright.async_api import Error as PatchrightError, TimeoutError as PatchrightTimeoutError  # noqa: PLC0415

    return (PatchrightTimeoutError, PatchrightError)


def _is_browser_timeout(exc: BaseException) -> bool:
    """True for a Playwright/Patchright navigation-or-selector timeout."""
    return isinstance(exc, _browser_error_types()[0])


def _is_browser_error(exc: BaseException) -> bool:
    """True for any Playwright/Patchright driver error."""
    return isinstance(exc, _browser_error_types()[1])


def looks_like_interstitial(html: str) -> bool:
    """True when `html` is Cloudflare's challenge document rather than a 1001Tracklists page.

    Only meaningful when the track container is ABSENT -- see `_INTERSTITIAL_MARKERS` for why a
    successful detail page also carries Turnstile markup.
    """
    lowered = html.lower()
    return any(marker in lowered for marker in _INTERSTITIAL_MARKERS)


def _xvfb_required(mode: str, *, platform: str, environ: dict[str, str] | None = None) -> bool:
    """Decide whether to start a virtual display.

    `auto` -- the default -- means "only where a headful browser would otherwise have no
    screen": Linux with no DISPLAY already exported. That leaves a developer's macOS laptop
    (real screen) and a Linux desktop session (real X) alone, and covers the worker container,
    which is the only place the answer is non-obvious.

    Module-level rather than a method on `XvfbDisplay` (repowise low_cohesion finding,
    phaze-bk9el.5): it is a pure classification over `(mode, platform, environ)` -- it never reads
    or writes an `XvfbDisplay` instance's own state, which is exactly the LCOM4 split the finding
    named. Exposed as `XvfbDisplay.is_required` for API-compatible external callers.
    """
    env = os.environ if environ is None else environ
    if mode == "always":
        return True
    if mode == "never":
        return False
    return platform.startswith("linux") and not env.get("DISPLAY")


def _terminate_process(process: Any) -> None:
    """Best-effort stop of a spawned Xvfb process.

    Module-level for the same reason as `_xvfb_required` above: it operates entirely on the
    `process` handle passed in, touching no `XvfbDisplay` field, so it was the other half of that
    class's LCOM4=2 split.
    """
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:  # pragma: no cover - defensive
        logger.warning("Failed to terminate Xvfb cleanly", exc_info=True)


class XvfbDisplay:
    """A virtual X display for the headful browser on a headless worker (phaze-fq9h.1).

    The browser MUST be headful -- headless fails Turnstile outright (phaze-dmvs) -- so a
    container with no X server needs one supplied. Rather than mutating `os.environ["DISPLAY"]`
    process-wide (a side effect every other coroutine in the worker would inherit), the display
    name is handed to the browser launch through its own `env`, so nothing outside this browser
    sees it.

    `spawn` is injected so the whole path is testable without an X server present.
    """

    #: Display numbers are probed upward from here. 99 is the conventional Xvfb base and is far
    #: above the 0-9 a real seat or an ssh -X forward would occupy.
    BASE_DISPLAY = 99
    MAX_DISPLAY_PROBES = 20
    #: The geometry the spike ran at. A too-small screen makes Turnstile's widget render in a
    #: layout no real user would have, which is exactly the kind of tell this engine avoids.
    GEOMETRY = "1920x1080x24"
    STARTUP_TIMEOUT_SECONDS = 10.0
    STARTUP_POLL_SECONDS = 0.1

    def __init__(
        self,
        *,
        spawn: Any = subprocess.Popen,
        socket_dir: Path | None = None,
        which: Any = shutil.which,
    ) -> None:
        self._spawn = spawn
        self._socket_dir = socket_dir if socket_dir is not None else Path("/tmp/.X11-unix")  # noqa: S108  # nosec B108
        self._which = which
        self._process: Any = None
        self._display: str | None = None

    @property
    def display(self) -> str | None:
        """The `:N` display name once started, else None."""
        return self._display

    #: Kept as `XvfbDisplay.is_required(...)` for callers/tests -- see `_xvfb_required` above for
    #: why the implementation itself lives at module scope.
    is_required = staticmethod(_xvfb_required)

    async def start(self) -> str:
        """Start Xvfb and return its display name.

        Raises:
            XvfbError: the Xvfb binary is missing, or no probed display number came up within
                STARTUP_TIMEOUT_SECONDS. Raised rather than silently falling back to headless --
                headless does not clear Turnstile, so a "fallback" would render every page an
                INTERSTITIAL_PERSISTED and look like a site-side block instead of a missing
                package in the image (phaze-fq9h.5).
        """
        binary = self._which("Xvfb")
        if binary is None:
            raise XvfbError("Xvfb is not installed but a virtual display is required for the headful render browser (see phaze-fq9h.5)")

        for offset in range(self.MAX_DISPLAY_PROBES):
            number = self.BASE_DISPLAY + offset
            if (self._socket_dir / f"X{number}").exists():
                continue
            process = self._spawn(
                [binary, f":{number}", "-screen", "0", self.GEOMETRY, "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if await self._await_socket(number, process):
                self._process = process
                self._display = f":{number}"
                logger.info("Started Xvfb virtual display for the render browser", display=self._display)
                return self._display
            _terminate_process(process)

        raise XvfbError(f"Could not start Xvfb on any display in :{self.BASE_DISPLAY}-:{self.BASE_DISPLAY + self.MAX_DISPLAY_PROBES - 1}")

    async def _await_socket(self, number: int, process: Any) -> bool:
        """Poll for Xvfb's unix socket, giving up if the process dies or the timeout expires."""
        socket_path = self._socket_dir / f"X{number}"
        deadline = asyncio.get_running_loop().time() + self.STARTUP_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            if socket_path.exists():
                return True
            if process.poll() is not None:
                # Almost always "server already active for display N" -- another worker won the
                # race for this number. Probe the next one rather than failing the render.
                return False
            await asyncio.sleep(self.STARTUP_POLL_SECONDS)
        return False

    def stop(self) -> None:
        """Stop the display, if one is running. Safe to call more than once."""
        if self._process is not None:
            _terminate_process(self._process)
            self._process = None
        self._display = None


class PatchrightLauncher:
    """Launches a HEADFUL Patchright Chrome, under Xvfb where there is no screen.

    Everything patchright-specific in this module lives here, so `TracklistRenderer` is testable
    against the `RenderBrowser` protocol alone.
    """

    def __init__(self, settings: BaseSettings | None = None, *, display: XvfbDisplay | None = None) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._display = display if display is not None else XvfbDisplay()
        self._playwright: Any = None
        self._browser: RenderBrowser | None = None
        self._user_agent: str | None = None

    @property
    def user_agent(self) -> str | None:
        """The honest UA to present, derived from the real browser's own at launch."""
        return self._user_agent

    async def launch(self) -> RenderBrowser:
        """Start the browser (once) and return it. Subsequent calls reuse the same process.

        Atomic on failure (phaze-ccm02): everything after the early-return guard runs inside a
        try/except that rolls back through `shutdown()` before re-raising. Without this, an
        exception from `chromium.launch()` (a misconfigured browser channel, an Xvfb hiccup) that
        lands AFTER `self._display.start()` and `async_playwright().start()` have already
        succeeded left `self._browser` at `None` while the just-started Xvfb process and
        Playwright Node driver were never terminated -- `TracklistRenderer.render()` swallows a
        browser error into a retryable `NAVIGATION_FAILED` outcome without calling `shutdown()`
        itself, so the next candidate re-entered this body and overwrote (orphaned) both handles.
        On a deterministically-failing environment that leaked one Xvfb + one Node driver process
        per candidate for the rest of the drain slice. `shutdown()` is idempotent and safe to call
        against any partial state, including "nothing started yet".
        """
        if self._browser is not None:
            return self._browser

        from patchright.async_api import async_playwright  # noqa: PLC0415

        try:
            env = dict(os.environ)
            if XvfbDisplay.is_required(self._settings.tracklist_render_xvfb, platform=sys.platform):
                env["DISPLAY"] = await self._display.start()

            self._playwright = await async_playwright().start()
            channel = self._settings.tracklist_render_browser_channel.strip()
            # headless=False is NOT a preference -- headless Patchright does not clear Turnstile
            # (phaze-dmvs). Never "optimize" this to True for CI; CI never renders (tests mock the
            # browser boundary), and a headless worker gets its screen from Xvfb above.
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                channel=channel or None,
                env=env,
            )
            self._user_agent = await self._derive_user_agent()
        except BaseException:
            # `BaseException`, not `Exception`, is deliberate (phaze-ccm02): this must also roll
            # back on `asyncio.CancelledError` -- itself a `BaseException` since 3.11 -- or a
            # cancelled launch leaves the just-started Xvfb process and Node driver orphaned, one
            # per candidate, exactly as it did before this handler existed. `raise` below means
            # nothing is swallowed; every exception, including cancellation, still propagates.
            await self.shutdown()
            raise
        return self._browser

    async def _derive_user_agent(self) -> str | None:
        """Return the browser's own UA with phaze's identifying token appended.

        The politeness rule (phaze-hu8v) is that 1001Tracklists must be able to tell who we are
        and reach us. The stealth rule (phaze-dmvs) is that the client must not misrepresent what
        it is. REPLACING the UA -- the way the httpx scraper does, where there is no browser to
        contradict it -- would break the second: a Chrome that claims not to be Chrome is
        inconsistent with every other signal the page can read, and inconsistency is the thing
        Turnstile scores. Appending satisfies both: the UA stays a truthful Chrome UA and gains
        "phaze/<version> (+<contact>)", the conventional identifying suffix for a declared bot.

        Read from an `about:blank` page, so it costs no request against the host budget. On any
        failure we return None and leave the browser's UA untouched -- an unannounced render is
        better than a broken one, and the failure is logged.
        """
        if self._browser is None:  # pragma: no cover - unreachable via launch()
            return None
        try:
            context = await self._browser.new_context()
            try:
                # `evaluate` is outside the RenderPage protocol on purpose -- the render loop
                # never scripts the page, and this one launch-time probe should not widen the
                # boundary every fake in the test suite has to implement.
                page = cast("Any", await context.new_page())
                base = await page.evaluate("navigator.userAgent")
            finally:
                await context.close()
        except Exception:
            logger.warning("Could not read the browser's own User-Agent; rendering with it unmodified", exc_info=True)
            return None
        return f"{base} {honest_user_agent_token()}"

    async def shutdown(self) -> None:
        """Close the browser and the driver, and stop the virtual display. Idempotent.

        Both `except Exception` blocks below are deliberately broad and non-fatal: this is a
        teardown path called from `launch()`'s own rollback (above) and from `TracklistRenderer`'s
        `__aexit__`, so a failure closing one handle must never raise over a failure closing the
        other, or prevent `self._display.stop()` from still running -- it is logged and swallowed
        instead, and the caller's own exception (if any) is what actually propagates.
        """
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                logger.warning("Failed to close the render browser cleanly", exc_info=True)
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                logger.warning("Failed to stop the patchright driver cleanly", exc_info=True)
            self._playwright = None
        self._display.stop()


class TracklistRenderer:
    """Renders 1001Tracklists detail pages, one browser reused across a batch.

    Lifetime model, and why it is split this way:

      * ONE BROWSER PER BATCH. Launching Chrome costs ~1-2s and a few hundred MB; the drain
        renders thousands of pages over months, so the process is created once per batch and
        reused (`async with TracklistRenderer() as renderer: ...`).
      * ONE CONTEXT PER PAGE, ALWAYS CLOSED. Each page gets a fresh context in a try/finally, so
        one page's cookies, storage and any half-finished challenge state cannot leak into the
        next -- and a page that times out or throws still releases its context rather than
        accumulating them inside the long-lived browser until it is OOM-killed.
      * RETRIES REUSE THE PAGE. A Turnstile retry is `page.reload()`, not a new context: the
        clearance cookie the challenge may already have set is exactly what the retry is hoping
        to build on. Throwing the context away between attempts would restart the challenge from
        zero every time.
    """

    def __init__(
        self,
        *,
        launcher: BrowserLauncher | None = None,
        settings: BaseSettings | None = None,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._launcher = launcher if launcher is not None else PatchrightLauncher(self._settings)
        self._browser: RenderBrowser | None = None

    async def __aenter__(self) -> Self:
        """Enter the batch scope. The browser starts lazily on the first render."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Tear the browser down at the end of the batch."""
        await self.aclose()

    async def aclose(self) -> None:
        """Shut the browser down. Idempotent."""
        self._browser = None
        await self._launcher.shutdown()

    async def _browser_handle(self) -> RenderBrowser:
        """Return the batch's browser, launching it on first use."""
        if self._browser is None:
            self._browser = await self._launcher.launch()
        return self._browser

    async def render_batch(self, urls: Sequence[str]) -> list[RenderResult]:
        """Render each URL in order, reusing one browser.

        Sequential on purpose: the crawl-delay budget is per HOST, so concurrent pages would spend
        the same budget faster without finishing the batch any sooner -- they would just queue on
        `reserve_host_request_slot` while holding N browser contexts open.
        """
        return [await self.render(url) for url in urls]

    async def render(self, url: str) -> RenderResult:
        """Render one detail page and return its HTML plus the outcome that produced it.

        Raises:
            DisallowedScrapeHostError: `url` is not an https 1001Tracklists URL. The same
                allow-list the httpx scraper enforces (phaze-k5zz) applies here, and matters more:
                a browser told to visit an attacker-chosen URL executes its JavaScript, with the
                worker's network position and this browser profile's cookies.
        """
        # Deliberately reusing the scraper's allow-list rather than restating it -- one host
        # policy, one place to change it.
        if not TracklistScraper._is_allowed_url(url):
            logger.warning("Refusing to render disallowed URL", url=url)
            raise DisallowedScrapeHostError(url)

        loop = asyncio.get_running_loop()
        started = loop.time()
        context: RenderContext | None = None
        html = ""
        # Mutated in place by _attempt_loop so a hard timeout mid-loop still reports the attempts
        # that DID complete -- a timeout that says "0 attempts" when three navigations were served
        # an interstitial hides the very signal an operator needs to tell a wedged browser from a
        # site that is refusing us.
        attempt_outcomes: list[RenderOutcome] = []
        outcome = RenderOutcome.INTERSTITIAL_PERSISTED
        error: str | None = None

        try:
            async with asyncio.timeout(self._settings.tracklist_render_page_timeout_seconds) as hard_deadline:
                browser = await self._browser_handle()
                context = await browser.new_context(**self._context_options())
                page = await context.new_page()
                outcome, html = await self._attempt_loop(page, url, hard_deadline, attempt_outcomes)
        except TimeoutError:
            outcome = RenderOutcome.TIMEOUT
            error = f"hard per-page timeout of {self._settings.tracklist_render_page_timeout_seconds}s expired"
            logger.warning("Detail-page render hit the hard timeout", url=url, attempts=len(attempt_outcomes))
        except Exception as exc:
            # ONLY driver errors become an outcome. Anything else is a bug in phaze, and a bug
            # that arrives at the caller as a tidy `NAVIGATION_FAILED` is a bug nobody ever finds.
            if not _is_browser_error(exc):
                raise
            outcome = RenderOutcome.NAVIGATION_FAILED
            error = str(exc)
            logger.warning("Detail-page render failed to navigate", url=url, attempts=len(attempt_outcomes), error=error)
        finally:
            if context is not None:
                await _close_context(context)

        result = RenderResult(
            url=url,
            outcome=outcome,
            html=html,
            attempts=len(attempt_outcomes),
            elapsed_seconds=loop.time() - started,
            error=error,
            attempt_outcomes=tuple(attempt_outcomes),
        )
        logger.info(
            "Rendered 1001Tracklists detail page",
            url=url,
            outcome=result.outcome.value,
            attempts=result.attempts,
            attempt_outcomes=[o.value for o in result.attempt_outcomes],
            elapsed_seconds=round(result.elapsed_seconds, 2),
            html_bytes=len(result.html),
        )
        return result

    def _context_options(self) -> dict[str, Any]:
        """Keyword arguments for `browser.new_context`.

        Intentionally minimal. Every knob added here (viewport overrides, locale spoofing, extra
        headers) is another chance to state something the browser itself contradicts, which is
        what Turnstile scores -- and several of them would be UA/fingerprint randomization, which
        the epic's ethics bound forbids outright.
        """
        user_agent = self._launcher.user_agent
        return {"user_agent": user_agent} if user_agent else {}

    async def _attempt_loop(
        self,
        page: RenderPage,
        url: str,
        hard_deadline: asyncio.Timeout,
        attempt_outcomes: list[RenderOutcome],
    ) -> tuple[RenderOutcome, str]:
        """Navigate, then reload with backoff while Turnstile keeps interposing.

        Appends each navigation's verdict to `attempt_outcomes` (the caller's list, so a hard
        timeout mid-loop still sees the completed attempts) and returns the final outcome with the
        last HTML seen. Bounded by `tracklist_render_turnstile_attempts` because every attempt is
        a real request against a shared 8s/request host budget: an unbounded retry would be both a
        hang and a rudeness.
        """
        max_attempts = self._settings.tracklist_render_turnstile_attempts
        selector_timeout_ms = self._settings.tracklist_render_selector_timeout_seconds * 1000
        navigation_timeout_ms = self._settings.tracklist_render_page_timeout_seconds * 1000
        html = ""

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                await _wait(self._backoff_seconds(attempt), hard_deadline)
            await _paced(hard_deadline)

            if attempt == 1:
                await page.goto(url, wait_until="domcontentloaded", timeout=navigation_timeout_ms)
            else:
                await page.reload(wait_until="domcontentloaded", timeout=navigation_timeout_ms)

            container_found = await _wait_for_container(page, selector_timeout_ms)
            html = await page.content()
            outcome = _classify(html, container_found=container_found)
            attempt_outcomes.append(outcome)

            if outcome is not RenderOutcome.INTERSTITIAL_PERSISTED:
                return outcome, html

            logger.info(
                "Turnstile interstitial served; retrying with reload",
                url=url,
                attempt=attempt,
                max_attempts=max_attempts,
            )

        logger.warning("Turnstile interstitial persisted through every attempt", url=url, attempts=max_attempts)
        return RenderOutcome.INTERSTITIAL_PERSISTED, html

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff before retry `attempt` (attempt 2 is the first retry).

        On TOP of the crawl-delay pacing, never instead of it. Turnstile hardening is usually
        transient, so backing off further each time gives it room to relax without turning a
        bounded retry into a long stall -- the hard per-page timeout still caps the total.
        """
        base = self._settings.tracklist_render_retry_backoff_seconds
        return float(base * (2 ** (attempt - 2))) if base > 0 else 0.0


# ---------------------------------------------------------------------------------------------
# Module-level render primitives.
#
# These five were `TracklistRenderer` staticmethods until phaze-bk9el.5: a repowise low_cohesion
# finding (LCOM4=2) split the class's 15 methods into exactly two groups -- the ones reading
# `self._settings` / `self._launcher` / `self._browser`, and these, which take only a page,
# HTML, a context or a deadline and never touch instance state. Moving the second group to module
# scope (unchanged bodies, `self` never referenced) makes that split literal instead of a metric,
# and every one of `TracklistRenderer`'s remaining methods now shares its instance state.
# `TracklistRenderer._paced` / `._wait` are exercised directly by
# tests/identify/services/test_tracklist_render.py; they moved with everything else, so those
# call sites now read `_paced(...)` / `_wait(...)` off this module instead.
# ---------------------------------------------------------------------------------------------


def _classify(html: str, *, container_found: bool) -> RenderOutcome:
    """Turn one attempt's page state into an outcome.

    The container check comes FIRST and wins outright: a cleared detail page still carries
    Turnstile widget markup, so consulting the markers first would misreport successful
    renders as blocked. Only when there is no container does the challenge-page marker set
    decide between "we never got in" (retryable) and "this set has no tracklist" (a real,
    cacheable negative).
    """
    if container_found:
        return RenderOutcome.OK
    if looks_like_interstitial(html):
        return RenderOutcome.INTERSTITIAL_PERSISTED
    return RenderOutcome.NO_TRACKLIST


async def _wait_for_container(page: RenderPage, timeout_ms: float) -> bool:
    """Wait for the track container; False if it never appeared within `timeout_ms`.

    A timeout here is an ordinary, expected result (interstitial, or a page with no
    tracklist), not an error -- which is why it is converted to a bool instead of propagating.

    The `except Exception` below stays broad rather than naming a Playwright/Patchright timeout
    type directly: those types are only reachable through the lazy `_browser_error_types()` import
    (patchright costs ~0.5s to import and most phaze processes never render a page), so the
    handler filters programmatically via `_is_browser_timeout` and re-raises anything it does not
    recognize -- narrowing the `except` clause itself would force the same eager import this
    module exists to avoid.
    """
    try:
        await page.wait_for_selector(TRACK_CONTAINER_SELECTOR, timeout=timeout_ms, state="attached")
    except Exception as exc:
        if _is_browser_timeout(exc):
            return False
        raise
    return True


async def _wait(seconds: float, hard_deadline: asyncio.Timeout) -> None:
    """Sleep, then push the hard deadline out by the time slept.

    Waiting politely is correct behavior, not a hang, so it must not eat the budget that
    exists to catch a wedged browser. Without the reschedule, a page whose 4 attempts each
    wait out an 8-12s crawl delay would consume ~40s of a 90s hang budget doing exactly what
    robots.txt asked of it.
    """
    if seconds <= 0:
        return
    await asyncio.sleep(seconds)
    _extend_deadline(hard_deadline, seconds)


async def _paced(hard_deadline: asyncio.Timeout) -> None:
    """Take a slot from the shared whole-host schedule before a navigation.

    `reserve_host_request_slot` is the SAME schedule `TracklistScraper` draws from: the
    crawl-delay is a per-host budget, so a search fetch and a browser navigation compete for
    one ceiling. See its docstring for why this is not a second, independent limiter.
    """
    loop = asyncio.get_running_loop()
    before = loop.time()
    await reserve_host_request_slot()
    _extend_deadline(hard_deadline, loop.time() - before)


async def _close_context(context: RenderContext) -> None:
    """Close a context, never letting cleanup failure mask the render's own outcome.

    Broad `except Exception` by design, matching `TracklistRenderer.shutdown()`'s own two cleanup
    blocks: a context that fails to close cleanly is logged, not raised, so it can never mask the
    render outcome this call's `finally` block is protecting.
    """
    try:
        await context.close()
    except Exception:
        logger.warning("Failed to close the render context cleanly", exc_info=True)


def _extend_deadline(hard_deadline: asyncio.Timeout, seconds: float) -> None:
    """Push an `asyncio.timeout` deadline out by `seconds`, if it has one."""
    when = hard_deadline.when()
    if when is not None and seconds > 0:
        hard_deadline.reschedule(when + seconds)
