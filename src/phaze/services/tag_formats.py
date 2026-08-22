"""Which tag family an opened mutagen object belongs to -- ONE resolver, shared by read and write.

phaze-wt9vw. Before this module the reader (``metadata_parsing.parse_format_tags``) and the writer
(``tag_write_disk.write_tags``) each carried their OWN copy of the dispatch, and both copies ended
in the same unguarded ``else: vorbis`` arm. That duplication is what made the ``.wma`` defect
invisible rather than merely wrong:

* ``write_tags`` fell through to ``_write_vorbis`` and put literal ``artist`` / ``date`` /
  ``tracknumber`` keys into an ASF file's extended content description;
* ``verify_write`` re-read through ``extract_tags``, whose dispatch fell through to ``_VORBIS_MAP``
  and looked for exactly those keys;
* so the verifier found everything it expected, returned no discrepancies, and the ``TagWriteLog``
  was stamped COMPLETED -- for a write no format-correct reader could see. Measured: real
  ``es.MetadataReader`` read seven empty strings off that file.

**The mechanism was the shared FALLBACK, not the key tables.** ``_WRITE_VORBIS_MAP`` was correct
for Vorbis; it was applied to ASF. Two independent dispatches that both guess "vorbis" when they
recognise nothing will always agree, and agreement is exactly what a verifier measures. So the
fix is not a fourth arm on each of the two dispatches -- it is ONE dispatch with NO fallback arm
to guess into.

**Why this is a resolver and not a fourth ``elif``.** A format nobody has mapped now cannot be
written at all: :func:`resolve_tag_format` raises, ``write_tags`` propagates, and
``write_and_verify_sync`` records ``FAILED`` with the reason. It cannot be written *wrongly* and
then confirmed, because there is no arm that accepts an unrecognised container. That is the
property AC 4 asks for -- structurally impossible rather than currently correct -- and it holds
for the NEXT unmapped format as well as for ASF.

The key tables themselves stay separate and deliberately duplicated -- ``_WRITE_*_MAP`` in
``tag_write_disk``, ``_*_MAP`` in ``metadata_parsing``. See ``test_write_and_read_maps_are_mutual_inverses``
for why: two independently-written tables make a typo in one detectable by round-tripping, which
a single shared table would hide. What they now share is the *format decision*, which is the part
that has to agree.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from mutagen._vorbis import VCommentDict
from mutagen.asf import ASF
from mutagen.id3 import ID3
from mutagen.mp4 import MP4


class TagFormat(StrEnum):
    """The tag families phaze can read and write. There is deliberately no ``UNKNOWN`` member."""

    ID3 = "id3"
    MP4 = "mp4"
    VORBIS = "vorbis"
    ASF = "asf"


class UnsupportedTagFormatError(ValueError):
    """An opened audio file belongs to no tag family phaze can read or write.

    Raised rather than defaulted, so that an unmapped container FAILS a write instead of silently
    receiving another format's key names -- see the module docstring.
    """


def resolve_tag_format(audio: Any, tags: Any) -> TagFormat:
    """Identify the tag family of an opened mutagen file. Never guesses.

    Args:
        audio: The object returned by ``mutagen.File()``.
        tags: Its ``.tags``. Passed separately because the WRITE path calls ``add_tags()`` first
            and must dispatch on the container that call installed, while the READ path dispatches
            on whatever was already on disk.

    Returns:
        The matching :class:`TagFormat`.

    Raises:
        UnsupportedTagFormatError: Nothing matched. This is the ONLY exit for an unrecognised
            container -- there is no default arm.

    **Order matters, and the first two arms are not interchangeable with the rest.**

    ``ID3`` is tested first, and against ``tags`` rather than ``audio``, because three different
    container types reach it: ``MP3`` (whose ``.tags`` is ``ID3``), and ``WAVE`` / ``AIFF``, whose
    ``add_tags()`` installs ``_WaveID3`` / ``_IFFID3``. Both of those ARE ID3 subclasses::

        _WaveID3 MRO: _WaveID3 -> ID3 -> ID3Tags -> DictProxy -> DictMixin
        _IFFID3  MRO: _IFFID3 -> IffID3 -> ID3 -> ID3Tags -> DictProxy

    so ``.wav`` and ``.aiff`` correctly receive real ID3 frames. This surprises people -- the
    phaze-wt9vw bead was filed on the assumption that both fell through to the Vorbis catch-all,
    and measurement disproved it. ``tests/review/services/test_tag_write_real_containers.py``
    pins it against real containers.

    ``VORBIS`` is matched on ``VCommentDict``, which is the genuine shared base of all three
    Vorbis-comment containers (verified: ``VCFLACDict``, ``OggVCommentDict`` and
    ``OggOpusVComment`` all subclass it) -- NOT used as a fallback for "everything else", which is
    precisely the bug this module exists to make unrepresentable.
    """
    if isinstance(tags, ID3):
        return TagFormat.ID3
    if isinstance(audio, MP4):
        return TagFormat.MP4
    if isinstance(audio, ASF):
        return TagFormat.ASF
    if isinstance(tags, VCommentDict):
        return TagFormat.VORBIS

    msg = f"unsupported tag format: {type(audio).__name__} with {type(tags).__name__} tags -- phaze cannot safely read or write this container"
    raise UnsupportedTagFormatError(msg)
