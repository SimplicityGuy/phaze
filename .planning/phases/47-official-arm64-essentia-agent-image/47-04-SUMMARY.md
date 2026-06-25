---
phase: 47-official-arm64-essentia-agent-image
plan: 04
subsystem: ci
tags: [ci, github-actions, docker, arm64, parity, essentia, ghcr, just, golden-reference, cloudimg]

# Dependency graph
requires:
  - phase: 47-01
    provides: "Dockerfile.agent-arm64 (the image this guard parity-checks + pushes; direct python3 -m saq CMD, OMP_NUM_THREADS=1, fix #4 hand-off)"
  - phase: 47-02
    provides: "build-arm64 job outputs.tags + outputs.labels + warmed scope=arm64 cache (the gated push consumes these)"
  - phase: 47-03
    provides: "scripts/parity/{dump_analysis.py,compare_analysis.py,reference.wav} — the shared dump tool + comparator + reference clip"
provides:
  - "parity-golden-x86 job (x86, uploads golden-x86.json via the shared just parity-dump)"
  - "parity-guard job (native arm64: cache-rebuild -> build-blocking compare -> GATED push with tags+labels+provenance/sbom)"
  - "just parity-dump (shared INTERP-selecting dump path both CI jobs delegate to) + just parity-check (operator mirror)"
  - "docs/arm64-agent-image.md (3.13 pin, 4 fixes, build + parity commands)"
affects: [51 (cloud-agent compose pins the parity-validated <version>-arm64 image)]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Golden-reference CI parity gate: x86 golden artifact -> native-arm64 compare -> gated publish (a divergent image never reaches GHCR)"
    - "Build-blocking compare ORDERED before the push step (a failed step skips later steps) — the gate needs no explicit if on the push beyond the PR guard"
    - "Shared just parity-dump recipe with an INTERP arg so the SAME dump path serves the uv-based x86 image and the --system 3.13 arm64 image"
    - "Cache-replay rebuild (load:true from scope=arm64) so the validated image is local for the dump, then a second cache-replay build-push with attestations (build-push-action cannot load+push together)"

key-files:
  created:
    - docs/arm64-agent-image.md
  modified:
    - .github/workflows/docker-publish.yml
    - justfile

key-decisions:
  - "Both parity jobs gate their network/numeric steps on `github.event_name != 'pull_request'`: the golden is produced from the PUSHED x86 api image (only exists on non-PR), so the compare + gated push run on non-PR pushes; PRs still get build+import-smoke coverage from build-arm64"
  - "x86 golden image resolved via a metadata-action step (bare api URL, mirrors the api tag set) and the first resolved tag is pulled — no hardcoded tag, works on both default-branch and release-tag pushes"
  - "Epsilon left at the comparator default (--atol 1e-4) as the INITIAL value; the first real native-arm64 CI run produces the authoritative x86<->arm64 deltas to tune it. BPM/key are asserted EXACT by compare_analysis.py._EXACT_FIELDS (a divergence is a real bug, not a tolerance to widen)"

requirements-completed: [CLOUDIMG-01, CLOUDIMG-02, CLOUDIMG-03]

# Metrics
duration: ~18min
completed: 2026-06-24
---

# Phase 47 Plan 04: arm64 Parity Guard + Gated Publish Summary

**Wired the CLOUDIMG-03 parity gate into CI: an x86 `parity-golden-x86` job emits `golden-x86.json` from the shared `dump_analysis.py`, and a native-arm64 `parity-guard` job rebuilds the cached image, runs the SAME dump over `reference.wav`, compares against the golden (build-blocking), and ONLY THEN pushes the validated image to GHCR with matching `-arm64` tags + OCI labels + provenance/sbom — the same job that proves spike fix #4 holds on real audio. Plus a `just parity-check` operator mirror and `docs/arm64-agent-image.md`.**

## Performance
- **Duration:** ~18 min
- **Completed:** 2026-06-24
- **Tasks:** 3
- **Files modified:** 3 (1 created, 2 modified)

## Accomplishments
- **`parity-golden-x86` job** (`ubuntu-latest`, `needs: build-and-push`, `permissions: { contents: read, packages: read }`): resolves the bare-repo x86 api image tag via `metadata-action`, provisions the ~33 essentia models via `actions/cache` keyed on `download_models.py`, pulls the freshly-built x86 image (non-PR), and produces `golden-x86.json` by **delegating to `just parity-dump`** (default `INTERP="uv run python"`) — uploaded via `upload-artifact`. The authoritative golden producer.
- **`parity-guard` job** (native `ubuntu-24.04-arm`, `needs: [build-arm64, parity-golden-x86]`, `permissions: { contents: read, packages: write }`): (b) **rebuilds** the arm64 image from `cache-from: type=gha,scope=arm64` with `load: true`/`push: false` (content-addressed cache ⇒ identical digest to build-arm64); (c) **build-blocking compare** — `just parity-dump ... python3` (direct `--system` interpreter) then `uv run python compare_analysis.py golden actual --atol 1e-4`; (d) **gated push** (`if: github.event_name != 'pull_request'`, a LATER step) with `push: true`, `provenance: true`, `sbom: true`, `tags: needs.build-arm64.outputs.tags` AND `labels: needs.build-arm64.outputs.labels`. A non-zero compare exit fails step (c) → step (d) is skipped → a divergent image never reaches GHCR (T-47-08).
- **`just parity-dump IMAGE [MODELS] [OUT] [INTERP]`** — the shared dump path both CI jobs delegate to (workflows delegate to just — MEMORY); the `INTERP` arg selects `uv run python` (x86 uv image) vs `python3` (arm64 `--system` 3.13). Copies the in-container output to the requested `OUT`.
- **`just parity-check [TAG]`** — operator mirror of the CI guard (provision models → dump arm64 actual via `python3` → compare against `golden-x86.json`).
- **`docs/arm64-agent-image.md`** — GSD marker on line 1; documents the 3.13 pin + scoped `requires-python` reconciliation (D-47-PY, including `python3` vs `uv run`), the TF/essentia pins, all 4 fixes in plain language, the runtime-libs-or-crash-loop rule, build commands, the full parity workflow (incl. the gated push carrying tags + OCI labels + attestations), and the `-arm64` tag naming for Phase 51.
- All new actions pinned to frozen 40-hex SHAs: `upload-artifact 043fb46…# v7.0.1`, `download-artifact 3e5f45b2…# v8.0.1`, `cache 27d5ce7…# v5.0.5`, plus reused `checkout`/`metadata-action`/`setup-buildx`/`build-push`/`login`/`setup-just`/`setup-uv`/`setup-python` SHAs.

## Epsilon + observed deltas (per `<output>`)
- **Chosen epsilon:** `--atol 1e-4` — the comparator default, set as the **initial** value.
- **Observed x86↔arm64 deltas:** **not yet available** in this execution environment. Like plan 47-02, the native-arm64 numeric run cannot be executed here (this host is x86 macOS with no native aarch64 runner, and a QEMU essentia compile is forbidden). The authoritative deltas are produced by the **first real `parity-guard` run on `ubuntu-24.04-arm`** in CI; the epsilon should be tuned there to just above the observed model-score noise floor.
- **BPM/key exact:** YES — enforced by `compare_analysis._EXACT_FIELDS = ("bpm", "musical_key", "mood", "style")`, which fail on any inequality. A BPM/key divergence is treated as a real bug to investigate (Assumption A4), never widened away.
- **Gated push carries the labels output:** YES — the push step sets `labels: ${{ needs.build-arm64.outputs.labels }}` (verified present in the workflow via `yaml.safe_load` + string assertion).
- **LD_PRELOAD fix-#4 fallback needed:** UNKNOWN until the first CI run. `OMP_NUM_THREADS=1` (baked in 47-01) is the primary mitigation; the `LD_PRELOAD` fallback is documented in both `Dockerfile.agent-arm64` and `docs/arm64-agent-image.md` to apply if the guard SIGSEGVs with `libgomp`/`libomp` in the faulthandler native stack.

## Task Commits
1. **Task 1: parity-golden-x86 job + parity-dump/parity-check just recipes** — `444a5d4` (feat)
2. **Task 2: parity-guard job (build-blocking compare + gated arm64 push)** — `c78addd` (feat)
3. **Task 3: document the arm64 agent image** — `0360f6a` (docs)

## Files Created/Modified
- `.github/workflows/docker-publish.yml` — added `parity-golden-x86` + `parity-guard` jobs.
- `justfile` — added `parity-dump` + `parity-check` recipes (group `docker`).
- `docs/arm64-agent-image.md` — new arm64 agent image service doc.

## Decisions Made
- **PR vs non-PR gating:** the golden is produced from the *pushed* x86 api image (push is non-PR only), so the golden upload, the arm64 dump/compare, and the gated push all gate on `github.event_name != 'pull_request'`. The arm64 image is still REBUILT from cache on PRs (validates the cache replay + load), and build-arm64 already import-smokes on PRs — so PRs retain build + boot coverage; numeric parity + publish run on branch/tag pushes.
- **x86 image resolution:** a `metadata-action` step mirrors the api job's tag set on the bare-repo URL and the first resolved tag is pulled, so the golden producer works on both default-branch (`latest`) and release-tag (`<version>`) pushes without a hardcoded tag.
- **Epsilon initial = comparator default:** `1e-4` is the placeholder the comparator already ships; tuning is deferred to the first real native-arm64 deltas (recorded above as the open follow-up), exactly as plan 47-03's "Next Phase Readiness" anticipated.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking] `packages: read` on the parity-golden-x86 job**
- **Found during:** Task 1
- **Issue:** The plan specified `permissions: { contents: read }` for the golden job, but the job **pulls** the freshly-built x86 api image from GHCR, which requires the workflow token to carry `packages: read`. With `contents: read` alone the authenticated pull of the repo's own package could fail.
- **Fix:** Set `permissions: { contents: read, packages: read }` (read-only — no write on the golden job; only `parity-guard` gets `packages: write` for the gated push).
- **Files modified:** `.github/workflows/docker-publish.yml`
- **Verification:** yamllint + actionlint clean; least-privilege preserved (golden job has no `packages: write`).
- **Committed in:** `444a5d4`

---

**Total deviations:** 1 auto-fixed (1 blocking).
**Impact on plan:** Minimal — a read-only scope addition required to pull the x86 image; the gated-push privilege boundary (write only on parity-guard, login only on non-PR) is unchanged.

## Issues Encountered
- The native-arm64 build + real-audio numeric parity run were **not** executed locally (x86 macOS host, no native aarch64 runner, QEMU compile forbidden) — same constraint plan 47-02 recorded. The jobs are validated by `yaml.safe_load` structural assertions + actionlint + yamllint; the real native build, the first x86↔arm64 deltas, the epsilon tuning, and the fix-#4 runtime proof are produced by the first `parity-guard` run on `ubuntu-24.04-arm` in CI.

## Threat Surface Notes (threat_model dispositions satisfied)
- **T-47-08** (divergent image → GHCR): the gated push is a step ORDERED AFTER the build-blocking compare; a non-zero `compare_analysis.py` exit stops the job before the push.
- **T-47-04** (provenance/label gap): the gated push sets `provenance: true` + `sbom: true` + `labels: ${{ needs.build-arm64.outputs.labels }}`.
- **T-47-09** (dual-OpenMP DoS): the guard drives real audio through `analyze_file` (not `np.sin`) — surfaces a fix-#4 regression before release.
- **T-47-03** (new CI actions): upload/download/cache pinned to frozen 40-hex SHAs.
- **T-47-02** (GHCR token): `permissions: { contents: read, packages: write }` on parity-guard only; login `if: github.event_name != 'pull_request'`.

## Next Phase Readiness
- The parity gate is wired end-to-end. **Open follow-up for the first CI run:** tune `--atol` from the observed native-arm64 deltas and confirm BPM/key are exactly equal + fix #4 holds (no `libomp` segfault).
- Phase 51 can pin the parity-validated `<version>-arm64` image in the cloud-agent compose.

## Self-Check: PASSED
- FOUND: `docs/arm64-agent-image.md`
- FOUND: `.github/workflows/docker-publish.yml` (jobs: build-and-push, build-arm64, parity-golden-x86, parity-guard)
- FOUND: `justfile` (parity-dump + parity-check)
- FOUND commits: `444a5d4`, `c78addd`, `0360f6a`
- Tests: 25 passed (`tests/test_parity/` + `tests/test_deployment/test_agent_compose.py`)

---
*Phase: 47-official-arm64-essentia-agent-image*
*Completed: 2026-06-24*
</content>
