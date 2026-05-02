---
phase: quick-260502-lqb
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - .github/workflows/docker-publish.yml
autonomous: true
requirements:
  - QUICK-260502-lqb-01
must_haves:
  truths:
    - "docker-publish.yml workflow contains zero references to Discord"
    - "docker-publish.yml workflow contains zero references to DISCORD_WEBHOOK"
    - "docker-publish.yml remains valid YAML and the build-and-push job still completes its existing build/push/cleanup/metrics steps"
    - "No other file under .github/ references Discord (the workflow under edit was the only one per pre-task grep)"
  artifacts:
    - path: ".github/workflows/docker-publish.yml"
      provides: "Docker build and push workflow without Discord notification"
      contains: "build-and-push"
  key_links:
    - from: ".github/workflows/docker-publish.yml"
      to: "build-and-push job"
      via: "final step is now '📊 Collect metrics' (line ~149) — no trailing Discord step"
      pattern: "Collect metrics"
---

<objective>
Remove the Discord notification step from `.github/workflows/docker-publish.yml` so the workflow no longer pings Discord on Docker image build completion.

Purpose: The user is decommissioning Discord notifications from CI. STATE.md shows the step was added on 2026-04-14 (quick task 260414-quo) and is now being reverted. The repo-level `DISCORD_WEBHOOK` secret will become unused but lives outside version control and is not in scope here.

Output: Updated workflow file with the Discord step deleted, leaving the existing build/push/cleanup/metrics steps untouched.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
</execution_context>

<context>
@.planning/STATE.md
@.github/workflows/docker-publish.yml

<scope_correction>
The orchestrator's planning context referenced a separate `notify` job around lines 165-174. After reading the file, this is incorrect — `docker-publish.yml` has a SINGLE job, `build-and-push` (defined at line 20). The Discord step is the FINAL step *within* that job, at lines 166-174.

Therefore:
- There is NO standalone `notify` job to delete
- Only the Discord step itself needs to be removed
- The job structure (matrix strategy, all preceding steps) stays exactly as-is
- The step immediately before Discord is "📊 Collect metrics" (lines 149-164); after deletion, that becomes the final step of the job
</scope_correction>

<exact_lines_to_remove>
Lines 166-174 (inclusive) of `.github/workflows/docker-publish.yml`:

```yaml
      - name: "📢 Send notification to Discord"
        uses: sarisia/actions-status-discord@eb045afee445dc055c18d3d90bd0f244fd062708  # v1.16.0
        if: always()
        with:
          title: phaze/${{ matrix.name }}
          description: |
            Build duration: ${{ steps.timer.outputs.duration || 'N/A' }}s
            Cache used: ${{ matrix.use_cache }}
          webhook: ${{ secrets.DISCORD_WEBHOOK }}
```

Note: the step's `description` block is multi-line (uses `|`), so the deletion spans the entire 9-line block plus the trailing newline. Remove the blank line *between* the "Collect metrics" step and the Discord step too, so there are no orphan blank lines at the end of the file. The file should end cleanly after line 164 (with a single trailing newline preserved per `end-of-file-fixer` pre-commit hook).
</exact_lines_to_remove>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Remove Discord notification step from docker-publish.yml</name>
  <files>.github/workflows/docker-publish.yml</files>
  <action>
Use the Edit tool to remove the entire Discord notification step (lines 166-174 in the original file) from `.github/workflows/docker-publish.yml`.

Steps:
1. Open the file and locate the block beginning with `      - name: "📢 Send notification to Discord"` (line 166) and ending with `          webhook: ${{ secrets.DISCORD_WEBHOOK }}` (line 174).
2. Delete this entire 9-line block, plus the single blank line that precedes it (separating it from the "Collect metrics" step). The "Collect metrics" step (lines 149-164) becomes the final step in the `build-and-push` job.
3. Preserve the file's trailing newline (required by the `end-of-file-fixer` pre-commit hook).
4. Do NOT modify any other section of the file. The matrix strategy, all preceding steps, the concurrency group, env vars, and permissions stay identical.
5. After the edit, run the verify commands below.

Rationale for the surgical approach: this is a pure deletion — no rewiring, no replacement step, no job-level changes. The `notify` job referenced in the planning context does not exist (there's only one job, `build-and-push`).
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze && grep -ic discord .github/workflows/docker-publish.yml | grep -qx 0 && grep -rci discord .github/ | grep -v ':0$' | grep -qv . && uv run python -c "import yaml; yaml.safe_load(open('.github/workflows/docker-publish.yml'))" && echo "VERIFY OK"</automated>
  </verify>
  <done>
- `grep -ic discord .github/workflows/docker-publish.yml` returns `0`
- `grep -rci discord .github/` reports `0` for every file (no matches anywhere under `.github/`)
- File parses as valid YAML (PyYAML `safe_load` succeeds)
- The "📊 Collect metrics" step is now the last step of the `build-and-push` job
- File ends with a single trailing newline (no double-blank artifacts at EOF)
- `pre-commit run --files .github/workflows/docker-publish.yml` passes (yamllint, actionlint, end-of-file-fixer, trailing-whitespace, check-jsonschema for workflows)
  </done>
</task>

</tasks>

<verification>
Final phase verification (run after the task completes):

```bash
# 1. Zero Discord references anywhere under .github/
grep -rci discord .github/ | grep -v ':0$' && echo "FAIL: Discord references still exist" || echo "PASS: no Discord references"

# 2. Workflow file is valid YAML
uv run python -c "import yaml; doc = yaml.safe_load(open('.github/workflows/docker-publish.yml')); assert 'jobs' in doc and 'build-and-push' in doc['jobs']; print('PASS: valid YAML, build-and-push job present')"

# 3. The build-and-push job still has the expected core steps
uv run python <<'PY'
import yaml
doc = yaml.safe_load(open(".github/workflows/docker-publish.yml"))
steps = [s.get("name", "") for s in doc["jobs"]["build-and-push"]["steps"]]
required = ["Checkout", "Build and push Docker image", "Collect metrics"]
for needle in required:
    assert any(needle in name for name in steps), f"Missing step containing: {needle}"
forbidden = ["Discord"]
for needle in forbidden:
    assert not any(needle in name for name in steps), f"Forbidden step still present: {needle}"
print("PASS: required steps present, Discord step absent")
PY

# 4. Pre-commit hooks pass on the modified file
pre-commit run --files .github/workflows/docker-publish.yml
```

All four checks must pass.
</verification>

<success_criteria>
- `.github/workflows/docker-publish.yml` no longer contains the Discord notification step
- `grep -rci discord .github/` reports `0` for every file under `.github/`
- The `build-and-push` job retains its matrix strategy and all build/push/cleanup/metrics steps unchanged
- Workflow remains syntactically valid YAML and passes actionlint + yamllint
- All pre-commit hooks pass on the modified file
- No commit uses `--no-verify` (per global memory: feedback_no_verify)
- The repo-level `DISCORD_WEBHOOK` secret is intentionally NOT touched (lives outside VCS; out of scope)
</success_criteria>

<output>
After completion, create `.planning/quick/260502-lqb-remove-discord-notification-step-from-do/260502-lqb-SUMMARY.md` documenting:
- Lines removed (final byte-level count) and resulting line count of the file
- Output of `grep -rci discord .github/` proving zero references remain
- Confirmation that pre-commit hooks passed
- Note that the `DISCORD_WEBHOOK` secret remains in repo settings (intentionally untouched)
- Commit SHA of the change
</output>
