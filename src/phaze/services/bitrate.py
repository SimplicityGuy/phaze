"""Display conversion for ``FileMetadata.bitrate``.

``bitrate`` is stored in BITS per second for every format (phaze-iw2k, see ``MetadataWriteRequest.bitrate``).
Every surface that shows it as kbps goes through :func:`bps_to_kbps`, so the unit and the rounding rule
live in one place.
"""

_BITS_PER_KILOBIT = 1000


def bps_to_kbps(bits_per_second: int) -> int:
    """Convert stored bits per second to whole kbps, rounding DOWN (floor).

    Floor is the rule the dedupe surfaces already used, so adopting it here leaves their output unchanged.
    Real rates that are not a round multiple of 1000 truncate: 64,040 bps renders as 64, 127,999 as 127.
    """
    return bits_per_second // _BITS_PER_KILOBIT
