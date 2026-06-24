---
phase: 47
slug: official-arm64-essentia-agent-image
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-24
---

# Phase 47 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (existing) — comparator unit tests only; image validation is CI-level |
| **Config file** | pyproject.toml [tool.pytest.ini_options] |
| **Quick run command** | `uv run pytest tests/test_parity/ tests/test_deployment/test_agent_compose.py -k 'parity or arm64' -x -q` |
| **Full suite command** | `uv run pytest` |
| **Image/CI guards** | hadolint (`docker run --rm -i hadolint/hadolint < Dockerfile.agent-arm64`); `build-arm64` import-smoke; `parity-guard` numeric compare — all run in CI on `ubuntu-24.04-arm` |
| **Estimated runtime** | unit ~5s; native arm64 build + parity ~10-20 min (separate CI job, does not gate x86) |

---

## Sampling Rate

- **After every task commit:** Run the task's `<automated>` verify (hadolint / pytest / yaml-parse).
- **After every plan wave:** Run the quick run command.
- **Before `/gsd:verify-work`:** `uv run pytest` green + hadolint clean on Dockerfile.agent-arm64.
- **Max feedback latency:** ~10s for unit/lint; native arm64 image + parity validated in CI.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 47-01-01 | 01 | 1 | CLOUDIMG-01 | T-47-01 | essentia+TF pinned (no moving tip) | lint+grep | `docker run --rm -i hadolint/hadolint < Dockerfile.agent-arm64` + content grep | ✅ created in-task | ✅ green |
| 47-01-02 | 01 | 1 | CLOUDIMG-01 | T-47-06 | runtime libs present; 3.14 contract untouched | lint+grep | hadolint + `grep libatomic1...` + `! grep 3.13,<3.15 pyproject.toml` | ✅ | ✅ green |
| 47-01-03 | 01 | 1 | CLOUDIMG-01 | T-47-09 | OMP fix baked (real-audio proof → 47-04) | lint+grep | hadolint + `grep OMP_NUM_THREADS=1` | ✅ | ✅ green |
| 47-02-01 | 02 | 2 | CLOUDIMG-02 | T-47-02/03/04 | native arm64, least-priv token, frozen SHA, provenance | yaml-parse | `python -c "yaml...build-arm64...ubuntu-24.04-arm"` | ✅ | ✅ green |
| 47-02-02 | 02 | 2 | CLOUDIMG-01 | T-47-06 | hadolint gate on new Dockerfile | yaml-parse+just | matrix has agent-arm64 + `just --list` recipes | ✅ | ✅ green |
| 47-02-03 | 02 | 2 | CLOUDIMG-02 | — | -arm64 tag strategy guarded | unit | `uv run pytest tests/test_deployment/test_agent_compose.py -k arm64` | ❌ W0 (new test) | ✅ green |
| 47-03-01 | 03 | 1 | CLOUDIMG-03 | T-47-07 | no silent pass on missing/None data | unit | `uv run pytest tests/test_parity/test_compare_analysis.py` | ❌ W0 (new test) | ✅ green |
| 47-03-02 | 03 | 1 | CLOUDIMG-03 | T-47-05 | deterministic synthetic reference (no PII/copyright) | cli+sha | dump `--help` + reference.wav sha256 stable + ruff | ✅ created in-task | ✅ green |
| 47-04-01 | 04 | 3 | CLOUDIMG-03 | T-47-03 | x86 golden via shared dump; cached models; frozen SHA | yaml-parse+just | `python -c "yaml...parity-golden-x86...upload-artifact"` + `just --list parity-check` | ✅ | ✅ green |
| 47-04-02 | 04 | 3 | CLOUDIMG-03, CLOUDIMG-01 | T-47-08/09 | build-blocking compare; fix #4 real-audio proof | yaml-parse | `python -c "yaml...parity-guard...needs...compare_analysis.py"` | ✅ | ✅ green |
| 47-04-03 | 04 | 3 | CLOUDIMG-01 | — | docs current (3.13 pin, 4 fixes, parity) | file+grep | `test -f docs/arm64-agent-image.md` + grep | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] `tests/test_parity/__init__.py` + `tests/test_parity/test_compare_analysis.py` — comparator unit tests (created in 47-03-01, no models/essentia)
- [x] `tests/test_deployment/test_agent_compose.py::test_docker_publish_arm64_job_tags_*` — -arm64 tag-strategy test (created in 47-02-03)
- [x] No framework install needed — pytest already configured

*All other validations (native arm64 build, import-smoke, numeric parity, fix-#4 real-audio) are CI-level image guards, not pytest coverage — per RESEARCH Validation Architecture.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| (none) | — | — | All phase behaviors have automated verification: hadolint, pytest unit tests, YAML-parse CI assertions, and CI image guards (import-smoke + native-arm64 parity-guard). |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (2 new test files)
- [x] No watch-mode flags
- [x] Feedback latency < 10s (unit/lint); image guards in CI
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-06-24

---

## Validation Audit 2026-06-24

Post-execution State-A audit. All 11 per-task automated commands re-run against the
merged implementation and pass; both Wave 0 test files exist and are green.

| Metric | Count |
|--------|-------|
| Requirements (CLOUDIMG-01/02/03) | 3 |
| Tasks COVERED | 11/11 |
| Gaps found | 0 |
| Resolved | 0 (none needed) |
| Escalated / manual-only | 0 |

**Evidence:**
- Unit: `tests/test_parity/test_compare_analysis.py` (14) + `tests/test_deployment/test_agent_compose.py` (11) → 25 passed.
- Dockerfile grep/lint: essentia SHA-pin `b9fa6cb…`, `tensorflow==2.20.0`, official `github.com/MTG/essentia`, runtime libs (libatomic1/ffmpeg/libsndfile1/libchromaprint-tools/libpq5), `OMP_NUM_THREADS=1`; `pyproject.toml` `requires-python = ">=3.14,<3.15"` untouched.
- YAML-parse: `build-arm64` runs-on `ubuntu-24.04-arm`; `parity-golden-x86` + `parity-guard` present, `parity-guard needs: [build-arm64, parity-golden-x86]`, golden uploads artifact, `compare_analysis.py` referenced (3×); `agent-arm64` lint-only entry in `docker-validate.yml`.
- Docs: `docs/arm64-agent-image.md` present with 3.13/OMP/parity content; `just` recipes `parity-check`/`parity-dump`/`parity-golden-regen`/`image-*-arm64` listed.

**CI-level guards (not local pytest, by design — RESEARCH Validation Architecture):** native arm64 essentia build, import-smoke, real-audio numeric parity, and fix-#4 dual-OpenMP proof execute on `ubuntu-24.04-arm` in CI; the locally-verifiable wiring for each is green. `nyquist_compliant: true` retained.
