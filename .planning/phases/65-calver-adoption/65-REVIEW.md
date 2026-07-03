---
phase: 65-calver-adoption
reviewed: 2026-07-03T00:00:00Z
depth: standard
files_reviewed: 9
files_reviewed_list:
  - .github/workflows/ci.yml
  - docker-compose.agent.yml
  - docker-compose.cloud-agent.yml
  - docs/arm64-agent-image.md
  - docs/cloud-burst.md
  - docs/configuration.md
  - docs/deployment.md
  - pyproject.toml
  - tests/agents/deployment/test_agent_compose.py
findings:
  critical: 0
  warning: 2
  info: 1
  total: 3
status: resolved
resolution: "WR-01 + WR-02 fixed in commit fix(65) — docker-publish.yml comment de-v'ed to :<version>; glob guard hardened to exact-shape (rejects v-prefix/wildcard). IN-01 cosmetic, accepted. All guard tests 13/13 green, actionlint + ruff clean."
---

# Phase 65: Code Review Report

**Reviewed:** 2026-07-03
**Depth:** standard
**Files Reviewed:** 9 (+ uv.lock confirmed machine-synced only)
**Status:** resolved (2 warnings fixed; 1 cosmetic Info accepted)

## Summary

Reviewed the CalVer-adoption diff against base `b82334a`. The highest-consequence change — `ci.yml`'s `on.push.tags` glob swap from `["v*.*.*"]` to `["[0-9]+.[0-9]+.[0-9]+"]` — is correct: the value is properly YAML-quoted (required since it starts with `[`), GitHub Actions' glob-filter dialect treats `.` as a literal character and `+` as "one-or-more of the preceding atom" (confirmed against the actionlint pass and the well-established real-world convention for numeric-only tag gating), so the pattern matches `2026.7.0` / `2026.12.10`, and correctly rejects `main`, `v2026.7.0` (leaked `v` prefix), `2026.7` (two-part), and `2026.7.0-rc1` (suffixed pre-release) because GitHub Actions tag filters require a full-string match. `actionlint` and the full `test_agent_compose.py` module (13/13, ruff clean) were re-run locally and pass.

`pyproject.toml`/`uv.lock` are correctly synced (`uv.lock`'s `phaze` entry was regenerated, not hand-edited — confirmed via `git diff`, single-line diff matching the `pyproject.toml` bump). `2026.7.0` parses as a valid PEP 440 version (confirmed via `packaging.version.Version`). The D-12/D-13 split (forward-looking examples rewritten, historical `vN.M` labels left verbatim) was applied consistently across `docs/deployment.md`, `docs/configuration.md`, `docs/arm64-agent-image.md`, `docs/cloud-burst.md`, and both compose files' comment blocks — a repo-wide grep for lingering `:v<version>` / `v*.*.*` strings outside `.planning/` (historical record, correctly untouched) found only the two items below.

The two Warning items are real but neither is a shipped-behavior regression: one is a documentation-drift miss in a file the phase intentionally left unedited (`docker-publish.yml`), the other is a pre-existing (not newly introduced) test-assertion looseness that the review focus explicitly asked to be checked.

## Warnings

### WR-01: `docker-publish.yml` still documents the pre-CalVer `:v<version>` tag shape

**File:** `.github/workflows/docker-publish.yml:107`
**Issue:** The phase intentionally left `docker-publish.yml` unedited (D-05/D-06, correct decision — its `type=semver` metadata-action needs no code change to emit `2026.7.0`/`2026.7`). However, the inline comment above the `tags:` block was not updated and now contradicts every other rewritten reference to this exact phrase:

```yaml
          # Phase 29 D-16 + WARNING-4: tag strategy is verified by
          # tests/test_deployment/test_agent_compose.py::test_docker_publish_workflow_tags_both_latest_and_version
          # which asserts BOTH a `:latest` and a `:v<version>` tag are produced.
```

This phase (65-01) explicitly retargeted that same test's docstring from `:v<version>` to `:<version>` (D-06), and 65-02 propagated the same `:v<version>` → `:<version>` fix to `docs/deployment.md` (3 sites), `docker-compose.agent.yml`, and `docker-compose.cloud-agent.yml`. This one site — living directly beside the functional YAML a future engineer will read first when touching the publish tag strategy — was missed, and now describes a tag shape (`v2026.7.0`) that the workflow will never actually produce under the new CalVer scheme. Confirmed via repo-wide grep: this is the only remaining `:v<version>` occurrence outside `.planning/` (historical/archival docs, correctly left alone).
**Fix:**
```yaml
          # which asserts BOTH a `:latest` and a `:<version>` tag are produced.
```

### WR-02: `test_ci_workflow_triggers_on_version_tags` gates the CalVer glob by substring, not exact match

**File:** `tests/agents/deployment/test_agent_compose.py:335-341`
**Issue:** The retargeted guard (the phase's own "highest-consequence" safety net per its threat model, T-65-01) checks presence with `any(CALVER_GLOB in str(t) for t in tags)` and absence with `not any("v*.*.*" in str(t) for t in tags)`. Both are substring checks, not exact-match checks, against each `tags:` list entry. This means a regression that changes the actual glob to, e.g., `"v[0-9]+.[0-9]+.[0-9]+"` (a leaked `v`-prefix reintroduced *combined* with the digit pattern) — or `"2026-only-[0-9]+.[0-9]+.[0-9]+"` — would satisfy the positive assertion (still contains the `CALVER_GLOB` substring) and would NOT be caught by the negative assertion (it isn't literally `"v*.*.*"`), silently passing CI while `ci.yml` no longer matches bare CalVer tags the way the docs/tests claim. This pattern was inherited from the pre-existing test (which used the same substring idiom for `"v*.*.*"`), so it isn't a regression introduced by this phase, but the phase's own review-focus criterion ("do the retargeted assertions actually gate the intended invariant... without weakening") is only partially met: the positive/negative pair narrows the space considerably but does not fully close the "malformed variant of the CalVer glob" gap for the single highest-consequence line in the repo.
**Fix:** Tighten to an exact-match assertion against the full tag pattern, e.g.:
```python
CALVER_GLOB = "[0-9]+.[0-9]+.[0-9]+"
assert isinstance(tags, list) and CALVER_GLOB in tags, (
    f'ci.yml must trigger on exactly the bare CalVer glob {CALVER_GLOB!r} as a discrete tags-list entry; got tags={tags!r}'
)
assert not any(t != CALVER_GLOB and "v" in str(t).lower() for t in tags), (
    "no `v`-prefixed or otherwise mutated tag glob variant may coexist with the CalVer glob (D-02: CalVer-only)"
)
```

## Info

### IN-01: Cosmetic column/comment misalignment introduced by the wider `2026.7.0` string

**File:** `docs/configuration.md:291`, `docs/cloud-burst.md:250`
**Issue:** Replacing `v4.0.0`/`v5.0.0` (6 chars) with `2026.7.0` (8 chars) shifted the trailing comment-alignment by 2 columns in two fenced code/table blocks (e.g. `docs/cloud-burst.md:250`'s `# pulls 2026.7.0-arm64` comment no longer lines up with the sibling `#` comments in the same block; `docs/configuration.md:291`'s table cell trailing-space padding is off by 2 relative to neighboring rows). Purely cosmetic — GFM tables render correctly regardless of pipe alignment, and no lint hook in this repo enforces markdown column alignment — but worth a pass during any future touch of these blocks.
**Fix:** Re-pad the affected lines' trailing whitespace/comment columns to match sibling lines in the same block.

---

_Reviewed: 2026-07-03_
_Reviewer: Claude (gsd-code-reviewer)_
_Depth: standard_
