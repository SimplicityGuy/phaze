"""Check deterministic integrity contracts for maintained documentation."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import html
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess  # nosec B404 -- fixed local tooling and repository reads only
import sys
import tempfile
import time
from typing import TYPE_CHECKING, NamedTuple, cast
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urldefrag, urlsplit
from urllib.request import Request, urlopen


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# isort: off
from scripts.audit_historical_evidence import forbidden_identifiers, link_targets, mermaid_blocks  # noqa: E402
from scripts.maintenance_inventory import documentation_class, is_documentation_surface  # noqa: E402
# isort: on

MERMAID_CLI_VERSION = "11.12.0"
_RENDER_EXIT_GRACE_SECONDS = 2.0
_RENDER_TIMEOUT_SECONDS = 60.0
_GENERATOR_MARKER = re.compile(r"^<!-- generated-by:\s*([^>]+?)\s*-->$", re.MULTILINE)
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")
_HTML_ANCHOR = re.compile(r"<[^>]+\b(?:id|name)=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")
_MARKDOWN_LINK_LABEL = re.compile(r"!?\[([^]]+)]\([^)]*\)")
_MARKDOWN_DECORATION = re.compile(r"[`*~]")
_MIGRATION_CLAIM = re.compile(r"\bhead,\s+\*\*`([^`]+)`\*\*", re.IGNORECASE)
_TEXT_SUFFIXES = frozenset({".adoc", ".md", ".mdx", ".rst", ".txt"})
_CHROME_CANDIDATES = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)


class LinkResolution(NamedTuple):
    """The deterministic classification of one documentation link."""

    kind: str
    path: Path | None
    fragment: str


class MermaidSource(NamedTuple):
    """One maintained Mermaid body and its source location."""

    path: str
    lineno: int
    body: str


class MermaidRenderResult(NamedTuple):
    """The outcome of one real Mermaid renderer process."""

    error: str | None
    terminated_after_render: bool


class MermaidRenderReport(NamedTuple):
    """All renderer failures and explicit forced-cleanup locations."""

    errors: tuple[str, ...]
    terminated_after_render: tuple[str, ...]


class MermaidProcessOutcome(NamedTuple):
    """Renderer lifecycle facts needed to classify one process result."""

    timed_out: bool
    stopped_after_complete: bool
    cleaned_lingering_group: bool
    cleanup_error: str | None


class CheckedLink(NamedTuple):
    """One maintained link after deterministic classification."""

    kind: str
    error: str | None
    external_target: str | None


class SourceLink(NamedTuple):
    """One extracted link with its maintained-document source location."""

    path: str
    lineno: int
    target: str


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603  # nosec B603 B607 -- fixed git executable, no shell
        ["git", *args],  # noqa: S607
        cwd=root,
        capture_output=True,
        check=True,
        text=True,
    ).stdout


def source_revision(root: Path = REPO_ROOT) -> str:
    """Return the committed source revision audited by this checkout."""
    return _git(root, "rev-parse", "HEAD^{commit}").strip()


def tracked_working_paths(root: Path = REPO_ROOT) -> tuple[str, ...]:
    """Return tracked and not-yet-added paths so a dirty-tree check fails safely."""
    output = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return tuple(sorted(path for path in output.split("\0") if path and (root / path).is_file()))


def _documentation_texts(root: Path, paths: Iterable[str]) -> dict[str, str]:
    return {
        path: (root / path).read_text(encoding="utf-8")
        for path in paths
        if is_documentation_surface(path) and (root / path).suffix.lower() in _TEXT_SUFFIXES
    }


def _generated_paths(texts: Mapping[str, str]) -> frozenset[str]:
    return frozenset(path for path, text in texts.items() if path.endswith((".md", ".mdx")) and _GENERATOR_MARKER.search(text))


def _maintained_paths(paths: Iterable[str], generated: frozenset[str]) -> tuple[str, ...]:
    return tuple(path for path in paths if is_documentation_surface(path) and documentation_class(path, generated=generated) == "maintained")


def maintained_documentation(root: Path = REPO_ROOT) -> tuple[str, ...]:
    """Return every currently maintained documentation path in the working tree."""
    paths = tracked_working_paths(root)
    return _maintained_paths(paths, _generated_paths(_documentation_texts(root, paths)))


def github_heading_slug(heading: str) -> str:
    """Approximate GitHub's rendered heading identifier for local-anchor validation."""
    heading = _MARKDOWN_LINK_LABEL.sub(r"\1", heading)
    heading = _MARKDOWN_DECORATION.sub("", heading)
    heading = _HTML_TAG.sub("", heading)
    heading = html.unescape(heading).lower()
    heading = "".join(character for character in heading if character.isalnum() or character in " _-")
    return heading.replace(" ", "-")


def document_anchors(text: str) -> frozenset[str]:
    """Return explicit and GitHub-style generated anchors for one document."""
    anchors = set(_HTML_ANCHOR.findall(text))
    occurrences: Counter[str] = Counter()
    fenced = False
    fence_marker = ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[0]
            if not fenced:
                fenced = True
                fence_marker = marker
            elif marker == fence_marker:
                fenced = False
            continue
        if fenced or (match := _HEADING.match(line)) is None:
            continue
        base = github_heading_slug(match.group(1))
        suffix = occurrences[base]
        occurrences[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return frozenset(anchors)


def resolve_link(root: Path, source: Path, target: str) -> LinkResolution:
    """Resolve one link without network I/O, percent-decoding its path and anchor."""
    raw = html.unescape(target).strip().strip("<>")
    if any(marker in raw for marker in ("{{", "{%", "${")):
        return LinkResolution("template", None, "")
    split = urlsplit(raw)
    if split.scheme or split.netloc:
        return LinkResolution("external", None, unquote(split.fragment))
    if split.path.startswith("/"):
        return LinkResolution("application-route", None, unquote(split.fragment))
    relative = unquote(split.path)
    candidate = source if not relative else source.parent / relative
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return LinkResolution("outside-repository", resolved, unquote(split.fragment))
    return LinkResolution("local", resolved, unquote(split.fragment))


def _check_link(
    root: Path,
    source: Path,
    target: str,
    anchors_by_path: Mapping[Path, frozenset[str]],
) -> CheckedLink:
    resolution = resolve_link(root, source, target)
    if resolution.kind == "external":
        return CheckedLink("external", None, target)
    if resolution.kind in {"template", "application-route"}:
        return CheckedLink("ignored", None, None)
    return _check_local_resolution(resolution, target, anchors_by_path)


def _check_local_resolution(
    resolution: LinkResolution,
    target: str,
    anchors_by_path: Mapping[Path, frozenset[str]],
) -> CheckedLink:
    if resolution.kind == "outside-repository":
        return CheckedLink("local", f"local link escapes repository: {target}", None)
    if resolution.path is None:
        return CheckedLink("local", f"could not resolve local target: {target}", None)
    if not resolution.path.exists():
        return CheckedLink("local", f"missing local target: {target}", None)
    if resolution.fragment and resolution.path in anchors_by_path and resolution.fragment not in anchors_by_path[resolution.path]:
        return CheckedLink("local", f"missing local anchor: {target}", None)
    return CheckedLink("local", None, None)


def _source_links(maintained: Sequence[str], texts: Mapping[str, str]) -> Iterable[SourceLink]:
    for path in maintained:
        if (text := texts.get(path)) is None:
            continue
        for lineno, target in link_targets(path, text):
            yield SourceLink(path, lineno, target)


def _local_link_errors(
    root: Path,
    maintained: Sequence[str],
    texts: Mapping[str, str],
) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    checked = 0
    errors: list[str] = []
    external: set[str] = set()
    anchors_by_path = {(root / path).resolve(): document_anchors(text) for path, text in texts.items() if path.endswith((".md", ".mdx"))}
    for link in _source_links(maintained, texts):
        result = _check_link(root, root / link.path, link.target, anchors_by_path)
        if result.kind == "ignored":
            continue
        if result.kind == "external":
            if result.external_target is not None:
                external.add(result.external_target)
            continue
        checked += 1
        if result.error is not None:
            errors.append(f"{link.path}:{link.lineno}: {result.error}")
    return checked, tuple(sorted(errors)), tuple(sorted(external))


def _indexed_paths(root: Path, index: Path, text: str) -> frozenset[Path]:
    indexed: set[Path] = set()
    for _, target in link_targets(index.relative_to(root).as_posix(), text):
        resolution = resolve_link(root, index, target)
        if resolution.kind == "local" and resolution.path is not None:
            indexed.add(resolution.path)
    return frozenset(indexed)


def _index_errors(root: Path, maintained: Sequence[str], texts: Mapping[str, str]) -> tuple[str, ...]:
    index = root / "docs" / "README.md"
    expected = {(root / path).resolve() for path in maintained if path.endswith(".md") and (root / path).resolve() != index.resolve()}
    missing = expected - _indexed_paths(root, index, texts["docs/README.md"])
    return tuple(f"docs/README.md: maintained guide is not indexed: {path.relative_to(root)}" for path in sorted(missing))


def _generator_errors(paths: Sequence[str], generated: frozenset[str], texts: Mapping[str, str]) -> tuple[str, ...]:
    errors: list[str] = []
    for path in sorted(generated):
        historical_class = documentation_class(path, generated=frozenset())
        if historical_class != "historical-evidence":
            marker = _GENERATOR_MARKER.search(texts[path])
            owner = marker.group(1) if marker is not None else "unknown"
            errors.append(f"{path}: unsupported generator ownership marker: {owner}")
    documented = set(paths)
    if not generated <= documented:
        errors.append("generator marker scan included an untracked path")
    return tuple(errors)


def _annotated_assignment(node: ast.AnnAssign, name: str) -> object | None:
    if isinstance(node.target, ast.Name) and node.target.id == name and node.value is not None:
        return cast("object", ast.literal_eval(node.value))
    return None


def _plain_assignment(node: ast.Assign, name: str) -> object | None:
    if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name:
        return cast("object", ast.literal_eval(node.value))
    return None


def _assignment_value(node: ast.stmt, name: str) -> object | None:
    if isinstance(node, ast.AnnAssign):
        return _annotated_assignment(node, name)
    if isinstance(node, ast.Assign):
        return _plain_assignment(node, name)
    return None


def _named_assignment(tree: ast.Module, name: str) -> object | None:
    for node in tree.body:
        if (value := _assignment_value(node, name)) is not None:
            return value
    return None


def _migration_pair(path: Path) -> tuple[str | None, tuple[str, ...]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    revision = _named_assignment(tree, "revision")
    down_revision = _named_assignment(tree, "down_revision")
    parents: tuple[str, ...]
    if isinstance(down_revision, str):
        parents = (down_revision,)
    elif isinstance(down_revision, tuple):
        parents = tuple(parent for parent in down_revision if isinstance(parent, str))
    else:
        parents = ()
    return revision if isinstance(revision, str) else None, parents


def migration_heads(root: Path = REPO_ROOT) -> frozenset[str]:
    """Derive current Alembic heads from literal revision relationships."""
    pairs = tuple(_migration_pair(path) for path in sorted((root / "alembic" / "versions").glob("*.py")))
    revisions = {revision for revision, _ in pairs if revision is not None}
    parents = {parent for _, pair_parents in pairs for parent in pair_parents}
    return frozenset(revisions - parents)


def _migration_errors(root: Path) -> tuple[str, ...]:
    heads = migration_heads(root)
    claims = frozenset(_MIGRATION_CLAIM.findall((root / "docs" / "database.md").read_text(encoding="utf-8")))
    errors: list[str] = []
    if len(heads) != 1:
        errors.append(f"alembic/versions: expected one migration head, found {sorted(heads)}")
    if claims != heads:
        errors.append(f"docs/database.md: migration-head claims {sorted(claims)} do not match {sorted(heads)}")
    return tuple(errors)


def _forbidden_errors(maintained: Sequence[str], texts: Mapping[str, str]) -> tuple[str, ...]:
    errors: list[str] = []
    for path in maintained:
        if (text := texts.get(path)) is None:
            continue
        for lineno, identifier in forbidden_identifiers(text):
            errors.append(f"{path}:{lineno}: forbidden local identifier: {identifier}")
    return tuple(sorted(errors))


def _mermaid_sources(maintained: Sequence[str], texts: Mapping[str, str]) -> tuple[tuple[MermaidSource, ...], tuple[str, ...]]:
    sources: list[MermaidSource] = []
    errors: list[str] = []
    for path in maintained:
        if not path.endswith((".md", ".mdx")):
            continue
        for block in mermaid_blocks(texts[path]):
            if not block.closed:
                errors.append(f"{path}:{block.lineno}: unclosed Mermaid fence")
            else:
                sources.append(MermaidSource(path, block.lineno, block.body))
    return tuple(sources), tuple(errors)


def _chrome_environment() -> dict[str, str]:
    environment = os.environ.copy()
    if environment.get("PUPPETEER_EXECUTABLE_PATH"):
        return environment
    for candidate in _CHROME_CANDIDATES:
        if candidate.is_file():
            environment["PUPPETEER_EXECUTABLE_PATH"] = str(candidate)
            return environment
    for executable in ("google-chrome", "chromium", "chromium-browser"):
        if found := shutil.which(executable):
            environment["PUPPETEER_EXECUTABLE_PATH"] = found
            break
    return environment


def _renderer_command(renderer: str | None) -> tuple[str, ...]:
    if renderer is not None:
        return (renderer,)
    npx = shutil.which("npx")
    if npx is None:
        raise RuntimeError("npx is required to run the pinned Mermaid CLI")
    return (npx, "--yes", f"@mermaid-js/mermaid-cli@{MERMAID_CLI_VERSION}")


def _complete_svg(path: Path) -> bool:
    if not path.is_file():
        return False
    contents = path.read_text(encoding="utf-8")
    return "<svg" in contents and "</svg>" in contents


def _process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _stop_renderer(process: subprocess.Popen[str]) -> str | None:
    """Stop the renderer and its browser after the complete output is durable."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    deadline = time.monotonic() + 5
    while _process_group_exists(process.pid):
        if time.monotonic() >= deadline:
            return "renderer process group remained alive after termination"
        time.sleep(0.05)
    return None


def _wait_for_renderer(process: subprocess.Popen[str], output_path: Path) -> MermaidProcessOutcome:
    """Wait for a complete SVG or a bounded renderer failure."""
    deadline = time.monotonic() + _RENDER_TIMEOUT_SECONDS
    exit_grace_deadline: float | None = None
    while process.poll() is None:
        now = time.monotonic()
        complete = _complete_svg(output_path)
        if complete:
            if exit_grace_deadline is None:
                exit_grace_deadline = now + _RENDER_EXIT_GRACE_SECONDS
            elif now >= exit_grace_deadline:
                return MermaidProcessOutcome(False, True, False, _stop_renderer(process))
        elif now >= deadline:
            return MermaidProcessOutcome(True, False, False, _stop_renderer(process))
        time.sleep(0.1)

    cleaned_lingering_group = _process_group_exists(process.pid)
    cleanup_error = _stop_renderer(process) if cleaned_lingering_group else None
    return MermaidProcessOutcome(False, False, cleaned_lingering_group, cleanup_error)


def _diagnostic(stderr: str, stdout: str, fallback: str) -> str:
    for candidate in (stderr.strip(), stdout.strip(), fallback):
        if candidate:
            return candidate
    return fallback


def _unexpected_exit(process: subprocess.Popen[str], outcome: MermaidProcessOutcome) -> bool:
    return not outcome.stopped_after_complete and process.returncode != 0


def _classify_renderer_result(
    source: MermaidSource,
    process: subprocess.Popen[str],
    output_path: Path,
    outcome: MermaidProcessOutcome,
) -> MermaidRenderResult:
    stdout, stderr = process.communicate()
    if outcome.cleanup_error:
        return MermaidRenderResult(f"{source.path}:{source.lineno}: {outcome.cleanup_error}", False)
    if outcome.timed_out:
        detail = _diagnostic(stderr, stdout, f"timed out after {_RENDER_TIMEOUT_SECONDS:g} seconds")
        return MermaidRenderResult(f"{source.path}:{source.lineno}: Mermaid renderer failed: {detail}", False)
    if _unexpected_exit(process, outcome):
        detail = _diagnostic(stderr, stdout, f"exit {process.returncode}")
        return MermaidRenderResult(f"{source.path}:{source.lineno}: Mermaid renderer failed: {detail}", False)
    if not _complete_svg(output_path):
        return MermaidRenderResult(f"{source.path}:{source.lineno}: Mermaid renderer produced no complete SVG", False)
    terminated_after_render = outcome.stopped_after_complete
    if outcome.cleaned_lingering_group:
        terminated_after_render = True
    return MermaidRenderResult(None, terminated_after_render)


def _render_source(source: MermaidSource, command: Sequence[str], directory: Path) -> MermaidRenderResult:
    stem = f"{Path(source.path).stem}-{source.lineno}"
    input_path = directory / f"{stem}.mmd"
    output_path = directory / f"{stem}.svg"
    input_path.write_text(source.body, encoding="utf-8")
    try:
        process = subprocess.Popen(  # noqa: S603  # nosec B603 -- explicit renderer path or pinned npx package
            [*command, "-i", str(input_path), "-o", str(output_path)],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_chrome_environment(),
            text=True,
        )
    except OSError as error:
        return MermaidRenderResult(f"{source.path}:{source.lineno}: Mermaid renderer failed: {error}", False)
    return _classify_renderer_result(source, process, output_path, _wait_for_renderer(process, output_path))


def render_mermaid_sources(sources: Sequence[MermaidSource], renderer: str | None = None) -> MermaidRenderReport:
    """Render each block independently to bound Chrome lifecycle and isolate failures."""
    try:
        command = _renderer_command(renderer)
    except RuntimeError as reason:
        return MermaidRenderReport((str(reason),), ())
    results: list[tuple[MermaidSource, MermaidRenderResult]] = []
    with tempfile.TemporaryDirectory(prefix="phaze-mermaid-") as temporary:
        for source in sources:
            with tempfile.TemporaryDirectory(prefix="block-", dir=temporary) as block_temporary:
                results.append((source, _render_source(source, command, Path(block_temporary))))
    return MermaidRenderReport(
        tuple(result.error for _, result in results if result.error is not None),
        tuple(f"{source.path}:{source.lineno}" for source, result in results if result.terminated_after_render),
    )


def _check_external_url(target: str) -> str | None:
    url, _ = urldefrag(target)
    if urlsplit(url).scheme not in {"http", "https"}:
        return None
    request = Request(url, method="HEAD", headers={"User-Agent": "phaze-documentation-integrity/1"})  # noqa: S310
    try:
        with urlopen(request, timeout=10):  # noqa: S310  # nosec B310 -- schemes are explicitly restricted above
            return None
    except HTTPError as error:
        return f"{target}: HTTP {error.code}"
    except (OSError, URLError) as error:
        return f"{target}: {error}"


def check_external_targets(
    targets: Sequence[str],
    checker: Callable[[str], str | None] = _check_external_url,
) -> tuple[str, ...]:
    """Check external links concurrently, separate from deterministic local results."""
    with ThreadPoolExecutor(max_workers=8) as executor:
        return tuple(sorted(error for error in executor.map(checker, targets) if error is not None))


def check_documentation(
    root: Path = REPO_ROOT,
    *,
    render_mermaid: bool = False,
    renderer: str | None = None,
    check_external: bool = False,
    external_checker: Callable[[str], str | None] = _check_external_url,
) -> dict[str, object]:
    """Run the maintained-document contract and return a stable JSON-ready report."""
    paths = tracked_working_paths(root)
    texts = _documentation_texts(root, paths)
    generated = _generated_paths(texts)
    maintained = _maintained_paths(paths, generated)
    local_count, local_errors, external_targets = _local_link_errors(root, maintained, texts)
    mermaid_sources, mermaid_structure_errors = _mermaid_sources(maintained, texts)
    mermaid_render_report = render_mermaid_sources(mermaid_sources, renderer) if render_mermaid else MermaidRenderReport((), ())
    errors = tuple(
        sorted(
            (
                *_index_errors(root, maintained, texts),
                *_generator_errors(paths, generated, texts),
                *_migration_errors(root),
                *_forbidden_errors(maintained, texts),
                *local_errors,
            )
        )
    )
    external_errors = check_external_targets(external_targets, external_checker) if check_external else ()
    return {
        "source_revision": source_revision(root),
        "maintained_documents": len(maintained),
        "indexed_maintained_markdown": len([path for path in maintained if path.endswith(".md") and path != "docs/README.md"]),
        "local_links_checked": local_count,
        "external_targets": list(external_targets),
        "external_errors": list(external_errors),
        "generator_markers": len(generated),
        "migration_heads": sorted(migration_heads(root)),
        "mermaid_blocks": len(mermaid_sources),
        "mermaid_cli_version": MERMAID_CLI_VERSION,
        "mermaid_rendered": len(mermaid_sources) - len(mermaid_render_report.errors) if render_mermaid else 0,
        "mermaid_terminated_after_render": list(mermaid_render_report.terminated_after_render),
        "mermaid_errors": sorted((*mermaid_structure_errors, *mermaid_render_report.errors)),
        "errors": list(errors),
    }


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-mermaid", action="store_true", help="render every maintained Mermaid block with the pinned real renderer")
    parser.add_argument("--mermaid-cli", help="path to an existing mmdc executable (default: pinned package through npx)")
    parser.add_argument("--check-external", action="store_true", help="check external HTTP links separately from deterministic local checks")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report = check_documentation(
        render_mermaid=args.render_mermaid,
        renderer=args.mermaid_cli,
        check_external=args.check_external,
    )
    print(json.dumps(report, indent=2, sort_keys=True))  # noqa: T201
    if report["errors"] or report["mermaid_errors"]:
        return 1
    return 2 if report["external_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
