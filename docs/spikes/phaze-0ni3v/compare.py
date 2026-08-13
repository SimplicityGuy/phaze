"""Diff two verify_easyloader.py dumps and classify every row.

BIT-IDENTICAL is the verdict that matters -- it means the patched build returned the same bytes
the unpatched one did. Anything else is reported with its magnitude rather than a pass/fail, so
the known-and-explained residuals (AAC's Perceptual Noise Substitution, one Opus ULP) are visible
as what they are instead of being hidden behind a tolerance.

Usage: compare.py <pre-patch.npz> <post-patch.npz>
"""

import sys

import numpy as np


def classify(x: np.ndarray, y: np.ndarray) -> tuple[str, str, str, str]:
    if x.dtype.kind in "US" or y.dtype.kind in "US":
        same = x.dtype == y.dtype and list(x) == list(y)
        return ("exception (same)" if same else "EXCEPTION-DIFF", str(x[0])[:70], str(y[0])[:70], "")
    if len(x) != len(y):
        return ("LENGTH-DIFF", str(len(x)), str(len(y)), f"delta={len(y) - len(x)}")
    if len(x) == 0:
        return ("empty (both)", "0", "0", "")

    diff = np.abs(x.astype(np.float64) - y.astype(np.float64))
    worst = float(diff.max())
    if worst == 0.0:
        return ("BIT-IDENTICAL", str(len(x)), "", "")

    rms = float(np.sqrt(np.mean(diff**2)))
    ref = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    db = 20 * np.log10(rms / ref) if ref > 0 else float("nan")
    return ("differs", str(len(x)), f"max={worst:.3e}", f"relRMS={db:.1f} dB")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    a = np.load(sys.argv[1], allow_pickle=False)
    b = np.load(sys.argv[2], allow_pickle=False)

    rows: list[tuple[str, str, str, str, str]] = []
    for key in sorted(set(a.files) | set(b.files)):
        if key not in a.files or key not in b.files:
            rows.append((key, "MISSING", "", "", ""))
            continue
        rows.append((key, *classify(a[key], b[key])))

    width = max(len(r[0]) for r in rows) + 2
    identical = sum(1 for r in rows if r[1] in ("BIT-IDENTICAL", "empty (both)", "exception (same)"))
    for row in rows:
        print(f"{row[0]:<{width}} {row[1]:<16} {row[2]:<12} {row[3]:<22} {row[4]}")
    print(f"\n{identical}/{len(rows)} rows unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
