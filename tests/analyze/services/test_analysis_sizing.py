"""phaze-rvcn: the host-derived thread/concurrency policy.

The bug this guards is silent and only appears on hardware nobody has yet: TensorFlow sizes
its intra-op pool from the machine's core count and gives each worker thread its own
allocation arena, so an UNCAPPED analyze process peaks higher on a bigger box. Every figure
in `docs/k8s-burst.md` is therefore a property of the 4-physical-core node it was measured
on, and a hardware upgrade would raise per-process peak while looking like an improvement.

What is assertable here is the CONTRACT, not the memory:

* the derivation keys on **physical** cores and produces
  ``intra_op x concurrency ~= physical_cores`` at every core count;
* the intra-op cap is a **maximum** (a 2-core VM derives 2, not 4) and never 1 unless the
  box genuinely has one physical core -- `phaze-7i0k` 5 measured 1 thread at +210.6% wall
  for 0.001 GiB less than 4;
* both knobs come out of **one** function, so they cannot drift apart;
* an operator-set env var always wins, per variable;
* detection tracks `sched_getaffinity` and the cgroup v2 quota, which is what makes the
  decoupling testable on a single machine.

The memory half is not assertable in a unit test -- it needs the real 3 GB model set on the
Linux burst node. It lives in the bead's measurement table (`docs/k8s-burst.md`
"Thread sizing is derived from the host").
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from phaze.config_backends import _default_local_registry
import phaze.services.analysis_sizing as sizing_mod
from phaze.services.analysis_sizing import (
    INTER_OP_ENV,
    INTRA_OP_ENV,
    OMP_ENV,
    PHYSICAL_CORES_ENV,
    AnalysisSizing,
    apply_thread_env,
    derive_sizing,
    detect_physical_cores,
)


if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _no_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default arm: nothing is set, so a fresh host takes the derivation."""
    for name in (INTRA_OP_ENV, INTER_OP_ENV, OMP_ENV, PHYSICAL_CORES_ENV):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# The policy itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("physical", "intra_op", "concurrency"),
    [
        (1, 1, 1),  # a 1-core VM: 1 thread is forced, not chosen
        (2, 2, 1),
        (3, 3, 1),
        (4, 4, 1),  # vox (Xeon E3-1271 v3) -- the node every published figure was measured on
        (6, 4, 1),  # floor division: the 2-core remainder is left idle, NOT rounded up
        (8, 4, 2),
        (16, 4, 4),
        (32, 4, 8),  # the hypothetical upgrade this bead exists for
        (64, 4, 16),
    ],
)
def test_derivation_table(physical: int, intra_op: int, concurrency: int) -> None:
    """The worked examples, including the two the bead names explicitly (4 and 32 cores)."""
    result = derive_sizing(physical)
    assert result.physical_cores == physical
    assert result.intra_op_threads == intra_op
    assert result.concurrency == concurrency
    assert result.inter_op_threads == 1
    assert result.omp_threads == intra_op  # OMP tracks intra-op, never the host


@pytest.mark.parametrize("physical", list(range(1, 65)))
def test_intra_op_times_concurrency_never_oversubscribes(physical: int) -> None:
    """``intra_op x concurrency <= physical_cores`` at EVERY core count, 1..64.

    The relation is stated as ``~=`` because floor division leaves a remainder below the
    intra-op cap; what must never happen is the product exceeding the core count, which is
    the shape ("more threads than cores") this module exists to prevent. The remainder is
    bounded by the cap, so the derivation is never worse than `intra_op - 1` cores idle.
    """
    result = derive_sizing(physical)
    product = result.intra_op_threads * result.concurrency
    assert product <= physical
    assert physical - product < result.intra_op_threads


def test_intra_op_never_exceeds_the_measured_knee() -> None:
    """Peak memory must not track the host. 4 is the cap at every core count above it.

    This single assertion is the whole point of the bead: on a 32-physical-core host TF
    would otherwise spin ~64 intra-op threads -- 8x vox's arenas -- and the per-process peak
    would rise silently.
    """
    for physical in (4, 8, 16, 32, 64, 128, 256):
        assert derive_sizing(physical).intra_op_threads == 4


def test_intra_op_is_a_cap_not_a_target() -> None:
    """Below the knee the derivation follows the box rather than oversubscribing to reach 4."""
    assert derive_sizing(2).intra_op_threads == 2
    assert derive_sizing(3).intra_op_threads == 3


def test_single_thread_is_only_derived_on_a_single_core_host() -> None:
    """`phaze-7i0k` 5: 1 thread costs +210.6% wall for 0.001 GiB less than 4 threads.

    So 1 must never be *chosen* -- it is only reachable when the host has exactly one
    schedulable physical core and there is nothing to choose.
    """
    assert derive_sizing(1).intra_op_threads == 1
    for physical in range(2, 65):
        assert derive_sizing(physical).intra_op_threads >= 2


def test_derivation_is_pure_in_its_input() -> None:
    """Same core count in, same everything out -- no hidden host read on the explicit path."""
    assert derive_sizing(32, source="test") == AnalysisSizing(
        physical_cores=32,
        intra_op_threads=4,
        inter_op_threads=1,
        omp_threads=4,
        concurrency=8,
        source="test",
    )


def test_zero_or_negative_core_count_floors_to_one() -> None:
    """A bogus detection result degrades to a runnable derivation, never to 0 threads."""
    assert derive_sizing(0).intra_op_threads == 1
    assert derive_sizing(-5).concurrency == 1


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_detection_tracks_the_affinity_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    """Physical cores are counted from SMT sibling groups among SCHEDULABLE CPUs only.

    This is the mechanism the single-machine verification leans on: restricting the
    process to two sibling pairs must derive 2 physical cores, not the machine's 4.
    """
    monkeypatch.setattr(sizing_mod, "_schedulable_cpus", lambda: {0, 1, 4, 5})
    monkeypatch.setattr(sizing_mod, "_cgroup_cpu_quota", lambda: None)
    monkeypatch.setattr(sizing_mod.platform, "system", lambda: "Linux")
    # Xeon E3-1271 v3 sibling map: (0,4) (1,5) (2,6) (3,7)
    monkeypatch.setattr(sizing_mod, "_physical_cores_from_sysfs", lambda cpus: len({frozenset({c, c ^ 4}) for c in cpus}))
    cores, source = detect_physical_cores()
    assert cores == 2
    assert source == "sysfs:thread_siblings_list"


def test_cgroup_cpu_quota_clamps_the_detected_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CPU *bandwidth* limit is invisible to `sched_getaffinity` and must still clamp.

    Under `cpu.max = "200000 100000"` the process still sees every CPU but is throttled to
    two CPUs of runtime; deriving from the affinity set would oversubscribe by the ratio.
    """
    monkeypatch.setattr(sizing_mod, "_schedulable_cpus", lambda: set(range(8)))
    monkeypatch.setattr(sizing_mod, "_cgroup_cpu_quota", lambda: 2)
    monkeypatch.setattr(sizing_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sizing_mod, "_physical_cores_from_sysfs", lambda _cpus: 4)
    cores, source = detect_physical_cores()
    assert cores == 2
    assert source.endswith("+cgroup:cpu.max")


def test_cgroup_quota_above_the_core_count_does_not_inflate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quota looser than the hardware is not extra cores."""
    monkeypatch.setattr(sizing_mod, "_schedulable_cpus", lambda: set(range(8)))
    monkeypatch.setattr(sizing_mod, "_cgroup_cpu_quota", lambda: 16)
    monkeypatch.setattr(sizing_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sizing_mod, "_physical_cores_from_sysfs", lambda _cpus: 4)
    assert detect_physical_cores()[0] == 4


def test_cgroup_cpu_max_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`cpu.max` shapes: unlimited, whole, fractional (rounds UP), and malformed."""
    target = tmp_path / "cpu.max"
    real_read = sizing_mod.Path

    def _patched(path: str) -> Path:
        return target if str(path) == "/sys/fs/cgroup/cpu.max" else real_read(path)

    monkeypatch.setattr(sizing_mod, "Path", _patched)

    target.write_text("max 100000")
    assert sizing_mod._cgroup_cpu_quota() is None
    target.write_text("400000 100000")
    assert sizing_mod._cgroup_cpu_quota() == 4
    target.write_text("350000 100000")  # 3.5 CPUs -> 4 cores' worth of parallelism
    assert sizing_mod._cgroup_cpu_quota() == 4
    target.write_text("50000 100000")  # half a CPU still needs one thread
    assert sizing_mod._cgroup_cpu_quota() == 1
    target.write_text("garbage")
    assert sizing_mod._cgroup_cpu_quota() is None


def test_cpu_list_parsing() -> None:
    """Kernel CPU lists come in both comma and range forms, sometimes mixed."""
    assert sizing_mod._parse_cpu_list("0,4") == {0, 4}
    assert sizing_mod._parse_cpu_list("0-3") == {0, 1, 2, 3}
    assert sizing_mod._parse_cpu_list("0-1,8-9") == {0, 1, 8, 9}
    assert sizing_mod._parse_cpu_list("") == set()


def test_detection_falls_back_to_logical_when_topology_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stripped /sys and /proc must still derive something runnable.

    The fallback deliberately OVER-estimates (logical, not logical/2): the intra-op cap
    bounds peak RSS whatever the input, so the cost lands on the concurrency knob only,
    whereas halving a genuinely non-SMT box would be a permanent throughput haircut.
    """
    monkeypatch.setattr(sizing_mod, "_schedulable_cpus", lambda: set(range(8)))
    monkeypatch.setattr(sizing_mod, "_cgroup_cpu_quota", lambda: None)
    monkeypatch.setattr(sizing_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(sizing_mod, "_physical_cores_from_sysfs", lambda _cpus: None)
    monkeypatch.setattr(sizing_mod, "_physical_cores_from_proc_cpuinfo", lambda _cpus: None)
    cores, source = detect_physical_cores()
    assert cores == 8
    assert source == "logical:sched_getaffinity"
    # ...and the memory-safety property survives the over-estimate:
    assert derive_sizing(cores).intra_op_threads == 4


def test_proc_cpuinfo_is_the_second_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """`(physical id, core id)` pairs, counted only over schedulable processors."""
    cpuinfo = "\n\n".join(f"processor\t: {cpu}\nphysical id\t: 0\ncore id\t: {cpu % 4}\nmodel name\t: x" for cpu in range(8))
    monkeypatch.setattr(sizing_mod.Path, "read_text", lambda _self, **_kwargs: cpuinfo)
    assert sizing_mod._physical_cores_from_proc_cpuinfo(set(range(8))) == 4
    assert sizing_mod._physical_cores_from_proc_cpuinfo({0, 1, 4, 5}) == 2


def test_physical_cores_env_overrides_the_whole_derivation(monkeypatch: pytest.MonkeyPatch) -> None:
    """One override moves BOTH knobs, keeping them in the relationship they must hold."""
    monkeypatch.setenv(PHYSICAL_CORES_ENV, "32")
    cores, source = detect_physical_cores()
    assert (cores, source) == (32, f"env:{PHYSICAL_CORES_ENV}")
    result = derive_sizing()
    assert (result.intra_op_threads, result.concurrency) == (4, 8)


def test_malformed_physical_cores_env_falls_back_to_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in a deployment env var must not brick the analyze path."""
    for bogus in ("not-a-number", "0", "-4"):
        monkeypatch.setenv(PHYSICAL_CORES_ENV, bogus)
        cores, source = detect_physical_cores()
        assert cores >= 1
        assert not source.startswith("env:")


# ---------------------------------------------------------------------------
# Applying it to the process environment
# ---------------------------------------------------------------------------


def test_apply_thread_env_stamps_all_three_variables() -> None:
    """TF intra-op, TF inter-op and OpenMP are all covered -- `phaze-7i0k` set all three."""
    env: dict[str, str] = {}
    result = apply_thread_env(env)
    assert env == {
        INTRA_OP_ENV: str(result.intra_op_threads),
        INTER_OP_ENV: "1",
        OMP_ENV: str(result.omp_threads),
    }


def test_apply_thread_env_never_overwrites_an_operator_value() -> None:
    """The derivation is a DEFAULT. A ConfigMap that sets a value keeps exactly that value."""
    env = {INTRA_OP_ENV: "2", INTER_OP_ENV: "3", OMP_ENV: "7"}
    apply_thread_env(env)
    assert env == {INTRA_OP_ENV: "2", INTER_OP_ENV: "3", OMP_ENV: "7"}


def test_apply_thread_env_fills_each_variable_independently() -> None:
    """Setting one variable must not silently discard the derivation of the other two."""
    env = {OMP_ENV: "1"}
    result = apply_thread_env(env)
    assert env[OMP_ENV] == "1"
    assert env[INTRA_OP_ENV] == str(result.intra_op_threads)
    assert env[INTER_OP_ENV] == "1"


def test_apply_thread_env_treats_a_blank_value_as_unset() -> None:
    """`FOO=` in a ConfigMap is not an operator choice of "zero threads"."""
    env = {INTRA_OP_ENV: "   "}
    result = apply_thread_env(env)
    assert env[INTRA_OP_ENV] == str(result.intra_op_threads)


def test_apply_thread_env_defaults_to_the_real_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production call site passes nothing and must stamp `os.environ` itself."""
    monkeypatch.delenv(INTRA_OP_ENV, raising=False)
    result = apply_thread_env()
    assert os.environ[INTRA_OP_ENV] == str(result.intra_op_threads)


# ---------------------------------------------------------------------------
# The one concurrency phaze picks without operator action
# ---------------------------------------------------------------------------


def test_zero_config_local_backend_cap_comes_from_the_same_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-03's implicit all-local registry derives its `cap`, so it cannot drift from the cap.

    This is the "fresh host is correct without operator action" half: the same
    ``derive_sizing`` call that sizes the thread pool sizes the lane, so an operator who
    never writes a `backends.toml` still gets 8 lanes on a 32-core box and 1 on vox.
    """
    monkeypatch.setenv(PHYSICAL_CORES_ENV, "32")
    (backend,) = _default_local_registry()
    assert backend.cap == derive_sizing().concurrency == 8

    monkeypatch.setenv(PHYSICAL_CORES_ENV, "4")
    (backend,) = _default_local_registry()
    assert backend.cap == 1  # reproduces the previous hardcoded value on the measured node
