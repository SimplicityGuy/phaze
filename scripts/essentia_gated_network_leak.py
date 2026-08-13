"""Standalone reproducer for the essentia streaming-network retention behind phaze-u1n7j (D-09).

**This file imports nothing from phaze, on purpose.** It is the artifact an upstream bug report
needs (phaze-09skl), so it must be runnable by someone who has essentia and no phaze: stdlib,
numpy and essentia only, with synthetic audio it writes itself. Nothing here reads the archive,
and no path outside a temporary directory is opened for write.

WHAT IT SHOWS. Building a *gated* streaming fan-out -- one ``Trimmer`` hung straight off a
``MonoLoader`` (the gate), then one ``Scale`` + ``Trimmer`` branch per window into a ``Pool`` --
and then dropping every Python reference to it does **not** release the C++ side. ``>>`` calls
into ``_essentia.connect`` / ``_essentia.poolConnect``, which allocate a connection buffer per
edge plus, for a Pool sink, an entire ``PoolStorage`` that Python never holds a reference to at
all. essentia's Python bindings expose no network object, so nothing walks it on teardown, and
``malloc_trim`` cannot recover the pages because they are still live-referenced rather than
merely un-returned to the OS. Peak RSS therefore climbs by roughly ``5 MiB x n_windows`` per
round, forever -- which in phaze was ~0.31 GiB per 30 minutes of audio analyzed and an
OOMKilled pod on every file past ~3 hours (measured: phaze-b2qs9).

Two facts this probe is built to expose, because both are easy to guess wrong:

* The retention is **per BRANCH, not per sample**: doubling ``--windows`` doubles it, doubling
  ``--window-sec`` does not move it. Its near-match to the size of the live PCM is a
  coincidence, and reading it as "the decoded audio is retained" sends the investigation at the
  buffers instead of at the edges.
* It is the **gate** that does it. ``--teardown drop --no-gate`` is flat; ``--teardown drop``
  with the gate leaks. A gate span under ~100 s retains nothing either, which is why the
  default geometry here is the production one (60 windows x 30 s) rather than something quicker.

``--teardown disconnect`` applies the phaze fix -- sever every edge with
``_StreamConnector.disconnect``, then ``gc.collect()`` because every streaming algorithm is born
into a reference cycle (``self.connections`` is keyed by a connector holding ``self``), so
refcounting alone never destroys the proxy that owns the C++ algorithm. Both halves are needed;
either alone leaves the curve linear.

Example, production geometry, Debian 13 / glibc 2.41 / essentia-tensorflow 2.1b6.dev1438::

    python scripts/essentia_gated_network_leak.py --rounds 6 --teardown drop
    python scripts/essentia_gated_network_leak.py --rounds 6 --teardown disconnect --assert-flat 150

The second form is the regression shape: it exits non-zero if the high-water grew by more than
``--assert-flat`` MiB across the rounds after the first.
"""

from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path
import resource
import sys
import tempfile
import time
from typing import Any

import essentia
import essentia.standard as ess_std
import essentia.streaming as ess
import numpy as np


# The margin phaze puts on the gate so the chunk's last window is complete. Immaterial to the
# retention; kept so this probe builds the network phaze builds, byte for byte.
GATE_MARGIN_SEC = 1.0
SINK_KEY_PREFIX = "w_"

# `ru_maxrss` is KiB on Linux and BYTES on macOS/BSD. Getting this wrong makes a 1000x error
# look like a result, so it is resolved once, explicitly.
_MAXRSS_TO_MIB = 1.0 / 1024.0 if sys.platform.startswith("linux") else 1.0 / (1024.0 * 1024.0)


def high_water_mib() -> float:
    """Process high-water RSS in MiB, from the kernel (never sampled, never in-process guessed)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _MAXRSS_TO_MIB


def current_rss_mib() -> float:
    """Instantaneous RSS in MiB where the platform exposes it cheaply, else 0.0."""
    try:
        with Path("/proc/self/status").open() as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def write_synthetic_audio(path: Path, duration_sec: float, rate: int) -> None:
    """Write a synthetic tone-plus-noise file. No corpus audio is ever needed to see the leak.

    The content is deliberately uninteresting: the retention is a property of the network's
    edges, not of the signal, so a swept sine with a little noise (enough that the encoder
    cannot collapse it to silence) reproduces it exactly as real music does.

    Built block-wise into one preallocated float32 buffer, and everything stays float32. That is
    not micro-optimisation: generating 31 minutes the obvious way costs ~1 GiB of float64
    temporaries, whose high-water then sits ABOVE the first round's and hides exactly the growth
    this probe exists to show. Pass ``--audio`` to skip the generation peak entirely.
    """
    n = int(duration_sec * rate)
    signal = np.empty(n, dtype=np.float32)
    rng = np.random.default_rng(20260813)
    block = rate * 60
    for lo in range(0, n, block):
        hi = min(lo + block, n)
        t = np.arange(lo, hi, dtype=np.float32) / np.float32(rate)
        freq = np.float32(220.0) + np.float32(40.0) * np.sin(np.float32(2.0 * np.pi / 30.0) * t)
        np.sin(np.float32(2.0 * np.pi) * freq * t, out=t)  # t is now the sweep, reused in place
        signal[lo:hi] = t * np.float32(0.4) + rng.standard_normal(hi - lo, dtype=np.float32) * np.float32(0.02)
    ess_std.MonoWriter(filename=str(path), sampleRate=rate, format="mp3", bitrate=192)(signal)


def run_round(audio: Path, rate: int, windows: list[tuple[int, float, float]], *, gated: bool, teardown: str) -> int:
    """Build phaze's chunk-decode network once, run it, tear it down the requested way.

    Returns the number of windows that produced audio. The build is a transcription of
    ``phaze.services.analysis._decode_windows_streaming`` with the phaze imports removed; the
    ``Scale(factor=1.0)`` interposer is load-bearing there (it gives each branch ``Trimmer`` a
    private parent to stop, so the earliest-ending window cannot shut the shared decode down)
    and is kept here so the shape under test is the shape that ships.
    """
    pool = essentia.Pool()
    loader = ess.MonoLoader(filename=str(audio), sampleRate=rate)
    branches: list[tuple[Any, Any]] = []
    gate: Any = None
    try:
        if not gated:
            source = loader.audio
        else:
            stop_at_sec = max(end for _idx, _start, end in windows)
            # No Scale interposer on the gate, deliberately: its parent MUST be the loader, so
            # that reaching endTime shuts the shared decode down. That is the whole point of the
            # gate -- and, measured, the whole source of the retention.
            gate = ess.Trimmer(sampleRate=rate, startTime=0.0, endTime=stop_at_sec + GATE_MARGIN_SEC)
            loader.audio >> gate.signal
            source = gate.signal
        for idx, start, end in windows:
            scale = ess.Scale(factor=1.0)
            trimmer = ess.Trimmer(sampleRate=rate, startTime=start, endTime=end)
            source >> scale.signal
            scale.signal >> trimmer.signal
            trimmer.signal >> (pool, f"{SINK_KEY_PREFIX}{idx}")
            branches.append((scale, trimmer))

        essentia.run(loader)

        produced = set(pool.descriptorNames())
        decoded = 0
        for idx, _start, _end in windows:
            key = f"{SINK_KEY_PREFIX}{idx}"
            if key not in produced:
                continue
            buf = pool[key]
            pool.remove(key)
            if len(buf) > 0:
                decoded += 1
        return decoded
    finally:
        if teardown == "disconnect":
            disconnect_network([*(algo for branch in branches for algo in branch), gate, loader])
        branches.clear()
        del loader, pool, gate


def disconnect_network(algos: list[Any]) -> None:
    """Sever every edge the algorithms hold -- the first half of phaze's fix.

    ``_StreamConnector.disconnect`` wraps ``_essentia.disconnect`` / ``poolDisconnect``, the only
    exposed calls that release the C++ side. Snapshot both levels first: ``disconnect`` mutates
    the list being iterated.
    """
    for algo in algos:
        connections = getattr(algo, "connections", None) or {}
        for connector, targets in list(connections.items()):
            for target in list(targets):
                connector.disconnect(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audio", type=Path, default=None, help="reuse an existing file instead of writing synthetic audio")
    parser.add_argument("--rounds", type=int, default=6, help="how many identical chunk decodes to run (default 6)")
    parser.add_argument("--windows", type=int, default=60, help="windows per round; phaze's fine chunk is 60 (default 60)")
    parser.add_argument("--window-sec", type=float, default=30.0, help="seconds per window; phaze's fine window is 30 (default 30)")
    parser.add_argument("--rate", type=int, default=44100, help="decode sample rate (default 44100)")
    parser.add_argument("--no-gate", action="store_true", help="build the fan-out WITHOUT the chunk gate — the control arm, which does not leak")
    parser.add_argument(
        "--teardown", choices=("drop", "disconnect"), default="drop", help="'drop' is the defective teardown; 'disconnect' is the fix"
    )
    parser.add_argument(
        "--assert-flat", type=float, default=None, metavar="MIB", help="exit non-zero if the high-water grows more than MIB after round 1"
    )
    parser.add_argument("--json", type=Path, default=None, help="also write the per-round rows here")
    args = parser.parse_args()

    windows = [(i, i * args.window_sec, (i + 1) * args.window_sec) for i in range(args.windows)]
    span = args.windows * args.window_sec
    out = sys.stdout

    with tempfile.TemporaryDirectory(prefix="essentia-leak-") as tmp:
        audio = args.audio
        if audio is None:
            audio = Path(tmp) / "synthetic.mp3"
            out.write(f"# writing {span + 60.0:.0f} s of synthetic audio at {args.rate} Hz ...\n")
            out.flush()
            write_synthetic_audio(audio, span + 60.0, args.rate)

        out.write(
            f"# essentia={essentia.__version__} gated={not args.no_gate} teardown={args.teardown} "
            f"rounds={args.rounds} windows={args.windows} window_sec={args.window_sec:g} rate={args.rate}\n"
        )
        out.write(f"{'round':>5} {'decoded':>7} {'rss_mib':>9} {'hwm_mib':>9} {'d_hwm':>8} {'sec':>7}\n")
        out.write(f"{'base':>5} {'-':>7} {current_rss_mib():9.1f} {high_water_mib():9.1f} {'-':>8} {'-':>7}\n")
        out.flush()

        rows: list[dict[str, float]] = []
        previous = high_water_mib()
        for r in range(args.rounds):
            t0 = time.monotonic()
            decoded = run_round(audio, args.rate, windows, gated=not args.no_gate, teardown=args.teardown)
            gc.collect()  # the second half of the fix; harmless (and, measured, useless) on its own
            elapsed = time.monotonic() - t0
            hwm = high_water_mib()
            rows.append(
                {
                    "round": r + 1,
                    "decoded": decoded,
                    "rss_mib": round(current_rss_mib(), 1),
                    "hwm_mib": round(hwm, 1),
                    "d_hwm_mib": round(hwm - previous, 1),
                    "sec": round(elapsed, 2),
                }
            )
            out.write(f"{r + 1:>5} {decoded:>7} {current_rss_mib():9.1f} {hwm:9.1f} {hwm - previous:8.1f} {elapsed:7.2f}\n")
            out.flush()
            previous = hwm

    # Both axes, and the verdict takes the WORSE of the two. The high-water is the number that
    # OOMKills a pod and so is the one that matters -- but it is monotone, so anything that
    # peaked before round 1 (self-generating the audio, most obviously) can sit above the early
    # rounds and hide their growth. Live RSS has no such blind spot and is noisier. A leak this
    # size (~5 MiB per branch per round) shows in both; reporting one alone is how a masked run
    # gets read as a flat one.
    hwm_growth = rows[-1]["hwm_mib"] - rows[0]["hwm_mib"] if rows else 0.0
    rss_growth = rows[-1]["rss_mib"] - rows[0]["rss_mib"] if rows else 0.0
    growth = max(hwm_growth, rss_growth)
    per_branch_kib = (growth * 1024.0) / (args.windows * max(len(rows) - 1, 1)) if rows else 0.0
    out.write(
        f"# growth after round 1: hwm {hwm_growth:+.1f} MiB, rss {rss_growth:+.1f} MiB "
        f"-> {growth:.1f} MiB ({per_branch_kib:.1f} KiB per window branch per round)\n"
    )

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {
                    "args": {k: str(v) for k, v in vars(args).items()},
                    "rows": rows,
                    "hwm_growth_mib": hwm_growth,
                    "rss_growth_mib": rss_growth,
                    "growth_mib": growth,
                },
                indent=2,
            )
        )

    if args.assert_flat is not None and growth > args.assert_flat:
        out.write(f"# FAIL: retention grew {growth:.1f} MiB, budget {args.assert_flat:.1f} MiB\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
