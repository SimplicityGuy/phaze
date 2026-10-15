"""Optional renderer for dated Mermaid evidence; never used by the maintained-doc gate.

The pinned CLI remains available for deliberate historical reproduction. Historical
sources must be supplied explicitly; this module never reclassifies them as current.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, NamedTuple


if TYPE_CHECKING:
    from collections.abc import Sequence


MERMAID_CLI_VERSION = "11.12.0"
_RENDER_EXIT_GRACE_SECONDS = 2.0
_RENDER_TIMEOUT_SECONDS = 60.0
_CHROME_CANDIDATES = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)


class MermaidSource(NamedTuple):
    """One explicitly supplied historical Mermaid body and its source location."""

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
