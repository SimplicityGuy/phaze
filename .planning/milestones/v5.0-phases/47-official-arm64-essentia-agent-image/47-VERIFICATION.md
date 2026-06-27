---
phase: 47-official-arm64-essentia-agent-image
verified: 2026-06-24T00:00:00Z
status: passed
score: 3/3 must-haves verified
overrides_applied: 0
re_verification: false
---

# Phase 47: Official arm64 essentia agent image — Verification Report

**Phase Goal:** Build the official arm64 essentia analysis agent image — compile essentia from source on a native arm64 CI runner, publish it to GHCR with `-arm64` tags + OCI labels, gated behind a numeric-parity guard that compares arm64 analysis output against an x86 golden so a parity-divergent image can never reach the registry. This unlocks the OCI Ampere A1 free-tier compute agent for later phases.
**Verified:** 2026-06-24
**Status:** PASSED
**Re-verification:** No — initial verification

> **Scope note:** Per explicit user context, the following items are inherently deferred to the first native `ubuntu-24.04-arm` CI run and are NOT treated as gaps or human-verification items — the plans documented this boundary explicitly: (1) the native arm64 essentia C++ compile, (2) the import-smoke on a real arm64 container, and (3) the real-audio parity numbers and fix-#4 runtime validation. All locally verifiable evidence is assessed below.

---

## Goal Achievement

### Observable Truths (from ROADMAP Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| SC1 | An official arm64-tagged phaze agent image exists that boots and imports essentia on arm64 hardware | VERIFIED | `Dockerfile.agent-arm64` exists with all 4 spike fixes baked in, correct `CMD ["python3", "-m", "saq", "phaze.tasks.agent_worker.settings"]`, runtime libs (`libatomic1 ffmpeg libsndfile1 libchromaprint-tools libpq5`) in the final stage; CI import-smoke (`python3 -c "import phaze.tasks.agent_worker; import essentia.standard"`) wired in `build-arm64` job before any push. Actual arm64 runtime deferred to first CI run by design. |
| SC2 | CI builds and pushes the arm64 image on a native arm64 runner (no QEMU) on the same release triggers as the x86 image, with matching tags | VERIFIED | `build-arm64` job in `docker-publish.yml` has `runs-on: ubuntu-24.04-arm`; `parity-guard` job does the gated push; `flavor: suffix=-arm64,onlatest=true` yields `latest-arm64` + `<version>-arm64`; job `outputs.tags` + `outputs.labels` both exposed and consumed by the push step. Tag-strategy regression test (`test_docker_publish_arm64_job_tags_latest_and_version`) passes. |
| SC3 | A CI parity guard runs full analysis on the arm64 image and confirms results match x86 within tolerance; the build fails if parity breaks | VERIFIED | `parity-guard` job on `ubuntu-24.04-arm` with `needs: [build-arm64, parity-golden-x86]`; steps ordered: (b) cache-rebuild/load, (c) build-blocking parity compare, (d) gated push — a failed compare exits non-zero before the push step, so a divergent image never reaches GHCR. `compare_analysis.py` encodes bpm/key/mood/style EXACT + `math.isclose(abs_tol)` for `danceability` + `features` model scores. 14 unit tests pass. Actual arm64 parity numbers deferred to first CI run by design. |

**Score: 3/3 truths verified**

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `Dockerfile.agent-arm64` | arm64 essentia-from-source agent build recipe | VERIFIED | `FROM python:3.13-slim-bookworm`, `ARG TF_VERSION=2.20.0`, `ARG ESSENTIA_SHA=b9fa6cb674ca43dfb94d28d293aeda441c6745db` (hardcoded), all 4 fixes present (symlink repoint, `LIBRARY_PATH`, `LD_LIBRARY_PATH` incl. `tensorflow.libs`, `OMP_NUM_THREADS=1`), `CMD ["python3", "-m", "saq", "phaze.tasks.agent_worker.settings"]`, no `uv run` anywhere |
| `.github/workflows/docker-publish.yml` | `build-arm64` job (native arm64, load + import-smoke, NO push) + `parity-golden-x86` + `parity-guard` jobs | VERIFIED | All 4 jobs present: `build-and-push`, `build-arm64` (ubuntu-24.04-arm, outputs: tags+labels), `parity-golden-x86`, `parity-guard` (ubuntu-24.04-arm, needs both prior jobs, steps: load→compare→gated push); `needs.build-arm64.outputs.labels` on gated push; `provenance: true`, `sbom: true` |
| `.github/workflows/docker-validate.yml` | Hadolint gate for `Dockerfile.agent-arm64` | VERIFIED | Matrix includes `agent-arm64` entry (`dockerfile: Dockerfile.agent-arm64`); x86 "Test Docker build" guarded with `if: matrix.name != 'agent-arm64'` |
| `scripts/parity/compare_analysis.py` | bpm/key exact + model-score epsilon comparator over the `analyze_file` dict | VERIFIED | Exports `compare(golden, actual, *, atol=1e-4) -> list[str]`; `_EXACT_FIELDS = ("bpm", "musical_key", "mood", "style")`; model scores via `_flatten_scores(features)` + `math.isclose(abs_tol=atol)`; None-vs-number is a failure (T-47-07 anti-silent-pass); non-zero-exit CLI |
| `scripts/parity/dump_analysis.py` | CLI: `analyze_file(file, models_dir)` → comparable JSON on stdout | VERIFIED | Imports `phaze.services.analysis.analyze_file` (lazy import past `--help`); projects to `{bpm, musical_key, mood, style, danceability, features}`; drops variable `windows`/count keys |
| `scripts/parity/reference.wav` | Deterministic non-degenerate parity reference audio | VERIFIED | 480044 bytes (under 500 KB limit); 30 s, 8 kHz, mono, 16-bit PCM; arithmetic C-major triad with 120 BPM envelope (no RNG); sha256 `d6786a1d3373ca3840aabb62a232a98e86d9bf803b04181723f240061dd96581` |
| `tests/test_parity/test_compare_analysis.py` | Unit tests for the comparator | VERIFIED | 14 tests, all pass — covers identical pass, bpm mismatch, key mismatch, within-epsilon pass, out-of-epsilon fail, None-vs-number fail, mood/style mismatch |
| `tests/test_deployment/test_agent_compose.py` | Tag-strategy regression test for `-arm64` build job | VERIFIED | `test_docker_publish_arm64_job_tags_latest_and_version` present; 1 test passes |
| `docs/arm64-agent-image.md` | arm64 agent image documentation | VERIFIED | GSD marker on line 1 (`<!-- generated-by: gsd-doc-writer -->`); documents 3.13 pin rationale, D-47-PY reconciliation (`python3` vs `uv run`), TF/essentia pins, all 4 fixes in plain language, runtime-libs rule, build commands, full parity workflow (gated push with tags + OCI labels), `-arm64` tag naming for Phase 51 |
| `justfile` | `image-build-arm64`, `image-push-arm64`, `parity-dump`, `parity-check`, `parity-golden-regen` recipes | VERIFIED | All 5 recipes confirmed present via `just --list` assertions in verify step; `parity-dump` has `INTERP` arg for `uv run python` vs `python3` selection |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `docker-publish.yml build-arm64` | `Dockerfile.agent-arm64` | `file: Dockerfile.agent-arm64, platforms: linux/arm64` | WIRED | Confirmed via yaml parse: `Dockerfile.agent-arm64` in blob, `linux/arm64` in blob |
| `docker-publish.yml build-arm64 meta step` | `parity-guard gated push` | `outputs.tags` + `outputs.labels` from `id: meta` | WIRED | `build-arm64` job declares `outputs: {tags, labels}`; parity-guard push step consumes `needs.build-arm64.outputs.tags` + `needs.build-arm64.outputs.labels` |
| `parity-guard compare step` | `scripts/parity/compare_analysis.py` | `uv run python compare_analysis.py golden actual --atol` | WIRED | `compare_analysis` confirmed in `docker-publish.yml` blob |
| `scripts/parity/dump_analysis.py` | `phaze.services.analysis.analyze_file` | `from phaze.services.analysis import analyze_file` (lazy import) | WIRED | Confirmed in `dump_analysis.py` lines 60-62 |
| `parity-guard` | `build-arm64` + `parity-golden-x86` | `needs: [build-arm64, parity-golden-x86]` | WIRED | Yaml parse confirms `parity-guard.needs == ['build-arm64', 'parity-golden-x86']` |
| Gated push step | GHCR `-arm64` tags + OCI labels | `push:true + labels: ${{ needs.build-arm64.outputs.labels }}` | WIRED | Yaml parse confirms `push:True`, `provenance:True`, `sbom:True`, `labels` template from `build-arm64` outputs |
| `Dockerfile.agent-arm64` | `phaze.tasks.agent_worker.settings` | `CMD ["python3", "-m", "saq", "phaze.tasks.agent_worker.settings"]` | WIRED | Direct interpreter (no `uv run`); `agent_worker` string confirmed in `docker-publish.yml` import-smoke |

---

### Data-Flow Trace (Level 4)

Level 4 skipped for this phase — artifacts are CI workflow configurations and a CLI tool, not dynamic-data rendering components. The data flow that matters is the parity pipeline: `reference.wav → dump_analysis.py (in image) → JSON → compare_analysis.py (on runner) → exit code → gated push`. This flow is verified structurally (see Key Links above) and will be validated at runtime in CI.

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Parity comparator unit tests (bpm/key exact, epsilon, anti-silent-pass) | `uv run pytest tests/test_parity/test_compare_analysis.py -x -q` | 14 passed in 0.02s | PASS |
| arm64 tag-strategy regression test | `uv run pytest tests/test_deployment/test_agent_compose.py -k arm64 -x -q` | 1 passed in 0.03s | PASS |
| All parity + deployment tests combined | `uv run pytest tests/test_parity/ tests/test_deployment/test_agent_compose.py -q` | 25 passed in 0.09s | PASS |
| `dump_analysis.py --help` exits 0 | `uv run python scripts/parity/dump_analysis.py --help >/dev/null` | Exit 0 | PASS |
| `reference.wav` byte-reproducible | `sha256 repeated twice` | Both runs: `d6786a1d...` | PASS |
| `Dockerfile.agent-arm64` contains no `uv run` | `grep "uv run" Dockerfile.agent-arm64` | No matches | PASS |
| `pyproject.toml` requires-python unchanged | `grep "requires-python" pyproject.toml` | `>=3.14,<3.15` | PASS |
| `docker-publish.yml` job structure | yaml parse assertions | Jobs: build-and-push, build-arm64, parity-golden-x86, parity-guard all present | PASS |

---

### Probe Execution

Step 7c: SKIPPED — no `scripts/*/tests/probe-*.sh` files declared or conventionally present for this phase. The parity toolkit does not include runnable probes (requires a native arm64 runner + essentia models).

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| CLOUDIMG-01 | 47-01, 47-02 | Official arm64 essentia image published to GHCR, essentia from source, spike fixes baked in | SATISFIED | `Dockerfile.agent-arm64` with all 4 fixes, runtime libs, correct CMD; CI `build-arm64` import-smoke + `parity-guard` gated push |
| CLOUDIMG-02 | 47-02, 47-04 | Built and pushed by CI on native arm64 runner (no QEMU), same release triggers, matching tags | SATISFIED | `ubuntu-24.04-arm` runner confirmed; `flavor: suffix=-arm64,onlatest=true`; parity-guard gated push with `outputs.tags` + `outputs.labels`; tag-strategy regression test green |
| CLOUDIMG-03 | 47-03, 47-04 | CI/test guard confirms arm64 produces results matching x86 within tolerance; build fails on parity breaks | SATISFIED | `parity-guard` job with build-blocking compare step ordered before push step; `compare_analysis.py` with exact + epsilon logic, 14 unit tests green; gated push only reached on compare success |

No orphaned CLOUDIMG requirements — CLOUDIMG-04 is explicitly deferred to Future Requirements in `REQUIREMENTS.md`.

---

### Anti-Patterns Found

Scan performed on: `Dockerfile.agent-arm64`, `.github/workflows/docker-publish.yml`, `.github/workflows/docker-validate.yml`, `scripts/parity/compare_analysis.py`, `scripts/parity/dump_analysis.py`, `scripts/parity/generate_reference.py`, `tests/test_parity/test_compare_analysis.py`, `docs/arm64-agent-image.md`, `justfile`

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| (none) | — | — | — | — |

No `TBD`, `FIXME`, or `XXX` markers found in any file modified by this phase. No stubs, placeholders, or empty implementations detected.

---

### Human Verification Required

None — all success criteria are verified structurally in this environment. The runtime arm64 items are CI-gated by explicit design (documented in all 4 PLANs and SUMMARYs), not human verification needs.

---

### Gaps Summary

No gaps. All 3 ROADMAP success criteria are verified at the level achievable on an x86 macOS host. The structural evidence (Dockerfile content, CI workflow wiring, comparator logic, test suite) is comprehensive and matches the plan intent. The arm64 runtime proof is owned by the first `ubuntu-24.04-arm` CI run, as designed.

---

_Verified: 2026-06-24_
_Verifier: Claude (gsd-verifier)_
