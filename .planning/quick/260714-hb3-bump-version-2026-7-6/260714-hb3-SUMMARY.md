---
quick_id: 260714-hb3
status: complete
---

# Quick Task 260714-hb3 — Summary

Bumped the project version **2026.7.5 → 2026.7.6** for the 2026.7.6 CalVer patch release.

- `pyproject.toml`: `version = "2026.7.6"`.
- `uv.lock`: `uv lock` updated exactly one line (`phaze v2026.7.5 → v2026.7.6`) — no dependency re-resolution.
- Verified resolved version prints `2026.7.6`.
- No git tag created — the annotated `2026.7.6` tag is pushed after this merges to `main`, which triggers `ci.yml` → `docker-publish.yml` to build + push `ghcr.io/simplicityguy/phaze:2026.7.6` and `phaze/job:2026.7.6` (the latter carries the #245 empty-analysis fix).

Commit: `2e407419` (code). This release exists to publish a fixed job image before the paused cloud drain (`route_control.force_local=true`, ~8,228 files awaiting) is resumed.
