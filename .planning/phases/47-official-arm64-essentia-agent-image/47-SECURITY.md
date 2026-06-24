<!-- GSD:SECURITY phase=47-official-arm64-essentia-agent-image -->
# Security Audit — Phase 47: Official arm64 essentia agent image

**Audited:** 2026-06-24
**ASVS Level:** 1
**block_on:** high
**Disposition:** SECURED — 10/10 threats CLOSED
**threats_open:** 0

The threat register was authored at PLAN time across all four sub-plans
(`register_authored_at_plan_time: true`). This audit verifies that each declared
mitigation is PRESENT in the implemented code — it does not scan for new threats.

## Threat Verification

| Threat ID | Category | Disposition | Status | Evidence (file:line) |
|-----------|----------|-------------|--------|----------------------|
| T-47-01 | Tampering (essentia/TF pin) | mitigate | CLOSED | `Dockerfile.agent-arm64:21` `ARG TF_VERSION=2.20.0` + `:52` `tensorflow==${TF_VERSION}` (exact, no range); `:62` `ARG ESSENTIA_SHA=b9fa6cb674ca43dfb94d28d293aeda441c6745db` (hardcoded 40-hex); `:66-67` `git clone https://github.com/MTG/essentia.git` (official repo) + full-clone `git checkout "${ESSENTIA_SHA}"`; no `ls-remote`/`HEAD` build-time resolution |
| T-47-02 | Elevation of Privilege (GHCR token) | mitigate | CLOSED | `docker-publish.yml:203-205` build-arm64 `packages: read`; `:315-317` parity-golden-x86 `packages: read`; `:410-413` parity-guard `packages: write` (sole pusher); login gated `if: github.event_name != 'pull_request'` at `:226`, `:365`, `:504`. Non-pushing jobs are read-only (commit 13319cd downgrade — expected strengthened state) |
| T-47-03 | Tampering (third-party Actions) | mitigate | CLOSED | All 26 `uses:` across both workflows pinned to 40-hex SHA + `# vX.Y.Z` comment (grep: zero floating tags). Only unpinned ref is the repo-local `./.github/actions/docker-build-cache` (first-party, not third-party) |
| T-47-04 | Repudiation (provenance/label gap) | mitigate | CLOSED | `docker-publish.yml:522-526` gated push sets `push: true`, `provenance: true`, `sbom: true`, `labels: ${{ needs.build-arm64.outputs.labels }}`, `tags: ${{ needs.build-arm64.outputs.tags }}`; `:210-212` build-arm64 exposes `outputs.tags`+`outputs.labels` from `id: meta` metadata-action |
| T-47-05 | Information Disclosure (image/fixture) | mitigate+accept | CLOSED | `Dockerfile.agent-arm64:113` `RUN mkdir -p /models` (models mounted at runtime, never downloaded at build — no `curl`/`wget <url>` build step; `wget` is only an apt build-dep package, never invoked); no secret/token/key/`.env` baked (grep clean). Accept half: `generate_reference.py:53-69` builds `reference.wav` arithmetically (no RNG, no seed → byte-reproducible) and fully synthetic (license-clean) — see Accepted Risks |
| T-47-06 | Denial of Service (missing runtime .so) | mitigate | CLOSED | `Dockerfile.agent-arm64:125-127` final-stage `apt-get install ... libatomic1 ffmpeg libsndfile1 libchromaprint-tools libpq5`; import-smoke `docker-publish.yml:297-298` `docker run --rm "${FIRST_TAG}" python3 -c "import phaze.tasks.agent_worker; import essentia.standard"` on the loaded image before any push |
| T-47-07 | Tampering (comparator silent pass) | mitigate | CLOSED | `compare_analysis.py:86-89` exactly-one-None → failure string; `:114-117` flatten union of golden+actual score keys via `.get()` so a missing/renamed score is a failure not a `KeyError`; tests `test_compare_analysis.py:149` (none-vs-number), `:158` (missing key, no raise), `:167` (extra key), `:176` (missing top-level field) |
| T-47-08 | Tampering (divergent image → GHCR) | mitigate | CLOSED | `docker-publish.yml:275-276` build-arm64 `push: false`/`load: true`; parity-guard compare step `:486-501` (non-zero `compare_analysis.py` exit fails the step) is ORDERED BEFORE the gated push step `:511-529` — a failed step skips later steps, so a divergent image is never pushed |
| T-47-09 | Denial of Service (dual-OpenMP segfault) | mitigate | CLOSED | `docker-publish.yml:495` `just parity-dump "${ARM64_IMAGE}" ./models scripts/parity/actual.json python3` over the real `reference.wav`; `dump_analysis.py:60-62` calls `phaze.services.analysis.analyze_file` (real audio, NOT `np.sin`); `Dockerfile.agent-arm64:108` `ENV OMP_NUM_THREADS=1` mitigation with documented `LD_PRELOAD` fallback `:103-104` |
| T-47-SC | Tampering (in-image installs / new deps) | accept | CLOSED | No NEW package dependencies: comparator/dump/generator use stdlib `math`/`json`/`wave`/`hashlib` + existing `numpy`; `Dockerfile.agent-arm64:52` installs only the pre-existing stack (`numpy pyyaml tensorflow==2.20.0`) + essentia from source (already a stack dependency). See Accepted Risks |

## Accepted Risks Log

- **T-47-SC (accept):** No new package dependencies introduced by Phase 47. The
  parity toolkit relies on the Python stdlib plus the already-present `numpy`; the
  arm64 Dockerfile installs only the toolchain the existing stack already declares
  (`numpy`, `pyyaml`, `tensorflow==2.20.0`) and builds essentia (an existing
  dependency, x86 wheel → aarch64 source) at a pinned commit SHA. The exact pins
  (T-47-01) bound the supply-chain surface. RESEARCH Package Legitimacy Audit:
  all pre-existing/distro packages; slopcheck N/A.

- **T-47-05 (accept half — reference fixture):** `scripts/parity/reference.wav` is
  fully synthetic — an arithmetically generated C-major triad × 120 BPM envelope
  (`generate_reference.py`), no RNG, no copyrighted audio, no PII. The generator is
  committed and byte-reproducible (sha256 stable across regenerations:
  `d6786a1d3373ca3840aabb62a232a98e86d9bf803b04181723f240061dd96581`).

## Unregistered Flags

None. No `## Threat Flags` section appears in any of the four Phase 47 SUMMARY
files; no new attack surface was reported by the executor during implementation.

## Notes / Deferred Runtime Validation

The following are NOT security gaps — they are runtime confirmations that, by
design, can only execute on a native `ubuntu-24.04-arm` CI runner (the audit host
is x86 macOS; a QEMU essentia compile is forbidden). The MITIGATIONS are present
in code and verified above; only their first live execution is pending:

- T-47-09 / fix #4 (OMP_NUM_THREADS=1): the `parity-guard` real-audio run is the
  first proof the dual-OpenMP mitigation holds. The mitigation is baked and the
  guard wiring is in place; a green run records the proof, a SIGSEGV triggers the
  documented `LD_PRELOAD` fallback.
- T-47-01 (ESSENTIA_SHA trust): the pinned SHA is "trusted only after the 47-04
  parity guard" — the gate that would catch a fresh-master divergence is wired and
  build-blocking. The pin itself (the tampering mitigation) is present in code.
- Parity epsilon (`--atol 1e-4`) is the initial value to be tuned from the first
  real x86↔arm64 deltas; BPM/key are asserted EXACT regardless.
