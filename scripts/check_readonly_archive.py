"""Prove that a directory is a READ-ONLY view: a write attempt must be refused.

Run it on the CONSUMER side against the mount point the consumer reads the archive through
(``docs/operations/readonly-archive-access.md``). Exit 0 only when the write is refused with
``EROFS`` / ``EACCES`` / ``EPERM``; exit 1 when the write succeeds (the probe file is removed again
at once) or fails for any other reason, because "no error we recognise" is not proof of read-only.

It never reads archive content: it only attempts to create one uniquely named, empty probe file.
"""

import argparse
import errno
import os
from pathlib import Path
import sys
import uuid


REFUSED = {errno.EROFS, errno.EACCES, errno.EPERM}


def probe_write(root: Path) -> tuple[bool, str]:
    """Try to create an empty file under ``root``. Returns ``(refused, detail)``."""
    probe = root / f".phaze-ro-probe-{uuid.uuid4().hex}"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        name = errno.errorcode.get(exc.errno or 0, str(exc.errno))
        if exc.errno in REFUSED:
            return True, f"write refused ({name})"
        return False, f"write failed for a reason that does not prove read-only ({name}: {exc.strerror})"
    os.close(fd)
    probe.unlink()
    return False, "WRITE SUCCEEDED: the mount is writable (probe file removed)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mount", type=Path, help="mount point the consumer reads the archive through")
    args = parser.parse_args(argv)
    if not args.mount.is_dir():
        sys.stderr.write(f"FAIL: {args.mount} is not a directory\n")
        return 1
    refused, detail = probe_write(args.mount)
    sys.stdout.write(f"{'PASS' if refused else 'FAIL'}: {args.mount}: {detail}\n")
    return 0 if refused else 1


if __name__ == "__main__":
    raise SystemExit(main())
