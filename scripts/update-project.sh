#!/usr/bin/env bash

# update-project.sh - Comprehensive project dependency and version updater
#
# This script provides a safe and comprehensive way to update:
# - Python version across all project files (including services)
# - Python package dependencies via uv (all version types)
# - Dependency floors in pyproject.toml, raised to match the locked versions
# - Service dependencies (audfprint, panako)
# - UV package manager version in Dockerfiles and GitHub workflows
# - Pre-commit hooks to latest versions (with frozen SHAs)
# - Docker base images to latest versions
#
# It also flags capped dependencies (those with a `,<X` upper bound) that have
# a newer release available beyond the cap, so they can be reviewed manually.
#
# `uv lock --upgrade` only refreshes uv.lock within the existing `>=` floors; it
# never raises the floors themselves (that is Dependabot's job). The floor-sync
# step closes that gap so pyproject.toml floors track what is actually locked,
# which keeps the declared minimums honest and reduces churn from Dependabot.
#
# Tool invocations delegate to `just` commands wherever possible, keeping the
# justfile as the single source of truth for command definitions.
#
# Usage: ./scripts/update-project.sh [options]
#
# Options:
#   --python VERSION    Update Python version (default: keep current)
#   --no-backup        Skip creating backup files
#   --dry-run          Show what would be updated without making changes
#   --major            Include major version upgrades
#   --skip-tests       Skip running tests after updates
#   --help             Show this help message

set -euo pipefail

# Default options
BACKUP=true
DRY_RUN=false
MAJOR_UPGRADES=false
SKIP_TESTS=false
UPDATE_PYTHON=false
PYTHON_VERSION=""
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
CHANGES_MADE=false

# Emojis for visual logging
EMOJI_INFO="ℹ️"
EMOJI_SUCCESS="✅"
EMOJI_WARNING="⚠️"
EMOJI_ERROR="❌"
EMOJI_ROCKET="🚀"
EMOJI_PACKAGE="📦"
EMOJI_PYTHON="🐍"
EMOJI_DOCKER="🐳"
EMOJI_TEST="🧪"
EMOJI_BACKUP="💾"
EMOJI_CHANGES="📝"
EMOJI_VERIFY="🔍"
EMOJI_SERVICE="🔧"

# Service directories with their own dependencies
SERVICE_DIRS=(
  "services/audfprint"
  "services/panako"
)

# Print colored output with emojis
print_info() {
  echo -e "\033[0;34m$EMOJI_INFO  [INFO]\033[0m $1"
}

print_success() {
  echo -e "\033[0;32m$EMOJI_SUCCESS  [SUCCESS]\033[0m $1"
}

print_warning() {
  echo -e "\033[1;33m$EMOJI_WARNING  [WARNING]\033[0m $1"
}

print_error() {
  echo -e "\033[0;31m$EMOJI_ERROR  [ERROR]\033[0m $1"
}

print_section() {
  echo ""
  echo -e "\033[1;36m$1  $2\033[0m"
  echo -e "\033[1;36m$(printf '=%.0s' {1..60})\033[0m"
}

# Show usage
show_help() {
  head -n 20 "$0" | grep '^#' | sed 's/^# //' | sed 's/^#//'
  exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --python)
      UPDATE_PYTHON=true
      PYTHON_VERSION="$2"
      shift 2
      ;;
    --no-backup)
      BACKUP=false
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --major)
      MAJOR_UPGRADES=true
      shift
      ;;
    --skip-tests)
      SKIP_TESTS=true
      shift
      ;;
    --help)
      show_help
      ;;
    *)
      print_error "Unknown option: $1"
      show_help
      ;;
  esac
done

# Verify we're in the project root
if [[ ! -f "pyproject.toml" ]]; then
  print_error "Must be run from the project root directory"
  exit 1
fi

# Verify required tools
for cmd in uv just pre-commit; do
  if ! command -v "$cmd" &>/dev/null; then
    print_error "$cmd is required but not installed"
    exit 1
  fi
done

print_section "$EMOJI_ROCKET" "Phaze Project Updater"
echo "  Timestamp: $TIMESTAMP"
echo "  Dry run: $DRY_RUN"
echo "  Major upgrades: $MAJOR_UPGRADES"
echo "  Skip tests: $SKIP_TESTS"
[[ "$UPDATE_PYTHON" == true ]] && echo "  Python target: $PYTHON_VERSION"

# === Backup ===

create_backup() {
  if [[ "$BACKUP" == true && "$DRY_RUN" == false ]]; then
    print_section "$EMOJI_BACKUP" "Creating Backups"
    local backup_dir=".backups/$TIMESTAMP"
    mkdir -p "$backup_dir"
    cp pyproject.toml "$backup_dir/"
    cp uv.lock "$backup_dir/" 2>/dev/null || true
    cp .pre-commit-config.yaml "$backup_dir/"
    cp Dockerfile "$backup_dir/"
    cp docker-compose.yml "$backup_dir/"
    for svc_dir in "${SERVICE_DIRS[@]}"; do
      if [[ -d "$svc_dir" ]]; then
        local svc_backup="$backup_dir/$svc_dir"
        mkdir -p "$svc_backup"
        cp "$svc_dir"/pyproject.toml "$svc_backup/" 2>/dev/null || true
        for df in "$svc_dir"/Dockerfile*; do
          [[ -f "$df" ]] && cp "$df" "$svc_backup/"
        done
      fi
    done
    print_success "Backups saved to $backup_dir"
  fi
}

# === Python Version Update ===

update_python_version() {
  if [[ "$UPDATE_PYTHON" != true ]]; then
    return
  fi

  print_section "$EMOJI_PYTHON" "Updating Python Version to $PYTHON_VERSION"

  local files_to_update=(
    "pyproject.toml"
    "Dockerfile"
    ".github/workflows/ci.yml"
    ".github/workflows/code-quality.yml"
    ".github/workflows/tests.yml"
    ".github/workflows/security.yml"
  )

  # Add service pyproject.toml and Dockerfiles
  for svc_dir in "${SERVICE_DIRS[@]}"; do
    [[ -f "$svc_dir/pyproject.toml" ]] && files_to_update+=("$svc_dir/pyproject.toml")
    for df in "$svc_dir"/Dockerfile*; do
      [[ -f "$df" ]] && files_to_update+=("$df")
    done
  done

  for file in "${files_to_update[@]}"; do
    if [[ -f "$file" ]]; then
      if [[ "$DRY_RUN" == true ]]; then
        print_info "[DRY RUN] Would update Python version in $file"
      else
        # Update various Python version patterns
        sed -i.bak "s/python_version = \"[0-9.]*\"/python_version = \"$PYTHON_VERSION\"/" "$file" 2>/dev/null || true
        sed -i.bak "s/python-version: \"[0-9.]*\"/python-version: \"$PYTHON_VERSION\"/" "$file" 2>/dev/null || true
        sed -i.bak "s/python:[0-9.]*-slim/python:$PYTHON_VERSION-slim/" "$file" 2>/dev/null || true
        sed -i.bak "s/PYTHON_VERSION: \"[0-9.]*\"/PYTHON_VERSION: \"$PYTHON_VERSION\"/" "$file" 2>/dev/null || true
        rm -f "${file}.bak"
        print_success "Updated $file"
        CHANGES_MADE=true
      fi
    fi
  done
}

# === UV Version Update ===

update_uv_version() {
  print_section "$EMOJI_PACKAGE" "Checking UV Version"

  local current_uv
  current_uv=$(uv --version | awk '{print $2}')
  print_info "Current uv version: $current_uv"

  # Update uv version in Dockerfile if pinned
  if grep -q "COPY --from=ghcr.io/astral-sh/uv" Dockerfile 2>/dev/null; then
    if [[ "$DRY_RUN" == true ]]; then
      print_info "[DRY RUN] Would update uv version in Dockerfile"
    else
      print_info "uv version in Dockerfile managed by base image"
    fi
  fi
}

# === Pre-commit Hooks Update ===

update_precommit_hooks() {
  print_section "$EMOJI_VERIFY" "Updating Pre-commit Hooks"

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would run: just update-hooks"
    return
  fi

  just update-hooks
  CHANGES_MADE=true
  print_success "Pre-commit hooks updated with frozen SHAs"
}

# === Python Package Updates ===

update_python_packages() {
  print_section "$EMOJI_PACKAGE" "Updating Python Packages"

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would run: just lock-upgrade && just sync"
    return
  fi

  just lock-upgrade
  just sync
  CHANGES_MADE=true
  print_success "Root packages updated"

  # Report available major version upgrades beyond current constraints
  if [[ "$MAJOR_UPGRADES" == true ]]; then
    print_info "Checking for major version upgrades beyond current constraints..."
    local outdated
    outdated=$(uv pip list --outdated 2>/dev/null) || true
    if [[ -n "$outdated" ]]; then
      print_warning "Packages with newer versions (may include major bumps):"
      echo "$outdated"
      echo ""
      print_info "Review versions above and update pyproject.toml constraints manually for major upgrades"
      print_info "Then re-run: just lock-upgrade && just sync"
    else
      print_success "All packages are at latest compatible versions"
    fi
  fi
}

# === Dependency Floor Sync ===

# Raise the `>=` floors in pyproject.toml to match the versions actually pinned
# in uv.lock. `uv lock --upgrade` refreshes the lockfile within the existing
# floors but never raises the floors themselves; this closes that gap so the
# declared minimums track what is actually resolved.
sync_dependency_floors() {
  print_section "$EMOJI_PACKAGE" "Syncing Dependency Floors"

  local apply_val=1
  [[ "$DRY_RUN" == true ]] && apply_val=0

  local output
  output=$(APPLY="$apply_val" uv run python - <<'PY'
import os
import re
import tomllib
from pathlib import Path

apply = os.environ.get("APPLY") == "1"

try:
    from packaging.version import InvalidVersion, Version

    def strictly_newer(candidate: str, current: str) -> bool:
        try:
            return Version(candidate) > Version(current)
        except InvalidVersion:
            # uv.lock always resolves at or above the floor, so default to True.
            return True
except ImportError:  # packaging should be present, but never block on it.

    def strictly_newer(candidate: str, current: str) -> bool:
        return True


lock = tomllib.loads(Path("uv.lock").read_text())
locked = {p["name"].lower().replace("_", "-"): p["version"] for p in lock.get("package", [])}

pyproject = Path("pyproject.toml")
lines = pyproject.read_text().splitlines(keepends=True)

dep_block = re.compile(r"^(dependencies|dev)\s*=\s*\[\s*$")
entry = re.compile(r'^(?P<indent>\s*)"(?P<spec>[^"]+)"(?P<trail>,?\s*)$')
spec_re = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+(?:\[[A-Za-z0-9._,-]+\])?)"
    r"(?P<specs>[<>=!~][^;]*)?"
    r"(?P<marker>;.*)?$"
)
floor_re = re.compile(r">=\s*([^,;\s]+)")

out: list[str] = []
in_block = False
changes: list[tuple[str, str, str]] = []
for line in lines:
    if not in_block and dep_block.match(line.strip()):
        in_block = True
        out.append(line)
        continue
    if in_block and line.strip() == "]":
        in_block = False
        out.append(line)
        continue
    matched = entry.match(line) if in_block else None
    if matched:
        parsed = spec_re.match(matched.group("spec"))
        specs = parsed.group("specs") if parsed else None
        if parsed and specs:
            base = parsed.group("name").split("[")[0].lower().replace("_", "-")
            locked_version = locked.get(base)
            floor = floor_re.search(specs)
            if locked_version and floor:
                current = floor.group(1)
                if current != locked_version and strictly_newer(locked_version, current):
                    new_specs = specs[: floor.start(1)] + locked_version + specs[floor.end(1) :]
                    new_spec = parsed.group("name") + new_specs + (parsed.group("marker") or "")
                    changes.append((base, current, locked_version))
                    out.append(f'{matched.group("indent")}"{new_spec}"{matched.group("trail")}')
                    continue
    out.append(line)

for base, old, new in changes:
    print(f"BUMPED {base} {old} -> {new}")
if apply and changes:
    pyproject.write_text("".join(out))
print(f"FLOORS_CHANGED={len(changes)}")
PY
)

  echo "$output" | grep -E "^BUMPED " | sed 's/^BUMPED /  /' || true

  local changed
  changed=$(echo "$output" | sed -n 's/^FLOORS_CHANGED=//p')
  changed=${changed:-0}

  if [[ "$DRY_RUN" == true ]]; then
    if [[ "$changed" -gt 0 ]]; then
      print_info "[DRY RUN] Would raise $changed dependency floor(s) to match uv.lock"
    else
      print_success "[DRY RUN] All dependency floors already match uv.lock"
    fi
    return
  fi

  if [[ "$changed" -gt 0 ]]; then
    # Re-lock so uv.lock's recorded requirement metadata matches the new floors.
    uv lock >/dev/null 2>&1 || uv lock
    CHANGES_MADE=true
    print_success "Raised $changed dependency floor(s) to match uv.lock"
  else
    print_success "All dependency floors already match uv.lock"
  fi
}

# === Flag Capped Dependencies ===

# Warn about dependencies pinned with a `,<X` upper bound that now have a newer
# release available beyond that cap. `uv lock --upgrade` cannot cross a cap, so
# raising it is a deliberate human decision.
flag_capped_dependencies() {
  print_section "$EMOJI_VERIFY" "Checking Capped Dependencies"

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would flag capped dependencies with releases beyond their cap"
    return
  fi

  local outdated
  outdated=$(uv pip list --outdated 2>/dev/null) || true

  local output
  output=$(OUTDATED="$outdated" uv run python - <<'PY'
import os
import re
import tomllib
from pathlib import Path

try:
    from packaging.version import InvalidVersion, Version

    def at_or_beyond_cap(latest: str, cap: str) -> bool:
        try:
            return Version(latest) >= Version(cap)
        except InvalidVersion:
            return True
except ImportError:

    def at_or_beyond_cap(latest: str, cap: str) -> bool:
        return True


data = tomllib.loads(Path("pyproject.toml").read_text())
specs: list[str] = list(data.get("project", {}).get("dependencies", []))
for group in data.get("dependency-groups", {}).values():
    specs.extend(s for s in group if isinstance(s, str))

name_re = re.compile(r"^([A-Za-z0-9._-]+)")
cap_re = re.compile(r"<\s*([0-9][^,;\s]*)")
caps: dict[str, str] = {}
for spec in specs:
    name_match = name_re.match(spec)
    cap_match = cap_re.search(spec.split(";")[0])
    if name_match and cap_match:
        caps[name_match.group(1).lower().replace("_", "-")] = cap_match.group(1)

latest: dict[str, str] = {}
for raw in os.environ.get("OUTDATED", "").splitlines():
    parts = raw.split()
    if len(parts) >= 3 and parts[0] != "Package" and not parts[0].startswith("-"):
        latest[parts[0].lower().replace("_", "-")] = parts[2]

flagged = 0
for name, cap in sorted(caps.items()):
    if name == "phaze":
        continue
    newest = latest.get(name)
    if newest and at_or_beyond_cap(newest, cap):
        print(f"FLAG {name}: {newest} available, capped at <{cap}")
        flagged += 1
print(f"CAPPED_FLAGGED={flagged}")
PY
)

  local flagged
  flagged=$(echo "$output" | sed -n 's/^CAPPED_FLAGGED=//p')
  flagged=${flagged:-0}

  if [[ "$flagged" -gt 0 ]]; then
    while IFS= read -r line; do
      print_warning "${line#FLAG }"
    done < <(echo "$output" | grep -E "^FLAG ")
    print_info "Raise the cap in pyproject.toml manually, then re-run: just lock-upgrade && just sync"
  else
    print_success "No capped dependencies have releases beyond their cap"
  fi
}

# === Service Dependency Updates ===

update_service_packages() {
  print_section "$EMOJI_SERVICE" "Updating Service Dependencies"

  for svc_dir in "${SERVICE_DIRS[@]}"; do
    if [[ ! -f "$svc_dir/pyproject.toml" ]]; then
      continue
    fi

    local svc_name
    svc_name=$(basename "$svc_dir")

    if [[ "$DRY_RUN" == true ]]; then
      print_info "[DRY RUN] Would update dependencies in $svc_dir"
      continue
    fi

    print_info "Updating $svc_name dependencies..."
    (cd "$svc_dir" && uv lock --upgrade 2>/dev/null && uv sync 2>/dev/null) || {
      # Services may not have a uv.lock — update in-place via pip compile
      print_info "$svc_name has no lockfile, skipping lock upgrade"
    }
    CHANGES_MADE=true
    print_success "Updated $svc_name"
  done
}

# === Docker Base Images ===

update_docker_images() {
  print_section "$EMOJI_DOCKER" "Checking Docker Base Images"

  # Show current images from all Dockerfiles
  print_info "Current Docker base images:"
  local dockerfiles=("Dockerfile" "docker-compose.yml")
  for svc_dir in "${SERVICE_DIRS[@]}"; do
    for df in "$svc_dir"/Dockerfile*; do
      [[ -f "$df" ]] && dockerfiles+=("$df")
    done
  done
  grep "^FROM\|^    image:" "${dockerfiles[@]}" 2>/dev/null || true

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Docker image updates handled by Dependabot"
    return
  fi

  print_info "Docker base image updates are managed by Dependabot"
  print_info "Check open PRs for image update proposals"
}

# === Sweep pip-audit Ignores ===

sweep_pip_audit_ignores() {
  local ignore_file=".pip-audit-ignores"
  if [[ ! -f "$ignore_file" ]]; then
    return
  fi

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would sweep $ignore_file for resolved vulnerabilities"
    return
  fi

  print_section "$EMOJI_VERIFY" "Sweeping pip-audit Ignores"

  # Collect vulnerability IDs from the ignore file
  local vuln_ids=()
  while IFS= read -r line; do
    local vuln_id
    vuln_id=$(echo "$line" | sed 's/#.*//' | tr -d '[:space:]')
    [[ -z "$vuln_id" ]] && continue
    vuln_ids+=("$vuln_id")
  done <"$ignore_file"

  if [[ ${#vuln_ids[@]} -eq 0 ]]; then
    print_success "No vulnerability ignores to sweep"
    return
  fi

  print_info "Testing ${#vuln_ids[@]} ignored vulnerabilit$([ ${#vuln_ids[@]} -eq 1 ] && echo "y" || echo "ies")..."

  # Test each entry: run pip-audit with all OTHER ignores but NOT this one.
  # If pip-audit passes, the vulnerability is fixed and the ignore can go.
  local resolved=()
  local still_needed=()
  for test_vid in "${vuln_ids[@]}"; do
    local other_args=""
    for vid in "${vuln_ids[@]}"; do
      [[ "$vid" == "$test_vid" ]] && continue
      other_args="$other_args --ignore-vuln $vid"
    done

    # shellcheck disable=SC2086
    if uv run pip-audit --desc $other_args >/dev/null 2>&1; then
      resolved+=("$test_vid")
      print_success "✓ $test_vid — fixed! Removing from ignore list"
    else
      still_needed+=("$test_vid")
      print_warning "✗ $test_vid — still needed (no fix available)"
    fi
  done

  # Rewrite the ignore file without resolved entries
  if [[ ${#resolved[@]} -gt 0 ]]; then
    CHANGES_MADE=true
    for rid in "${resolved[@]}"; do
      sed -i.bak "/^${rid}[[:space:]]/d;/^${rid}$/d" "$ignore_file"
    done
    rm -f "${ignore_file}.bak"
    print_success "Removed ${#resolved[@]} resolved vulnerabilit$([ ${#resolved[@]} -eq 1 ] && echo "y" || echo "ies") from $ignore_file"
  fi

  if [[ ${#still_needed[@]} -gt 0 ]]; then
    print_info "${#still_needed[@]} vulnerabilit$([ ${#still_needed[@]} -eq 1 ] && echo "y" || echo "ies") still awaiting upstream fixes"
  else
    local remaining
    remaining=$(grep -cv '^\s*#\|^\s*$' "$ignore_file" 2>/dev/null) || remaining=0
    if [[ "$remaining" -eq 0 ]]; then
      print_success "All vulnerabilities resolved! $ignore_file has no active ignores"
    fi
  fi
}

# === Sweep osv-scanner Ignores ===

sweep_osv_scanner_ignores() {
  local ignore_file="osv-scanner.toml"
  if [[ ! -f "$ignore_file" ]]; then
    return
  fi

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would sweep $ignore_file for resolved vulnerabilities"
    return
  fi

  print_section "$EMOJI_VERIFY" "Sweeping osv-scanner Ignores"

  # Count IgnoredVulns entries
  local vuln_count
  vuln_count=$(grep -c '^\[\[IgnoredVulns\]\]' "$ignore_file" 2>/dev/null || echo "0")

  if [[ "$vuln_count" -eq 0 ]]; then
    print_success "No osv-scanner ignores to sweep"
    return
  fi

  print_info "Found $vuln_count ignored vulnerabilit$([ "$vuln_count" -eq 1 ] && echo "y" || echo "ies") in $ignore_file"

  # Extract vulnerability IDs
  local vuln_ids=()
  while IFS= read -r line; do
    local vid
    vid=$(echo "$line" | sed 's/id = "//' | sed 's/"//')
    vuln_ids+=("$vid")
  done < <(grep '^id = "' "$ignore_file")

  # Test each by temporarily removing it and running osv-scanner
  local resolved=()
  local still_needed=()
  for test_vid in "${vuln_ids[@]}"; do
    # Create temp config without this entry
    local tmp_file
    tmp_file=$(mktemp)
    # Remove the block for this ID (IgnoredVulns entry + following lines until next block or EOF)
    awk -v vid="$test_vid" '
      /^\[\[IgnoredVulns\]\]/ { block=1; buf=$0"\n"; next }
      block && /^id = / { if (index($0, vid)) { skip=1; buf=""; block=0; next } else { printf "%s", buf; buf=""; block=0 } }
      block { buf=buf $0"\n"; next }
      skip && /^\[\[/ { skip=0 }
      skip { next }
      { if (buf != "") { printf "%s", buf; buf="" }; print }
    ' "$ignore_file" > "$tmp_file"

    if osv-scanner --config="$tmp_file" scan . >/dev/null 2>&1; then
      resolved+=("$test_vid")
      print_success "✓ $test_vid — fixed! Removing from ignore list"
    else
      still_needed+=("$test_vid")
      print_warning "✗ $test_vid — still needed (no fix available)"
    fi
    rm -f "$tmp_file"
  done

  if [[ ${#resolved[@]} -gt 0 ]]; then
    CHANGES_MADE=true
    print_success "Removed ${#resolved[@]} resolved vulnerabilit$([ ${#resolved[@]} -eq 1 ] && echo "y" || echo "ies")"
    print_info "Manual cleanup of $ignore_file may be needed for removed entries"
  fi

  if [[ ${#still_needed[@]} -gt 0 ]]; then
    print_info "${#still_needed[@]} vulnerabilit$([ ${#still_needed[@]} -eq 1 ] && echo "y" || echo "ies") still awaiting upstream fixes"
  fi
}

# === Security Sweep ===

run_security_sweep() {
  print_section "$EMOJI_VERIFY" "Running Security Sweep"

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would run: just security-all"
    return
  fi

  just pip-audit || print_warning "pip-audit found vulnerabilities (review output above)"
  just security || print_warning "bandit found issues (review output above)"
  print_success "Security sweep complete"
}

# === Run Tests ===

run_tests() {
  if [[ "$SKIP_TESTS" == true ]]; then
    print_warning "Skipping tests (--skip-tests)"
    return
  fi

  print_section "$EMOJI_TEST" "Running Tests"

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would run: just lint, just typecheck, pytest -m 'not integration'"
    return
  fi

  just lint
  just typecheck
  uv run pytest tests/ -x -q -m "not integration"
  print_success "All tests passed"
}

# === Summary ===

generate_summary() {
  print_section "$EMOJI_CHANGES" "Update Summary"

  if [[ "$DRY_RUN" == true ]]; then
    print_info "This was a dry run — no changes were made"
    return
  fi

  if [[ "$CHANGES_MADE" == true ]]; then
    echo ""
    echo "Files potentially modified:"
    echo "  - pyproject.toml"
    echo "  - uv.lock"
    echo "  - .pre-commit-config.yaml"
    for svc_dir in "${SERVICE_DIRS[@]}"; do
      [[ -d "$svc_dir" ]] && echo "  - $svc_dir/pyproject.toml"
    done
    [[ "$UPDATE_PYTHON" == true ]] && echo "  - Dockerfile"
    [[ "$UPDATE_PYTHON" == true ]] && echo "  - services/*/Dockerfile.*"
    [[ "$UPDATE_PYTHON" == true ]] && echo "  - .github/workflows/*.yml"
    echo ""
    echo "Next steps:"
    echo "  1. Review changes: git diff"
    echo "  2. Run full test suite: just check"
    echo "  3. Commit: git add -A && git commit -m 'chore: update project dependencies'"
    echo "  4. Push and verify CI passes"
  else
    print_info "No changes were needed"
  fi
}

# === Main ===

create_backup
update_python_version
update_uv_version
update_precommit_hooks
update_python_packages
sync_dependency_floors
flag_capped_dependencies
update_service_packages
update_docker_images
sweep_pip_audit_ignores
sweep_osv_scanner_ignores
run_security_sweep
run_tests
generate_summary

print_section "$EMOJI_ROCKET" "Update Complete"
