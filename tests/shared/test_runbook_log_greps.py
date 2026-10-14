"""Every log string ``docs/runbook.md`` tells the operator to ``grep`` for must exist in ``src/phaze`` (phaze-5rvda).

The operator reading container stdout is the only log consumer (no Loki, no log exporter), so a
reworded log line silently breaks a recovery step. The strings are DERIVED from the runbook's own
``| grep "..."`` commands rather than restated here, so a new runbook grep is guarded without
anyone remembering to add it.
"""

from __future__ import annotations

from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[2]
RUNBOOK = REPO / "docs" / "runbook.md"
SRC = REPO / "src" / "phaze"

#: ``logs <service> | grep "<string>"`` -- only a log-reading grep, not ``docker volume ls | grep -E``.
_LOG_GREP = re.compile(r'logs\s+\S+\s*\|\s*grep\s+"([^"]+)"')


def _runbook_log_greps() -> list[str]:
    return _LOG_GREP.findall(RUNBOOK.read_text(encoding="utf-8"))


def test_the_runbook_log_greps_are_found() -> None:
    assert {"stranded 'active' jobs reaped", "schema at head"} <= set(_runbook_log_greps())


def test_every_runbook_log_grep_exists_in_the_source() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in SRC.rglob("*.py"))
    missing = [s for s in _runbook_log_greps() if s not in corpus]
    assert not missing, f"docs/runbook.md greps for log string(s) no longer in src/phaze: {missing}"
