"""Essentia analysis child CLI (Phase 101, OBS-03 — phaze-bo3p.1).

Executed as ``python -m phaze.analysis_child <file> --models-dir <dir> [...]`` by the
shared parent driver (``phaze.services.analysis_exec``). Runs the CPU-bound
``analyze_file`` in THIS process — a real child of the pod/worker parent — so the
parent's asyncio event loop is never GIL-starved by essentia's C++ extensions and
can service progress POSTs mid-analysis (the Phase 101 fix for the 0→100% bar jump).

fd contract (the banner-capture half of OBS-03): BEFORE essentia is imported, the
original stdout (fd 1) is ``os.dup``'d into a private protocol handle and fd 1 is
re-pointed at fd 2 via ``os.dup2``. essentia/TensorFlow C++ banners are written
directly to fd 1/2 — never through Python logging — so after the re-route EVERY raw
write the child makes outside the protocol (banners, stray prints) lands on the
stderr pipe, which the parent frames line-by-line into structlog. The protocol
channel stays machine-clean. This closes the capture/routing TODO the in-process
``asyncio.to_thread`` model deferred to this phase (job_runner phaze-sfbx.4).

Protocol — one JSON object per line on the saved protocol channel:

    {"type": "progress", "analyzed": N, "total": M}   per fine-window bump
    {"type": "result", "result": {...}}               terminal success line → exit 0
    {"type": "error", "message": "..."}               terminal failure line → exit 1

The ``result`` value is the ``analyze_file`` dict verbatim (representative
aggregates + ``windows`` + the five-field coverage contract). It already crosses
HTTP JSON to the control plane today, so the protocol's JSON round-trip introduces
no representation change (byte-identical windowed output — success criterion 4).

IMPORT-BOUNDARY INVARIANT (D-25 family, enforced by tests/shared/core/test_task_split.py):
module load MUST NOT import essentia (the wheel is platform-gated in pyproject.toml)
nor phaze.database / phaze.tasks.session / sqlalchemy.ext.asyncio (the pod is
Postgres-less). The essentia-bound target import is deferred to call time behind
``_load_target``.

``PHAZE_ANALYSIS_CHILD_TARGET`` (``module:attr``, default
``phaze.services.analysis:analyze_file``) overrides the analysis callable. This is a
TEST-ONLY seam: integration tests point it at a slow/fake stub so the REAL subprocess
protocol can be exercised without an essentia wheel. Production never sets it.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import sys
from typing import IO, TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable


_TARGET_ENV = "PHAZE_ANALYSIS_CHILD_TARGET"
_DEFAULT_TARGET = "phaze.services.analysis:analyze_file"
# Mirrors the worker-side cap (tasks/functions.py::_ERROR_DETAIL_MAX): bound the error
# text before it crosses the process boundary so a huge traceback never bloats the pipe.
_ERROR_MESSAGE_MAX = 2000


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the child argv. Windowing flags are optional: absent flags are NOT passed
    through to the target, so ``analyze_file``'s own defaults stay authoritative."""
    parser = argparse.ArgumentParser(prog="phaze.analysis_child", description="phaze essentia analysis child (Phase 101)")
    parser.add_argument("file_path", help="audio file to analyze")
    parser.add_argument("--models-dir", required=True, help="essentia TF models directory")
    parser.add_argument("--fine-window-sec", type=int, default=None)
    parser.add_argument("--coarse-window-sec", type=int, default=None)
    parser.add_argument("--fine-min-sec", type=int, default=None)
    parser.add_argument("--fine-cap", type=int, default=None)
    parser.add_argument("--coarse-cap", type=int, default=None)
    return parser.parse_args(argv)


def _open_protocol_channel() -> IO[str]:
    """Claim the original stdout for the protocol, then re-route fd 1 → fd 2.

    Must run BEFORE the essentia import: essentia/TF banners write straight to fd 1/2
    from C++, so only a file-descriptor-level re-route (not sys.stdout reassignment)
    diverts them onto the stderr pipe the parent frames. Line-buffered so every
    protocol line is flushed to the parent as it is written.
    """
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(protocol_fd, "w", buffering=1)


def _load_target() -> Callable[..., Any]:
    """Deferred, env-overridable import of the analysis callable.

    Deferral keeps module load essentia-free (the platform-gated wheel is absent on
    linux-arm64 and the import-boundary test loads this module without it). The
    production path (env unset or default) is a LITERAL import; only the explicit
    test seam goes through importlib.
    """
    spec = os.environ.get(_TARGET_ENV)
    if spec is None or spec == _DEFAULT_TARGET:
        from phaze.services.analysis import analyze_file  # noqa: PLC0415  # deferred essentia-bound import

        return analyze_file
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        msg = f"malformed {_TARGET_ENV}={spec!r}; expected 'module:attr'"
        raise RuntimeError(msg)
    # The dynamic import is safe by trust model: the value comes from THIS process's own
    # env, and whoever sets a child's environment already controls code execution
    # (argv/PATH/PYTHONPATH), so no new capability is granted.
    module = importlib.import_module(module_name)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import -- test-only env seam
    target: Callable[..., Any] = getattr(module, attr)
    return target


def _emit(protocol: IO[str], obj: dict[str, Any]) -> None:
    """Write one protocol line. ``json.dumps`` is strict (no default=) so a
    non-JSON-serializable result is a loud error, never a silently mangled payload."""
    protocol.write(json.dumps(obj) + "\n")
    protocol.flush()


def run(args: argparse.Namespace, protocol: IO[str]) -> int:
    """Execute the analysis and speak the protocol; returns the process exit code.

    Split from ``main`` so unit tests can drive the protocol against an in-memory
    stream without touching real file descriptors.
    """
    try:
        target = _load_target()

        def _progress(analyzed: int, total: int) -> None:
            _emit(protocol, {"type": "progress", "analyzed": analyzed, "total": total})

        kwargs: dict[str, Any] = {"progress_cb": _progress}
        for name in ("fine_window_sec", "coarse_window_sec", "fine_min_sec", "fine_cap", "coarse_cap"):
            value = getattr(args, name)
            if value is not None:
                kwargs[name] = value
        result = target(args.file_path, args.models_dir, **kwargs)
        _emit(protocol, {"type": "result", "result": result})
    except Exception as exc:
        # Best-effort terminal error line: if the pipe itself is broken (parent died)
        # the suppress keeps the nonzero exit as the sole failure signal.
        with contextlib.suppress(Exception):
            _emit(protocol, {"type": "error", "message": f"{type(exc).__name__}: {exc}"[:_ERROR_MESSAGE_MAX]})
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: claim the protocol channel FIRST (pre-essentia), then run."""
    args = _parse_args(argv)
    with _open_protocol_channel() as protocol:
        return run(args, protocol)


if __name__ == "__main__":  # pragma: no cover  # CLI invocation guard
    sys.exit(main())
