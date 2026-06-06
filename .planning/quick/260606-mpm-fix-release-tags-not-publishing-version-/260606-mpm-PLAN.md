---
phase: quick-260606-mpm
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - .github/workflows/ci.yml
  - tests/test_deployment/test_agent_compose.py
  - docs/deployment.md
  - .env.example.agent
autonomous: true
requirements: [RELEASE-TAG-01]
must_haves:
  truths:
    - "Pushing a 3-part git tag (vX.Y.Z) triggers .github/workflows/ci.yml"
    - "detect-changes forces code-changed=true on tag refs, so the docker-publish job runs on a tag push"
    - "docker/metadata-action emits :vX.Y.Z and :X.Y image tags for a 3-part semver tag (existing config, now reachable)"
    - "The guard test fails if the tag trigger or the tag-ref code-changed forcing is removed"
    - "Docs and .env example present the 3-part vX.Y.Z scheme as the published image-tag format"
  artifacts:
    - path: ".github/workflows/ci.yml"
      provides: "push.tags trigger + detect-changes tag-ref forcing"
      contains: "tags:"
    - path: "tests/test_deployment/test_agent_compose.py"
      provides: "strengthened CI tag-pipeline guard test"
      contains: "def test_"
    - path: "docs/deployment.md"
      provides: "3-part semver release-tag documentation"
    - path: ".env.example.agent"
      provides: "PHAZE_IMAGE_TAG 3-part example"
  key_links:
    - from: ".github/workflows/ci.yml push.tags"
      to: "workflow run on tag push"
      via: "tags: [\"v*.*.*\"]"
      pattern: "tags:"
    - from: ".github/workflows/ci.yml detect-changes"
      to: "code-changed=true on tag ref"
      via: "github.ref_type == 'tag' early-exit"
      pattern: "ref_type|refs/tags"
    - from: "detect-changes code-changed=true"
      to: "docker-publish job runs"
      via: "needs.detect-changes.outputs.code-changed == 'true'"
      pattern: "code-changed"
---

<objective>
Fix release tags not publishing version-tagged Docker images to GHCR.

Pushing a git release tag currently runs NO workflow (`ci.yml` has only `push: branches` + `pull_request` triggers), and `docker-publish.yml` is `workflow_call`-only, so a tag push never builds or pushes an image. Even if it did fire, the `detect-changes` gate would skip `docker-publish` because tag refs are not forced to `code-changed=true`. The documented `PHAZE_IMAGE_TAG=v4.0.0` pin is therefore unusable.

Purpose: make a future `git push` of a 3-part semver tag (`vX.Y.Z`) trigger the full pipeline and publish `ghcr.io/simplicityguy/phaze:vX.Y.Z` (+ `:X.Y`). Lock the fix in with a strengthened guard test that fails if either the tag trigger or the tag-ref forcing regresses.

Output: tag trigger + tag-ref change detection in `ci.yml`, a strengthened guard test, and doc/env examples confirmed on the 3-part scheme.

Scope: CI workflow + test + docs only. No application source changes. Re-tagging the existing 2-part `v4.0` git tag is a maintainer release action and is OUT OF SCOPE — do not retag.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
</execution_context>

<context>
@./CLAUDE.md

<interfaces>
<!-- Current ci.yml detect-changes step env wiring (the block to extend). -->
From .github/workflows/ci.yml (jobs.detect-changes.steps[id=filter].env):
  EVENT_NAME: ${{ github.event_name }}
  BASE_SHA:   ${{ github.event.pull_request.base.sha }}
  HEAD_SHA:   ${{ github.sha }}
  BEFORE_SHA: ${{ github.event.before }}

Existing early-exit branch (extend this, do NOT replace):
  if EVENT_NAME == "schedule" || EVENT_NAME == "workflow_dispatch":
      echo "code-changed=true"; exit 0

Existing triggers (keep both, ADD the tag trigger):
  on:
    push:
      branches: ["**"]
    pull_request:
      branches: ["**"]

<!-- Existing guard-test helpers in tests/test_deployment/test_agent_compose.py -->
- PyYAML parses the workflow `on:` key as the boolean True in some cases — when
  reading triggers, look up the key as `True` (bool) if the string key "on" is
  absent. The test file currently has NO ci.yml loader; add one.
- The file already imports: pathlib.Path, re, typing.Any, yaml.
- Existing pattern: PUBLISH_WORKFLOW_PATH = parents[2] / ".github" / "workflows" / "docker-publish.yml"
- docker/metadata-action tag block in docker-publish.yml already contains
  `type=semver,pattern={{version}}`, `type=semver,pattern={{major}}.{{minor}}`,
  and `type=ref,event=tag` — leave that block as-is.
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add tag trigger + tag-ref change detection to ci.yml</name>
  <files>.github/workflows/ci.yml</files>
  <action>
Two edits to .github/workflows/ci.yml, preserving existing yamllint-strict formatting (document-start `---`, flow-style `["..."]` lists matching the existing `branches: ["**"]`, emoji step names, 2-space indent).

1. Trigger: under `on.push`, add a `tags` key alongside the existing `branches`. Use 3-part semver enforcement: `tags: ["v*.*.*"]`. Keep `on.push.branches: ["**"]` and the entire `on.pull_request.branches: ["**"]` block unchanged. (PRs do not carry tags, so no PR change is needed.)

2. detect-changes tag forcing: in `jobs.detect-changes.steps` (id `filter`), add `REF_TYPE: ${{ github.ref_type }}` to the step `env:` block (alongside EVENT_NAME/BASE_SHA/HEAD_SHA/BEFORE_SHA). Then extend the existing schedule/workflow_dispatch early-exit `if` to ALSO match tag refs by adding the condition `[[ "${REF_TYPE}" == "tag" ]]` to the OR chain. Update the early-exit echo to mention tag builds (e.g. `"⚙️ Scheduled/manual/tag build — running full pipeline"`). Do not touch the existing PR / branch / before-sha diff logic below the early-exit — tag pushes never reach it because they exit early.

Do not change docker-publish.yml — its metadata-action already emits `type=semver,pattern={{version}}`, `{{major}}.{{minor}}`, and `type=ref,event=tag`; those become reachable once tags trigger the pipeline.
  </action>
  <verify>
    <automated>uv run python -c "import yaml,io; d=yaml.safe_load(open('.github/workflows/ci.yml')); on=d.get('on', d.get(True)); assert 'v*.*.*' in on['push']['tags'], on['push']; step=[s for j in d['jobs'].values() for s in j.get('steps',[]) if s.get('id')=='filter'][0]; assert 'REF_TYPE' in step['env']; assert 'tag' in step['run'] and 'code-changed=true' in step['run']; print('ok')"</automated>
  </verify>
  <done>ci.yml has `on.push.tags: ["v*.*.*"]` (branches + PR triggers intact), the detect-changes step wires `REF_TYPE` and forces `code-changed=true` for tag refs via the early-exit branch, and the YAML parses cleanly.</done>
</task>

<task type="auto">
  <name>Task 2: Strengthen the CI tag-pipeline guard test</name>
  <files>tests/test_deployment/test_agent_compose.py</files>
  <action>
Add new test coverage to tests/test_deployment/test_agent_compose.py matching the file's existing style (module-level path constant, `yaml.safe_load`, small helpers, descriptive docstrings). Keep all existing tests — including `test_docker_publish_workflow_tags_both_latest_and_version` — unchanged.

Add a CI workflow path constant: `CI_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"`.

Add a helper `_load_ci_workflow_triggers(data)` (or inline) that returns the `on:` mapping, resolving the PyYAML boolean-key gotcha: prefer the string key `"on"`, fall back to the boolean key `True` (`data.get("on", data.get(True))`). Assert it is a dict before use.

Add two tests:

(a) `test_ci_workflow_triggers_on_version_tags` — load ci.yml, resolve triggers, assert a `push` trigger exists and its `tags` list is present and includes a 3-part semver glob (`"v*.*.*"`). Also assert the existing `push.branches` is still present (regression guard so the tag edit did not drop branch CI). Failure message must point at adding `on.push.tags`.

(b) `test_ci_detect_changes_forces_code_changed_on_tags` — load ci.yml, locate `jobs["detect-changes"]`, find the step with `id == "filter"`, read its `run` string and `env` dict. Assert (i) the step `env` wires a ref-type/ref variable (accept `REF_TYPE` referencing `github.ref_type`, OR a `REF`/ref var referencing `github.ref`), and (ii) the `run` script forces `code-changed=true` for tags — assert the run contains both a tag check token (`ref_type`/`"tag"`/`refs/tags`) and `code-changed=true`. Keep the assertion robust to reasonable rewordings (case-insensitive `tag` match on the run body). Failure message must explain that tag pushes would otherwise skip the docker-publish job.

Run `uv run ruff check tests/test_deployment/test_agent_compose.py` and `uv run mypy tests/test_deployment/test_agent_compose.py` are satisfied (type-annotate helpers; tests return `-> None`).
  </action>
  <verify>
    <automated>uv run pytest tests/test_deployment/test_agent_compose.py -q</automated>
  </verify>
  <done>The two new tests pass against the Task 1 ci.yml; reverting either ci.yml edit makes the corresponding test fail; the original docker-publish tag test and all other compose tests still pass; ruff + mypy clean on the test file.</done>
</task>

<task type="auto">
  <name>Task 3: Confirm 3-part vX.Y.Z scheme in docs and env example</name>
  <files>docs/deployment.md, .env.example.agent</files>
  <action>
Make the published image-tag scheme explicitly 3-part `vX.Y.Z` in the docs and confirm the env example. The existing `v4.0.0` example values are already correct 3-part examples — keep them. Do NOT change milestone-version prose (e.g. the "Phaze v4.0 Deployment Guide" title or "Production deployment of Phaze v4.0") and do NOT change `git checkout v4.0.0` (that is a git-tag checkout example for the app-server rebuild, already 3-part).

docs/deployment.md — in the `docker-publish.yml` Build Pipeline section (around the "Tag strategy" bullet, ~line 263) and/or the "Pinning the agent image for production" section (~line 354), add one sentence stating that release tags MUST be 3-part semver (`vX.Y.Z`) for the `{{version}}` / `{{major}}.{{minor}}` image tags to be produced, and that `ci.yml` triggers the publish pipeline on `push` of a `v*.*.*` tag. Keep the existing "tags both `:latest` and `:v<version>`" statements. Ensure no place in the file presents a 2-part `v4.0` value as a valid published IMAGE tag (scan the file; correct any to `vX.Y.Z` / `v4.0.0` if found — milestone prose like "v4.0" is fine).

.env.example.agent — the file already documents `PHAZE_IMAGE_TAG=v4.0.0` as the pin example and defaults to `latest`. Confirm consistency; if helpful, tighten the comment to note the tag must be 3-part `vX.Y.Z` (matching the published image tags). No functional change to the `PHAZE_IMAGE_TAG=latest` default line is required.

Preserve the gsd-doc-writer marker on line 1 of docs/deployment.md.
  </action>
  <verify>
    <automated>grep -nE "v[0-9]+\.[0-9]+\.[0-9]+|v\*\.\*\.\*|3-part|vX\.Y\.Z" docs/deployment.md .env.example.agent && ! grep -nE "PHAZE_IMAGE_TAG=v[0-9]+\.[0-9]+([^.0-9]|$)" docs/deployment.md .env.example.agent</automated>
  </verify>
  <done>docs/deployment.md states the 3-part `vX.Y.Z` release-tag requirement and the `v*.*.*` publish trigger; no 2-part value is presented as a valid published image tag; `.env.example.agent` pin example is a consistent 3-part `v4.0.0`; line-1 doc marker preserved.</done>
</task>

</tasks>

<verification>
Full guard + lint sweep (proxy for the un-pushable tag acceptance criterion):

```bash
uv run pytest tests/test_deployment/test_agent_compose.py -q
uv run ruff check .
pre-commit run --all-files
```

actionlint, yamllint (strict), and check-jsonschema run inside the pre-commit suite and must pass on the edited ci.yml. Do not use `--no-verify`.
</verification>

<success_criteria>
- ci.yml triggers on `push` of `v*.*.*` tags while keeping branch + PR CI.
- detect-changes forces `code-changed=true` on tag refs, so `docker-publish` runs on a tag push.
- The strengthened guard test fails if either the tag trigger or the tag-ref forcing is removed (proves it is not false-confidence static-only coverage).
- docs/deployment.md + .env.example.agent present the 3-part `vX.Y.Z` published-image scheme consistently.
- pre-commit suite (actionlint, yamllint strict, check-jsonschema, ruff, mypy) passes; tests pass via `uv run pytest`.
</success_criteria>

<output>
Create `.planning/quick/260606-mpm-fix-release-tags-not-publishing-version-/260606-mpm-SUMMARY.md` when done.
</output>
