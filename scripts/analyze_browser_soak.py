"""Real-browser verification of the Analyze workspace at 200K scale (Phase 95, phaze-zqvh.5).

Standalone ``uv run --with playwright`` companion to ``scripts/perf_analyze_workspace.py``
(server-side numbers, phaze-zqvh.1/.2/.4). That script measures the server render + payload; this
one drives a REAL headless Chromium against a LIVE ``uvicorn`` process (started separately, pointed
at the perf DB) to close the phaze-zqvh.1 baseline's one measured gap: browser time-to-interactive,
JS heap, and a >=30-minute soak, plus explicit behavior-preservation + residual-size interaction
checks (phaze-zqvh.5 acceptance criteria).

Read-only: issues only GET/navigation traffic against the already-running app -- no writes, no
seeding, no product code touched.

INCREMENTAL-WRITE DISCIPLINE (a prior run of this script silently produced zero output over a
31-minute soak -- everything was buffered in memory and only written by ``main()`` after
``main_async`` returned, so a mid-run crash or hang lost the entire run). This version instead:

* Prints every soak sample immediately with ``flush=True`` (also run with ``PYTHONUNBUFFERED=1``
  to defeat pipe buffering when stdout is redirected to a log file).
* Appends each soak sample as one JSON line to ``--out`` (or ``--out`` + ``.jsonl`` if ``--out``
  ends in ``.json``) as it is taken, opening/writing/flushing/closing the file per sample -- so
  everything up to the last completed sample is on disk even if the process is killed mid-soak.
* Wraps the whole run in a top-level try/except that prints the traceback to stdout (flushed)
  before exiting nonzero, so a failure is never silent.
* The full structured report (open + behavior + residual-size + soak summary) is STILL written to
  ``--out`` at the end for convenient single-file consumption, but that final write is a
  convenience, not the durable record -- the ``.jsonl`` sample log is.

Usage::

    uv run --with playwright playwright install chromium   # once
    uv run --with playwright python scripts/analyze_browser_soak.py \\
        --base-url http://127.0.0.1:8123 --soak-minutes 30 --sample-interval-seconds 120 \\
        --out /tmp/analyze_browser_soak_report.json

Smoke-test first with ``--soak-minutes 2`` before trusting a long run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import traceback
from typing import TYPE_CHECKING, Any

from playwright.async_api import Page, async_playwright


if TYPE_CHECKING:
    from collections.abc import Callable


# The single 5s chrome poll (shell.html) only fires while the tab is visible; headless Chromium
# pages default to visible (Page Visibility API reports "visible" with no explicit backgrounding),
# so the poll fires on its natural cadence with no extra wiring needed here.
_POLL_INTERVAL_S = 5.0


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _print(msg: str) -> None:
    print(msg, flush=True)  # noqa: T201


def _samples_path(out: str | None) -> Path | None:
    """Derive the incremental JSONL sample-log path from --out (or None if --out wasn't given)."""
    if not out:
        return None
    p = Path(out)
    return p.with_suffix(p.suffix + ".jsonl") if p.suffix else p.with_name(p.name + ".jsonl")


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(record, default=str))
        fh.write("\n")
        fh.flush()


async def _cdp_heap_bytes(page: Page) -> float:
    """Read JSHeapUsedSize via CDP Performance.getMetrics (bytes). The Performance domain must be
    explicitly enabled on the session before getMetrics returns anything -- each call opens and
    tears down its own short-lived CDP session (cheap, avoids holding one open across the soak)."""
    cdp = await page.context.new_cdp_session(page)
    try:
        await cdp.send("Performance.enable")
        metrics = await cdp.send("Performance.getMetrics")
    finally:
        await cdp.detach()
    for m in metrics.get("metrics", []):
        if m.get("name") == "JSHeapUsedSize":
            return float(m["value"])
    return float("nan")


async def _install_longtask_collector(page: Page) -> None:
    """Inject a buffered PerformanceObserver counting/summing `longtask` entries.

    Installed via ``add_init_script`` so it is present before any page script runs (survives the
    navigation that follows). Exposes ``window.__soakLongTasks`` (array of {duration, startTime})
    the soak loop drains via ``window.__drainLongTasks()`` each sample.
    """
    await page.add_init_script(
        """
        (() => {
            window.__soakLongTasks = [];
            window.__drainLongTasks = () => {
                const drained = window.__soakLongTasks;
                window.__soakLongTasks = [];
                return drained;
            };
            try {
                const obs = new PerformanceObserver((list) => {
                    for (const entry of list.getEntries()) {
                        window.__soakLongTasks.push({ duration: entry.duration, startTime: entry.startTime });
                    }
                });
                obs.observe({ type: 'longtask', buffered: true });
            } catch (e) {
                window.__longTaskUnsupported = String(e);
            }
        })();
        """
    )


async def _install_poll_counter(page: Page) -> dict[str, Any]:
    """Count /pipeline/stats requests as direct evidence the single 5s poll is firing."""
    state: dict[str, Any] = {"count": 0, "last_ts_ms": None}

    def _on_request(request: Any) -> None:
        if "/pipeline/stats" in request.url:
            state["count"] += 1
            state["last_ts_ms"] = _now_ms()

    page.on("request", _on_request)
    return state


async def measure_open(page: Page, base_url: str) -> dict[str, Any]:
    """OPEN measurement: navigation timing (domContentLoaded / load / first-idle) + JS heap after load."""
    url = f"{base_url}/s/analyze"
    t0 = _now_ms()
    resp = await page.goto(url, wait_until="load")
    load_wall_ms = _now_ms() - t0
    status = resp.status if resp else None

    nav_timing = await page.evaluate(
        """() => {
            const nav = performance.getEntriesByType('navigation')[0];
            if (!nav) return null;
            return {
                startTime: nav.startTime,
                domContentLoadedEventEnd: nav.domContentLoadedEventEnd,
                loadEventEnd: nav.loadEventEnd,
                responseEnd: nav.responseEnd,
                domInteractive: nav.domInteractive,
            };
        }"""
    )

    # "First idle" proxy: time from navigation start to the first requestIdleCallback firing
    # AFTER the load event -- the browser's own signal that the main thread has caught up on
    # queued work (script parse/compile, htmx:load node scan, Alpine directive compilation).
    first_idle_ms = await page.evaluate(
        """() => new Promise((resolve) => {
            const navStart = performance.getEntriesByType('navigation')[0]?.startTime ?? 0;
            const done = () => resolve(performance.now() - navStart);
            if ('requestIdleCallback' in window) {
                requestIdleCallback(done, { timeout: 30000 });
            } else {
                setTimeout(done, 0);
            }
        })"""
    )

    heap_bytes = await _cdp_heap_bytes(page)

    row_count = await page.evaluate("() => document.querySelectorAll('#analyze-file-table tbody tr').length")
    lane_count = await page.evaluate("() => document.querySelectorAll('#analyze-lanes > *').length")

    return {
        "status": status,
        "load_wall_clock_ms": load_wall_ms,
        "navigation_timing": nav_timing,
        "first_idle_after_navstart_ms": first_idle_ms,
        "js_heap_used_bytes_after_load": heap_bytes,
        "rendered_file_row_count": row_count,
        "rendered_lane_card_count": lane_count,
    }


async def _reset_to_default_view(page: Page, base_url: str) -> None:
    """Force the Analyze workspace back to its default bounded working-set view (status='').

    A full re-navigation rather than chasing the "Clear filter" button through htmx swaps -- robust
    against the button/element being replaced mid-locator-resolution across the several fragment
    swaps the filter/pager checks just drove, and cheap enough (~1-2s) to not distort the later
    residual-size / soak measurements, which explicitly re-warm before timing anything.
    """
    await page.goto(f"{base_url}/s/analyze", wait_until="load")


async def check_behavior_preservation(page: Page, poll_state: dict[str, Any], base_url: str) -> dict[str, Any]:
    """Success criterion 4: lane cards update on poll, record drill-in, filter+pager, poll doesn't reset state."""
    results: dict[str, Any] = {}

    # --- lane grid identity across an idle poll tick (phaze-zqvh.3 hash-skip) ---
    await page.evaluate(
        """() => {
            const el = document.getElementById('analyze-lanes');
            if (el) el.__soakProbe = 'unchanged-marker';
        }"""
    )
    before_hash = await page.evaluate("() => document.getElementById('analyze-lanes')?.getAttribute('data-lanes-hash')")
    polls_before = poll_state["count"]
    await page.wait_for_timeout(int(_POLL_INTERVAL_S * 2 * 1000) + 500)  # >= 2 poll ticks
    polls_after = poll_state["count"]
    probe_survived = await page.evaluate("() => document.getElementById('analyze-lanes')?.__soakProbe === 'unchanged-marker'")
    after_hash = await page.evaluate("() => document.getElementById('analyze-lanes')?.getAttribute('data-lanes-hash')")
    results["poll_fired_at_least_once"] = polls_after > polls_before
    results["poll_tick_count_during_2_ticks_wait"] = polls_after - polls_before
    results["lane_grid_node_identity_stable_across_ticks"] = bool(probe_survived)
    results["lane_grid_hash_before"] = before_hash
    results["lane_grid_hash_after"] = after_hash
    results["lane_grid_hash_unchanged"] = before_hash == after_hash

    # --- per-file row drill-in opens /record/{id} into the slide-in panel ---
    row_locator = page.locator("#analyze-file-table tbody tr").first
    await row_locator.click()
    await page.wait_for_selector("#record-body h2", timeout=10_000)
    record_open = await page.evaluate("() => document.querySelector('[role=dialog][aria-modal=true]')?.getAttribute('aria-modal') === 'true'")
    record_body_nonempty = await page.evaluate("() => (document.getElementById('record-body')?.textContent || '').trim().length > 0")
    results["record_drill_in_opens_dialog"] = bool(record_open)
    results["record_body_populated"] = bool(record_body_nonempty)
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)

    # --- windowed-progress State cell: assert the template renders SOME status word for every
    #     visible row (WCAG 1.4.1 -- never hue-only), and specifically look for the windowed
    #     "N/M windows" form this corpus's synthetic seed does not happen to populate (see
    #     95-VERIFICATION.md residual finding) -- recorded, not silently assumed. ---
    state_cell_texts = await page.evaluate(
        "() => Array.from(document.querySelectorAll('#analyze-file-table tbody tr td:last-child')).slice(0, 200).map(td => td.textContent.trim())"
    )
    results["state_cell_sample_first_200"] = state_cell_texts[:10]
    results["state_cell_all_nonempty"] = all(bool(t) for t in state_cell_texts)
    results["state_cell_windowed_form_present_in_sample"] = any("windows" in t or t.startswith("window ") for t in state_cell_texts)

    # --- status filter bar + pagination ---
    await page.select_option("#analyze-filter-status", "failed")
    await page.wait_for_selector("#analyze-filter-bar")
    await page.wait_for_function("() => document.getElementById('analyze-filter-status')?.value === 'failed'")
    await page.wait_for_timeout(600)  # let the hx-get response land
    filtered_row_count = await page.evaluate("() => document.querySelectorAll('#analyze-file-table tbody tr').length")
    results["filter_failed_row_count"] = filtered_row_count

    page_text_before = await page.evaluate("() => document.querySelector('#analyze-files-view nav p')?.textContent || ''")
    next_btn = page.locator("#analyze-files-view nav button", has_text="Next")
    pagination_available = await next_btn.count() > 0
    results["pagination_controls_present"] = pagination_available
    if pagination_available:
        await next_btn.click()
        await page.wait_for_timeout(600)
        page_text_after_next = await page.evaluate("() => document.querySelector('#analyze-files-view nav p')?.textContent || ''")
        results["pagination_next_advanced"] = page_text_after_next != page_text_before and "Page 2" in page_text_after_next

        # a poll tick must NOT reset filter/page (phaze-zqvh.2 acceptance)
        filter_before_tick = await page.evaluate("() => document.getElementById('analyze-filter-status')?.value")
        page_before_tick = await page.evaluate("() => document.querySelector('#analyze-files-view nav p')?.textContent || ''")
        await page.wait_for_timeout(int(_POLL_INTERVAL_S * 1000) + 1000)  # >= 1 poll tick
        filter_after_tick = await page.evaluate("() => document.getElementById('analyze-filter-status')?.value")
        page_after_tick = await page.evaluate("() => document.querySelector('#analyze-files-view nav p')?.textContent || ''")
        results["filter_survives_poll_tick"] = filter_before_tick == filter_after_tick == "failed"
        results["page_position_survives_poll_tick"] = page_before_tick == page_after_tick

        prev_btn = page.locator("#analyze-files-view nav button", has_text="Previous")
        if await prev_btn.count() > 0:
            await prev_btn.click()
            await page.wait_for_timeout(600)
    else:
        results["pagination_next_advanced"] = None
        results["filter_survives_poll_tick"] = None
        results["page_position_survives_poll_tick"] = None

    # Return to the default bounded working-set view for the checks that follow (residual-size
    # interaction + the soak both want the natural ~13K-row default, not the "failed" filter lens).
    await _reset_to_default_view(page, base_url)

    return results


async def check_residual_size_interaction(page: Page, base_url: str) -> dict[str, Any]:
    """Explicit interaction-responsiveness check at the default ~13K-row working-set size."""
    row_count = await page.evaluate("() => document.querySelectorAll('#analyze-file-table tbody tr').length")

    # Scroll responsiveness: time a full-page scroll-to-bottom-and-back via rAF round trips.
    scroll_ms = await page.evaluate(
        """() => new Promise((resolve) => {
            const t0 = performance.now();
            window.scrollTo(0, document.body.scrollHeight);
            requestAnimationFrame(() => {
                window.scrollTo(0, 0);
                requestAnimationFrame(() => resolve(performance.now() - t0));
            });
        })"""
    )

    # Click responsiveness: time from click dispatch to the record slide-in body being populated.
    t0 = _now_ms()
    await page.locator("#analyze-file-table tbody tr").first.click()
    await page.wait_for_selector("#record-body h2", timeout=10_000)
    click_to_record_ms = _now_ms() - t0
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(300)

    # Filter-switch responsiveness at this row count.
    t0 = _now_ms()
    await page.select_option("#analyze-filter-status", "in_flight")
    await page.wait_for_function("() => document.getElementById('analyze-filter-status')?.value === 'in_flight'")
    await page.wait_for_timeout(400)
    filter_switch_ms = _now_ms() - t0
    # restore the default bounded working-set view for the soak that follows
    await _reset_to_default_view(page, base_url)

    return {
        "working_set_row_count": row_count,
        "scroll_round_trip_ms": scroll_ms,
        "click_to_record_open_ms": click_to_record_ms,
        "filter_switch_ms": filter_switch_ms,
    }


async def run_soak(
    page: Page,
    poll_state: dict[str, Any],
    minutes: float,
    interval_s: float,
    on_sample: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Sample JS heap + long-task churn every ``interval_s`` for ``minutes`` (default flow: idle tab, poll running).

    ``on_sample``, if given, is called SYNCHRONOUSLY with each sample as soon as it is taken --
    the caller uses this to print + append-to-disk incrementally so a mid-soak crash still leaves
    every completed sample on disk (see module docstring).
    """
    samples: list[dict[str, Any]] = []
    end_at = time.monotonic() + minutes * 60.0
    sample_idx = 0
    while True:
        heap_bytes = await _cdp_heap_bytes(page)
        drained = await page.evaluate("() => window.__drainLongTasks ? window.__drainLongTasks() : []")
        durations = [float(t["duration"]) for t in drained]
        sample = {
            "sample_index": sample_idx,
            "t_wall_s_since_soak_start": round((minutes * 60.0 - (end_at - time.monotonic())), 1),
            "js_heap_used_bytes": heap_bytes,
            "poll_request_count_cumulative": poll_state["count"],
            "long_task_count_since_last_sample": len(durations),
            "long_task_total_ms_since_last_sample": sum(durations),
            "long_task_max_ms_since_last_sample": max(durations) if durations else 0.0,
        }
        samples.append(sample)
        if on_sample is not None:
            on_sample(sample)
        sample_idx += 1
        remaining = end_at - time.monotonic()
        if remaining <= 0:
            break
        await page.wait_for_timeout(int(min(interval_s, remaining) * 1000))
    return samples


def _summarize_soak(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if len(samples) < 2:
        return {"note": "insufficient samples for trend analysis"}
    heaps = [s["js_heap_used_bytes"] for s in samples]
    first_half = heaps[: len(heaps) // 2] or heaps
    second_half = heaps[len(heaps) // 2 :] or heaps
    mean_first = sum(first_half) / len(first_half)
    mean_second = sum(second_half) / len(second_half)
    growth_ratio = (mean_second / mean_first) if mean_first else float("nan")
    lt_totals = [s["long_task_total_ms_since_last_sample"] for s in samples]
    lt_first = lt_totals[: len(lt_totals) // 2] or lt_totals
    lt_second = lt_totals[len(lt_totals) // 2 :] or lt_totals
    return {
        "sample_count": len(samples),
        "heap_bytes_min": min(heaps),
        "heap_bytes_max": max(heaps),
        "heap_bytes_first_half_mean": mean_first,
        "heap_bytes_second_half_mean": mean_second,
        "heap_growth_ratio_second_over_first_half": growth_ratio,
        "heap_flat_pass": growth_ratio < 1.5,  # generous bound -- GC sawtooth is expected, not a trend
        "long_task_ms_first_half_mean": sum(lt_first) / len(lt_first),
        "long_task_ms_second_half_mean": sum(lt_second) / len(lt_second),
        "long_task_not_growing_pass": (sum(lt_second) / len(lt_second)) <= (sum(lt_first) / len(lt_first)) * 2 + 50,
    }


async def main_async(args: argparse.Namespace, samples_path: Path | None) -> dict[str, Any]:
    report: dict[str, Any] = {"base_url": args.base_url, "soak_minutes": args.soak_minutes}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        await _install_longtask_collector(page)
        poll_state = await _install_poll_counter(page)

        _print("Measuring OPEN (navigation timing + JS heap)...")
        report["open"] = await measure_open(page, args.base_url)
        _print(f"  open: {json.dumps(report['open'], default=str)}")

        _print("Checking behavior preservation...")
        report["behavior_preservation"] = await check_behavior_preservation(page, poll_state, args.base_url)
        _print(f"  behavior_preservation: {json.dumps(report['behavior_preservation'], default=str)}")

        _print("Checking residual-size interaction responsiveness...")
        report["residual_size_interaction"] = await check_residual_size_interaction(page, args.base_url)
        _print(f"  residual_size_interaction: {json.dumps(report['residual_size_interaction'], default=str)}")

        def _on_sample(sample: dict[str, Any]) -> None:
            _print(f"  soak sample: {json.dumps(sample, default=str)}")
            if samples_path is not None:
                _append_jsonl(samples_path, {"kind": "soak_sample", **sample})

        _print(f"Starting {args.soak_minutes}-minute soak (sampling every {args.sample_interval_seconds}s)...")
        soak_samples = await run_soak(page, poll_state, args.soak_minutes, args.sample_interval_seconds, on_sample=_on_sample)
        report["soak_samples"] = soak_samples
        report["soak_summary"] = _summarize_soak(soak_samples)
        report["poll_request_count_total"] = poll_state["count"]
        _print(f"Soak complete. summary: {json.dumps(report['soak_summary'], default=str)}")

        await context.close()
        await browser.close()
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Real-browser verification of the Analyze workspace at 200K scale (phaze-zqvh.5).")
    parser.add_argument("--base-url", default="http://127.0.0.1:8123", help="Base URL of the running dev instance")
    parser.add_argument("--soak-minutes", type=float, default=30.0, help="Soak duration in minutes (default 30)")
    parser.add_argument("--sample-interval-seconds", type=float, default=120.0, help="Soak sample interval in seconds (default 120 = 2min)")
    parser.add_argument("--out", default=None, help="Optional path to write the full JSON report (samples also stream to <out>.jsonl incrementally)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    samples_path = _samples_path(args.out)
    if samples_path is not None:
        # Truncate any stale sample log from a prior run before starting a fresh one.
        samples_path.write_text("")
        _print(f"Streaming soak samples incrementally to {samples_path}")
    try:
        report = asyncio.run(main_async(args, samples_path))
    except Exception:
        _print("FATAL: analyze_browser_soak.py crashed -- traceback follows (samples up to the last logged line, if any, are on disk):")
        traceback.print_exc(file=sys.stdout)
        sys.stdout.flush()
        return 1

    payload = json.dumps(report, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(payload)
        _print(f"Wrote full report to {args.out}")
    _print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
