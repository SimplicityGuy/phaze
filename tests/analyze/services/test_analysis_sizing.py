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
import pathlib
import subprocess
from typing import TYPE_CHECKING, Any

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
    from collections.abc import Mapping
    from pathlib import Path


# ---------------------------------------------------------------------------
# Seams
#
# Every detection source in the cascade is platform-specific, so ONE test host can only ever
# execute one of them for real: sysfs topology and `/proc/cpuinfo` exist only on Linux,
# `sysctl -n hw.physicalcpu_max` only on Darwin. The remaining branches are not dead -- each
# one is the live path on some production host -- so they are reached here through file-tree
# and `subprocess` seams that let a single machine drive all of them, on either platform.
# ---------------------------------------------------------------------------


def _fake_fs(monkeypatch: pytest.MonkeyPatch, files: Mapping[str, str | OSError]) -> None:
    """Redirect the module's absolute reads of `/sys` + `/proc` at an in-test mapping.

    `analysis_sizing` reaches the kernel only through ``Path(<absolute literal>).read_text``,
    so swapping the module's `Path` name is the whole seam. A path absent from ``files``
    raises `FileNotFoundError`, which is exactly what a stripped container `/sys` does.
    """
    real_path = pathlib.Path

    class _FakeFile:
        def __init__(self, path: str) -> None:
            self._path = path

        def __truediv__(self, other: str) -> _FakeFile:
            return _FakeFile(f"{self._path}/{other}")

        def read_text(self, **_kwargs: Any) -> str:
            content = files.get(self._path)
            if content is None:
                raise FileNotFoundError(self._path)
            if isinstance(content, OSError):
                raise content
            return content

    def _shim(path: str) -> Any:
        text = str(path)
        return _FakeFile(text) if text.startswith(("/sys/", "/proc/")) else real_path(text)

    monkeypatch.setattr(sizing_mod, "Path", _shim)


def _siblings_tree(logical: int, physical: int, *, name: str = "thread_siblings_list") -> dict[str, str]:
    """A sysfs topology tree for ``logical`` CPUs over ``physical`` cores (SMT siblings paired)."""
    return {
        f"/sys/devices/system/cpu/cpu{cpu}/topology/{name}": ",".join(str(sib) for sib in range(cpu % physical, logical, physical))
        for cpu in range(logical)
    }


def _fake_sysctl(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", returncode: int = 0, raises: BaseException | None = None) -> list[list[str]]:
    """Stand in for the Darwin `sysctl(8)` call, recording the argv it was handed."""
    calls: list[list[str]] = []

    def _run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args=argv, returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(sizing_mod.subprocess, "run", _run)
    return calls


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


def _documented_worked_examples() -> list[tuple[str, int, int, int, int]]:
    """The module docstring's "Worked examples of the same one policy" table, parsed.

    Read out of :data:`sizing_mod.__doc__` rather than transcribed, so the assertion is
    against *what the module currently documents*. A hand-copied table would let the two
    drift the moment somebody edits one of them -- and a wrong derivation is silent.
    """
    rows: list[tuple[str, int, int, int, int]] = []
    for line in (sizing_mod.__doc__ or "").splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 5:  # not the worked-examples table
            continue
        try:
            physical, intra_op, concurrency, product = (int(cell) for cell in cells[1:])
        except ValueError:  # the header and the alignment rule
            continue
        rows.append((cells[0], physical, intra_op, concurrency, product))
    return rows


_DOCUMENTED_EXAMPLES = _documented_worked_examples()


def test_the_documented_worked_examples_table_is_still_findable() -> None:
    """Guard the guard: a renamed/reformatted table must fail loudly, not silently pass 0 rows.

    Without this, deleting the docstring table would turn the drift test below into an empty
    parametrization -- a green run asserting nothing at all.
    """
    assert len(_DOCUMENTED_EXAMPLES) == 4
    assert {row[1] for row in _DOCUMENTED_EXAMPLES} == {2, 4, 8, 32}


@pytest.mark.parametrize(("host", "physical", "intra_op", "concurrency", "product"), _DOCUMENTED_EXAMPLES)
def test_code_agrees_with_every_documented_worked_example(host: str, physical: int, intra_op: int, concurrency: int, product: int) -> None:
    """`docs table` == `derive_sizing`, row for row: vox 4->4/1, 8->4/2, 32->4/8, 2->2/1.

    This is the acceptance the bead cares about more than the coverage number. The module's
    whole promise is that a host nobody has provisioned yet gets a correct default **without
    operator action**; the documented policy is the specification of that default, so nothing
    but an executable assertion stops the prose and the code parting ways unnoticed.
    """
    result = derive_sizing(physical, source=f"docs:{host}")
    assert result.physical_cores == physical
    assert result.intra_op_threads == intra_op
    assert result.concurrency == concurrency
    # The `product` column is the policy itself: intra_op x concurrency ~= physical_cores.
    assert result.intra_op_threads * result.concurrency == product
    assert product == physical  # every documented example is an exact fit, by construction
    assert result.omp_threads == intra_op
    assert result.inter_op_threads == 1


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
# Detection source 0: the schedulable set itself
# ---------------------------------------------------------------------------


def test_schedulable_cpus_prefers_the_affinity_mask(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sched_getaffinity` is the honest answer under `taskset` / a cpuset cgroup.

    `os.cpu_count()` reports the MACHINE and would ignore all three restrictions, so it must
    lose to the mask wherever the mask exists.
    """
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: {0, 1, 4, 5}, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    assert sizing_mod._schedulable_cpus() == {0, 1, 4, 5}


def test_schedulable_cpus_falls_back_when_the_call_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `OSError` out of `sched_getaffinity` degrades to the machine count, not to a crash."""

    def _boom(_pid: int) -> set[int]:
        raise OSError("EPERM")

    monkeypatch.setattr(os, "sched_getaffinity", _boom, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 6)
    assert sizing_mod._schedulable_cpus() == set(range(6))


def test_schedulable_cpus_falls_back_where_the_symbol_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Darwin path: `os.sched_getaffinity` does not exist, so the machine count is used.

    This is why the module reaches it via `getattr` rather than naming it directly.
    """
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 10)
    assert sizing_mod._schedulable_cpus() == set(range(10))


def test_schedulable_cpus_never_returns_the_empty_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """`os.cpu_count()` is documented as possibly `None`; the derivation must stay runnable."""
    monkeypatch.delattr(os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert sizing_mod._schedulable_cpus() == {0}


# ---------------------------------------------------------------------------
# Detection source 1: sysfs topology (Linux)
# ---------------------------------------------------------------------------


def test_sysfs_counts_distinct_sibling_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """8 logical CPUs over 4 SMT pairs is 4 physical cores, not 8."""
    _fake_fs(monkeypatch, _siblings_tree(logical=8, physical=4))
    assert sizing_mod._physical_cores_from_sysfs(set(range(8))) == 4


def test_sysfs_tracks_a_restricted_affinity_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two whole sibling pairs out of four cores is 2 physical cores -- the `taskset` case."""
    _fake_fs(monkeypatch, _siblings_tree(logical=8, physical=4))
    assert sizing_mod._physical_cores_from_sysfs({0, 1, 4, 5}) == 2


def test_sysfs_keys_on_the_unintersected_sibling_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """One schedulable sibling of a core still IS one physical core; two collapse to one.

    Both cases fall out of keying on the kernel's FULL sibling set, which is why the module
    deliberately does not intersect it with the schedulable set. Asserting the two together
    is what pins that choice: `{0, 1}` (one sibling each of two cores) and `{0, 4}` (both
    siblings of one core) must not produce the same answer.
    """
    _fake_fs(monkeypatch, _siblings_tree(logical=8, physical=4))
    assert sizing_mod._physical_cores_from_sysfs({0, 1}) == 2
    assert sizing_mod._physical_cores_from_sysfs({0, 4}) == 1


def test_sysfs_falls_back_to_core_cpus_list_on_newer_kernels(monkeypatch: pytest.MonkeyPatch) -> None:
    """`thread_siblings_list` was renamed `core_cpus_list`; either name must work alone."""
    _fake_fs(monkeypatch, _siblings_tree(logical=4, physical=2, name="core_cpus_list"))
    assert sizing_mod._physical_cores_from_sysfs(set(range(4))) == 2


def test_sysfs_returns_none_when_topology_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stripped container `/sys` yields nothing at all -- the caller must fall through."""
    _fake_fs(monkeypatch, {})
    assert sizing_mod._physical_cores_from_sysfs({0, 1}) is None


def test_sysfs_refuses_to_half_guess_a_partially_readable_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    """One unreadable CPU invalidates the whole count, rather than under-reporting cores.

    Counting the readable CPUs only would silently derive 1 physical core on a 4-core box and
    permanently halve throughput -- the failure mode the module's "do not half-guess" comment
    names.
    """
    tree = _siblings_tree(logical=4, physical=2)
    del tree["/sys/devices/system/cpu/cpu3/topology/thread_siblings_list"]
    _fake_fs(monkeypatch, tree)
    assert sizing_mod._physical_cores_from_sysfs(set(range(4))) is None


def test_sysfs_treats_an_unparseable_sibling_list_as_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kernel emitting something this parser does not understand is not a 0-core host."""
    _fake_fs(
        monkeypatch,
        {
            "/sys/devices/system/cpu/cpu0/topology/thread_siblings_list": "not-a-cpu-list",
            "/sys/devices/system/cpu/cpu0/topology/core_cpus_list": "",
        },
    )
    assert sizing_mod._physical_cores_from_sysfs({0}) is None


def test_sysfs_returns_none_for_an_empty_schedulable_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CPUs in, no count out -- `0` must never be reported as a core count."""
    _fake_fs(monkeypatch, _siblings_tree(logical=4, physical=2))
    assert sizing_mod._physical_cores_from_sysfs(set()) is None


def test_sysfs_survives_a_permission_error_not_just_a_missing_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hardened `/sys` mount raises `PermissionError`, which is an `OSError` all the same."""
    _fake_fs(monkeypatch, {"/sys/devices/system/cpu/cpu0/topology/thread_siblings_list": PermissionError("EACCES")})
    assert sizing_mod._physical_cores_from_sysfs({0}) is None


@pytest.mark.parametrize("raw", ["0-x", "0-3,bogus", "1,2,three", "0-"])
def test_malformed_cpu_lists_parse_to_nothing(raw: str) -> None:
    """Partial garbage yields the EMPTY set, never a partially-parsed count.

    Returning `{0, 1, 2, 3}` for `"0-3,bogus"` would be a plausible-looking wrong answer that
    then silently sizes the thread pool; the empty set routes the caller to the next source.
    """
    assert sizing_mod._parse_cpu_list(raw) == set()


def test_cpu_list_parsing_tolerates_whitespace_and_empty_chunks() -> None:
    """Kernel files are newline/space terminated and occasionally emit an empty chunk."""
    assert sizing_mod._parse_cpu_list(" 0 , 4 ") == {0, 4}
    assert sizing_mod._parse_cpu_list("0,,4") == {0, 4}


# ---------------------------------------------------------------------------
# Detection source 2: /proc/cpuinfo (Linux fallback)
# ---------------------------------------------------------------------------


def _cpuinfo(*blocks: str) -> str:
    return "\n\n".join(blocks)


def test_proc_cpuinfo_returns_none_when_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `/proc/cpuinfo` (a stripped container, or not Linux) falls through to the next source."""
    _fake_fs(monkeypatch, {})
    assert sizing_mod._physical_cores_from_proc_cpuinfo({0, 1}) is None


def test_proc_cpuinfo_returns_none_without_topology_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Many ARM kernels emit neither `physical id` nor `core id` -- x86-specific, by design.

    Returning `None` (rather than counting `processor` lines) is what keeps the LOGICAL count
    attributable to the last-resort source instead of being mislabelled `proc:cpuinfo`.
    """
    _fake_fs(monkeypatch, {"/proc/cpuinfo": _cpuinfo("processor\t: 0\nBogoMIPS\t: 108.00", "processor\t: 1\nBogoMIPS\t: 108.00")})
    assert sizing_mod._physical_cores_from_proc_cpuinfo({0, 1}) is None


def test_proc_cpuinfo_returns_none_on_a_partially_annotated_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """A schedulable processor missing `core id` invalidates the count, same as sysfs."""
    _fake_fs(
        monkeypatch,
        {"/proc/cpuinfo": _cpuinfo("processor\t: 0\nphysical id\t: 0\ncore id\t: 0", "processor\t: 1\nphysical id\t: 0")},
    )
    assert sizing_mod._physical_cores_from_proc_cpuinfo({0, 1}) is None


def test_proc_cpuinfo_skips_a_block_with_an_unparseable_processor_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-integer `processor` value cannot be matched against the affinity set, so it is skipped.

    It must not be counted (it might not be schedulable) and must not raise.
    """
    _fake_fs(
        monkeypatch,
        {
            "/proc/cpuinfo": _cpuinfo(
                "processor\t: 0\nphysical id\t: 0\ncore id\t: 0",
                "processor\t: not-a-number\nphysical id\t: 9\ncore id\t: 9",
            )
        },
    )
    assert sizing_mod._physical_cores_from_proc_cpuinfo({0, 1}) == 1


def test_proc_cpuinfo_ignores_unschedulable_processors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocks outside the affinity mask are skipped even when fully annotated."""
    blocks = [f"processor\t: {cpu}\nphysical id\t: 0\ncore id\t: {cpu % 4}" for cpu in range(8)]
    _fake_fs(monkeypatch, {"/proc/cpuinfo": _cpuinfo(*blocks)})
    assert sizing_mod._physical_cores_from_proc_cpuinfo(set(range(8))) == 4
    assert sizing_mod._physical_cores_from_proc_cpuinfo({0, 1, 4, 5}) == 2


def test_proc_cpuinfo_counts_sockets_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """`core id` repeats per socket, so the pair -- not `core id` alone -- is the key."""
    blocks = [f"processor\t: {cpu}\nphysical id\t: {cpu // 2}\ncore id\t: {cpu % 2}" for cpu in range(4)]
    _fake_fs(monkeypatch, {"/proc/cpuinfo": _cpuinfo(*blocks)})
    assert sizing_mod._physical_cores_from_proc_cpuinfo(set(range(4))) == 4


def test_proc_cpuinfo_returns_none_for_an_empty_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A readable but empty `/proc/cpuinfo` is not a 0-core host."""
    _fake_fs(monkeypatch, {"/proc/cpuinfo": ""})
    assert sizing_mod._physical_cores_from_proc_cpuinfo({0}) is None


# ---------------------------------------------------------------------------
# Detection source 3: Darwin sysctl (the dev-machine path)
# ---------------------------------------------------------------------------


def test_darwin_reads_hw_physicalcpu_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """The happy path, and the argv it uses: absolute path, fixed args, no shell (B603)."""
    calls = _fake_sysctl(monkeypatch, stdout="10\n")
    assert sizing_mod._physical_cores_from_darwin() == 10
    assert calls == [["/usr/sbin/sysctl", "-n", "hw.physicalcpu_max"]]


def test_darwin_returns_none_when_sysctl_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """`check=False`, so a non-zero exit surfaces as empty stdout rather than an exception."""
    _fake_sysctl(monkeypatch, stdout="", returncode=1)
    assert sizing_mod._physical_cores_from_darwin() is None


def test_darwin_returns_none_on_unparseable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """An OID that exists but is not an integer must fall through, not raise."""
    _fake_sysctl(monkeypatch, stdout="hw.physicalcpu_max: unknown oid\n")
    assert sizing_mod._physical_cores_from_darwin() is None


def test_darwin_treats_zero_as_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """`0` cores is not a host; it is a bad read, and must route to the next source."""
    _fake_sysctl(monkeypatch, stdout="0\n")
    assert sizing_mod._physical_cores_from_darwin() is None


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("/usr/sbin/sysctl"),  # the binary is absent (Linux, or a slim image)
        PermissionError("EACCES"),  # exec denied by a sandbox / seccomp profile
        subprocess.TimeoutExpired(cmd=["/usr/sbin/sysctl"], timeout=5),  # the timeout=5 fired
        subprocess.SubprocessError("spawn failed"),
    ],
    ids=["missing-binary", "exec-denied", "timeout", "spawn-failure"],
)
def test_darwin_swallows_every_subprocess_failure(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    """`except (OSError, subprocess.SubprocessError)`: sizing must never fail the analyze path.

    A missing `sysctl` is the ordinary case on Linux, where this function is called only if
    `platform.system()` lied -- so a raise here would be a crash on the production platform.
    """
    _fake_sysctl(monkeypatch, raises=error)
    assert sizing_mod._physical_cores_from_darwin() is None


# ---------------------------------------------------------------------------
# The cascade: which source wins, and what happens when it does not answer
# ---------------------------------------------------------------------------


def _stub_cascade(
    monkeypatch: pytest.MonkeyPatch,
    *,
    system: str = "Linux",
    schedulable: set[int] | None = None,
    quota: int | None = None,
    sysfs: int | None = None,
    cpuinfo: int | None = None,
    darwin: int | None = None,
) -> None:
    monkeypatch.setattr(sizing_mod.platform, "system", lambda: system)
    monkeypatch.setattr(sizing_mod, "_schedulable_cpus", lambda: set(range(8)) if schedulable is None else schedulable)
    monkeypatch.setattr(sizing_mod, "_cgroup_cpu_quota", lambda: quota)
    monkeypatch.setattr(sizing_mod, "_physical_cores_from_sysfs", lambda _cpus: sysfs)
    monkeypatch.setattr(sizing_mod, "_physical_cores_from_proc_cpuinfo", lambda _cpus: cpuinfo)
    monkeypatch.setattr(sizing_mod, "_physical_cores_from_darwin", lambda: darwin)


def test_cascade_falls_from_sysfs_to_proc_cpuinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordering: sysfs first, `/proc/cpuinfo` only once sysfs declines to answer.

    The source name is carried into the log line and any bug report, so it must name the
    source that actually produced the number.
    """
    _stub_cascade(monkeypatch, sysfs=None, cpuinfo=4)
    assert detect_physical_cores() == (4, "proc:cpuinfo")


def test_cascade_prefers_sysfs_over_proc_cpuinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both Linux sources answer, the topology files win -- they track `taskset` exactly."""
    _stub_cascade(monkeypatch, sysfs=4, cpuinfo=2)
    assert detect_physical_cores() == (4, "sysfs:thread_siblings_list")


def test_cascade_uses_sysctl_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Darwin never consults `/sys` or `/proc` -- neither exists, and both would mis-answer."""
    _stub_cascade(monkeypatch, system="Darwin", darwin=4, sysfs=99, cpuinfo=99)
    assert detect_physical_cores() == (4, "darwin:hw.physicalcpu_max")


def test_cascade_falls_from_darwin_to_the_logical_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed `sysctl` on Darwin lands on the same last resort as a stripped Linux `/sys`."""
    _stub_cascade(monkeypatch, system="Darwin", darwin=None, schedulable=set(range(6)))
    assert detect_physical_cores() == (6, "logical:sched_getaffinity")


def test_detection_never_claims_more_cores_than_are_schedulable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`sysctl` reports the MACHINE, so a restricted process must still be clamped to its slice.

    `hw.physicalcpu_max` has no notion of the affinity mask; without the clamp a `taskset`-ed
    process on a 10-core Mac would derive 10 and oversubscribe its 2 CPUs by 5x.
    """
    _stub_cascade(monkeypatch, system="Darwin", darwin=10, schedulable={0, 1})
    assert detect_physical_cores() == (2, "darwin:hw.physicalcpu_max")


def test_detection_floors_at_one_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every source declining, on a host reporting nothing schedulable, still derives 1."""
    _stub_cascade(monkeypatch, schedulable=set())
    cores, _source = detect_physical_cores()
    assert cores == 1


def test_a_quota_of_one_clamps_all_the_way_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """`cpu.max = "100000 100000"` on a 32-core box is a 1-core process. Both knobs follow."""
    _stub_cascade(monkeypatch, schedulable=set(range(64)), quota=1, sysfs=32)
    cores, source = detect_physical_cores()
    assert (cores, source) == (1, "sysfs:thread_siblings_list+cgroup:cpu.max")
    assert derive_sizing(cores).intra_op_threads == 1


# ---------------------------------------------------------------------------
# The cgroup v2 bandwidth clamp
# ---------------------------------------------------------------------------


def test_cgroup_quota_is_none_without_cgroup_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `cpu.max` at all (cgroup v1, or not Linux) means no bandwidth clamp."""
    _fake_fs(monkeypatch, {})
    assert sizing_mod._cgroup_cpu_quota() is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("max 100000", None),  # the unlimited default -- no clamp
        ("400000 100000", 4),  # a whole 4 CPUs
        ("350000 100000", 4),  # 3.5 CPUs rounds UP: throttled parallelism, not 3 cores
        ("50000 100000", 1),  # half a CPU still needs one thread
        ("100000 100000\n", 1),  # trailing newline, as the kernel actually writes it
        ("garbage", None),  # wrong arity
        ("", None),  # empty file
        ("100000 100000 100000", None),  # wrong arity the other way
        ("abc def", None),  # right arity, unparseable numbers
        ("0 100000", None),  # a zero quota is not "no CPUs"; it is a bad read
        ("-1 100000", None),  # negative quota
        ("100000 0", None),  # a zero period would divide by zero
    ],
    ids=[
        "unlimited",
        "whole",
        "fractional-rounds-up",
        "sub-one-cpu",
        "trailing-newline",
        "malformed-arity",
        "empty",
        "too-many-fields",
        "unparseable-ints",
        "zero-quota",
        "negative-quota",
        "zero-period",
    ],
)
def test_cgroup_cpu_max_shapes(monkeypatch: pytest.MonkeyPatch, raw: str, expected: int | None) -> None:
    """Every `cpu.max` shape a container runtime can produce. Malformed always means "no clamp".

    Falling back to `None` rather than to some parsed remnant matters: a wrong clamp is a
    permanent, silent throughput cap, whereas no clamp leaves the affinity-set answer intact.
    """
    _fake_fs(monkeypatch, {"/sys/fs/cgroup/cpu.max": raw})
    assert sizing_mod._cgroup_cpu_quota() == expected


def test_cgroup_quota_read_survives_a_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cgroup namespace that denies the read is "no clamp", not a crash."""
    _fake_fs(monkeypatch, {"/sys/fs/cgroup/cpu.max": PermissionError("EACCES")})
    assert sizing_mod._cgroup_cpu_quota() is None


# ---------------------------------------------------------------------------
# The operator override
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "2", "32", " 32 ", "+8"])
def test_a_valid_override_short_circuits_every_detection_source(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """The escape hatch is checked FIRST, so a lying hypervisor cannot override the operator."""
    _stub_cascade(monkeypatch, sysfs=4, quota=1)
    monkeypatch.setenv(PHYSICAL_CORES_ENV, raw)
    cores, source = detect_physical_cores()
    assert cores == int(raw)
    assert source == f"env:{PHYSICAL_CORES_ENV}"


@pytest.mark.parametrize("raw", ["", "   ", "\n"])
def test_a_blank_override_is_not_an_override(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """`PHAZE_ANALYSIS_PHYSICAL_CORES=` in a ConfigMap is an unset variable, not "zero cores"."""
    _stub_cascade(monkeypatch, sysfs=4)
    monkeypatch.setenv(PHYSICAL_CORES_ENV, raw)
    assert detect_physical_cores() == (4, "sysfs:thread_siblings_list")


@pytest.mark.parametrize("raw", ["not-a-number", "0", "-4", "4.5", "four"])
def test_a_malformed_override_warns_and_falls_back(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str) -> None:
    """A typo in a deployment env var must not brick analyze -- but must not pass silently either.

    The warning is the whole reason the fallback is safe: a mis-typed override that silently
    reverted to detection would look exactly like an override that worked.
    """
    _stub_cascade(monkeypatch, sysfs=4)
    monkeypatch.setenv(PHYSICAL_CORES_ENV, raw)
    with caplog.at_level("WARNING", logger=sizing_mod.log.name):
        cores, source = detect_physical_cores()
    assert (cores, source) == (4, "sysfs:thread_siblings_list")
    assert PHYSICAL_CORES_ENV in caplog.text


def test_the_override_is_the_documented_single_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """One override moves BOTH knobs together -- the property that keeps them from drifting."""
    _stub_cascade(monkeypatch, sysfs=4)
    monkeypatch.setenv(PHYSICAL_CORES_ENV, "32")
    assert derive_sizing() == AnalysisSizing(
        physical_cores=32,
        intra_op_threads=4,
        inter_op_threads=1,
        omp_threads=4,
        concurrency=8,
        source=f"env:{PHYSICAL_CORES_ENV}",
    )


# ---------------------------------------------------------------------------
# End to end: a fresh host with nothing configured
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "logical", "physical", "intra_op", "concurrency"),
    [
        ("vox (Xeon E3-1271 v3)", 8, 4, 4, 1),
        ("8-physical-core box", 16, 8, 4, 2),
        ("32-physical-core box", 64, 32, 4, 8),
        ("2-physical-core VM", 4, 2, 2, 1),
    ],
)
def test_a_fresh_linux_host_derives_correctly_from_real_sysfs_text(
    monkeypatch: pytest.MonkeyPatch, host: str, logical: int, physical: int, intra_op: int, concurrency: int
) -> None:
    """The documented table again, this time driven end to end from kernel topology TEXT.

    `derive_sizing()` is called with no arguments and nothing in the environment, exactly as
    `services/analysis.py` calls it at import: the only input is the sysfs sibling map. This
    is the "a new host is correct without operator action" claim, executed.
    """
    monkeypatch.setattr(sizing_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(logical)), raising=False)
    _fake_fs(monkeypatch, _siblings_tree(logical=logical, physical=physical))

    result = derive_sizing()
    assert (result.physical_cores, result.intra_op_threads, result.concurrency) == (physical, intra_op, concurrency)
    assert result.source == "sysfs:thread_siblings_list"


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
