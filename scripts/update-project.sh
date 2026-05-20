#!/usr/bin/env bash

# update-project.sh - Comprehensive project dependency and version updater
#
# This script provides a safe and comprehensive way to update:
# - Python version across all project files (root + services)
# - Python package dependencies via uv (all version types)
# - Dependency floors in pyproject.toml, raised to match the locked versions
# - Service dependencies (audfprint, panako)
# - Node.js dependencies (any package.json; no-op while phaze is CDN-based)
# - uv binary pin in Dockerfiles + setup-uv action SHA pin in workflows
# - Pre-commit hooks to latest versions (with frozen SHAs)
# - Docker base images (managed by Dependabot)
#
# It also flags capped dependencies (those with a `,<X` upper bound) that have
# a newer release available beyond the cap, so they can be reviewed manually.
#
# Tool invocations delegate to `just` commands wherever possible, keeping the
# justfile as the single source of truth for command definitions.
#
# Ecosystem behavior:
#   Python (uv):  `uv lock --upgrade` refreshes uv.lock within the existing
#                 `>=` floors (this includes major bumps). It never raises the
#                 floors themselves, so sync_dependency_floors() does that after
#                 the lock so pyproject.toml minimums track what is locked.
#   uv binary:    Dockerfiles pin `ghcr.io/astral-sh/uv:latest`; the setup-uv
#                 GitHub Action is tracked by Dependabot. Nothing to bump here.
#
# Usage: ./scripts/update-project.sh [options]
#
# Options:
#   --python VERSION   Update Python version (default: keep current)
#   --no-backup        Skip creating backup files
#   --dry-run          Show what would be updated without making changes
#   --major            Report major version upgrades beyond current constraints
#   --skip-tests       Skip running tests after updates
#   --help, -h         Show this help message

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
EMOJI_GIT="🔀"

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

# Show usage — print the leading comment block (without the shebang)
show_help() {
  sed -n '3,/^$/p' "$0" | sed 's/^# \{0,1\}//;s/^#$//'
  exit 0
}

# Portable in-place sed (BSD/macOS vs GNU)
sed_inplace() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "$1" "$2"
  else
    sed -i "$1" "$2"
  fi
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
    --help | -h)
      show_help
      ;;
    *)
      print_error "Unknown option: $1"
      show_help
      ;;
  esac
done

# Verify we're in the project root
if [[ ! -f "pyproject.toml" ]] || [[ ! -f "uv.lock" ]]; then
  print_error "This script must be run from the project root directory"
  exit 1
fi

# Verify required tools
for cmd in uv just pre-commit git curl jq; do
  if ! command -v "$cmd" &>/dev/null; then
    print_error "$cmd is required but not installed"
    exit 1
  fi
done

# Warn (don't block) on a dirty tree so rollback stays meaningful
if [[ -n $(git status --porcelain) ]]; then
  print_warning "You have uncommitted changes. Consider committing or stashing them for safe rollback."
  print_info "Continuing anyway since we're in automated mode..."
fi

# === Backups ===

BACKUP_DIR=".backups/project-updates-${TIMESTAMP}"
if [[ "$BACKUP" == true ]] && [[ "$DRY_RUN" == false ]]; then
  mkdir -p "$BACKUP_DIR"
  print_info "$EMOJI_BACKUP Creating backups in $BACKUP_DIR/"
fi

backup_file() {
  local file=$1
  if [[ "$BACKUP" == true ]] && [[ -f "$file" ]] && [[ "$DRY_RUN" == false ]]; then
    local backup_path
    backup_path="$BACKUP_DIR/$(dirname "$file")"
    mkdir -p "$backup_path"
    cp "$file" "$backup_path/$(basename "$file").backup"
  fi
}

# === Change tracking ===

# Regular arrays for broad bash compatibility
PACKAGE_CHANGES=()
FILE_CHANGES=()
PYTHON_VERSION_CHANGE=""
SECURITY_PIP_RESOLVED=0
SECURITY_PIP_REMAINING=0
SECURITY_OSV_RESOLVED=0
SECURITY_OSV_REMAINING=0

# Safe array length under `set -u`
array_length() {
  local array_name=$1
  eval "echo \${#${array_name}[@]}" 2>/dev/null || echo 0
}

# Diff uv.lock before/after to populate the package-change summary
capture_package_changes() {
  if [[ "$DRY_RUN" == true ]]; then
    return
  fi

  if [[ -f "$BACKUP_DIR/uv.lock.backup" ]]; then
    print_info "$EMOJI_CHANGES Analyzing package changes..."

    local old_packages new_packages
    old_packages=$(grep -E "^name = |^version = " "$BACKUP_DIR/uv.lock.backup" | paste -d' ' - - | sed 's/name = "\(.*\)" version = "\(.*\)"/\1==\2/')
    new_packages=$(grep -E "^name = |^version = " "uv.lock" | paste -d' ' - - | sed 's/name = "\(.*\)" version = "\(.*\)"/\1==\2/')

    while IFS= read -r old_pkg; do
      local pkg_name old_version new_version
      pkg_name=$(echo "$old_pkg" | cut -d'=' -f1)
      old_version=$(echo "$old_pkg" | cut -d'=' -f3)
      new_version=$(echo "$new_packages" | grep "^$pkg_name==" | cut -d'=' -f3 || echo "")

      if [[ -n "$new_version" ]] && [[ "$old_version" != "$new_version" ]]; then
        PACKAGE_CHANGES+=("$pkg_name: $old_version → $new_version")
        CHANGES_MADE=true
      fi
    done <<<"$old_packages"
  fi
}

# === Python Version Update ===

update_python_version() {
  if [[ "$UPDATE_PYTHON" != true ]]; then
    return
  fi

  print_section "$EMOJI_PYTHON" "Updating Python Version to $PYTHON_VERSION"

  local current_version
  current_version=$(grep 'requires-python = ">=' pyproject.toml | head -1 | sed 's/.*>=\([0-9.]*\).*/\1/')
  PYTHON_VERSION_CHANGE="$current_version → $PYTHON_VERSION"

  if [[ "$current_version" == "$PYTHON_VERSION" ]]; then
    print_info "Python version is already $PYTHON_VERSION"
    return
  fi

  # Derived formats: ruff target (py314) and the next minor for the upper bound
  local ruff_target next_minor major minor
  ruff_target="py${PYTHON_VERSION//./}"
  major="${PYTHON_VERSION%%.*}"
  minor="${PYTHON_VERSION##*.}"
  next_minor="${major}.$((minor + 1))"

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would update Python $current_version → $PYTHON_VERSION in:"
    print_info "  • pyproject.toml (root + services): requires-python, mypy, ruff target"
    print_info "  • .github/workflows/*.yml (PYTHON_VERSION)"
    print_info "  • Dockerfile(s) (python:X-slim)"
    return
  fi

  # pyproject.toml files (root + services)
  for pyproject in pyproject.toml "${SERVICE_DIRS[@]/%//pyproject.toml}"; do
    [[ -f "$pyproject" ]] || continue
    backup_file "$pyproject"
    # requires-python range form (">=X.Y,<A.B") then simple form (">=X.Y")
    sed_inplace "s/requires-python = \">=[0-9.]*,<[0-9.]*\"/requires-python = \">=$PYTHON_VERSION,<$next_minor\"/" "$pyproject"
    sed_inplace "s/requires-python = \">=[0-9.]*\"/requires-python = \">=$PYTHON_VERSION\"/" "$pyproject"
    # mypy python_version and ruff target-version
    sed_inplace "s/python_version = \"[0-9.]*\"/python_version = \"$PYTHON_VERSION\"/" "$pyproject"
    sed_inplace "s/target-version = \"py[0-9]*\"/target-version = \"$ruff_target\"/" "$pyproject"
    print_success "Updated $pyproject"
    FILE_CHANGES+=("$pyproject: Python $current_version → $PYTHON_VERSION")
    CHANGES_MADE=true
  done

  # GitHub workflows that reference PYTHON_VERSION
  for workflow in .github/workflows/*.yml; do
    if [[ -f "$workflow" ]] && grep -q "PYTHON_VERSION" "$workflow"; then
      backup_file "$workflow"
      sed_inplace "s/PYTHON_VERSION: \"[0-9.]*\"/PYTHON_VERSION: \"$PYTHON_VERSION\"/" "$workflow"
      print_success "Updated $workflow"
      FILE_CHANGES+=("$workflow: Python $current_version → $PYTHON_VERSION")
      CHANGES_MADE=true
    fi
  done

  # Dockerfiles (root + services)
  local dockerfiles=("Dockerfile")
  for svc_dir in "${SERVICE_DIRS[@]}"; do
    for df in "$svc_dir"/Dockerfile*; do
      [[ -f "$df" ]] && dockerfiles+=("$df")
    done
  done
  for df in "${dockerfiles[@]}"; do
    if [[ -f "$df" ]] && grep -q "python:[0-9.]*-slim" "$df"; then
      backup_file "$df"
      sed_inplace "s/python:[0-9.]*-slim/python:$PYTHON_VERSION-slim/" "$df"
      print_success "Updated $df"
      FILE_CHANGES+=("$df: Python $current_version → $PYTHON_VERSION")
      CHANGES_MADE=true
    fi
  done
}

# === UV Version ===

# Pin the uv binary in Dockerfiles to the latest uv release, and SHA-pin the
# astral-sh/setup-uv GitHub Action (with a `# vX.Y` comment) in workflows.
update_uv_version() {
  print_section "$EMOJI_DOCKER" "Updating UV Version"

  # Latest uv release (for the Docker image pin)
  local latest_uv
  latest_uv=$(curl -s https://api.github.com/repos/astral-sh/uv/releases/latest | jq -r '.tag_name' | sed 's/^v//')
  if [[ -z "$latest_uv" ]] || [[ "$latest_uv" == "null" ]]; then
    print_warning "Could not determine latest uv version from GitHub (rate limited?)"
    return
  fi
  print_info "Latest uv version: $latest_uv"

  # Latest setup-uv release tag + its commit SHA (for the action pin)
  local latest_setup_uv latest_setup_uv_commit
  latest_setup_uv=$(curl -s https://api.github.com/repos/astral-sh/setup-uv/releases/latest | jq -r '.tag_name')
  latest_setup_uv_commit=$(curl -s "https://api.github.com/repos/astral-sh/setup-uv/commits/$latest_setup_uv" | jq -r '.sha')
  if [[ -n "$latest_setup_uv" ]] && [[ "$latest_setup_uv" != "null" ]]; then
    print_info "Latest setup-uv action: $latest_setup_uv (commit: ${latest_setup_uv_commit:0:7})"
  fi

  # Collect Dockerfiles (root + services)
  local dockerfiles=("Dockerfile") svc_dir df
  for svc_dir in "${SERVICE_DIRS[@]}"; do
    for df in "$svc_dir"/Dockerfile*; do
      [[ -f "$df" ]] && dockerfiles+=("$df")
    done
  done

  # Current pinned uv version in Dockerfiles
  local current_uv=""
  for df in "${dockerfiles[@]}"; do
    [[ -f "$df" ]] || continue
    current_uv=$(grep -oE "ghcr.io/astral-sh/uv:[^ ]+" "$df" | head -1 | cut -d: -f2)
    [[ -n "$current_uv" ]] && break
  done

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would pin uv image $current_uv → $latest_uv and setup-uv → ${latest_setup_uv_commit:0:7} ($latest_setup_uv)"
    return
  fi

  # Pin uv image in Dockerfiles
  if [[ -n "$current_uv" ]] && [[ "$current_uv" != "$latest_uv" ]]; then
    for df in "${dockerfiles[@]}"; do
      [[ -f "$df" ]] || continue
      backup_file "$df"
      sed_inplace "s|ghcr.io/astral-sh/uv:[^ ]*|ghcr.io/astral-sh/uv:$latest_uv|" "$df"
      print_success "Updated $df (uv $current_uv → $latest_uv)"
      FILE_CHANGES+=("$df: uv $current_uv → $latest_uv")
      CHANGES_MADE=true
    done
  else
    print_success "uv image in Dockerfiles already at ${current_uv:-unknown}"
  fi

  # SHA-pin setup-uv action in workflows
  if [[ -n "$latest_setup_uv_commit" ]] && [[ "$latest_setup_uv_commit" != "null" ]]; then
    local workflow current_ref
    for workflow in .github/workflows/*.yml; do
      [[ -f "$workflow" ]] && grep -q "astral-sh/setup-uv@" "$workflow" || continue
      current_ref=$(grep -oE "astral-sh/setup-uv@[A-Za-z0-9.]+" "$workflow" | head -1 | cut -d'@' -f2)
      if [[ "$current_ref" != "$latest_setup_uv_commit" ]]; then
        backup_file "$workflow"
        sed_inplace "s|\(astral-sh/setup-uv@\).*|\1$latest_setup_uv_commit  # $latest_setup_uv|" "$workflow"
        print_success "Updated $(basename "$workflow") (setup-uv → ${latest_setup_uv_commit:0:7} $latest_setup_uv)"
        FILE_CHANGES+=("$(basename "$workflow"): setup-uv → ${latest_setup_uv_commit:0:7} ($latest_setup_uv)")
        CHANGES_MADE=true
      fi
    done
  fi
}

# === Pre-commit Hooks ===

update_precommit_hooks() {
  print_section "$EMOJI_VERIFY" "Updating Pre-commit Hooks"

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would run: just update-hooks"
    return
  fi

  backup_file ".pre-commit-config.yaml"
  if just update-hooks; then
    print_success "Pre-commit hooks updated with frozen SHAs"
    FILE_CHANGES+=(".pre-commit-config.yaml: Updated hooks to latest (frozen SHAs)")
    CHANGES_MADE=true
  else
    print_warning "Failed to update pre-commit hooks"
  fi
}

# === Python Package Updates ===

update_python_packages() {
  print_section "$EMOJI_PACKAGE" "Updating Python Packages"

  if [[ "$BACKUP" == true ]] && [[ "$DRY_RUN" == false ]]; then
    backup_file "uv.lock"
    backup_file "pyproject.toml"
  fi

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would run: just lock-upgrade && just sync"
    return
  fi

  if just lock-upgrade && just sync; then
    print_success "Root packages updated"
    capture_package_changes
  else
    print_error "Failed to update root packages"
    exit 1
  fi

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
    FILE_CHANGES+=("pyproject.toml: raised $changed dependency floor(s) to match uv.lock")
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

    backup_file "$svc_dir/pyproject.toml"
    backup_file "$svc_dir/uv.lock"

    print_info "Updating $svc_name dependencies..."
    (cd "$svc_dir" && uv lock --upgrade 2>/dev/null && uv sync 2>/dev/null) || {
      # Services may not have a uv.lock — update in-place via pip compile
      print_info "$svc_name has no lockfile, skipping lock upgrade"
    }
    CHANGES_MADE=true
    print_success "Updated $svc_name"
  done
}

# === Node.js Dependency Updates ===

# Update Node.js dependencies for any package.json in the tree. phaze is
# currently CDN-based (HTMX/Tailwind, no Node build), so this normally no-ops —
# it keeps the script symmetric with the discogsography reference and ready if a
# frontend with a package.json is ever added.
update_node_packages() {
  print_section "$EMOJI_PACKAGE" "Updating Node.js Dependencies"

  # Discover package.json files (skip vendored / tooling dirs)
  local pkg_files=()
  while IFS= read -r pkg; do
    [[ -n "$pkg" ]] && pkg_files+=("$pkg")
  done < <(find . -maxdepth 2 -name package.json \
    -not -path './node_modules/*' -not -path './.git/*' -not -path './.claude/*' 2>/dev/null)

  if [[ ${#pkg_files[@]} -eq 0 ]]; then
    print_info "No package.json found, skipping Node.js updates"
    return 0
  fi

  if ! command -v npm >/dev/null 2>&1; then
    print_warning "npm not installed, skipping Node.js updates"
    return 0
  fi

  local pkg pkg_dir
  for pkg in "${pkg_files[@]}"; do
    pkg_dir=$(dirname "$pkg")
    if [[ "$DRY_RUN" == true ]]; then
      print_info "[DRY RUN] Would update npm packages in $pkg_dir"
      continue
    fi
    backup_file "$pkg"
    backup_file "$pkg_dir/package-lock.json"
    print_info "Updating npm packages in $pkg_dir..."
    if (cd "$pkg_dir" && npm update --save && npm install >/dev/null 2>&1); then
      print_success "Updated $pkg_dir (package.json + package-lock.json)"
      FILE_CHANGES+=("$pkg_dir/package.json: Updated npm dependencies")
      CHANGES_MADE=true
    else
      print_warning "Failed to update npm packages in $pkg_dir"
    fi
  done
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

  SECURITY_PIP_RESOLVED=${#resolved[@]}
  SECURITY_PIP_REMAINING=${#still_needed[@]}

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

  if ! command -v osv-scanner >/dev/null 2>&1; then
    print_info "osv-scanner not installed locally, skipping osv-scanner ignore sweep"
    return
  fi

  if [[ "$DRY_RUN" == true ]]; then
    print_info "[DRY RUN] Would sweep $ignore_file for resolved vulnerabilities"
    return
  fi

  print_section "$EMOJI_VERIFY" "Sweeping osv-scanner Ignores"

  # Extract vulnerability IDs
  local vuln_ids=()
  while IFS= read -r vid; do
    [[ -n "$vid" ]] && vuln_ids+=("$vid")
  done < <(grep '^id = "' "$ignore_file" | sed 's/^id = "\(.*\)"/\1/')

  if [[ ${#vuln_ids[@]} -eq 0 ]]; then
    print_success "No osv-scanner ignores to sweep"
    return
  fi

  print_info "Testing ${#vuln_ids[@]} osv-scanner ignored vulnerabilit$([ ${#vuln_ids[@]} -eq 1 ] && echo "y" || echo "ies")..."

  # Test each by temporarily removing its [[IgnoredVulns]] block and rescanning
  local resolved=()
  local still_needed=()
  for test_vid in "${vuln_ids[@]}"; do
    local tmp_file
    tmp_file=$(mktemp)
    awk -v vid="$test_vid" '
      /^\[\[IgnoredVulns\]\]/ { block=1; buf=$0"\n"; next }
      block && /^id = / { if (index($0, vid)) { skip=1; buf=""; block=0; next } else { printf "%s", buf; buf=""; block=0 } }
      block { buf=buf $0"\n"; next }
      skip && /^\[\[/ { skip=0 }
      skip { next }
      { if (buf != "") { printf "%s", buf; buf="" }; print }
    ' "$ignore_file" >"$tmp_file"

    if osv-scanner --config="$tmp_file" scan . >/dev/null 2>&1; then
      resolved+=("$test_vid")
      print_success "✓ $test_vid — fixed! Removing from ignore list"
    else
      still_needed+=("$test_vid")
      print_warning "✗ $test_vid — still needed (no fix available)"
    fi
    rm -f "$tmp_file"
  done

  SECURITY_OSV_RESOLVED=${#resolved[@]}
  SECURITY_OSV_REMAINING=${#still_needed[@]}

  if [[ ${#resolved[@]} -gt 0 ]]; then
    CHANGES_MADE=true
    print_success "Removed ${#resolved[@]} resolved vulnerabilit$([ ${#resolved[@]} -eq 1 ] && echo "y" || echo "ies")"
    print_info "Manual cleanup of $ignore_file may be needed for removed entries"
  fi

  if [[ ${#still_needed[@]} -gt 0 ]]; then
    print_info "${#still_needed[@]} vulnerabilit$([ ${#still_needed[@]} -eq 1 ] && echo "y" || echo "ies") still awaiting upstream fixes"
  fi
}

# === Run Tests ===

run_tests() {
  if [[ "$SKIP_TESTS" == true ]]; then
    print_warning "Skipping tests (--skip-tests)"
    return 0
  fi
  if [[ "$DRY_RUN" == true ]]; then
    return 0
  fi

  print_section "$EMOJI_TEST" "Running Tests"

  print_info "Running linters..."
  if just lint; then
    print_success "Linting passed"
  else
    print_warning "Linting failed - review the changes"
  fi

  print_info "Running type checks..."
  if just typecheck; then
    print_success "Type checks passed"
  else
    print_warning "Type checks failed - review the changes"
  fi

  print_info "Running Python tests..."
  if just test; then
    print_success "Python tests passed"
  else
    print_warning "Python tests failed - review the changes"
  fi
}

# === Summary ===

generate_summary() {
  print_section "$EMOJI_CHANGES" "Update Summary"

  if [[ "$DRY_RUN" == true ]]; then
    print_info "This was a dry run. No changes were made."
    print_info "Run without --dry-run to apply changes."
    return
  fi

  if [[ "$CHANGES_MADE" == false ]]; then
    print_success "Everything is already up to date! No changes were needed."
    return
  fi

  if [[ -n "$PYTHON_VERSION_CHANGE" ]]; then
    echo ""
    echo "🐍 Python Version:"
    echo "  $PYTHON_VERSION_CHANGE"
  fi

  if [[ $(array_length PACKAGE_CHANGES) -gt 0 ]]; then
    echo ""
    echo "📦 Package Updates:"
    printf '%s\n' "${PACKAGE_CHANGES[@]:-}" | sort | while IFS= read -r change; do
      [[ -n "$change" ]] && echo "  • $change"
    done
  fi

  if [[ $(array_length FILE_CHANGES) -gt 0 ]]; then
    echo ""
    echo "📄 File Updates:"
    printf '%s\n' "${FILE_CHANGES[@]:-}" | sort | while IFS= read -r change; do
      [[ -n "$change" ]] && echo "  • $change"
    done
  fi

  # Security sweep results
  local total_resolved=$((SECURITY_PIP_RESOLVED + SECURITY_OSV_RESOLVED))
  local total_remaining=$((SECURITY_PIP_REMAINING + SECURITY_OSV_REMAINING))
  if [[ $total_resolved -gt 0 ]] || [[ $total_remaining -gt 0 ]]; then
    echo ""
    echo "🔒 Security (CVE Sweep):"
    [[ $total_resolved -gt 0 ]] && echo "  • $total_resolved CVE ignore$([ $total_resolved -eq 1 ] && echo "" || echo "s") resolved and removed"
    [[ $total_remaining -gt 0 ]] && echo "  • $total_remaining CVE$([ $total_remaining -eq 1 ] && echo "" || echo "s") still awaiting upstream fixes"
  fi

  # Next steps
  print_section "$EMOJI_GIT" "Next Steps"
  echo "1. Review the changes:"
  echo "   git diff --stat"
  echo "   git diff uv.lock pyproject.toml"
  echo ""
  echo "2. Run the full check suite:"
  echo "   just check"
  echo ""
  echo "3. Commit the changes:"
  echo "   git add -A && git commit -m 'chore: update project dependencies'"
  echo ""
  echo "4. Push on a feature branch and open a PR; verify CI passes."
}

# === Manual Verification Steps ===

show_verification_steps() {
  print_section "$EMOJI_VERIFY" "Manual Verification Steps"

  echo "Please verify the following before merging:"
  echo ""
  echo "1. 🐳 Docker builds:"
  echo "   docker compose build --no-cache"
  echo ""
  echo "2. 🧪 Service health:"
  echo "   docker compose up -d && docker compose ps   # services should be 'healthy'"
  echo ""
  echo "3. 📊 Review dependency changes:"
  echo "   uv run pip-audit --desc"
  echo "   git diff uv.lock | grep -E \"^[+-]version\""
  echo ""

  if [[ "$BACKUP" == true ]]; then
    echo "💾 Backups are stored in: $BACKUP_DIR/"
    echo "   To restore: cp $BACKUP_DIR/uv.lock.backup uv.lock && uv sync"
  fi
}

# === Verify Components ===

verify_components() {
  print_section "$EMOJI_VERIFY" "Verifying Project Components"

  local components=("pyproject.toml" "uv.lock" "Dockerfile" "docker-compose.yml")
  for svc_dir in "${SERVICE_DIRS[@]}"; do
    components+=("$svc_dir/pyproject.toml")
  done

  local found=0 total=0 missing=()
  for file in "${components[@]}"; do
    total=$((total + 1))
    if [[ -e "$file" ]]; then
      found=$((found + 1))
    else
      missing+=("$file")
    fi
  done

  print_success "Found $found/$total expected components"
  if [[ ${#missing[@]} -gt 0 ]]; then
    print_warning "Missing ${#missing[@]} component(s):"
    for component in "${missing[@]}"; do
      echo "  ⚠️  $component"
    done
    print_info "This may be normal if components were removed from the project."
  fi
}

# === Error Handling ===

handle_error() {
  local exit_code=$1
  print_error "An error occurred (exit code: $exit_code)"
  if [[ "$BACKUP" == true ]] && [[ "$DRY_RUN" == false ]] && [[ -d "$BACKUP_DIR" ]]; then
    print_info "You can restore from backup with:"
    echo "  cp $BACKUP_DIR/uv.lock.backup uv.lock"
    echo "  cp $BACKUP_DIR/pyproject.toml.backup pyproject.toml"
    echo "  uv sync"
  fi
  exit "$exit_code"
}

trap 'handle_error $?' ERR

# === Main ===

main() {
  print_section "$EMOJI_ROCKET" "Phaze Project Updater"
  echo "  Timestamp: $TIMESTAMP"
  echo "  Dry run: $DRY_RUN"
  echo "  Major upgrades: $MAJOR_UPGRADES"
  echo "  Skip tests: $SKIP_TESTS"
  [[ "$UPDATE_PYTHON" == true ]] && echo "  Python target: $PYTHON_VERSION"

  verify_components
  update_python_version
  update_uv_version
  update_precommit_hooks
  update_python_packages
  sync_dependency_floors
  flag_capped_dependencies
  update_service_packages
  update_node_packages
  sweep_pip_audit_ignores
  sweep_osv_scanner_ignores
  run_tests
  generate_summary

  if [[ "$DRY_RUN" == false ]] && [[ "$CHANGES_MADE" == true ]]; then
    show_verification_steps
  fi

  print_section "$EMOJI_ROCKET" "Update Complete"
}

main
