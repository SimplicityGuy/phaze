"""Audit historical evidence without rewriting its point-in-time claims."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import html
from io import BytesIO
import json
from pathlib import Path
import re
import subprocess  # nosec B404 -- fixed git argv, no shell, repository-local reads only
import sys
import tarfile
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import unquote, urlsplit


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# isort: off
from scripts.maintenance_inventory import build_inventory, documentation_class  # noqa: E402
# isort: on

ARCHIVE_BOUNDARIES = {
    ".planning/": ".planning/README.md",
    "docs/spikes/": "docs/spikes/README.md",
    "docs/superpowers/specs/": "docs/superpowers/specs/README.md",
    "docs/telemetry/measurements/": "docs/telemetry/measurements/README.md",
    "docs/documentation-audit-2026-08-19.md": "docs/README.md",
}

_APPROVED_ROLE_IDENTIFIERS = ("host-compute-alt", "host-compute", "host-prod", "host-store", "operator")
_FORBIDDEN = (
    re.compile(r"/Users/[^/<\s]+", re.IGNORECASE),
    re.compile(r"/Volumes/[^/<\s]+", re.IGNORECASE),
    re.compile(r"/media/[A-Za-z0-9][^/<\s]*", re.IGNORECASE),
    re.compile(r"users_[a-z0-9]+_(?:code_public|orca_workspaces)", re.IGNORECASE),
)
_NUMBER = re.compile(r"(?<![A-Za-z0-9_])\d+(?:[.,]\d+)*(?![A-Za-z0-9_])")
_LOCAL_ROOT = re.compile(r"/Users/[^/\s]+(?:(?:/Code/public)|(?:/orca/workspaces))?", re.IGNORECASE)
_ARCHIVE_ENTRY = re.compile(r"(?:/media/[A-Za-z0-9._-]+|<archive-mount>/<set-[^>]+>)", re.IGNORECASE)
_ENCODED_LOCAL_ROOT = re.compile(r"users_[a-z0-9]+_(?:code_public|orca_workspaces)", re.IGNORECASE)
_TOKEN_PART = re.compile(r"[A-Za-z0-9_-]+|[^A-Za-z0-9_-]+")
_MARKDOWN_LINK = re.compile(r"(?<![A-Za-z0-9_])!?\[[^\]]*\]\(([^)\s]+)")
_MARKDOWN_REFERENCE = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)")
_HTML_LINK = re.compile(r"\b(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_INLINE_CODE = re.compile(r"`[^`]*`")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_MERMAID_START = re.compile(
    r"^(?:flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|gantt|pie|journey|mindmap|timeline|quadrantChart|xychart|sankey)\b"
)


class AuditResult(TypedDict):
    base_revision: str
    historical_files_scanned: int
    binary_files_scanned: int
    scrubbed_files: int
    replacements: dict[str, int]
    source_host_or_account_identifiers: int
    forbidden_occurrences_after: int
    exact_transform_mismatches: list[str]
    numeric_files_compared: int
    numeric_tokens_compared: int
    numeric_mismatches: list[str]
    archive_boundaries: dict[str, str]
    missing_archive_boundaries: list[str]
    local_links_valid: int
    local_links_historical_by_boundary: int
    historical_link_targets: list[str]
    mermaid_blocks_valid: int
    mermaid_blocks_invalid: list[str]
    graph_files_valid: int
    graph_reference_errors: list[str]
    errors: list[str]


@dataclass
class CorpusAudit:
    replacements: Counter[str] = field(default_factory=Counter)
    forbidden_occurrences_after: int = 0
    exact_transform_mismatches: list[str] = field(default_factory=list)
    numeric_mismatches: list[str] = field(default_factory=list)
    numeric_tokens_compared: int = 0
    scrubbed_files: int = 0
    local_links_valid: int = 0
    local_links_historical_by_boundary: int = 0
    historical_link_targets: list[str] = field(default_factory=list)
    mermaid_blocks_valid: int = 0
    mermaid_blocks_invalid: list[str] = field(default_factory=list)
    binary_files_scanned: int = 0
    source_identifiers: set[str] = field(default_factory=set)
    current_texts: list[str] = field(default_factory=list)


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(  # noqa: S603  # nosec B603 B607 -- fixed git executable, no shell
        ["git", *args],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout


def _historical_tree_at_revision(revision: str) -> dict[str, bytes]:
    archive = _git_bytes(
        "archive",
        revision,
        ".planning",
        "docs/spikes",
        "docs/superpowers/specs",
        "docs/telemetry/measurements",
        "docs/documentation-audit-2026-08-19.md",
    )
    with tarfile.open(fileobj=BytesIO(archive)) as tar:
        return {member.name: extracted.read() for member in tar.getmembers() if member.isfile() and (extracted := tar.extractfile(member))}


def _current_historical_paths() -> set[str]:
    listing = _git_bytes("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8")
    paths = [path for path in listing.split("\0") if path and (REPO_ROOT / path).is_file()]
    generated = frozenset(
        path
        for path in paths
        if path.endswith(".md")
        and any(line.startswith("<!-- generated-by:") for line in (REPO_ROOT / path).read_text(encoding="utf-8", errors="ignore").splitlines())
    )
    return {path for path in paths if documentation_class(path, generated=generated) == "historical-evidence"}


def numeric_tokens(text: str) -> tuple[str, ...]:
    """Return ordered numeric evidence tokens, excluding approved placeholder identifiers."""
    return tuple(_NUMBER.findall(_normalized_local_paths(text)))


def _normalized_local_paths(text: str) -> str:
    text = _LOCAL_ROOT.sub("<scratch>", text)
    text = _ARCHIVE_ENTRY.sub("<archive-entry>", text)
    return _ENCODED_LOCAL_ROOT.sub("scratch", text)


def _approved_identifier_only_change(before: str, after: str) -> bool:
    """Return whether every changed span became one approved identifier placeholder."""
    before = _normalized_local_paths(before)
    after = _normalized_local_paths(after)
    if before == after:
        return True
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    if len(before_lines) != len(after_lines):
        return False
    for before_line, after_line in zip(before_lines, after_lines, strict=True):
        if before_line == after_line:
            continue
        before_parts = _TOKEN_PART.findall(before_line)
        after_parts = _TOKEN_PART.findall(after_line)
        if len(before_parts) != len(after_parts):
            return False
        for before_part, after_part in zip(before_parts, after_parts, strict=True):
            if before_part != after_part and not _approved_role_token_change(before_part, after_part):
                return False
    return True


def _role_source_fragment(before: str, after: str) -> str | None:
    for identifier in _APPROVED_ROLE_IDENTIFIERS:
        start = after.find(identifier)
        if start < 0:
            continue
        end = start + len(identifier)
        prefix = after[:start]
        suffix = after[end:]
        if before.startswith(prefix) and before.endswith(suffix) and len(before) > len(prefix) + len(suffix):
            source_end = len(before) - len(suffix) if suffix else len(before)
            return before[len(prefix) : source_end]
    return None


def _approved_role_token_change(before: str, after: str) -> bool:
    return _role_source_fragment(before, after) is not None


def _role_source_fragments(before: str, after: str) -> set[str]:
    before_parts = _TOKEN_PART.findall(_normalized_local_paths(before))
    after_parts = _TOKEN_PART.findall(_normalized_local_paths(after))
    if len(before_parts) != len(after_parts):
        return set()
    return {
        source
        for before_part, after_part in zip(before_parts, after_parts, strict=True)
        if before_part != after_part and (source := _role_source_fragment(before_part, after_part)) is not None
    }


def _replacement_counts(before: str, after: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    counts["local_absolute_root"] += after.count("<scratch>") - before.count("<scratch>")
    counts["archive_directory"] += after.count("<archive-mount>/<set-") - before.count("<archive-mount>/<set-")
    counts["encoded_local_root"] += len(_ENCODED_LOCAL_ROOT.findall(before)) - len(_ENCODED_LOCAL_ROOT.findall(after))
    normalized_before = _normalized_local_paths(before)
    normalized_after = _normalized_local_paths(after)
    before_parts = _TOKEN_PART.findall(normalized_before)
    after_parts = _TOKEN_PART.findall(normalized_after)
    if len(before_parts) == len(after_parts):
        counts["host_or_account"] += sum(
            before_part != after_part and _approved_role_token_change(before_part, after_part)
            for before_part, after_part in zip(before_parts, after_parts, strict=True)
        )
    return counts


def _unfenced_lines(text: str) -> Iterator[tuple[int, str]]:
    marker = ""
    for lineno, line in enumerate(text.splitlines(), start=1):
        match = _FENCE.match(line)
        if match:
            fence = match.group(1)
            if not marker:
                marker = fence[0]
            elif fence[0] == marker:
                marker = ""
            continue
        if not marker:
            yield lineno, line


def _local_link_status(path: str, target: str) -> str | None:
    target = html.unescape(target).strip("<>")
    if any(marker in target for marker in ("{{", "{%", "${")):
        return None
    split = urlsplit(target)
    if split.scheme or split.netloc or not split.path or split.path.startswith(("#", "/")):
        return None
    decoded = unquote(split.path)
    source_relative = (REPO_ROOT / path).parent / decoded
    root_relative = REPO_ROOT / decoded
    return "valid" if source_relative.exists() or root_relative.exists() else "historical_by_boundary"


def _links(path: str, text: str) -> Iterator[tuple[int, str, str]]:
    for lineno, line in _unfenced_lines(text):
        targets = [match.group(1) for match in _HTML_LINK.finditer(line)]
        if path.endswith(".md"):
            line = _INLINE_CODE.sub("", line)
            targets.extend(match.group(1) for match in _MARKDOWN_LINK.finditer(line))
            reference = _MARKDOWN_REFERENCE.match(line)
            if reference:
                targets.append(reference.group(1))
        for target in targets:
            status = _local_link_status(path, target)
            if status is not None:
                yield lineno, target, status


def _mermaid_blocks(text: str) -> Iterator[tuple[int, str]]:
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = _FENCE.match(lines[index])
        if not match or match.group(2).strip().lower() != "mermaid":
            index += 1
            continue
        start = index + 1
        marker = match.group(1)[0]
        index += 1
        body: list[str] = []
        while index < len(lines):
            closing = _FENCE.match(lines[index])
            if closing and closing.group(1)[0] == marker:
                break
            body.append(lines[index])
            index += 1
        if index == len(lines):
            yield start, "unclosed fence"
            return
        meaningful = [line.strip() for line in body if line.strip() and not line.lstrip().startswith("%%")]
        if not meaningful or not _MERMAID_START.match(meaningful[0]):
            yield start, "missing or unsupported diagram declaration"
        elif sum(line.startswith("subgraph ") for line in meaningful) != sum(line == "end" for line in meaningful):
            yield start, "unbalanced subgraph/end"
        else:
            yield start, "valid"
        index += 1


def _boundary_for(path: str) -> str | None:
    for prefix, boundary in ARCHIVE_BOUNDARIES.items():
        if path == prefix or path.startswith(prefix):
            return boundary
    return None


def _graph_reference_errors(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])
    identifiers = [node.get("id") for node in nodes]
    identifier_set = set(identifiers)
    errors: list[str] = []
    if len(identifier_set) != len(identifiers):
        errors.append(f"{path.relative_to(REPO_ROOT)}: duplicate node id")
    for index, edge in enumerate(data.get("links", data.get("edges", []))):
        for key in ("source", "target", "_src", "_tgt"):
            if key in edge and edge[key] not in identifier_set:
                errors.append(f"{path.relative_to(REPO_ROOT)}: edge {index} has dangling {key}")
    for index, hyperedge in enumerate(data.get("hyperedges", [])):
        if any(node not in identifier_set for node in hyperedge.get("nodes", [])):
            errors.append(f"{path.relative_to(REPO_ROOT)}: hyperedge {index} has a dangling node")
    return errors


def _match_renames(baseline_paths: set[str], current_paths: set[str]) -> dict[str, str]:
    added_paths = current_paths - baseline_paths
    renamed: dict[str, str] = {}
    for old_path in sorted(baseline_paths - current_paths):
        candidates = [new_path for new_path in sorted(added_paths) if _approved_identifier_only_change(old_path, new_path)]
        if len(candidates) == 1:
            renamed[old_path] = candidates[0]
            added_paths.remove(candidates[0])
    return renamed


def _forbidden_occurrences(text: str) -> int:
    return sum(len(pattern.findall(text)) for pattern in _FORBIDDEN)


def _record_links_and_mermaid(state: CorpusAudit, path: str, text: str) -> None:
    for lineno, target, status in _links(path, text):
        if status == "valid":
            state.local_links_valid += 1
        else:
            state.local_links_historical_by_boundary += 1
            state.historical_link_targets.append(f"{path}:{lineno}: {target}")
    for lineno, status in _mermaid_blocks(text):
        if status == "valid":
            state.mermaid_blocks_valid += 1
        else:
            state.mermaid_blocks_invalid.append(f"{path}:{lineno}: {status}")


def _record_binary_file(state: CorpusAudit, old_path: str, path: str, before: bytes, after: bytes) -> None:
    state.binary_files_scanned += 1
    if before != after:
        state.exact_transform_mismatches.append(f"{old_path} -> {path}: binary content changed")
    state.forbidden_occurrences_after += _forbidden_occurrences(after.decode("latin-1"))


def _record_text_file(state: CorpusAudit, old_path: str, path: str, before: str, after: str) -> None:
    state.replacements.update(_replacement_counts(before, after))
    state.source_identifiers.update(_role_source_fragments(before, after))
    state.current_texts.append(after)
    state.scrubbed_files += before != after
    if not _approved_identifier_only_change(before, after):
        state.exact_transform_mismatches.append(f"{old_path} -> {path}")
    before_numbers = numeric_tokens(before)
    if before_numbers != numeric_tokens(after):
        state.numeric_mismatches.append(f"{old_path} -> {path}")
    state.numeric_tokens_compared += len(before_numbers)
    state.forbidden_occurrences_after += _forbidden_occurrences(after)
    _record_links_and_mermaid(state, path, after)


def _accumulate_historical_file(state: CorpusAudit, old_path: str, path: str, before: bytes) -> None:
    current_file = REPO_ROOT / path
    if not current_file.is_file():
        state.exact_transform_mismatches.append(f"{old_path} -> {path}: current file missing")
        return
    after = current_file.read_bytes()
    try:
        decoded_before = before.decode("utf-8")
        decoded_after = after.decode("utf-8")
    except UnicodeDecodeError:
        _record_binary_file(state, old_path, path, before, after)
        return
    _record_text_file(state, old_path, path, decoded_before, decoded_after)


def _count_source_identifier_occurrences(identifiers: set[str], texts: list[str]) -> int:
    patterns = [re.compile(rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])", re.IGNORECASE) for identifier in identifiers]
    return sum(len(pattern.findall(text)) for pattern in patterns for text in texts)


def _boundary_error(prefix: str, boundary: str) -> str | None:
    boundary_path = REPO_ROOT / boundary
    if not boundary_path.is_file():
        return f"{prefix}: missing {boundary}"
    content = boundary_path.read_text(encoding="utf-8")
    if "historical" not in content.lower() or "historical_by_boundary" not in content:
        return f"{prefix}: {boundary} lacks the archive/link contract"
    return None


def _validate_boundaries() -> tuple[dict[str, str], list[str]]:
    boundaries: dict[str, str] = {}
    missing: list[str] = []
    for prefix, boundary in ARCHIVE_BOUNDARIES.items():
        if error := _boundary_error(prefix, boundary):
            missing.append(error)
        else:
            boundaries[prefix] = boundary
    return boundaries, missing


def _population_error(current_paths: set[str], expected_paths: set[str]) -> str | None:
    if current_paths == expected_paths:
        return None
    added = sorted(current_paths - expected_paths)
    removed = sorted(expected_paths - current_paths)
    return f"historical population changed (added={added}, removed={removed})"


def _assemble_errors(
    state: CorpusAudit,
    missing_boundaries: list[str],
    graph_errors: list[str],
    boundary_owner_missing: bool,
    population_error: str | None,
) -> list[str]:
    candidates = (
        (state.forbidden_occurrences_after, f"{state.forbidden_occurrences_after} prohibited identifier occurrence(s) remain"),
        (state.exact_transform_mismatches, f"{len(state.exact_transform_mismatches)} file(s) differ beyond the approved substitutions"),
        (state.numeric_mismatches, f"{len(state.numeric_mismatches)} file(s) changed numeric evidence"),
        (missing_boundaries, f"{len(missing_boundaries)} archive boundary error(s)"),
        (state.mermaid_blocks_invalid, f"{len(state.mermaid_blocks_invalid)} invalid Mermaid block(s)"),
        (graph_errors, f"{len(graph_errors)} graph reference error(s)"),
        (boundary_owner_missing, "one or more historical files have no archive-boundary owner"),
        (population_error, population_error or ""),
    )
    return [message for condition, message in candidates if condition]


def _build_result(
    revision: str,
    historical: list[str],
    state: CorpusAudit,
    boundaries: dict[str, str],
    missing_boundaries: list[str],
    graph_file_count: int,
    graph_errors: list[str],
    errors: list[str],
) -> AuditResult:
    return {
        "base_revision": revision,
        "historical_files_scanned": len(historical),
        "binary_files_scanned": state.binary_files_scanned,
        "scrubbed_files": state.scrubbed_files,
        "replacements": dict(sorted(state.replacements.items())),
        "source_host_or_account_identifiers": len(state.source_identifiers),
        "forbidden_occurrences_after": state.forbidden_occurrences_after,
        "exact_transform_mismatches": state.exact_transform_mismatches,
        "numeric_files_compared": len(historical),
        "numeric_tokens_compared": state.numeric_tokens_compared,
        "numeric_mismatches": state.numeric_mismatches,
        "archive_boundaries": boundaries,
        "missing_archive_boundaries": missing_boundaries,
        "local_links_valid": state.local_links_valid,
        "local_links_historical_by_boundary": state.local_links_historical_by_boundary,
        "historical_link_targets": state.historical_link_targets,
        "mermaid_blocks_valid": state.mermaid_blocks_valid,
        "mermaid_blocks_invalid": state.mermaid_blocks_invalid,
        "graph_files_valid": graph_file_count if not graph_errors else 0,
        "graph_reference_errors": graph_errors,
        "errors": errors,
    }


def audit(base: str) -> AuditResult:
    """Audit the live worktree against one immutable historical baseline."""
    inventory = build_inventory(base)
    revision = str(inventory["revision"])
    historical = list(dict(inventory["documentation"])["historical-evidence"])
    baseline_paths = set(historical)
    current_paths = _current_historical_paths()
    renamed = _match_renames(baseline_paths, current_paths)
    expected_paths = (baseline_paths - set(renamed)) | set(renamed.values())
    baseline = _historical_tree_at_revision(revision)
    state = CorpusAudit()
    for old_path in historical:
        _accumulate_historical_file(state, old_path, renamed.get(old_path, old_path), baseline[old_path])
    state.forbidden_occurrences_after += _count_source_identifier_occurrences(state.source_identifiers, state.current_texts)

    boundaries, missing_boundaries = _validate_boundaries()
    graph_files = (REPO_ROOT / ".planning/graphs/graph.json", REPO_ROOT / ".planning/graphs/.last-build-snapshot.json")
    graph_errors = [error for graph_file in graph_files for error in _graph_reference_errors(graph_file)]
    errors = _assemble_errors(
        state,
        missing_boundaries,
        graph_errors,
        any(_boundary_for(renamed.get(path, path)) is None for path in historical),
        _population_error(current_paths, expected_paths),
    )
    return _build_result(revision, historical, state, boundaries, missing_boundaries, len(graph_files), graph_errors, errors)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="immutable starting revision for the historical corpus")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    result = audit(args.base)
    print(json.dumps(result, indent=2, sort_keys=True))  # noqa: T201
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
