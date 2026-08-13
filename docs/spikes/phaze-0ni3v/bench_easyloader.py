"""Time ONE EasyLoader read and print it, in a fresh process.

Deliberately minimal: `run_bench.sh` invokes this once per data point so that no measurement
inherits another's allocator or page-cache state, which is the process model phaze-b2qs9 section 1
established and phaze-han03 reused.

Usage: bench_easyloader.py <file> <sampleRate> <startTime> <endTime>
Prints: "<seconds> <n_samples>"
"""

import sys
import time
from typing import Any


def main() -> int:
    if len(sys.argv) != 5:
        print(__doc__)
        return 2

    from essentia.standard import EasyLoader  # noqa: PLC0415 - keep import cost out of the timer

    filename = sys.argv[1]
    sample_rate = int(sys.argv[2])
    start_time = float(sys.argv[3])
    end_time = float(sys.argv[4])

    loader: Any = EasyLoader(filename=filename, sampleRate=sample_rate, startTime=start_time, endTime=end_time)
    started = time.perf_counter()
    audio = loader()
    elapsed = time.perf_counter() - started

    print(f"{elapsed:.4f} {len(audio)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
