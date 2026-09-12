"""BEUI-03 docs-content guard (Phase 71).

Locks the operator-facing documentation deliverables of BEUI-03 so a future edit cannot
silently drop them:

* ``docs/runbook.md`` exists and covers the four operator topics — the force-local incident
  revert (incl. the A4 held-file note), reading the N backend lanes, spillover, and per-backend
  ``_FILE`` secrets — and prints **no literal secret value** (T-71-11).
* ``docs/configuration.md`` reconciles the ``cloud_target`` contradiction (T-71-12): it states the
  flat selector was **removed in Phase 67** (not "deprecated but still works") and carries the
  trivial 1:1 ``cloud_target`` -> ``backends`` equivalence.

This is a pure filesystem structural guard mirroring the repo's established hermetic idiom
(``test_docs_ia_current.py``): repo-root ``Path`` constants, ``read_text`` parse-then-assert, one
assertion per behavior, every assert carries a precise offender message, and ZERO ``phaze.*``
imports -> hermetic. Being import-free it is immune to the ``get_settings`` lru_cache leak /
``saq_jobs`` stub cross-test poison and passes in isolation via ``just test-bucket shared``. It
needs no DB fixture.
"""

from __future__ import annotations

from pathlib import Path
import re


_REPO_ROOT = Path(__file__).resolve().parents[3]  # tests/shared/core/X.py -> repo root
_DOCS = _REPO_ROOT / "docs"
_RUNBOOK = _DOCS / "runbook.md"
_CONFIGURATION = _DOCS / "configuration.md"
_CONFIG_SOURCE = _REPO_ROOT / "src" / "phaze" / "config.py"
_ANALYSIS_EXEC_SOURCE = _REPO_ROOT / "src" / "phaze" / "services" / "analysis_exec.py"
_ANALYSIS_ENQUEUE_SOURCE = _REPO_ROOT / "src" / "phaze" / "services" / "analysis_enqueue.py"
_QUEUE_DEFAULTS_SOURCE = _REPO_ROOT / "src" / "phaze" / "tasks" / "_shared" / "queue_defaults.py"
_SAQ_REAP_SOURCE = _REPO_ROOT / "src" / "phaze" / "tasks" / "_saq_reap.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _contains_all(haystack: str, needles: list[str]) -> list[str]:
    """Return the needles (case-insensitive) that are MISSING from haystack."""
    lowered = haystack.lower()
    return [n for n in needles if n.lower() not in lowered]


def _captured_int(text: str, pattern: str, label: str) -> int:
    match = re.search(pattern, text, re.DOTALL)
    assert match is not None, f"could not derive {label} from its runtime source"
    return int(match.group(1))


def test_runbook_exists() -> None:
    """``docs/runbook.md`` must exist — it is the BEUI-03 operator runbook deliverable."""
    assert _RUNBOOK.is_file(), f"missing operator runbook — create {_RUNBOOK.relative_to(_REPO_ROOT)}"


def test_runbook_covers_force_local_incident_revert() -> None:
    """The runbook documents the force-local master toggle / incident revert."""
    text = _read(_RUNBOOK)
    missing = _contains_all(
        text,
        [
            "force-local",  # the toggle by name
            "FORCED LOCAL",  # engaged pill copy
            "CLOUD ROUTING",  # normal pill copy
            "route_control",  # the durable row it writes
            "reversible",  # no-redeploy incident semantics
            "drain",  # gated leg 1
            "duration router",  # gated leg 2
        ],
    )
    assert not missing, f"runbook.md is missing force-local incident-revert coverage: {missing}"


def test_runbook_documents_held_file_behavior() -> None:
    """A4: already-held AWAITING_CLOUD files stay held while forced — must be documented."""
    text = _read(_RUNBOOK)
    assert "held" in text.lower(), "runbook.md must document the A4 held-file behavior (contains 'held')"
    missing = _contains_all(text, ["AWAITING_CLOUD", "stay held"])
    assert not missing, f"runbook.md held-file note is incomplete: {missing}"


def test_runbook_covers_reading_the_lanes() -> None:
    """The runbook explains reading the N lanes: rank order, in-flight/cap, offline, admission."""
    text = _read(_RUNBOOK)
    missing = _contains_all(
        text,
        [
            "rank",  # rank ascending = dispatch preference
            "dispatch preference",
            "in_flight",  # {in_flight}/{cap}
            "cap",
            "offline",  # lane offline state
            "quota",  # Kueue quota-wait
            "Inadmissible",  # vs Inadmissible
        ],
    )
    assert not missing, f"runbook.md is missing lane-reading coverage: {missing}"


def test_runbook_covers_spillover() -> None:
    """The runbook explains spillover across backends by rank and cap."""
    text = _read(_RUNBOOK)
    assert "spillover" in text.lower() or "spill" in text.lower(), "runbook.md must cover spillover behavior"
    missing = _contains_all(text, ["rank", "cap", "eligible"])
    assert not missing, f"runbook.md spillover coverage is incomplete: {missing}"


def test_runbook_covers_file_secrets() -> None:
    """The runbook cross-references the per-backend ``_FILE`` secret convention."""
    text = _read(_RUNBOOK)
    missing = _contains_all(text, ["_FILE", "configuration.md", "never print a secret"])
    assert not missing, f"runbook.md is missing per-backend _FILE secret coverage: {missing}"


def test_runbook_prints_no_literal_secret_values() -> None:
    """T-71-11: the runbook references secrets by NAME only — never a literal value.

    Guards against a future edit pasting a real token / key / PEM block into the runbook. We
    forbid the obvious literal-secret shapes: a PEM header, a real ``phaze_agent_<token>`` value
    (a bare ``phaze_agent_`` prefix in prose is fine; the guard fires only when 12+ token
    characters follow it), and an inline ``password=<value>`` / ``secret=<value>`` assignment.
    """
    text = _read(_RUNBOOK)
    offenders: list[str] = []
    if "-----BEGIN" in text:
        offenders.append("a PEM/private-key block (-----BEGIN ...)")
    if re.search(r"phaze_agent_[A-Za-z0-9_\-]{12,}", text):
        offenders.append("a literal phaze_agent_ bearer token value")
    if re.search(r"(?i)(password|secret_access_key|sa_token)\s*=\s*['\"]?[A-Za-z0-9/+]{8,}", text):
        offenders.append("an inline secret assignment (password/secret_access_key/sa_token = <value>)")
    assert not offenders, f"runbook.md appears to embed a literal secret value — reference secrets by name only: {offenders}"


def test_configuration_states_cloud_target_removed() -> None:
    """T-71-12: configuration.md says cloud_target was REMOVED in Phase 67 (no live-selector).

    The ``### Cloud target`` section must present it as removed and point at the registry — it must
    NOT still describe ``PHAZE_CLOUD_TARGET`` as a live "single routing selector".
    """
    text = _read(_CONFIGURATION)
    lowered = text.lower()
    assert "removed in" in lowered and "phase 67" in lowered, "configuration.md must state cloud_target was removed in Phase 67"
    assert "backend registry" in lowered or "backends.toml" in lowered, "configuration.md must point cloud_target readers at the backend registry"
    # The stale live-selector framing must be gone from the Cloud target section heading.
    assert "### Cloud target (`PHAZE_CLOUD_TARGET`)" not in text, (
        "the stale '### Cloud target (PHAZE_CLOUD_TARGET)' live-selector heading must be reconciled"
    )


def test_configuration_documents_cloud_target_to_backends_equivalence() -> None:
    """configuration.md carries the trivial 1:1 cloud_target -> backends equivalence."""
    text = _read(_CONFIGURATION)
    missing = _contains_all(
        text,
        [
            "equivalence",  # the 1:1 mapping is called out
            'kind="compute"',  # a1 -> compute
            'kind="kueue"',  # k8s -> kueue
            'kind="local"',  # local -> implicit local
        ],
    )
    assert not missing, f"configuration.md is missing the 1:1 cloud_target->backends equivalence: {missing}"


def test_process_file_policy_docs_match_runtime_and_replay_sources() -> None:
    """Current operator docs derive the exceptional analysis policy from live source.

    ``WORKER_JOB_TIMEOUT`` and ``WORKER_MAX_RETRIES`` remain valid generic defaults, so a prose-only
    assertion for ``0``/``2`` could still pass while the producer or recovery hook drifted. Read the
    producer, the replay chokepoint, the settings derivation, and the generic-reaper predicate first;
    then require both maintained operator docs to state the values those sources actually encode.
    """
    enqueue = _read(_ANALYSIS_ENQUEUE_SOURCE)
    queue_defaults = _read(_QUEUE_DEFAULTS_SOURCE)
    config = _read(_CONFIG_SOURCE)
    reaper = _read(_SAQ_REAP_SOURCE)

    producer = re.search(
        r'return await queue\.enqueue\(\s*"process_file".*?timeout=(\d+),.*?'
        r"heartbeat=get_settings\(\)\.analysis_job_heartbeat_sec,.*?retries=(\d+),",
        enqueue,
        re.DOTALL,
    )
    assert producer is not None, "analysis_enqueue.py no longer exposes the process_file timeout/heartbeat/retry policy"
    producer_timeout, producer_retries = (int(value) for value in producer.groups())

    replay = re.search(r'"process_file": \((\d+), (\d+)\)', queue_defaults)
    assert replay is not None, "queue_defaults.py no longer pins process_file replay policy"
    replay_timeout, replay_retries = (int(value) for value in replay.groups())
    assert (replay_timeout, replay_retries) == (producer_timeout, producer_retries)
    assert '"process_file": "analysis_job_heartbeat_sec"' in queue_defaults

    stall = _captured_int(
        config,
        r"analysis_stall_timeout_sec:\s*int\s*=\s*Field\(\s*default=(\d+)",
        "analysis stall timeout",
    )
    multiplier = _captured_int(
        config,
        r"_ANALYSIS_OUTER_HEARTBEAT_MULTIPLIER\s*=\s*(\d+)",
        "analysis heartbeat multiplier",
    )
    heartbeat = stall * multiplier
    assert "<> 0" in reaper, "generic SAQ key reapers no longer exclude explicit timeout=0 rows"

    required = [
        f"timeout={producer_timeout}",
        f"retries={producer_retries}",
        "heartbeat",
        "derived",
        "excluded",
    ]
    for path in (_CONFIGURATION, _RUNBOOK):
        text = _read(path)
        missing = _contains_all(text, required)
        assert not missing, f"{path.name} is missing current process_file policy terms derived from source: {missing}"

    configuration = _read(_CONFIGURATION)
    assert f"`{heartbeat}` at the `{stall}` default" in configuration, (
        "configuration.md must show the derived process_file heartbeat using the live stall default and multiplier"
    )
    assert "letting them time out on the local file server" not in configuration, (
        "configuration.md must not describe cloud routing as a workaround for the retired process_file wall-clock timeout"
    )


def test_analyze_lane_concurrency_doc_matches_child_process_boundary() -> None:
    """The analyze concurrency guidance reflects the live per-job child process boundary."""
    source = _read(_ANALYSIS_EXEC_SOURCE)
    assert '_CHILD_MODULE = "phaze.analysis_child"' in source
    assert "await asyncio.create_subprocess_exec(" in source

    configuration = _read(_CONFIGURATION)
    analyze_row = next(
        (line for line in configuration.splitlines() if "`PHAZE_LANE_ANALYZE_CONCURRENCY`" in line),
        "",
    )
    assert "analysis_child" in analyze_row and "per active job" in analyze_row, (
        "configuration.md must describe the analyze lane as one analysis_child subprocess per active job"
    )
    assert "in-process essentia" not in analyze_row.lower(), "configuration.md must not restore the retired in-process Essentia wording"
