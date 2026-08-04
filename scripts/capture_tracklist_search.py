"""Live-capture 1001Tracklists SEARCH-RESULT pages (phaze-fq9h.6).

The sibling of ``capture_tracklist_render.py``, and it exists for the same reason: every request
to 1001tracklists.com costs 8 s of a whole-host budget that is the binding constraint on the
entire drain (epic phaze-fq9h), so a request spent must never need re-spending. That script
recorded DETAIL pages for the parser (phaze-fq9h.4); this one records SEARCH pages for the result
scorer (phaze-fq9h.6), which cannot be honestly developed against hand-written HTML -- the whole
point of the bead is that the scorer must beat a *real* plausible-but-wrong top row, and a
fabricated decoy is one the scorer trivially beats by construction.

TWO DIFFERENCES FROM THE DETAIL CAPTURE, both measured rather than assumed:

1. **No browser.** phaze-fq9h.1 measured the search endpoint as NOT Turnstile-gated -- a plain
   HTTP POST to ``search/result.php`` returns the results page. So this script uses the httpx
   path (``TracklistScraper._build_headers()``, the honest identifying UA) and never starts
   Chrome. Detail pages still need the renderer; search does not.
2. **It reuses the scraper's pacing, it does not add its own.** Every request goes through the
   module-level :func:`reserve_host_request_slot`, the same whole-host schedule the scraper and
   the renderer share. A second limiter here would be a second budget, which is exactly the thing
   robots.txt's host-wide ``Crawl-delay: 8`` forbids.

RUN IT MANUALLY, FROM A RESIDENTIAL IP, ONE PROCESS AT A TIME. Never wire it into CI: the test
suite parses the recorded bytes and makes no live requests.

Usage::

    uv run python scripts/capture_tracklist_search.py --list
    uv run python scripts/capture_tracklist_search.py            # capture the planned query set
    uv run python scripts/capture_tracklist_search.py --query "sven vath time warp" --slug adhoc
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import datetime as dt
import json
from pathlib import Path
import sys

from bs4 import BeautifulSoup
import httpx

from phaze.services.tracklist_scraper import TracklistScraper, reserve_host_request_slot


DEFAULT_OUT = Path("tests/identify/fixtures/tracklist_search")


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    """One deliberately-chosen query, and the reason it is worth a host request.

    ``why`` is written into the JSON sidecar. A fixture whose provenance is "somebody searched
    this once" cannot be judged later; a fixture that records what shape it was meant to
    demonstrate can be checked against that claim.
    """

    slug: str
    query: str
    why: str


# The capture budget, spelled out. Chosen before spending anything, so the run is auditable
# against its own plan rather than against a request count nobody committed to in advance.
#
# Every query below is a public search against a public site about public events -- no
# local-archive identifier is involved, so the captures are safe to commit under the repo's
# no-local-identifiers convention.
PLANNED_QUERIES: tuple[PlannedQuery, ...] = (
    PlannedQuery(
        slug="sven-vath-time-warp-2024",
        query="Sven Vath Time Warp 2024",
        why=(
            "THE WRONG-MATCH CASE. Both detail fixtures captured by phaze-fq9h.1 are Sven Vath "
            "'Time Warp' sets from 2024 -- the Maimarkthalle set (2024-10-25) and the BBC Radio 1 "
            "Dance broadcast (2024-10-12). Same artist, same year, event strings where one CONTAINS "
            "the other, 13 days apart. If the top row is the wrong one of those two, no amount of "
            "artist or event similarity separates them; only the date does."
        ),
    ),
    PlannedQuery(
        slug="sven-vath-time-warp",
        query="Sven Vath Time Warp",
        why=(
            "The recurring-festival shape with NO year discriminator. Time Warp runs annually, so "
            "this returns the same artist at the same event across many years -- the case where a "
            "date-blind scorer picks whichever year the site happened to rank first."
        ),
    ),
    PlannedQuery(
        slug="carl-cox-space-ibiza-2016",
        query="Carl Cox Space Ibiza 2016",
        why=(
            "A second artist/residency with a long multi-year run, so the scorer is exercised on "
            "more than one artist's row formatting. Carl Cox's Space residency ran weekly for "
            "years, which makes same-artist same-venue different-date decoys abundant."
        ),
    ),
    PlannedQuery(
        slug="amelie-lens-awakenings",
        query="Amelie Lens Awakenings",
        why=(
            "Recurring festival, no year, different artist -- corroborates that the row selectors "
            "and the multi-year decoy shape are not specific to the Sven Vath rows."
        ),
    ),
    PlannedQuery(
        slug="time-warp-2024",
        query="Time Warp 2024",
        why=(
            "Event-only query: many DIFFERENT artists at the same event on the same date. The "
            "inverse discrimination problem from the queries above -- here event and date match "
            "almost perfectly across every row and only the artist separates them, which is the "
            "case that justifies the artist term carrying the largest weight."
        ),
    ),
    PlannedQuery(
        slug="no-such-set",
        query="Zzyzx Quorum Nonesuch Festival 2019",
        why=(
            "THE BELOW-THRESHOLD CASE. A plausible-looking but invented artist/event pair. Needed "
            "as a real capture and not a hand-written empty page, because 'no confident match' has "
            "to be proven against whatever the site ACTUALLY returns for an unmatchable query -- "
            "which may well be a non-empty page of loosely-related rows rather than zero rows."
        ),
    ),
)


def _sidecar(*, planned: PlannedQuery, status: int, html: str, url: str, result_count: int, row_count: int) -> dict[str, object]:
    return {
        "bead": "phaze-fq9h.6",
        "query": planned.query,
        "why": planned.why,
        "captured_utc": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "final_url": url,
        "status": status,
        "html_bytes": len(html.encode()),
        "row_selector_matches": row_count,
        "parsed_results": result_count,
    }


async def _capture_one(client: httpx.AsyncClient, scraper: TracklistScraper, planned: PlannedQuery, out: Path) -> dict[str, object]:
    """Spend exactly one host request on *planned* and write the bytes + sidecar."""
    await reserve_host_request_slot()
    response = await client.post(TracklistScraper.SEARCH_URL, data={"main_search": planned.query, "search_selection": "9"})
    html = response.text

    row_count = 0
    result_count = 0
    if response.status_code == 200:
        row_count = len(BeautifulSoup(html, "lxml").select(TracklistScraper._SEARCH_ITEM_SELECTOR))
        try:
            result_count = len(scraper._parse_search_results(html))
        except Exception as exc:
            print(f"    parse raised: {exc}")  # noqa: T201

    out.mkdir(parents=True, exist_ok=True)
    (out / f"{planned.slug}.html").write_text(html, encoding="utf-8")
    meta = _sidecar(planned=planned, status=response.status_code, html=html, url=str(response.url), result_count=result_count, row_count=row_count)
    (out / f"{planned.slug}.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return meta


async def _run(queries: tuple[PlannedQuery, ...], out: Path) -> int:
    scraper = TracklistScraper()
    print(f"Spending {len(queries)} host request(s) into {out}/\n")  # noqa: T201
    async with httpx.AsyncClient(headers=TracklistScraper._build_headers(), timeout=30.0, follow_redirects=True) as client:
        for index, planned in enumerate(queries, start=1):
            print(f"[{index}/{len(queries)}] {planned.query!r}")  # noqa: T201
            meta = await _capture_one(client, scraper, planned, out)
            print(f"    status={meta['status']} bytes={meta['html_bytes']} rows={meta['row_selector_matches']} parsed={meta['parsed_results']}")  # noqa: T201
    await scraper.close()
    print(f"\nSpent exactly {len(queries)} host request(s).")  # noqa: T201
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--query", help="capture one ad-hoc query instead of the planned set")
    parser.add_argument("--slug", help="fixture basename for --query")
    parser.add_argument("--why", default="ad-hoc capture", help="provenance note for --query")
    parser.add_argument("--list", action="store_true", help="print the planned query set and spend nothing")
    args = parser.parse_args(argv)

    if args.list:
        for planned in PLANNED_QUERIES:
            print(f"{planned.slug:32s} {planned.query!r}\n    {planned.why}\n")  # noqa: T201
        return 0

    if args.query:
        if not args.slug:
            parser.error("--query requires --slug")
        queries: tuple[PlannedQuery, ...] = (PlannedQuery(slug=args.slug, query=args.query, why=args.why),)
    else:
        queries = PLANNED_QUERIES

    return asyncio.run(_run(queries, args.out))


if __name__ == "__main__":
    sys.exit(main())
