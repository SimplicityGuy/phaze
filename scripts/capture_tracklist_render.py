"""Live-capture 1001Tracklists detail pages through the render engine (phaze-fq9h.1).

TWO JOBS, BOTH OF WHICH EXIST TO STOP US SPENDING HOST REQUESTS TWICE.

1. RECORD FIXTURES. Downstream beads -- the detail parser (phaze-fq9h.4) and result scoring
   (phaze-fq9h.6) -- need real rendered HTML to develop against. Every request to
   1001tracklists.com costs 8s of a whole-host budget that is the binding constraint on the entire
   drain, so the honest way to build a parser is against recorded bytes, not by re-fetching. What
   this script writes into ``tests/identify/fixtures/tracklist_render/`` IS that record.

2. MEASURE TURNSTILE. phaze-hu8v asks whether Turnstile still fires now that we pace at the
   published crawl-delay with an honest User-Agent. That question can only be answered by looking,
   and looking costs requests, so the observation is done once, deliberately, and written down --
   ``--trials N`` renders the same page N times and reports the raw per-attempt outcomes.

RUN IT MANUALLY, FROM A RESIDENTIAL IP, ONE PROCESS AT A TIME. 1001Tracklists' FAQ says its
anti-scraping is IP-range blocking (datacenter/VPN/TOR), so a run from a cloud host measures the
block, not the site. Never wire this into CI: the test suite mocks the browser boundary precisely
so that CI makes no live requests.

Usage::

    uv run python scripts/capture_tracklist_render.py --trials 6
    uv run python scripts/capture_tracklist_render.py --url <detail-url> --max-attempts 1 --out /tmp/cap
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import time

from phaze.config import get_settings
from phaze.services.tracklist_render import RenderOutcome, RenderResult, TracklistRenderer


# The spike's ground-truth page: Sven Väth @ Time Warp, Maimarkthalle, 2024-10-25, whose
# og:description states 52 tracks. Public URL of a public event -- no local-archive identifier is
# involved, so it is safe to commit under the repo's no-local-identifiers convention.
ANCHOR_URL = "https://www.1001tracklists.com/tracklist/25fhn7c9/sven-vath-time-warp-maimarkthalle-mannheim-germany-2024-10-25.html"

DEFAULT_OUT = Path("tests/identify/fixtures/tracklist_render")


@dataclass
class TrialRecord:
    """One render of one URL, reduced to what the write-up needs."""

    trial: int
    url: str
    outcome: str
    attempts: int
    attempt_outcomes: list[str]
    elapsed_seconds: float
    html_bytes: int
    track_container_count: int
    error: str | None


def _count_containers(html: str) -> int:
    """Count track-row elements without importing the parser.

    A deliberately dumb regex over class attributes: this script must not depend on
    phaze-fq9h.4's selectors, and the number is only a sanity signal in the report ("did we get
    the anchor's 52 rows?"). Matching inside `class="..."` rather than counting bare occurrences
    of the token, because the live page also mentions `tlpItem` once in script/CSS -- a bare count
    reports 53 for a 52-track page.
    """
    return len(re.findall(r'class="[^"]*\btlpItem\b', html))


def _external_id(url: str) -> str:
    """Return the 1001Tracklists short id from a detail URL, for naming captures."""
    match = re.search(r"/tracklist/([^/?#]+)", url)
    return match.group(1) if match else "unknown"


def _write_capture(out_dir: Path, name: str, result: RenderResult) -> Path:
    """Write one render's HTML next to a small JSON sidecar describing its provenance."""
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{name}.html"
    html_path.write_text(result.html, encoding="utf-8")
    (out_dir / f"{name}.json").write_text(
        json.dumps(
            {
                "url": result.url,
                "outcome": result.outcome.value,
                "attempts": result.attempts,
                "attempt_outcomes": [o.value for o in result.attempt_outcomes],
                "html_bytes": len(result.html),
                "track_container_count": _count_containers(result.html),
                "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "bead": "phaze-fq9h.1",
            },
            indent=2,
            # Key-sorted so the repo's pretty-format-json pre-commit hook does not rewrite every
            # sidecar the moment it is captured.
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return html_path


async def _run(args: argparse.Namespace) -> int:
    """Render `--url` `--trials` times through one browser and report what Turnstile did."""
    settings = get_settings()
    if args.max_attempts is not None:
        settings.tracklist_render_turnstile_attempts = args.max_attempts

    out_dir = Path(args.out)
    urls: list[str] = args.url or [ANCHOR_URL]
    records: list[TrialRecord] = []

    # ONE renderer for the whole run -- one browser process shared by every trial. That is the
    # acceptance criterion ("one browser reused across a batch") and it is also what makes the
    # Turnstile counts below meaningful: a fresh browser per trial would measure cold-start
    # clearance, not the steady state a months-long drain actually runs in.
    async with TracklistRenderer(settings=settings) as renderer:
        for trial in range(1, args.trials + 1):
            for url in urls:
                result = await renderer.render(url)
                record = TrialRecord(
                    trial=trial,
                    url=url,
                    outcome=result.outcome.value,
                    attempts=result.attempts,
                    attempt_outcomes=[o.value for o in result.attempt_outcomes],
                    elapsed_seconds=round(result.elapsed_seconds, 2),
                    html_bytes=len(result.html),
                    track_container_count=_count_containers(result.html),
                    error=result.error,
                )
                records.append(record)
                print(json.dumps(record.__dict__), flush=True)  # noqa: T201

                # Keep the first capture of each (page, outcome). Later trials are for the counts,
                # not more bytes -- a second copy of the same page costs disk and reveals nothing.
                name = f"{_external_id(url)}-{result.outcome.value}"
                if result.html and not (out_dir / f"{name}.html").exists():
                    print(f"wrote {_write_capture(out_dir, name, result)}", flush=True)  # noqa: T201

    _report(records, max_attempts=settings.tracklist_render_turnstile_attempts)
    return 0 if any(r.outcome == RenderOutcome.OK.value for r in records) else 1


def _report(records: list[TrialRecord], *, max_attempts: int) -> None:
    """Print the raw Turnstile counts phaze-hu8v asked for.

    Counts, never rates or adjectives: "2 of 14 renders were served an interstitial on the first
    navigation" is checkable and stays true; "Turnstile rarely fires" is neither.
    """
    navigations = sum(r.attempts for r in records)
    print(  # noqa: T201
        json.dumps(
            {
                "renders": len(records),
                "distinct_urls": len({r.url for r in records}),
                "navigations": navigations,
                "first_navigation_cleared": sum(1 for r in records if r.attempt_outcomes[:1] == [RenderOutcome.OK.value]),
                "first_navigation_interstitial": sum(1 for r in records if r.attempt_outcomes[:1] == [RenderOutcome.INTERSTITIAL_PERSISTED.value]),
                "cleared_after_retry": sum(1 for r in records if r.outcome == RenderOutcome.OK.value and r.attempts > 1),
                "outcome_counts": {outcome: sum(1 for r in records if r.outcome == outcome) for outcome in sorted({r.outcome for r in records})},
                "max_attempts": max_attempts,
            },
            indent=2,
        ),
        flush=True,
    )


def main() -> int:
    """Parse arguments and run the capture."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", action="append", help="detail page to render; repeatable (defaults to the spike anchor)")
    parser.add_argument("--trials", type=int, default=1, help="how many times to render EACH url")
    parser.add_argument("--max-attempts", type=int, default=None, help="override the bounded Turnstile retry cap for this run")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="directory to write captured HTML into")
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
