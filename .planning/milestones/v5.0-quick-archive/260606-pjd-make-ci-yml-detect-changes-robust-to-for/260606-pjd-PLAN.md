---
phase: quick-260606-pjd
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - .github/workflows/ci.yml
  - tests/test_deployment/test_agent_compose.py
autonomous: true
requirements: [CI-DETECT-CHANGES-FORCE-PUSH]
must_haves:
  truths:
    - "A force-pushed branch's push-event detect-changes no longer fails with `fatal: bad object`"
    - "When BEFORE_SHA is unreachable in the clone, the diff falls back to origin/main...HEAD"
    - "Normal pushes, PRs, new-branch pushes, and schedule/dispatch/tag paths are unchanged"
    - "A guard test fails if the reachability fallback is removed"
  artifacts:
    - path: ".github/workflows/ci.yml"
      provides: "detect-changes filter step with force-push reachability fallback"
      contains: "git cat-file -e"
    - path: "tests/test_deployment/test_agent_compose.py"
      provides: "guard test locking in the force-push fallback"
      contains: "def test_ci_detect_changes"
  key_links:
    - from: "tests/test_deployment/test_agent_compose.py"
      to: ".github/workflows/ci.yml filter step run script"
      via: "_ci_detect_changes_filter_step()"
      pattern: "cat-file"
---

<objective>
Make ci.yml's `detect-changes` job robust to force-pushes. On a force-pushed branch the
push-event `id: filter` step runs `git diff "${BEFORE_SHA}" "${HEAD_SHA}"` where
`BEFORE_SHA` (`github.event.before`) is the pre-force-push tip — unreachable in the fresh
CI clone — causing `fatal: bad object <old-tip>` and exit 128.

Purpose: Force-pushed branches must still compute changed files instead of failing CI.
Output: A reachability fallback in the `else` branch of the filter step, plus a guard test.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@./CLAUDE.md

<interfaces>
<!-- Current detect-changes filter step (ci.yml lines 37-74). Only the elif/else
     branch of the diff selection changes. Everything else stays byte-identical. -->

From .github/workflows/ci.yml (the diff-selection block):
```yaml
          if [[ "${EVENT_NAME}" == "pull_request" ]]; then
            CHANGED_FILES=$(git diff --name-only "${BASE_SHA}" "${HEAD_SHA}")
          elif [[ "${BEFORE_SHA}" == "0000000000000000000000000000000000000000" ]]; then
            # First push to a new branch — compare against default branch
            CHANGED_FILES=$(git diff --name-only "origin/main...${HEAD_SHA}")
          else
            CHANGED_FILES=$(git diff --name-only "${BEFORE_SHA}" "${HEAD_SHA}")
          fi
```
Note: the detect-changes checkout already uses `fetch-depth: 0`, and `origin/main` is
already an established assumption in the zero-SHA branch above.

From tests/test_deployment/test_agent_compose.py:
```python
def _ci_detect_changes_filter_step() -> dict[str, Any]:
    """Locate the `detect-changes` job's `id: filter` step in ci.yml."""
    # returns the step dict (has .get("run") and .get("env"))

def test_ci_detect_changes_forces_code_changed_on_tags() -> None:
    # existing style to match: loads step via helper, asserts on str(step.get("run"))
```
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add force-push reachability fallback to ci.yml filter step</name>
  <files>.github/workflows/ci.yml</files>
  <action>
In the `detect-changes` job's `id: filter` step run script, extend ONLY the
`elif "${BEFORE_SHA}" == "0000...0"` condition so it also fires when `BEFORE_SHA`
is not a reachable commit object in this clone (the force-push case). Replace the
existing zero-SHA `elif` line with a compound condition:

  elif [[ "${BEFORE_SHA}" == "0000000000000000000000000000000000000000" ]] || ! git cat-file -e "${BEFORE_SHA}^{commit}" 2>/dev/null; then

Update the inline comment to explain BOTH cases (e.g. "New branch (zero SHA) or
force-pushed branch whose before-SHA is gone — compare against default branch").
The fallback body stays `CHANGED_FILES=$(git diff --name-only "origin/main...${HEAD_SHA}")`.

Constraints:
- Do NOT touch the schedule/dispatch/tag early-exit, the `pull_request` branch, or the
  final `else` (normal-push) branch body.
- `! git cat-file -e ...` is used as a condition, so it does NOT abort under `set -e`.
- Keep it shellcheck/actionlint clean: `BEFORE_SHA` stays quoted; `^{commit}` peeling is
  fine inside double quotes. yamllint strict — preserve the `run: |` block indentation,
  spacing, and emoji step-name style already in the file.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze && python -c "import yaml; s=[x for x in yaml.safe_load(open('.github/workflows/ci.yml'))['jobs']['detect-changes']['steps'] if x.get('id')=='filter'][0]['run']; assert 'git cat-file -e' in s and 'origin/main...' in s, 'fallback missing'; print('OK')"</automated>
  </verify>
  <done>The filter step's `elif` fires for both the zero-SHA new-branch case and an unreachable BEFORE_SHA, falling back to `origin/main...HEAD`; all other branches unchanged.</done>
</task>

<task type="auto">
  <name>Task 2: Add guard test for the force-push fallback</name>
  <files>tests/test_deployment/test_agent_compose.py</files>
  <action>
Add a new `test_ci_detect_changes_*` function (e.g.
`test_ci_detect_changes_survives_force_push`) directly after
`test_ci_detect_changes_forces_code_changed_on_tags`. Reuse
`_ci_detect_changes_filter_step()`. Match the existing CI-test style: a docstring
explaining WHY (force-push makes `github.event.before` unreachable → `fatal: bad object`),
then read `run = str(step.get("run") or "")` and assert the run script contains BOTH:
  1. a reachability probe — `"git cat-file -e" in run` (or `"cat-file" in run`), AND
  2. the default-branch fallback diff — `"origin/main..." in run`.
Use a descriptive assertion message naming the force-push failure mode (as the tag test
does) so a future refactor that drops the guard fails loudly.
  </action>
  <verify>
    <automated>cd /Users/Robert/Code/public/phaze && uv run pytest tests/test_deployment/test_agent_compose.py -q</automated>
  </verify>
  <done>New guard test passes; it asserts the filter step references `git cat-file -e` and an `origin/main...` fallback, and would fail if the reachability fallback is removed.</done>
</task>

</tasks>

<verification>
Run the full target test file and pre-commit hooks (frozen SHAs, NEVER --no-verify):
- `cd /Users/Robert/Code/public/phaze && uv run pytest tests/test_deployment/test_agent_compose.py -q`
- `cd /Users/Robert/Code/public/phaze && pre-commit run --all-files` (actionlint shellcheck-clean on the new `git cat-file` guard, yamllint strict, check-jsonschema)
</verification>

<success_criteria>
- A force-pushed branch's push-event detect-changes falls back to `origin/main...HEAD` instead of erroring with `bad object`.
- Schedule/dispatch/tag early-exit, PR path, zero-SHA new-branch path, and normal-push path are byte-for-byte unchanged in behavior.
- The new guard test passes and fails if the reachability fallback is dropped.
- All pre-commit hooks pass without --no-verify.
</success_criteria>

<output>
Create `.planning/quick/260606-pjd-make-ci-yml-detect-changes-robust-to-for/260606-pjd-SUMMARY.md` when done.
</output>
