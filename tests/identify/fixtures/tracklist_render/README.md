# Recorded 1001Tracklists detail-page renders (phaze-fq9h.1)

Real HTML, captured live through `TracklistRenderer` on **2026-08-02** by
`scripts/capture_tracklist_render.py`, from a residential IP, headful Patchright Chrome, paced at
the published robots.txt crawl-delay with phaze's honest User-Agent.

**These bytes exist so nobody has to spend a host request to get them again.** The whole-host
budget is one request per 8 s — the binding constraint on the entire drain (phaze-fq9h) — so the
detail parser (phaze-fq9h.4) and result scoring (phaze-fq9h.6) are meant to be developed and
regression-tested against these files, not against the live site. Re-capture only when the site's
markup is suspected of having changed, and then say so in the bead.

| File | Page | Track rows | Bytes |
|------|------|-----------:|------:|
| `25fhn7c9-ok.html` | Sven Väth @ Time Warp, Maimarkthalle Mannheim, 2024-10-25 — the spike anchor | 52 | 349 128 |
| `19h6nw7t-ok.html` | Sven Väth @ BBC Radio 1 Dance Presents Time Warp, 2024-10-12 | 12 | 181 991 |

The anchor is the acceptance criterion's page: its `og:description` states 52 tracks and the
capture carries exactly 52 `.tlpItem` rows. The second file is a short listing, kept so the parser
is developed against more than one shape. Each `.html` has a `.json` sidecar recording the URL,
outcome, attempt sequence and capture time.

No local-archive identifier is involved: these are public pages about public events, captured from
the public site. (The string "Robert" in the anchor is the artist Robert Hood.)

## What Turnstile actually did — the phaze-hu8v observation

phaze-hu8v asked whether Turnstile still fires now that the scraper paces at crawl-delay 8 with an
honest UA. Measured here, 2026-08-02, one browser reused across the run:

- **16 renders across 15 distinct detail pages** (the anchor was rendered in both runs).
- **16 navigations total** — i.e. every render needed exactly one.
- **16 of 16 cleared on the FIRST navigation. 0 interstitials. 0 retries. 0 failures.**
- Wall clock per render: 11.18 s – 19.96 s, on top of the 8–12 s pacing wait before each.

Two honest caveats, both of which matter more than the headline:

1. **This does not prove Turnstile is gone.** The spike (phaze-dmvs, 2026-07-24) measured 6/8
   clearing on first navigation under otherwise similar conditions. 16/16 versus 6/8 is a real
   difference, but n is small, the two runs are a week apart, and Cloudflare's scoring is not a
   published constant. The bounded retry loop stays.
2. **No interstitial capture exists here** because none was served. A fixture for the challenge
   document would have to be fabricated, and a fabricated fixture is exactly the thing that makes
   `looks_like_interstitial` look tested when it is not. The unit tests use a clearly synthetic
   challenge document instead, and say so.

The reverse finding is the one that changed the code: **a successfully rendered detail page can
still contain Turnstile widget markup** (`challenges.cloudflare.com/turnstile`) — 4 of the 16
captures do, including the anchor, while the other 12 have no mention of it at all. So "the page
mentions Turnstile" can never mean "we were blocked", and `_classify` checks for the track
container first. Marker-first classification would have reported 4 of these 16 successful renders
as blocked, and the inconsistency across otherwise identical pages is what makes that failure mode
hard to spot: it would have looked like intermittent blocking rather than a bug.
