---
phase: 74
slug: docs-runbook-n-lane-compute-ui-verification
status: verified
threats_open: 0
asvs_level: 1
created: 2026-07-06
---

# Phase 74 — Security

> Per-phase security contract: threat register, accepted risks, and audit trail.
> Register authored at plan time across all 4 PLAN files (`register_authored_at_plan_time: true`),
> so this run VERIFIED each declared mitigation exists in the implementation — it did NOT scan for
> new threats. Every closure below is backed by a direct file:line read, not documentation intent.
>
> Phase 74 is docs + tests + a docstring-only correction (no behavioral code change). T-74-06 is a
> CONDITIONAL mitigation whose trigger (a proven shared-session probe race) did NOT fire per the
> 74-03 Variant B arbiter (deterministic PASS across 6 runs); the required evidence is therefore
> (a) the docstring now reflects the non-race reality and (b) the bounded probe timeout is preserved
> — NOT that serialization was added.

---

## Trust Boundaries

| Boundary | Description | Data Crossing |
|----------|-------------|---------------|
| operator → docs | `docs/multi-compute.md` and compose examples are read by a human operator; no runtime input path, no network/auth boundary crossed | non-secret worked examples |
| operator env → compose | `PHAZE_CLOUD_AGENT_IMAGE` / `PHAZE_CLOUD_AGENT_CMD` overrides are set by the operator at their own host; no external/untrusted input path | non-secret image tag + launcher command |
| test → DB | Regression tests seed/read agent rows on the test Postgres; no production or network boundary | non-secret agent rows |
| lane probe → DB session | The compute probe crosses into the shared `AsyncSession`; concerns availability/correctness, not a security boundary | non-secret `{id,kind,rank,cap,in_flight,available,quota_wait,inadmissible}` |

---

## Threat Register

| Threat ID | Category | Component | Disposition | Mitigation | Status |
|-----------|----------|-----------|-------------|------------|--------|
| T-74-01 | Information Disclosure | `docs/multi-compute.md` worked compose/backends.toml examples | mitigate | Inline-secret grep (`ssh-rsa` / `BEGIN … PRIVATE KEY` / `postgres(ql)://<literal>`) returns nothing; the `*_FILE` secret-pointer pattern is present (3 hits) — examples reference `*_FILE` env pointers only, never a literal token/key/DSN | closed |
| T-74-02 | Tampering | new compose override vars documented as shell `command:` | accept | Operator-controlled at their own host; no external input path; docs document only the two accepted override values. No new exposed port/credential. Accepted risk logged below. | closed |
| T-74-03 | Tampering | parametrized compose `image`/`command` defaults | mitigate | Guard test (`tests/agents/deployment/test_cloud_agent_compose.py`) asserts the DEFAULT still renders arm64: `"ghcr.io/simplicityguy/phaze:" in image` (:124), `"PHAZE_IMAGE_TAG" in image` (:125), `"-arm64}" in image` (:128), and the stripped command DEFAULT `["python3","-m","saq"]` with `uv` forbidden (:162,:166) — mis-parametrization dropping the arm64 pin fails CI | closed |
| T-74-04 | Information Disclosure | compose env surface | accept | No secret value added; `PHAZE_AGENT_KIND=compute`, `network_mode`, volumes and `*_FILE` machinery (`PHAZE_QUEUE_URL_FILE`, `PHAZE_AGENT_TOKEN_FILE`, `PHAZE_PUSH_*_FILE`) unchanged (compose :40-41,:70); only two operator-set image/command vars with arm64 defaults added. Accepted risk logged below. | closed |
| T-74-05 | (test-only) | new regression tests | accept | Test code only; no runtime attack surface. Lane-dict assertion `set(lane) == expected_keys` (`test_lane_snapshot.py:300`) certifies lanes carry only `{id,kind,rank,cap,in_flight,available,quota_wait,inadmissible}` — no secret/config key; the only `ssh_user` reference is `ssh_user=None` (:493) in a config factory. Accepted risk logged below. | closed |
| T-74-06 | Denial of Service (availability) | `_probe_availability` shared-session fan-out with N≥2 online compute backends | mitigate (conditional — race NOT proven) | Arbiter (74-03 Variant B) PASSED → serialization correctly NOT applied per D-04. Required evidence present: (a) docstring corrected — the retired "caps compute at ≤1 / at most ONE probe" claim is gone; docstring now states "N compute backends are legal" and records the proven-race-free fan-out (`backends.py:651-663`); (b) bounded `asyncio.wait_for(backend.is_available(session), _PROBE_TIMEOUT_SEC)` preserved (`backends.py:644`, `_PROBE_TIMEOUT_SEC = 1.5` at :589) so no lane can stall the poll | closed |
| T-74-07 | Information Disclosure | probe logging / lane dicts | accept | `_probe_one` logs `backend_id` ONLY (`backends.py:646` `logger.info("backend_lane_probe_offline", backend_id=backend.id)`) — never SecretStr/token; lane dicts carry no config/secret (`get_backend_lane_snapshot` docstring + T-74-05 key-set assertion). Unchanged from T-71-01. Accepted risk logged below. | closed |

---

## Accepted Risks

| Threat ID | Accepted Risk | Rationale |
|-----------|---------------|-----------|
| T-74-02 | Operator can set arbitrary `PHAZE_CLOUD_AGENT_CMD` / `PHAZE_CLOUD_AGENT_IMAGE` shell/image values | Operator-controlled at their own host; single-user tool; no external/untrusted input path reaches these vars. No new exposed port or credential. |
| T-74-04 | Two new operator-set compose env vars widen the documented env surface | No secret value added; `*_FILE` secret machinery, `PHAZE_AGENT_KIND`, ports and volumes unchanged; both vars carry arm64 defaults. |
| T-74-05 | New regression tests add code surface | Test-only code; no runtime/network attack surface; lane dicts asserted secret-free. |
| T-74-07 | Probe logging + lane dicts could in principle disclose config | Verified `backend_id`-only logging and a secret-free lane-dict key set; unchanged invariant from Phase 71 (T-71-01). |

---

## Unregistered Flags

None. No SUMMARY.md carried a `## Threat Flags` section; no new attack surface appeared during
implementation beyond the two documented operator-set compose vars (already covered by T-74-02/04).

---

## Audit Trail

- T-74-01: `grep -inE '(ssh-rsa|BEGIN .*PRIVATE KEY|postgres(ql)?://[^$])' docs/multi-compute.md` → empty; `_FILE` pointer count = 3.
- T-74-03: `tests/agents/deployment/test_cloud_agent_compose.py:106-168` — arm64-default assertions read directly.
- T-74-06: `src/phaze/services/backends.py:589,632-666` — timeout constant + `_probe_one` bounded `wait_for` + corrected `_probe_availability` docstring read directly; no `asyncio.gather` → serialization change (concurrent fan-out retained, matching the Variant B PASS disposition).
- T-74-07: `src/phaze/services/backends.py:646,680-692` — `backend_id`-only log + secret-free lane-dict docstring.
- Implementation files were NOT modified by this audit (read-only).
