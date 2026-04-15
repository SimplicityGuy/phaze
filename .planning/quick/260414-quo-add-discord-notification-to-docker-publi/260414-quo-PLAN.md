---
phase: 260414-quo
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - .github/workflows/docker-publish.yml
autonomous: true
requirements:
  - QUO-260414-01
must_haves:
  truths:
    - "On every workflow run (success or failure), a Discord notification is sent for each matrix service (api, audfprint, panako)"
    - "The notification title reads 'phaze/<service-name>' for each matrix entry"
    - "The notification body includes the build duration (or 'N/A' fallback) and the cache used flag"
    - "The Discord webhook secret is read from secrets.DISCORD_WEBHOOK"
    - "yamllint and actionlint pass on the modified workflow file"
  artifacts:
    - path: ".github/workflows/docker-publish.yml"
      provides: "Docker build/push workflow with appended Discord notification step"
      contains: "sarisia/actions-status-discord@eb045afee445dc055c18d3d90bd0f244fd062708"
  key_links:
    - from: ".github/workflows/docker-publish.yml (Discord step)"
      to: "secrets.DISCORD_WEBHOOK"
      via: "with.webhook"
      pattern: "webhook: \\$\\{\\{ secrets.DISCORD_WEBHOOK \\}\\}"
    - from: ".github/workflows/docker-publish.yml (Discord step)"
      to: "steps.timer.outputs.duration"
      via: "with.description (with N/A fallback)"
      pattern: "steps.timer.outputs.duration \\|\\| 'N/A'"
---

<objective>
Append a Discord notification step to the `build-and-push` job in `.github/workflows/docker-publish.yml`, mirroring the discogsography `build.yml` pattern verbatim, adapted only for the project name (phaze).

Purpose: Get build success/failure pings in Discord for each matrix service (api, audfprint, panako), matching the proven discogsography pattern this user already relies on.
Output: Modified `.github/workflows/docker-publish.yml` with one new final step in the `build-and-push` job.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
</execution_context>

<context>
@.planning/STATE.md
@.github/workflows/docker-publish.yml

<interfaces>
<!-- The exact step shape to mirror, from discogsography/.github/workflows/build.yml lines 335-343.
     This is the authoritative source — copy verbatim, adapting only the title prefix. -->

```yaml
      - name: 📢 Send notification to Discord
        uses: sarisia/actions-status-discord@eb045afee445dc055c18d3d90bd0f244fd062708 # v1.16.0
        if: always()
        with:
          title: discogsography/${{ matrix.name }}
          description: |
            Build duration: ${{ steps.timer.outputs.duration || 'N/A' }}s
            Cache used: ${{ matrix.use_cache }}
          webhook: ${{ secrets.DISCORD_WEBHOOK }}
```

Adaptation: change `discogsography/` → `phaze/` in the title. Everything else stays identical.
</interfaces>

<current_workflow_tail>
<!-- The current final step of build-and-push (line 149+). The new step is appended after this. -->

```yaml
      - name: "📊 Collect metrics"
        if: always()
        env:
          START_TIME: ${{ steps.timer.outputs.start_time }}
          SERVICE_NAME: ${{ matrix.name }}
          CACHE_USED: ${{ matrix.use_cache }}
          CACHE_HIT: ${{ steps.docker-cache.outputs.cache-hit }}
        run: |
          end_time=$(date +%s)
          duration=$((end_time - START_TIME))
          echo "::notice title=Build Metrics::Service: ${SERVICE_NAME}, Duration: ${duration}s, Cache Used: ${CACHE_USED}"

          # Check cache hit rate
          if [[ -n "${CACHE_HIT}" ]]; then
            echo "::notice title=Cache Performance::Docker cache hit for ${SERVICE_NAME}"
          fi
```

Note: phaze's timer step only emits `start_time`, not `duration`. The `${{ steps.timer.outputs.duration || 'N/A' }}` expression in the discogsography pattern will resolve to `'N/A'` here. This is intentional — DO NOT restructure the metrics step to add a duration output. Mirror the upstream pattern verbatim.
</current_workflow_tail>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Append Discord notification step to docker-publish.yml</name>
  <files>.github/workflows/docker-publish.yml</files>
  <action>
Append a new step to the END of the `build-and-push` job's `steps:` list (after the existing `📊 Collect metrics` step at line 149). Use Edit to insert the following block immediately after the `Collect metrics` step's closing `fi` line, preserving two-space indentation consistent with surrounding steps:

```yaml
      - name: "📢 Send notification to Discord"
        uses: sarisia/actions-status-discord@eb045afee445dc055c18d3d90bd0f244fd062708 # v1.16.0
        if: always()
        with:
          title: phaze/${{ matrix.name }}
          description: |
            Build duration: ${{ steps.timer.outputs.duration || 'N/A' }}s
            Cache used: ${{ matrix.use_cache }}
          webhook: ${{ secrets.DISCORD_WEBHOOK }}
```

Critical specifics:
- The step `name` MUST be wrapped in double quotes with the literal `📢` emoji character (matches the existing file's quoting convention for emoji-prefixed step names — see lines 41, 45, 52, 55, 73, etc., though many use `\u` escape sequences. Use the literal emoji as the file accepts both; verify with yamllint after).
- Action pin MUST be exactly `sarisia/actions-status-discord@eb045afee445dc055c18d3d90bd0f244fd062708 # v1.16.0` — frozen SHA, do not change.
- `if: always()` — runs on success, failure, and cancellation.
- Title MUST be `phaze/${{ matrix.name }}` (NOT `discogsography/...`).
- Description body is a literal block scalar (`|`) with two lines: build duration with `|| 'N/A'` fallback, and cache used flag.
- Webhook MUST reference `${{ secrets.DISCORD_WEBHOOK }}`.
- Do NOT modify the `Collect metrics` step. Do NOT add a `duration` output to the timer step. The `'N/A'` fallback is intentional per the planning brief.
- This step uses `with:` (not `run:`), so `${{ }}` interpolation in `with:` values is allowed and consistent with the rest of the file (see lines 77, 85, 93, 111, etc.).
- Ensure the file ends with a single trailing newline (pre-commit `end-of-file-fixer` hook).
  </action>
  <verify>
    <automated>uv run pre-commit run --files .github/workflows/docker-publish.yml</automated>
  </verify>
  <done>
- New `📢 Send notification to Discord` step exists as the final step in the `build-and-push` job
- Step uses the exact frozen SHA pin `eb045afee445dc055c18d3d90bd0f244fd062708`
- Title is `phaze/${{ matrix.name }}`
- `if: always()` is set
- Webhook references `secrets.DISCORD_WEBHOOK`
- `pre-commit run --files .github/workflows/docker-publish.yml` passes (yamllint + actionlint + check-jsonschema for GitHub workflows all green)
- `Collect metrics` step is unchanged
  </done>
</task>

</tasks>

<verification>
- `uv run pre-commit run --files .github/workflows/docker-publish.yml` passes cleanly (yamllint, actionlint, check-jsonschema, end-of-file-fixer, trailing-whitespace)
- Visual diff shows exactly one new step appended; no other lines modified
- The new step is the LAST step in the `build-and-push` job
</verification>

<success_criteria>
- `.github/workflows/docker-publish.yml` contains a Discord notification step matching the discogsography pattern verbatim (with `phaze/` title prefix)
- All phaze pre-commit hooks pass on the modified file
- No other workflow files, scripts, or configuration are touched
</success_criteria>

<output>
After completion, no SUMMARY file is required for quick tasks. The task is complete when the verify command passes and the change is committed.
</output>
