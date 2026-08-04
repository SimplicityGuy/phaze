"""Tests for the Patchright detail-page render engine (phaze-fq9h.1).

NO LIVE REQUEST IS MADE ANYWHERE IN THIS MODULE, and no browser is launched. Every case drives
`TracklistRenderer` through fake objects satisfying the `RenderBrowser` / `RenderContext` /
`RenderPage` protocols -- that boundary exists so the retry logic, the outcome classification and
the cleanup guarantees are testable without Chrome, an X server, or a request against a host whose
whole-system budget is one request per 8 seconds.

The HTML fixtures under `tests/identify/fixtures/tracklist_render/` ARE real captures (see
`scripts/capture_tracklist_render.py`), recorded once so that neither this suite nor the
downstream parser has to spend a host request again.
"""

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from patchright.async_api import Error as BrowserError, TimeoutError as BrowserTimeoutError
import pytest

from phaze.config import get_settings
from phaze.services.tracklist_render import (
    TRACK_CONTAINER_SELECTOR,
    PatchrightLauncher,
    RenderOutcome,
    RenderResult,
    TracklistRenderer,
    XvfbDisplay,
    XvfbError,
    looks_like_interstitial,
)
from phaze.services.tracklist_scraper import DisallowedScrapeHostError, honest_user_agent_token


FIXTURES = Path(__file__).parent.parent / "fixtures" / "tracklist_render"

ANCHOR_URL = "https://www.1001tracklists.com/tracklist/25fhn7c9/sven-vath-time-warp-maimarkthalle-mannheim-germany-2024-10-25.html"
OTHER_URL = "https://www.1001tracklists.com/tracklist/rm6xzck/sven-vath-time-warp-germany-2021-10-30.html"

# A page that cleared: it HAS the track container and ALSO carries Turnstile widget markup, which
# is the shape that makes marker-first classification wrong (see `_INTERSTITIAL_MARKERS`).
TRACKLIST_HTML = """
<html><head><title>Sven Vath @ Time Warp</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></head>
<body><div class="tlpTog bItm tlpItem trRow1">track one</div>
<div class="tlpTog bItm tlpItem trRow2">track two</div></body></html>
"""

# Cloudflare's challenge document: no track container, unambiguous challenge markers.
INTERSTITIAL_HTML = """
<html><head><title>Just a moment...</title></head><body>
<div id="cf-please-wait">Verifying you are human</div>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>
</body></html>
"""

# A real 1001Tracklists page that simply has no listing -- rendered fine, nothing to parse.
EMPTY_HTML = "<html><head><title>Sven Vath @ Somewhere</title></head><body><div class='tlpTBIcon'></div></body></html>"


# --- Fakes for the browser boundary ---


class FakePage:
    """A `RenderPage` whose per-navigation HTML is scripted by the test."""

    def __init__(self, pages: list[str], *, goto_error: Exception | None = None, navigation_delay: float = 0.0) -> None:
        self._pages = pages
        self._goto_error = goto_error
        self._navigation_delay = navigation_delay
        self.goto_calls: list[str] = []
        self.reload_calls = 0
        self.selector_waits: list[str] = []
        self._index = -1

    async def goto(self, url: str, *, wait_until: str, timeout: float) -> None:
        """Record a first navigation and advance to the first scripted page."""
        assert wait_until == "domcontentloaded"
        assert timeout > 0
        if self._navigation_delay:
            await asyncio.sleep(self._navigation_delay)
        if self._goto_error is not None:
            raise self._goto_error
        self.goto_calls.append(url)
        self._index += 1

    async def reload(self, *, wait_until: str, timeout: float) -> None:
        """Record a retry navigation and advance to the next scripted page."""
        assert wait_until == "domcontentloaded"
        assert timeout > 0
        self.reload_calls += 1
        self._index += 1

    async def wait_for_selector(self, selector: str, *, timeout: float, state: str) -> object:
        """Succeed only if the current scripted page contains the selector's class."""
        assert state == "attached"
        assert timeout > 0
        self.selector_waits.append(selector)
        if "tlpItem" in self._current():
            return object()
        raise BrowserTimeoutError(f"Timeout {timeout}ms exceeded waiting for {selector}")

    async def content(self) -> str:
        """Return the current scripted page's HTML."""
        return self._current()

    def _current(self) -> str:
        """The HTML for the navigation we are on, clamping past the end of the script."""
        return self._pages[min(self._index, len(self._pages) - 1)]


class FakeContext:
    """A `RenderContext` that hands out one `FakePage` and records its own closure."""

    def __init__(self, page: FakePage, *, options: dict[str, Any]) -> None:
        self.page = page
        self.options = options
        self.closed = 0

    async def new_page(self) -> FakePage:
        """Return this context's single page."""
        return self.page

    async def close(self) -> None:
        """Record the close so tests can assert the always-close guarantee."""
        self.closed += 1


class FakeBrowser:
    """A `RenderBrowser` handing out one context per `new_context` call."""

    def __init__(self, pages_per_render: list[list[str]], **page_kwargs: Any) -> None:
        self._pages_per_render = pages_per_render
        self._page_kwargs = page_kwargs
        self.contexts: list[FakeContext] = []
        self.closed = 0

    async def new_context(self, **kwargs: Any) -> FakeContext:
        """Create a fresh context, scripted with the next render's page sequence."""
        index = min(len(self.contexts), len(self._pages_per_render) - 1)
        context = FakeContext(FakePage(self._pages_per_render[index], **self._page_kwargs), options=kwargs)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        """Record browser shutdown."""
        self.closed += 1


class FakeLauncher:
    """A `BrowserLauncher` that counts launches, so browser reuse is observable."""

    def __init__(self, browser: FakeBrowser, *, user_agent: str | None = None) -> None:
        self.browser = browser
        self.launches = 0
        self.shutdowns = 0
        self._user_agent = user_agent

    async def launch(self) -> FakeBrowser:
        """Hand back the one browser, counting the call."""
        self.launches += 1
        return self.browser

    async def shutdown(self) -> None:
        """Count the shutdown."""
        self.shutdowns += 1

    @property
    def user_agent(self) -> str | None:
        """The UA the renderer should put on each context."""
        return self._user_agent


def _settings(**overrides: Any) -> Any:
    """A settings object with render knobs tuned for fast, deterministic tests.

    Backoff is zeroed so the retry loop does not really sleep; the crawl-delay pacing is patched
    out separately by the `no_host_pacing` autouse fixture. Nothing here weakens the logic under
    test -- both are wall-clock, not behavior.
    """
    base = {
        "tracklist_render_turnstile_attempts": 3,
        "tracklist_render_page_timeout_seconds": 30.0,
        "tracklist_render_selector_timeout_seconds": 1.0,
        "tracklist_render_retry_backoff_seconds": 0.0,
        "tracklist_render_xvfb": "never",
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


@pytest.fixture(autouse=True)
def no_host_pacing():
    """Replace the shared crawl-delay reservation with a no-op for the whole module.

    The real one sleeps 8-12s per navigation by design (phaze-hu8v). Tests that care about the
    pacing assert on the mock's call count instead -- which is the stronger check anyway, since
    "was a slot taken for every navigation" is the invariant, not "did we sleep".
    """
    with patch("phaze.services.tracklist_render.reserve_host_request_slot", new_callable=AsyncMock) as paced:
        yield paced


# --- Outcome classification ---


class TestRenderOutcomes:
    """The three outcomes a rendered page can have, and why they must stay distinct."""

    async def test_track_container_yields_ok(self):
        """A page with the track container renders OK on the first navigation."""
        browser = FakeBrowser([[TRACKLIST_HTML]])
        launcher = FakeLauncher(browser)

        async with TracklistRenderer(launcher=launcher, settings=_settings()) as renderer:
            result = await renderer.render(ANCHOR_URL)

        assert result.outcome is RenderOutcome.OK
        assert result.attempts == 1
        assert result.attempt_outcomes == (RenderOutcome.OK,)
        assert result.is_usable
        assert not result.is_retryable
        assert "tlpItem" in result.html

    async def test_page_without_tracklist_is_a_real_negative(self):
        """No container and no challenge markers is NO_TRACKLIST -- cacheable, not retryable."""
        browser = FakeBrowser([[EMPTY_HTML]])

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            result = await renderer.render(ANCHOR_URL)

        assert result.outcome is RenderOutcome.NO_TRACKLIST
        assert result.attempts == 1
        assert not result.is_retryable
        assert not result.is_usable

    async def test_interstitial_every_attempt_is_distinct_from_no_tracklist(self):
        """A challenge that never clears reports INTERSTITIAL_PERSISTED, bounded by the cap.

        This is the whole point of the engine: collapsing this into NO_TRACKLIST would let the
        drain cache a retryable block as "this set is not on 1001Tracklists", permanently.
        """
        browser = FakeBrowser([[INTERSTITIAL_HTML] * 5])

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            result = await renderer.render(ANCHOR_URL)

        assert result.outcome is RenderOutcome.INTERSTITIAL_PERSISTED
        assert result.outcome is not RenderOutcome.NO_TRACKLIST
        assert result.attempts == 3  # the configured cap, not unbounded
        assert result.attempt_outcomes == (RenderOutcome.INTERSTITIAL_PERSISTED,) * 3
        assert result.is_retryable
        assert not result.is_usable

    async def test_retry_loop_clears_a_flaky_interstitial(self):
        """An interstitial followed by a good page clears on reload -- the measured ~6/8 case."""
        browser = FakeBrowser([[INTERSTITIAL_HTML, TRACKLIST_HTML]])

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            result = await renderer.render(ANCHOR_URL)

        assert result.outcome is RenderOutcome.OK
        assert result.attempts == 2
        assert result.attempt_outcomes == (RenderOutcome.INTERSTITIAL_PERSISTED, RenderOutcome.OK)
        page = browser.contexts[0].page
        # The retry RELOADS the same page rather than navigating a fresh one, so any clearance
        # cookie the challenge already set survives into the next attempt.
        assert page.goto_calls == [ANCHOR_URL]
        assert page.reload_calls == 1

    async def test_container_wins_over_challenge_markers(self):
        """A cleared detail page carries Turnstile markup; it must still classify as OK.

        `TRACKLIST_HTML` embeds the Turnstile widget script exactly like the live page does. If
        classification consulted the markers before the container, every successful render would
        be reported as blocked.
        """
        assert "turnstile" in TRACKLIST_HTML.lower()
        browser = FakeBrowser([[TRACKLIST_HTML]])

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            result = await renderer.render(ANCHOR_URL)

        assert result.outcome is RenderOutcome.OK

    async def test_navigation_error_is_reported_as_navigation_failed(self):
        """A driver-level navigation error is retryable and says nothing about the content."""
        browser = FakeBrowser([[TRACKLIST_HTML]], goto_error=BrowserError("net::ERR_CONNECTION_RESET"))

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            result = await renderer.render(ANCHOR_URL)

        assert result.outcome is RenderOutcome.NAVIGATION_FAILED
        assert result.error is not None
        assert "ERR_CONNECTION_RESET" in result.error
        assert result.is_retryable
        assert browser.contexts[0].closed == 1

    async def test_unexpected_exception_is_not_swallowed(self):
        """A non-browser bug propagates instead of masquerading as a render outcome."""
        browser = FakeBrowser([[TRACKLIST_HTML]], goto_error=ValueError("bug in phaze, not in the browser"))

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            with pytest.raises(ValueError, match="bug in phaze"):
                await renderer.render(ANCHOR_URL)

        assert browser.contexts[0].closed == 1

    async def test_selector_error_that_is_not_a_timeout_propagates(self):
        """Only a selector TIMEOUT means "no container"; other driver errors are real failures."""
        browser = FakeBrowser([[TRACKLIST_HTML]])

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            page = None

            async def exploding_wait(*_args: Any, **_kwargs: Any) -> object:
                raise BrowserError("Target page, context or browser has been closed")

            original_new_context = browser.new_context

            async def patched_new_context(**kwargs: Any) -> FakeContext:
                nonlocal page
                context = await original_new_context(**kwargs)
                page = context.page
                page.wait_for_selector = exploding_wait  # type: ignore[method-assign]
                return context

            browser.new_context = patched_new_context  # type: ignore[method-assign]
            result = await renderer.render(ANCHOR_URL)

        assert result.outcome is RenderOutcome.NAVIGATION_FAILED


class TestLooksLikeInterstitial:
    """The marker set, checked directly."""

    @pytest.mark.parametrize(
        "html",
        [
            "<title>Just a moment...</title>",
            "<div>Verifying you are human</div>",
            "<script src='/cdn-cgi/challenge-platform/h/b/x'></script>",
            "<script>window._cf_chl_opt={};</script>",
            "<p>Enable JavaScript and cookies to continue</p>",
        ],
    )
    def test_challenge_documents_are_recognized(self, html: str):
        """Each marker identifies Cloudflare's challenge document."""
        assert looks_like_interstitial(html)

    @pytest.mark.parametrize(
        "html",
        [
            TRACKLIST_HTML,
            EMPTY_HTML,
            "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>",
            "<div class='cf-turnstile'></div>",
        ],
    )
    def test_widget_markup_alone_is_not_a_challenge(self, html: str):
        """The embedded Turnstile WIDGET appears on pages we were let through -- not a marker."""
        assert not looks_like_interstitial(html)


# --- Lifetime, cleanup and pacing ---


class TestBrowserLifetime:
    """One browser per batch, one context per page, always closed."""

    async def test_one_browser_is_reused_across_a_batch(self):
        """`render_batch` launches the browser once for N pages, with a fresh context each."""
        browser = FakeBrowser([[TRACKLIST_HTML]])
        launcher = FakeLauncher(browser)

        async with TracklistRenderer(launcher=launcher, settings=_settings()) as renderer:
            results = await renderer.render_batch([ANCHOR_URL, OTHER_URL, ANCHOR_URL])

        assert [r.outcome for r in results] == [RenderOutcome.OK] * 3
        assert launcher.launches == 1
        assert len(browser.contexts) == 3
        assert all(context.closed == 1 for context in browser.contexts)

    async def test_context_is_closed_on_success(self):
        """The happy path still releases its context."""
        browser = FakeBrowser([[TRACKLIST_HTML]])

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            await renderer.render(ANCHOR_URL)

        assert browser.contexts[0].closed == 1

    async def test_context_close_failure_does_not_mask_the_result(self):
        """A cleanup failure is logged, not raised -- the render's own outcome survives."""
        browser = FakeBrowser([[TRACKLIST_HTML]])

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            original = browser.new_context

            async def failing_close_context(**kwargs: Any) -> FakeContext:
                context = await original(**kwargs)

                async def boom() -> None:
                    raise BrowserError("context already closed")

                context.close = boom  # type: ignore[method-assign]
                return context

            browser.new_context = failing_close_context  # type: ignore[method-assign]
            result = await renderer.render(ANCHOR_URL)

        assert result.outcome is RenderOutcome.OK

    async def test_aexit_shuts_the_browser_down(self):
        """Leaving the batch scope shuts the launcher down exactly once."""
        launcher = FakeLauncher(FakeBrowser([[TRACKLIST_HTML]]))

        async with TracklistRenderer(launcher=launcher, settings=_settings()) as renderer:
            await renderer.render(ANCHOR_URL)

        assert launcher.shutdowns == 1

    async def test_aclose_is_idempotent_and_relaunches_after(self):
        """`aclose` can be called twice, and a later render relaunches rather than using a stale handle."""
        launcher = FakeLauncher(FakeBrowser([[TRACKLIST_HTML]]))
        renderer = TracklistRenderer(launcher=launcher, settings=_settings())

        await renderer.render(ANCHOR_URL)
        await renderer.aclose()
        await renderer.aclose()
        await renderer.render(ANCHOR_URL)
        await renderer.aclose()

        assert launcher.launches == 2
        assert launcher.shutdowns == 3

    async def test_hard_timeout_reports_timeout_and_still_closes_the_context(self):
        """A wedged page hits the hard wall clock, is reported as TIMEOUT, and releases its context."""
        browser = FakeBrowser([[TRACKLIST_HTML]], navigation_delay=5.0)
        settings = _settings(tracklist_render_page_timeout_seconds=0.05)

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=settings) as renderer:
            result = await renderer.render(ANCHOR_URL)

        assert result.outcome is RenderOutcome.TIMEOUT
        assert result.is_retryable
        assert result.error is not None
        assert "0.05s" in result.error
        assert browser.contexts[0].closed == 1

    async def test_disallowed_url_never_reaches_the_browser(self):
        """The SSRF allow-list is enforced before any browser is launched.

        A browser pointed at an attacker-chosen URL executes its JavaScript from the worker's
        network position -- strictly worse than the httpx scraper's version of this bug.
        """
        launcher = FakeLauncher(FakeBrowser([[TRACKLIST_HTML]]))

        async with TracklistRenderer(launcher=launcher, settings=_settings()) as renderer:
            with pytest.raises(DisallowedScrapeHostError):
                await renderer.render("https://evil.example.com/tracklist/abc/x.html")
            with pytest.raises(DisallowedScrapeHostError):
                await renderer.render("http://www.1001tracklists.com/tracklist/abc/x.html")

        assert launcher.launches == 0


class TestHostPacing:
    """Every navigation takes a slot from the SHARED whole-host schedule."""

    async def test_a_slot_is_taken_for_every_navigation(self, no_host_pacing):
        """Three interstitial attempts spend three slots -- retries are requests too."""
        browser = FakeBrowser([[INTERSTITIAL_HTML] * 4])

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            result = await renderer.render(ANCHOR_URL)

        assert result.attempts == 3
        assert no_host_pacing.await_count == 3

    async def test_batch_paces_every_page(self, no_host_pacing):
        """A batch of three pages takes three slots, one per page's single navigation."""
        browser = FakeBrowser([[TRACKLIST_HTML]])

        async with TracklistRenderer(launcher=FakeLauncher(browser), settings=_settings()) as renderer:
            await renderer.render_batch([ANCHOR_URL, OTHER_URL, ANCHOR_URL])

        assert no_host_pacing.await_count == 3

    async def test_pacing_wait_does_not_consume_the_hang_budget(self, no_host_pacing):
        """Time spent being polite extends the hard deadline instead of eating it.

        Without this, a page whose attempts each wait out an 8-12s crawl delay would spend most
        of its hang budget doing exactly what robots.txt asked, and time out for being well
        behaved (phaze-fq9h.1).
        """

        async def slow_slot() -> None:
            await asyncio.sleep(0.2)

        no_host_pacing.side_effect = slow_slot

        async with asyncio.timeout(10.0) as deadline:
            before = deadline.when()
            await TracklistRenderer._paced(deadline)
            after = deadline.when()

        assert before is not None
        assert after is not None
        assert after - before >= 0.15

    async def test_backoff_grows_and_extends_the_deadline(self):
        """Retry backoff is exponential from the configured base, and also extends the deadline."""
        renderer = TracklistRenderer(
            launcher=FakeLauncher(FakeBrowser([[TRACKLIST_HTML]])), settings=_settings(tracklist_render_retry_backoff_seconds=5.0)
        )

        assert renderer._backoff_seconds(2) == 5.0
        assert renderer._backoff_seconds(3) == 10.0
        assert renderer._backoff_seconds(4) == 20.0

        zero = TracklistRenderer(launcher=FakeLauncher(FakeBrowser([[TRACKLIST_HTML]])), settings=_settings())
        assert zero._backoff_seconds(2) == 0.0

        async with asyncio.timeout(10.0) as deadline:
            before = deadline.when()
            await TracklistRenderer._wait(0.2, deadline)
            after = deadline.when()
            await TracklistRenderer._wait(0.0, deadline)
            unchanged = deadline.when()

        assert before is not None
        assert after is not None
        assert after - before >= 0.15
        assert unchanged == after


class TestContextOptions:
    """What the renderer tells the browser about itself."""

    async def test_honest_user_agent_is_applied_to_the_context(self):
        """The launcher's UA is passed through to every context it creates."""
        browser = FakeBrowser([[TRACKLIST_HTML]])
        launcher = FakeLauncher(browser, user_agent="Mozilla/5.0 Chrome/1.2.3 phaze/9.9 (+https://example.invalid)")

        async with TracklistRenderer(launcher=launcher, settings=_settings()) as renderer:
            await renderer.render(ANCHOR_URL)

        assert browser.contexts[0].options == {"user_agent": launcher.user_agent}

    async def test_no_user_agent_leaves_the_browser_untouched(self):
        """With no UA available, no context options are set at all -- never a spoofed default."""
        browser = FakeBrowser([[TRACKLIST_HTML]])

        async with TracklistRenderer(launcher=FakeLauncher(browser, user_agent=None), settings=_settings()) as renderer:
            await renderer.render(ANCHOR_URL)

        assert browser.contexts[0].options == {}


# --- Xvfb ---


class FakeProcess:
    """A stand-in for an Xvfb `subprocess.Popen`."""

    def __init__(self, *, exits_with: int | None = None) -> None:
        self._exits_with = exits_with
        self.terminated = 0
        self.waited = 0

    def poll(self) -> int | None:
        """Return the exit code if this fake process is scripted to die immediately."""
        return self._exits_with

    def terminate(self) -> None:
        """Record termination."""
        self.terminated += 1

    def wait(self, timeout: float | None = None) -> int:
        """Record the reap."""
        assert timeout is None or timeout > 0
        self.waited += 1
        return 0


class TestXvfbRequired:
    """When a virtual display is needed at all."""

    @pytest.mark.parametrize(
        ("mode", "platform", "environ", "expected"),
        [
            ("auto", "linux", {}, True),
            ("auto", "linux", {"DISPLAY": ":0"}, False),
            ("auto", "darwin", {}, False),
            ("always", "darwin", {"DISPLAY": ":0"}, True),
            ("never", "linux", {}, False),
        ],
    )
    def test_decision_matrix(self, mode: str, platform: str, environ: dict[str, str], expected: bool):
        """`auto` means "Linux with no screen" -- the headless-worker case and nothing else."""
        assert XvfbDisplay.is_required(mode, platform=platform, environ=environ) is expected

    def test_auto_reads_the_real_environment_by_default(self):
        """Omitting `environ` consults `os.environ` rather than assuming an empty one."""
        with patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False):
            assert XvfbDisplay.is_required("auto", platform="linux") is False


class TestXvfbDisplay:
    """Starting, probing and stopping the virtual display."""

    async def test_missing_binary_raises_instead_of_falling_back_to_headless(self, tmp_path: Path):
        """No Xvfb is a hard error.

        Falling back to headless would turn a packaging bug (phaze-fq9h.5) into what looks like a
        site-side block: headless never clears Turnstile, so every page would report
        INTERSTITIAL_PERSISTED and the real cause would be invisible.
        """
        display = XvfbDisplay(socket_dir=tmp_path, which=lambda _name: None)

        with pytest.raises(XvfbError, match="Xvfb is not installed"):
            await display.start()

    async def test_starts_on_the_first_free_display(self, tmp_path: Path):
        """The first probed number whose socket appears becomes DISPLAY."""
        process = FakeProcess()
        commands: list[list[str]] = []

        def spawn(command: list[str], **_kwargs: Any) -> FakeProcess:
            commands.append(command)
            (tmp_path / f"X{XvfbDisplay.BASE_DISPLAY}").touch()
            return process

        display = XvfbDisplay(socket_dir=tmp_path, which=lambda _name: "/usr/bin/Xvfb", spawn=spawn)

        assert await display.start() == f":{XvfbDisplay.BASE_DISPLAY}"
        assert display.display == f":{XvfbDisplay.BASE_DISPLAY}"
        assert commands[0][:2] == ["/usr/bin/Xvfb", f":{XvfbDisplay.BASE_DISPLAY}"]
        assert XvfbDisplay.GEOMETRY in commands[0]

        display.stop()
        assert process.terminated == 1
        assert display.display is None
        display.stop()  # idempotent
        assert process.terminated == 1

    async def test_occupied_display_numbers_are_skipped(self, tmp_path: Path):
        """An existing socket means another worker owns that number; probe upward.

        Two render workers on one host is a normal deployment, not an error.
        """
        (tmp_path / f"X{XvfbDisplay.BASE_DISPLAY}").touch()

        def spawn(command: list[str], **_kwargs: Any) -> FakeProcess:
            (tmp_path / f"X{XvfbDisplay.BASE_DISPLAY + 1}").touch()
            assert command[1] == f":{XvfbDisplay.BASE_DISPLAY + 1}"
            return FakeProcess()

        display = XvfbDisplay(socket_dir=tmp_path, which=lambda _name: "/usr/bin/Xvfb", spawn=spawn)

        assert await display.start() == f":{XvfbDisplay.BASE_DISPLAY + 1}"

    async def test_a_dead_xvfb_moves_on_to_the_next_number(self, tmp_path: Path):
        """If Xvfb exits immediately (lost the race for a number), try the next one."""
        attempts: list[str] = []

        def spawn(command: list[str], **_kwargs: Any) -> FakeProcess:
            attempts.append(command[1])
            if len(attempts) == 1:
                return FakeProcess(exits_with=1)
            (tmp_path / f"X{XvfbDisplay.BASE_DISPLAY + 1}").touch()
            return FakeProcess()

        display = XvfbDisplay(socket_dir=tmp_path, which=lambda _name: "/usr/bin/Xvfb", spawn=spawn)

        assert await display.start() == f":{XvfbDisplay.BASE_DISPLAY + 1}"
        assert attempts == [f":{XvfbDisplay.BASE_DISPLAY}", f":{XvfbDisplay.BASE_DISPLAY + 1}"]

    async def test_exhausting_every_probe_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """When no display ever comes up, say so instead of launching a doomed browser."""
        monkeypatch.setattr(XvfbDisplay, "MAX_DISPLAY_PROBES", 2)
        monkeypatch.setattr(XvfbDisplay, "STARTUP_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(XvfbDisplay, "STARTUP_POLL_SECONDS", 0.01)

        display = XvfbDisplay(socket_dir=tmp_path, which=lambda _name: "/usr/bin/Xvfb", spawn=lambda *_a, **_k: FakeProcess())

        with pytest.raises(XvfbError, match="Could not start Xvfb"):
            await display.start()

    def test_terminate_failure_is_swallowed(self, tmp_path: Path):
        """A stubborn Xvfb must not turn shutdown into an exception."""

        class StubbornProcess(FakeProcess):
            def terminate(self) -> None:
                raise OSError("no such process")

        display = XvfbDisplay(socket_dir=tmp_path)
        display._process = StubbornProcess()
        display.stop()

        assert display.display is None


# --- PatchrightLauncher (no browser launched: the patchright objects are faked) ---


class TestPatchrightLauncherUserAgent:
    """The honest-UA derivation, which is where politeness and fingerprint consistency meet."""

    class _UAPage:
        def __init__(self, ua: str) -> None:
            self._ua = ua

        async def evaluate(self, script: str) -> str:
            assert script == "navigator.userAgent"
            return self._ua

    class _UAContext:
        def __init__(self, ua: str) -> None:
            self._ua = ua
            self.closed = 0

        async def new_page(self) -> "TestPatchrightLauncherUserAgent._UAPage":
            return TestPatchrightLauncherUserAgent._UAPage(self._ua)

        async def close(self) -> None:
            self.closed += 1

    class _UABrowser:
        def __init__(self, ua: str) -> None:
            self.ua = ua
            self.context: Any = None

        async def new_context(self, **_kwargs: Any) -> "TestPatchrightLauncherUserAgent._UAContext":
            self.context = TestPatchrightLauncherUserAgent._UAContext(self.ua)
            return self.context

        async def close(self) -> None:
            pass

    async def test_identifying_token_is_appended_to_the_real_chrome_ua(self):
        """phaze's token is APPENDED, never substituted for the browser's own UA.

        Replacing it would make a real Chrome claim not to be Chrome -- an inconsistency
        Turnstile scores -- while appending keeps the UA truthful AND identifies us (phaze-hu8v).
        """
        chrome_ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
        launcher = PatchrightLauncher(_settings())
        launcher._browser = self._UABrowser(chrome_ua)

        derived = await launcher._derive_user_agent()

        assert derived == f"{chrome_ua} {honest_user_agent_token()}"
        assert derived is not None
        assert derived.startswith(chrome_ua)
        assert "phaze/" in derived
        assert launcher._browser.context.closed == 1

    async def test_ua_probe_failure_leaves_the_browser_ua_alone(self):
        """If the probe fails we render with the browser's own UA rather than not at all."""

        class BrokenBrowser:
            async def new_context(self, **_kwargs: Any) -> Any:
                raise BrowserError("browser closed")

            async def close(self) -> None:
                pass

        launcher = PatchrightLauncher(_settings())
        launcher._browser = BrokenBrowser()

        assert await launcher._derive_user_agent() is None

    async def test_launch_returns_the_cached_browser(self):
        """A second `launch()` reuses the running browser instead of starting a second Chrome."""
        launcher = PatchrightLauncher(_settings())
        browser = self._UABrowser("Chrome/1")
        launcher._browser = browser

        assert await launcher.launch() is browser
        assert launcher.user_agent is None


class TestPatchrightLauncherLaunch:
    """The launch call itself, with `async_playwright` faked -- no Chrome, no X server.

    Worth covering despite the fakes: the two things asserted here (never headless, DISPLAY
    threaded through `env` rather than `os.environ`) are exactly the invariants that would be
    quietly "optimized" away by someone making CI faster, and both would fail silently in
    production -- headless renders every page as an interstitial.
    """

    class _FakeChromium:
        def __init__(self, browser: Any) -> None:
            self.browser = browser
            self.launch_kwargs: dict[str, Any] = {}

        async def launch(self, **kwargs: Any) -> Any:
            self.launch_kwargs = kwargs
            return self.browser

    class _FakeDriver:
        def __init__(self, chromium: Any) -> None:
            self.chromium = chromium
            self.stopped = 0

        async def stop(self) -> None:
            self.stopped += 1

    def _patched_playwright(self, browser: Any) -> tuple[Any, Any]:
        """Build a fake `async_playwright()` returning a driver whose chromium yields `browser`."""
        chromium = self._FakeChromium(browser)
        driver = self._FakeDriver(chromium)

        class _Starter:
            async def start(self) -> Any:
                return driver

        return (lambda: _Starter()), chromium

    async def test_launch_is_headful_and_uses_the_configured_channel(self):
        """Patchright is launched HEADFUL on the configured channel; headless never clears Turnstile."""
        browser = TestPatchrightLauncherUserAgent._UABrowser("Mozilla/5.0 Chrome/141.0.0.0 Safari/537.36")
        async_playwright, chromium = self._patched_playwright(browser)
        launcher = PatchrightLauncher(_settings(tracklist_render_browser_channel="chrome"))

        with patch("patchright.async_api.async_playwright", async_playwright):
            launched = await launcher.launch()

        assert launched is browser
        assert chromium.launch_kwargs["headless"] is False
        assert chromium.launch_kwargs["channel"] == "chrome"
        assert launcher.user_agent is not None
        assert launcher.user_agent.startswith("Mozilla/5.0 Chrome/141.0.0.0 Safari/537.36 ")

    async def test_empty_channel_falls_back_to_the_bundled_chromium(self):
        """An empty channel setting means None (Patchright's own patched Chromium), not ''."""
        browser = TestPatchrightLauncherUserAgent._UABrowser("Chrome/1")
        async_playwright, chromium = self._patched_playwright(browser)
        launcher = PatchrightLauncher(_settings(tracklist_render_browser_channel="  "))

        with patch("patchright.async_api.async_playwright", async_playwright):
            await launcher.launch()

        assert chromium.launch_kwargs["channel"] is None

    async def test_xvfb_display_is_passed_through_env_not_os_environ(self):
        """The virtual display reaches the browser via its own `env`, leaving the process alone.

        Mutating `os.environ["DISPLAY"]` would export the render browser's screen to every other
        coroutine in the worker -- a process-wide side effect for a per-browser need.
        """
        browser = TestPatchrightLauncherUserAgent._UABrowser("Chrome/1")
        async_playwright, chromium = self._patched_playwright(browser)

        class FakeDisplay:
            def __init__(self) -> None:
                self.stopped = 0

            async def start(self) -> str:
                return ":99"

            def stop(self) -> None:
                self.stopped += 1

        display = FakeDisplay()
        launcher = PatchrightLauncher(_settings(tracklist_render_xvfb="always"), display=display)  # type: ignore[arg-type]

        with patch("patchright.async_api.async_playwright", async_playwright):
            await launcher.launch()

        assert chromium.launch_kwargs["env"]["DISPLAY"] == ":99"
        assert "DISPLAY" not in os.environ or os.environ["DISPLAY"] != ":99"

        await launcher.shutdown()
        assert display.stopped == 1

    async def test_shutdown_is_idempotent_and_tolerates_failures(self):
        """Shutdown closes what exists, logs what refuses, and can run with nothing to close."""

        class FailingBrowser:
            async def close(self) -> None:
                raise BrowserError("already gone")

        class FailingDriver:
            async def stop(self) -> None:
                raise BrowserError("driver already stopped")

        launcher = PatchrightLauncher(_settings())
        await launcher.shutdown()  # nothing started yet

        launcher._browser = FailingBrowser()
        launcher._playwright = FailingDriver()
        await launcher.shutdown()

        assert launcher._browser is None
        assert launcher._playwright is None


# --- Recorded live captures ---


class TestRecordedCaptures:
    """Regression cover from real bytes, so a marker/selector change is caught without a request."""

    def test_anchor_capture_carries_the_52_track_listing(self):
        """The committed anchor capture is a real cleared render with the spike's 52 rows.

        The count is the acceptance criterion's number, asserted against the actual recorded
        page rather than a synthetic fixture that could drift from reality unnoticed.
        """
        html = (FIXTURES / "25fhn7c9-ok.html").read_text(encoding="utf-8")
        import re

        assert len(re.findall(r'class="[^"]*\btlpItem\b', html)) == 52
        assert TRACK_CONTAINER_SELECTOR == ".tlpItem"

    def test_anchor_capture_is_not_misread_as_a_challenge(self):
        """The real cleared page mentions Turnstile and must still not trip the marker set."""
        html = (FIXTURES / "25fhn7c9-ok.html").read_text(encoding="utf-8")

        assert "turnstile" in html.lower()
        assert not looks_like_interstitial(html)


class TestRenderResultSemantics:
    """The contract the drain (phaze-fq9h.7) reads."""

    @pytest.mark.parametrize(
        ("outcome", "usable", "retryable"),
        [
            (RenderOutcome.OK, True, False),
            (RenderOutcome.NO_TRACKLIST, False, False),
            (RenderOutcome.INTERSTITIAL_PERSISTED, False, True),
            (RenderOutcome.TIMEOUT, False, True),
            (RenderOutcome.NAVIGATION_FAILED, False, True),
        ],
    )
    def test_only_no_tracklist_is_a_cacheable_negative(self, outcome: RenderOutcome, usable: bool, retryable: bool):
        """NO_TRACKLIST is the ONLY outcome that means "this set is not on 1001Tracklists"."""
        result = RenderResult(url=ANCHOR_URL, outcome=outcome, html="", attempts=1, elapsed_seconds=0.1)

        assert result.is_usable is usable
        assert result.is_retryable is retryable
