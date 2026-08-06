"""Host-derived thread + concurrency sizing for the analyze path (phaze-rvcn).

**Why this module exists at all.** TensorFlow sizes **both** of its thread pools from the
machine's core count, and pool threads carry allocation arena. Left at those defaults,
phaze's per-process analyze peak is a function of *the host's core count* rather than of the
workload -- so every sizing figure the `phaze-esut` -> `phaze-7i0k` -> `phaze-15sw` ->
`phaze-3j67` -> `phaze-0582` investigation produced is valid only for the 4-physical-core node
it was measured on. Move to a 32-physical-core host and TF sizes both pools at ~64, and the
per-process peak moves with them -- risking the node-scoped OOM failure mode
(`oom-kill:constraint=CONSTRAINT_NONE`) that ADR-0005 and seven spikes exist to eliminate, on
a bigger box with more headroom to hide it until it doesn't.

**Pinning the pools is therefore not an optimization. It is the mechanism that decouples
per-process peak memory from host core count.** Without it no published sizing number is
portable -- and the *magnitude* on a bigger box is not knowable from a 4-core node, which is
precisely the argument for removing the dependence rather than trying to measure it. What was
measured here (§ the constants below): the memory term is the **inter-op** pool, which this
module pins at the constant 1; with it pinned, per-process peak is flat at 1.127-1.155 GiB
across intra-op 1..8 and effective core counts 1..4, against 0.2-0.3% run-to-run
repeatability. A constant cannot track a core count on any host, measured or not.

So this module encodes the *policy*, derived at runtime, rather than a number:

    intra_op_threads x concurrency  ~=  physical_cores

Both knobs live here, in one function (:func:`derive_sizing`), because they are **not
independent** and must not be tuned separately: `phaze-3j67`'s measured concurrency knee at
W=2 was obtained with *default* threading, where a single extractor consumed ~6.2 of the
node's 8 logical cores. Cap intra-op and that knee moves. Splitting the two across two
config surfaces is exactly how they drift apart.

Worked examples of the same one policy:

| host                 | physical | intra-op | concurrency | product |
| -------------------- | -------: | -------: | ----------: | ------: |
| vox (Xeon E3-1271 v3)|        4 |        4 |           1 |       4 |
| 8-physical-core box  |        8 |        4 |           2 |       8 |
| 32-physical-core box |       32 |        4 |           8 |      32 |
| 2-physical-core VM   |        2 |        2 |           1 |       2 |

**PHYSICAL cores, not `nproc`.** `phaze-3j67` 4/6 measured essentia's linear-scaling claim
as validated to the physical core count (3.61x on 4 workers, 90.3% efficiency) and
contradicted past it (4 -> 8 workers gained +3.9% for double the per-file latency, because
workers 5-8 land on hyperthread siblings). Throughput per busy logical core splits cleanly
in two at exactly the physical count: 6.2-6.9 files/h/core at or below 4 runnable threads,
3.5-3.8 above. SMT is not free capacity for this workload.

**Detection is of *schedulable* physical cores, not of the machine's.** `sched_getaffinity`
and the cgroup v2 CPU quota are both honoured, so a `taskset`-restricted or CPU-limited
container derives from what it can actually run on. That is also what makes the decoupling
testable on one machine (`phaze-rvcn` verification): restrict the effective core count and
the derived thread count -- and with it the peak -- moves, while the host does not.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import platform
import re
import subprocess  # sysctl(8) on Darwin only; fixed argv, no shell


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The measured constants
# ---------------------------------------------------------------------------

# The intra-op cap. It is a WALL-CLOCK knee, not a memory knee -- and that attribution is a
# correction, measured for phaze-rvcn against the CURRENT code (model-major + batch 32) on the
# burst node, one exec'd process per arm, `VmHWM` read once at exit, node idle:
#
#   4 physical cores (8 logical), inter-op pinned at 1, OMP = intra-op:
#     intra-op 1  1.1449 GiB  475.8 s   (+172.8% wall)
#     intra-op 2  1.1303 GiB  268.0 s   ( +53.7% wall)
#     intra-op 4  1.1509 GiB  174.4 s   <- derived
#     intra-op 8  1.1286 GiB  171.5 s   (  -1.7% wall, and it would scale with the host)
#
#   2 physical cores (4 logical), same pinning -- the KNEE MOVES WITH THE INPUT:
#     intra-op 1  1.1365 GiB  474.8 s   ( +77.4% wall)
#     intra-op 2  1.1494 GiB  267.6 s   <- derived
#     intra-op 4  1.1547 GiB  259.6 s   (  -3.0% wall)
#     intra-op 8  1.1308 GiB  262.7 s   (  -1.8% wall)
#
# Two things to read off that. First, **peak is flat in the intra-op count** -- 1.1286-1.1547
# GiB across 1..8 threads AND both core counts, a 2.3% spread with no trend, against 0.2-0.3%
# run-to-run repeatability. `phaze-7i0k` 5 and `phaze-3j67` 5 attributed a memory saving to
# this knob, but they set all three variables together against essentia's default batch of 64;
# `phaze-0582` has since taken the batch to 32, which removed the arena the pool was
# multiplying. The memory saving now belongs entirely to `_INTER_OP_THREADS` below.
#
# Second, **the wall-clock floor is reached at intra-op == physical cores, at both core
# counts** -- which is exactly what `min(4, physical_cores)` derives, and is why this is a
# derivation rather than the literal 4 the spikes recommended. Below it the cost is steep at
# every size (1 thread: +172.8% at 4 cores, +77.4% at 2), so 1 is never chosen. Above it the
# gain is inside 2% and the thread count would start tracking the host again, which is the
# coupling this module exists to break.
_INTRA_OP_KNEE_THREADS = 4

# TF inter-op parallelism (concurrent *operations*, as opposed to threads within one op).
# **This is the knob that carries the memory, and pinning it at a constant is what makes the
# published sizing figures portable.** Measured independently, 4 physical cores, everything
# else at TF's defaults:
#
#   nothing set (TF sizes both pools from the core count)  1.3349 GiB  160.8 s
#   TF_NUM_INTRAOP_THREADS=4 alone                         1.3375 GiB  165.6 s  (+0.2% peak)
#   OMP_NUM_THREADS=4 alone                                1.3419 GiB  160.8 s  (+0.5% peak)
#   TF_NUM_INTEROP_THREADS=1 alone                         1.1312 GiB  171.7 s  (-15.3% peak)
#   all three (4 / 1 / 4)                                  1.1509 GiB  174.4 s  (-13.8% peak)
#
# Pinned at 1 rather than derived, and deliberately so: essentia drives one
# `TensorflowPredict*` graph at a time through a synchronous binding, so there is no second
# independent op for a wider inter-op pool to run -- it is pure arena cost. A CONSTANT is also
# strictly better than a cap for the portability question, because a constant cannot track the
# core count on any host, measured or not.
_INTER_OP_THREADS = 1


# ---------------------------------------------------------------------------
# Environment surface (all optional -- a fresh host is correct with none of them set)
# ---------------------------------------------------------------------------

INTRA_OP_ENV = "TF_NUM_INTRAOP_THREADS"
INTER_OP_ENV = "TF_NUM_INTEROP_THREADS"
OMP_ENV = "OMP_NUM_THREADS"

# Operator escape hatch for the detection itself, for the case the derivation cannot see:
# a cgroup CPU limit expressed some other way, a hypervisor lying about topology, or an
# operator who wants the 32-core policy on a 4-core box for a soak test. Applies to the
# WHOLE derivation (both knobs), which is the point -- overriding the input keeps the two
# outputs in the relationship this module exists to preserve.
PHYSICAL_CORES_ENV = "PHAZE_ANALYSIS_PHYSICAL_CORES"


@dataclass(frozen=True)
class AnalysisSizing:
    """One derivation. Every field is a function of :attr:`physical_cores`."""

    physical_cores: int
    intra_op_threads: int
    inter_op_threads: int
    omp_threads: int
    concurrency: int
    # How `physical_cores` was obtained -- carried so the log line (and any bug report
    # against a surprising derivation) names the source rather than just the number.
    source: str

    def as_thread_env(self) -> dict[str, str]:
        """The three TF/OpenMP env vars this derivation implies."""
        return {
            INTRA_OP_ENV: str(self.intra_op_threads),
            INTER_OP_ENV: str(self.inter_op_threads),
            OMP_ENV: str(self.omp_threads),
        }


# ---------------------------------------------------------------------------
# Physical-core detection
# ---------------------------------------------------------------------------


def _schedulable_cpus() -> set[int]:
    """The logical CPUs this process may actually run on.

    `sched_getaffinity` is the honest answer under `taskset`, a cpuset cgroup, or a
    container's `cpuset.cpus` -- `os.cpu_count()` reports the machine and would ignore all
    three. Falls back to the machine count where the call is unavailable (non-Linux).

    Reached via ``getattr`` rather than ``os.sched_getaffinity`` directly because the symbol
    does not exist in the Darwin stubs -- mypy runs on the dev machine, the analyze path runs
    on Linux, and a ``sys.platform`` branch here would still fail the Darwin type check.
    """
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is not None:
        try:
            return set(getaffinity(0))
        except OSError:
            pass
    return set(range(os.cpu_count() or 1))


def _cgroup_cpu_quota() -> int | None:
    """Effective whole CPUs permitted by the cgroup v2 CPU bandwidth limit, if any.

    `cpu.max` is `"<quota> <period>"` in microseconds, or `"max <period>"` for unlimited.
    A bandwidth limit is not a cpuset: the process still *sees* every CPU and
    `sched_getaffinity` still returns all of them, but it is throttled to `quota/period`
    CPUs of runtime. Deriving a thread pool from the affinity set under such a limit
    oversubscribes by exactly the ratio -- so the quota is taken as an upper bound too.

    Rounded UP: a 3.5-CPU quota is 4 cores' worth of parallelism with throttling, and
    rounding down would under-thread a deliberately fractional allocation.
    """
    try:
        raw = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    parts = raw.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, math.ceil(quota / period))


def _physical_cores_from_sysfs(schedulable: set[int]) -> int | None:
    """Count distinct SMT sibling groups among the schedulable CPUs, from sysfs topology.

    `thread_siblings_list` (`core_cpus_list` on newer kernels) names every logical CPU
    sharing one physical core, so canonicalizing each schedulable CPU to its sibling set and
    counting the distinct sets is the physical-core count *available to this process* --
    which is the quantity the policy keys on, and the one that moves under `taskset`.
    """
    groups: set[frozenset[int]] = set()
    for cpu in schedulable:
        base = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        for name in ("thread_siblings_list", "core_cpus_list"):
            try:
                raw = (base / name).read_text(encoding="utf-8").strip()
            except OSError:
                continue
            siblings = _parse_cpu_list(raw)
            if siblings:
                # The kernel's FULL sibling set is the group key, deliberately NOT intersected
                # with `schedulable`: two schedulable siblings of one core must collapse to one
                # physical core, and one schedulable sibling of a core still IS one physical
                # core. Both cases fall out of keying on the unintersected set.
                groups.add(frozenset(siblings))
                break
        else:
            return None  # topology unreadable for at least one CPU -- do not half-guess
    return len(groups) or None


def _parse_cpu_list(raw: str) -> set[int]:
    """Parse a kernel CPU list (`"0,4"`, `"0-3"`, `"0-3,8-11"`) into a set of ints."""
    cpus: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo, _, hi = chunk.partition("-")
            try:
                cpus.update(range(int(lo), int(hi) + 1))
            except ValueError:
                return set()
        else:
            try:
                cpus.add(int(chunk))
            except ValueError:
                return set()
    return cpus


def _physical_cores_from_proc_cpuinfo(schedulable: set[int]) -> int | None:
    """Second, independent Linux source: distinct ``(physical id, core id)`` pairs.

    Used when sysfs topology is unreadable (a stripped container `/sys`). Only processors
    in ``schedulable`` are counted, so this tracks `taskset` the same way sysfs does.
    x86-specific -- many ARM kernels emit neither key, in which case this returns ``None``
    and the caller falls back.
    """
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8")
    except OSError:
        return None
    pairs: set[tuple[str, str]] = set()
    for block in re.split(r"\n\s*\n", text):
        processor = physical_id = core_id = None
        for line in block.splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key == "processor":
                try:
                    processor = int(value)
                except ValueError:
                    processor = None
            elif key == "physical id":
                physical_id = value
            elif key == "core id":
                core_id = value
        if processor is None or processor not in schedulable:
            continue
        if physical_id is None or core_id is None:
            return None
        pairs.add((physical_id, core_id))
    return len(pairs) or None


def _physical_cores_from_darwin() -> int | None:
    """`hw.physicalcpu_max` on Darwin. Dev-machine path only -- production analyze is Linux."""
    try:
        out = subprocess.run(  # fixed argv, no shell, no interpolation
            ["/usr/sbin/sysctl", "-n", "hw.physicalcpu_max"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return int(out.stdout.strip()) or None
    except ValueError:
        return None


def detect_physical_cores() -> tuple[int, str]:
    """Schedulable physical cores, and the name of the source that produced the number.

    Order: explicit override, then sysfs topology, then `/proc/cpuinfo`, then Darwin
    `sysctl`, then the schedulable *logical* count as a last resort. Every source is
    intersected with `sched_getaffinity` and clamped by the cgroup v2 CPU quota, so a
    restricted container derives from its own slice rather than from the machine.

    **The last-resort fallback deliberately over-estimates** (logical, not logical/2). It
    cannot make the memory decoupling unsafe -- :data:`_INTRA_OP_KNEE_THREADS` caps the
    thread pool at 4 whatever the input, which is the property that bounds peak RSS -- and
    halving a genuinely non-SMT box (Ampere, Graviton, most ARM) would silently halve
    derived concurrency on exactly the hardware where the topology files are most often
    absent. The cost of the over-estimate is bounded to the concurrency knob; the cost of an
    under-estimate would be a permanent throughput haircut nobody notices.
    """
    raw_override = os.environ.get(PHYSICAL_CORES_ENV)
    if raw_override is not None and raw_override.strip():
        try:
            value = int(raw_override)
        except ValueError:
            value = 0
        if value >= 1:
            return value, f"env:{PHYSICAL_CORES_ENV}"
        log.warning("%s=%r is not a positive int; deriving from the host instead", PHYSICAL_CORES_ENV, raw_override)

    schedulable = _schedulable_cpus()
    quota = _cgroup_cpu_quota()

    detected: int | None = None
    source = "logical:sched_getaffinity"
    if platform.system() == "Darwin":
        detected = _physical_cores_from_darwin()
        source = "darwin:hw.physicalcpu_max"
    else:
        detected = _physical_cores_from_sysfs(schedulable)
        source = "sysfs:thread_siblings_list"
        if detected is None:
            detected = _physical_cores_from_proc_cpuinfo(schedulable)
            source = "proc:cpuinfo"

    if detected is None:
        detected = len(schedulable) or 1
        source = "logical:sched_getaffinity"

    # A Darwin/last-resort count can exceed the affinity set; never claim more cores than
    # there are schedulable logical CPUs.
    detected = min(detected, len(schedulable) or detected)

    if quota is not None and quota < detected:
        return max(1, quota), f"{source}+cgroup:cpu.max"
    return max(1, detected), source


# ---------------------------------------------------------------------------
# The policy -- one function, both knobs
# ---------------------------------------------------------------------------


def derive_sizing(physical_cores: int | None = None, *, source: str | None = None) -> AnalysisSizing:
    """Derive every thread/concurrency knob from the schedulable physical-core count.

    This is **the** expression of ``intra_op_threads x concurrency ~= physical_cores``. Both
    outputs come out of this one function so they cannot drift apart across config surfaces:
    the intra-op cap moves the concurrency knee (`phaze-3j67` 5 -- with 4 threads per process
    the node needs W=4 rather than W=2-3 to saturate), so a concurrency chosen anywhere else
    would be chosen against the wrong threading.

        intra_op    = min(4, physical_cores)      # 4 is the measured knee, and it is a CAP
        inter_op    = 1                           # essentia drives one graph at a time
        omp         = intra_op                    # same pool sizing for essentia's own OpenMP
        concurrency = physical_cores // intra_op  # floor: never oversubscribe the box

    ``concurrency`` uses floor division deliberately. On a core count that is not a multiple
    of the intra-op cap (6, 10, 14 ...) the remainder is left idle rather than rounded up
    into oversubscription, because the failure this module exists to prevent is a host whose
    threads outrun its cores. The residual is at most `intra_op - 1` cores and is recovered,
    if an operator wants it, through their own `cap` -- which stays operator-owned.
    """
    if physical_cores is None:
        physical_cores, detected_source = detect_physical_cores()
        source = source or detected_source
    physical_cores = max(1, physical_cores)

    intra_op = min(_INTRA_OP_KNEE_THREADS, physical_cores)
    concurrency = max(1, physical_cores // intra_op)

    return AnalysisSizing(
        physical_cores=physical_cores,
        intra_op_threads=intra_op,
        inter_op_threads=_INTER_OP_THREADS,
        omp_threads=intra_op,
        concurrency=concurrency,
        source=source or "explicit",
    )


# ---------------------------------------------------------------------------
# Applying the derivation to the process environment
# ---------------------------------------------------------------------------


def apply_thread_env(env: dict[str, str] | None = None) -> AnalysisSizing:
    """Stamp the derived thread caps onto ``env`` (default: :data:`os.environ`).

    **Must be called before TensorFlow initializes** -- TF reads all three variables when it
    builds its thread pools, so a value set after the first session is constructed is read
    by nothing. `services/analysis.py` calls this at module import, immediately before
    ``import essentia``, alongside the existing pre-import ``TF_CPP_MIN_LOG_LEVEL`` stamp.

    **An already-set variable is never overwritten.** A deployment that sets
    ``TF_NUM_INTRAOP_THREADS`` in its ConfigMap keeps exactly the value it set -- the
    derivation is the *default*, not an override of the operator. Each variable is
    considered independently, and that independence is load-bearing rather than tidy:
    ``Dockerfile.agent-arm64`` sets ``OMP_NUM_THREADS=1`` as a **correctness** mitigation for
    the dual-OpenMP-runtime segfault (`docs/arm64-agent-image.md` fix #4), not as a sizing
    choice. That ``1`` must survive, while the two TF variables next to it are still derived.

    Returns the derivation so the caller can log it; the returned object describes what was
    *derived*, which may differ from what is now in the environment if the operator set a
    value explicitly. The log line records both.
    """
    target = os.environ if env is None else env
    sizing = derive_sizing()

    applied: dict[str, str] = {}
    inherited: dict[str, str] = {}
    for name, value in sizing.as_thread_env().items():
        existing = target.get(name)
        if existing is not None and existing.strip():
            inherited[name] = existing
            continue
        target[name] = value
        applied[name] = value

    log.info(
        "analyze thread sizing: physical_cores=%d (%s) -> intra_op=%d inter_op=%d omp=%d concurrency=%d; applied=%s operator_set=%s",
        sizing.physical_cores,
        sizing.source,
        sizing.intra_op_threads,
        sizing.inter_op_threads,
        sizing.omp_threads,
        sizing.concurrency,
        applied or "{}",
        inherited or "{}",
    )
    return sizing
