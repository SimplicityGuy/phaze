"""Shared hashing utilities."""

import hashlib
from pathlib import Path


_HASH_CHUNK_SIZE = 65536


def compute_sha256(file_path: Path) -> str:
    """Compute SHA-256 hex digest of a file using chunked reads.

    Reads the file in 64KB chunks to avoid loading entire files into memory.

    Args:
        file_path: Path to the file to hash.

    Returns:
        64-character lowercase hex digest string.
    """
    sha256 = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK_SIZE):
            sha256.update(chunk)
    return sha256.hexdigest()
