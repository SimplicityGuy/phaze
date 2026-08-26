"""A stand-in ``analyze_file`` that reports the trace it is running inside.

Loaded by ``phaze.analysis_child`` through ``PHAZE_ANALYSIS_CHILD_TARGET`` -- the module's
own documented test seam -- so the REAL child process, the REAL argv and the REAL
environment plumbing are exercised while essentia is not. It reports its pid and
interpreter as well as its trace id, so a test cannot pass by accidentally running in the
parent process.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from opentelemetry import trace

from phaze.telemetry.context import TRACEPARENT_ENV


def report_trace(file_path: str, models_dir: str, **_kwargs: Any) -> dict[str, Any]:
    """Return the child's view of the trace, in the ``analyze_file`` result position."""
    span_context = trace.get_current_span().get_span_context()
    return {
        "file_path": file_path,
        "models_dir": models_dir,
        "pid": os.getpid(),
        "executable": sys.executable,
        "traceparent_seen": bool(os.environ.get(TRACEPARENT_ENV, "").strip()),
        "child_trace_id": format(span_context.trace_id, "032x") if span_context.is_valid else None,
    }
