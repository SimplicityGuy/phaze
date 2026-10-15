# Tracklist acquisition retirement

The external acquisition support was removed in external tracklist retirement after the operator received an explicit response that scraping is not allowed and no programmatic API is offered.

Search, detail rendering, parsing, automatic drain continuation, refresh/prioritization endpoints, captured provider fixtures and acquisition scripts are removed. The production image no longer installs Patchright, Chrome or Xvfb. Playwright remains a development-only browser testing dependency.

Stored tracklists, source provenance, local cue companions, embedded tags, Discogs matching and cue generation remain available. There is no replacement external acquisition provider in this change.

Stop the old controller worker before upgrading, so an already-running old process cannot finish a provider request. Deploy the new image to the API and controller, then run the normal startup migration. Migration 082 drops the retired lookup cache, per-file outcomes, priority flags and drain arm state; it preserves tracklists, versions, tracks and Discogs links. New tracklists default to the manual source. Old queued acquisition jobs have no registered handler in the new worker and cannot issue requests. A downgrade recreates empty acquisition tables with the drain disarmed; removed lookup history is not restored.

Ajax JSON search and captcha re-queue are canceled. See `docs/design/0024-tracklist-source-retirement.md` for the operator authority and superseded decisions. Historical planning and spike records do not authorize renewed acquisition.

## Boundary: operator-triggered provider lookups

The retirement above covers scraping and scheduled acquisition. It does not cover operator-triggered lookups through the in-repo provider framework, which are a different thing: a person asks for one lookup, the request goes through an official API within a fixed budget and pacing, and the result is a candidate for review. No scheduled, background or automatic acquisition is revived, and the retired scraping stays retired. The measured evidence and constraints for the first such provider are in `docs/design/tracklist-provider-trackid.md`; the contract extension is in `docs/design/0025-remote-provider-sources.md`.
