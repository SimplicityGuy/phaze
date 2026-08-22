"""phaze-wt9vw: tag writes against REAL containers, read back by a NON-mutagen consumer.

Every "the tag was written correctly" assertion in the tag-write cluster was, until this module,
mutagen validating mutagen. ``TestWriteVorbisFormat``'s own class docstring says *"via mock"*, and
every test in it (and in ``TestWriteMP4Format``) is ``MagicMock`` + ``assert_any_call``: they
assert the CALL SHAPE and never that a byte lands on disk that a real reader can see. That is the
``ffprobe``-checking-``ffmpeg`` shape [ADR-0012](../../../docs/design/0012-verification-fidelity-and-operator-attribution.md)
rule 3 forbids, and it is exactly how ``phaze-3ea41`` shipped: real tooling, real container, and
the one consumer that could not read the artifact was never handed it.

This module closes four rows of the ``phaze-d2hgv.6`` producer→consumer seam inventory at once:

* **B5** — the filed bug, now fixed. ``.wma`` used to take ``_write_vorbis``'s catch-all and write
  literal Vorbis key names into an ASF file, and ``verify_write`` read them back through the same
  ``_VORBIS_MAP`` and reported success. See :func:`test_written_tags_are_visible_to_essentia`.
* **B2** — the whole ``tests/`` tree contained ZERO invocations of a non-mutagen tag reader.
  Every test here reads back through ``es.MetadataReader``, which is a genuine second
  implementation and is already a test dependency via
  ``tests/analyze/services/pipeline/test_extraction_analysis_handoff.py``.
* **B3** — ``_write_vorbis`` had never run against a real FLAC/Ogg/Opus container, and its
  ``del audio[key]`` branch — *undo's entire mechanism*, a destructive operation — had never
  executed against one at all. See :func:`test_vorbis_delete_branch_removes_tags_from_real_container`.
* **B4** — ``_write_mp4`` likewise, including the MP4 dispatch arm that no test took. See
  :func:`test_mp4_dispatch_branch_writes_real_atoms`.

**Why ``ffprobe`` is NOT the reader used here.** It is a weak discriminator for the ASF bug and
would have passed the pre-fix code: ffmpeg's ASF demuxer passes unknown extended-content-description
attributes through verbatim, so it reports ``TAG:artist=...`` for the WRONG write too, differing
only in a detail (``TAG:tracknumber`` for the wrong write vs the mapped ``TAG:track`` for a correct
one). Reaching for the producer's neighbouring tool because it is convenient is the mistake, not
the tool. ``es.MetadataReader`` distinguishes the two totally — measured, against the pre-fix code:

    phaze-written .wma  ->  ('', '', '', '', '', '', '')          # every field EMPTY
    spec-correct .wma   ->  ('Real Title', 'Real Artist', ...)    # all fields readable

**What fails without the fix, and how to reproduce it — read this before trying.** The container
tests were committed BEFORE the fix (commit ``a943872b``, against source ``7db055e5``) precisely so
the red run would be real rather than reconstructed. That recorded run is::

    FAILED test_written_tags_are_visible_to_essentia[wma]
    FAILED test_verify_write_agrees_with_the_independent_reader[wma]
    2 failed, 34 passed

Exactly two, both ``wma``. **The other 34 passed against pre-fix code too**, and that is not a
weakness — they are regression pins for behaviour that was already correct but untested, some of it
correct only by coincidence (see :func:`test_dispatch_branch_taken_per_container`). Do not read a
green run of this module as evidence that the ASF fix is present; only those two parameters carry
that.

**Reverting only ``src/`` today will NOT reproduce those two failures** — it produces a collection
error for the entire module, because the AC-4 tests added after the fix import
``phaze.services.tag_formats``, ``_ASF_MAP`` and ``_WRITERS``, none of which exist pre-fix. That is
measured, not assumed. To see the red run, take *this file* at ``a943872b`` against ``src/`` at
``7db055e5``; the two must move together.

The AC-4 tests at the bottom are a third category: they assert the *shape* of the read/write split
rather than any one format's behaviour, and they are what stops the next unmapped container from
reproducing the bug.

**No fixture audio is committed.** Every container is produced by real ``ffmpeg`` at test time,
one second of generated sine — the same discipline (and the same encoder-availability gating) as
``test_extraction_analysis_handoff.py``, whose header explains why a committed fixture would be
the wrong shape here.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import mutagen
from mutagen.id3 import ID3
from mutagen.mp4 import MP4
import pytest

from phaze.services import tag_write_disk
from phaze.services.metadata import extract_tags
from phaze.services.metadata_parsing import _ASF_MAP, _ID3_MAP, _MP4_MAP, _VORBIS_MAP
from phaze.services.tag_formats import TagFormat, UnsupportedTagFormatError, resolve_tag_format
from phaze.services.tag_write_disk import (
    _WRITE_ASF_MAP,
    _WRITE_ID3_MAP,
    _WRITE_MP4_MAP,
    _WRITE_VORBIS_MAP,
    _WRITERS,
    verify_write,
    write_and_verify_sync,
    write_tags,
)


if TYPE_CHECKING:
    from pathlib import Path


_HAS_FFMPEG = shutil.which("ffmpeg") is not None

pytestmark = pytest.mark.skipif(not _HAS_FFMPEG, reason="ffmpeg not installed on this runner")

# The tag values every test writes. Deliberately plain ASCII: this module is about WHICH KEY the
# value lands under and whether a second implementation can find it, not about encoding edge
# cases (``TestVerifyWrite`` in test_tag_writer.py owns NFC normalization).
_TAGS: dict[str, str | int | list[str] | None] = {
    "artist": "Test Artist",
    "title": "Test Title",
    "album": "Test Album",
    "year": "2024",
    "genre": "Rock",
    "track_number": 3,
}

# ``es.MetadataReader`` returns a fixed-position tuple. Only the positions this module asserts on
# are named; the reader yields several more (comment, date, and the audio properties after them).
_ES_TITLE, _ES_ARTIST, _ES_ALBUM = 0, 1, 2
_ES_GENRE, _ES_TRACK, _ES_YEAR = 4, 5, 6


def _encoder_present(name: str) -> bool:
    """Whether this ffmpeg build can encode ``name``.

    Codec availability is a compile-time option, not a property of the ffmpeg version — the
    reason ``test_extraction_analysis_handoff.py`` gates on encoders rather than on the binary.
    ``libopus`` and ``vorbis`` are the two that genuinely vary across distro packages and the
    static CI build; ``flac``, ``wmav2``, ``aac`` and the ``pcm_*`` family are always present.
    """
    # Fixed argv, no shell, no interpolation (a test fixture).
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-encoders"], capture_output=True, text=True, check=False)  # noqa: S607
    return f" {name} " in proc.stdout


def _run_ffmpeg(*args: str) -> None:
    """Run a fixed ffmpeg argv, raising with its stderr when it fails."""
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", *args], capture_output=True, text=True, check=False)  # noqa: S603, S607
    if proc.returncode != 0:  # pragma: no cover - a broken fixture, not a tested path
        msg = f"fixture ffmpeg failed (exit {proc.returncode}): {proc.stderr.strip()}"
        raise RuntimeError(msg)


# Container recipes: extension -> (encoder, extra ffmpeg args). The extension drives both the
# muxer ffmpeg picks and the mutagen class that opens it, which is the whole point.
_RECIPES: dict[str, tuple[str, list[str]]] = {
    "mp3": ("libmp3lame", []),
    "flac": ("flac", []),
    "ogg": ("vorbis", ["-strict", "-2"]),
    "opus": ("libopus", ["-ar", "48000"]),
    "m4a": ("aac", ["-f", "ipod"]),
    "wma": ("wmav2", []),
    "wav": ("pcm_s16le", []),
    "aiff": ("pcm_s16be", []),
    "aac": ("aac", []),
}


def _make_container(tmp_path: Path, ext: str) -> Path:
    """Generate one real, playable container of the requested type: 1 s of sine, no tags."""
    encoder, extra = _RECIPES[ext]
    if not _encoder_present(encoder):  # pragma: no cover - depends on the runner's ffmpeg build
        pytest.skip(f"this ffmpeg build cannot encode {encoder!r}")
    dest = tmp_path / f"sample.{ext}"
    _run_ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ac", "2", "-ar", "44100", *extra, "-c:a", encoder, str(dest))
    return dest


def _essentia_read(path: Path) -> tuple[str, ...]:
    """Read tags with the REAL ``es.MetadataReader`` -- a second implementation, not mutagen.

    Imported inside the function, matching
    ``test_video_audio.py::test_real_extraction_output_is_readable_by_the_real_consumer_es_metadatareader``:
    essentia is a hard dependency on every platform whose wheel exists, so an import failure here
    should surface loudly rather than be silently skipped past.
    """
    import essentia.standard as es

    return tuple(str(v) for v in es.MetadataReader(filename=str(path))()[:7])


# Formats whose tags a correct write must leave READABLE by a second implementation. ``.aac`` is
# absent deliberately -- it supports no tags at all, which is its own test below.
_TAGGABLE = ("mp3", "flac", "ogg", "opus", "m4a", "wma", "wav", "aiff")


@pytest.mark.parametrize("ext", _TAGGABLE)
def test_written_tags_are_visible_to_essentia(tmp_path: Path, ext: str) -> None:
    """B2/B5, and the acceptance criterion of phaze-wt9vw: phaze writes, a NON-mutagen reader reads.

    This is the test that fails against pre-phaze-wt9vw code on ``ext="wma"``. The pre-fix writer
    puts literal ``artist``/``date``/``tracknumber`` keys into the ASF extended content description;
    ``es.MetadataReader`` -- which looks for the ASF names ``Author``/``WM/Year``/``WM/TrackNumber``
    -- finds nothing and returns ``('', '', '', '', '', '', '')``. Every other parameter passes both
    before and after: they are pins on behaviour nothing else covers.

    Mutagen deliberately appears NOWHERE in the assertions. A mutagen read-back would be satisfied
    by the broken write, which is the entire defect.
    """
    path = _make_container(tmp_path, ext)
    write_tags(str(path), dict(_TAGS))

    got = _essentia_read(path)

    assert got[_ES_TITLE] == "Test Title", f".{ext}: title invisible to es.MetadataReader (got {got!r})"
    assert got[_ES_ARTIST] == "Test Artist", f".{ext}: artist invisible to es.MetadataReader (got {got!r})"
    assert got[_ES_ALBUM] == "Test Album", f".{ext}: album invisible to es.MetadataReader (got {got!r})"
    assert got[_ES_GENRE] == "Rock", f".{ext}: genre invisible to es.MetadataReader (got {got!r})"
    assert got[_ES_TRACK] == "3", f".{ext}: track number invisible to es.MetadataReader (got {got!r})"
    assert got[_ES_YEAR] == "2024", f".{ext}: year invisible to es.MetadataReader (got {got!r})"


@pytest.mark.parametrize("ext", _TAGGABLE)
def test_verify_write_agrees_with_the_independent_reader(tmp_path: Path, ext: str) -> None:
    """``verify_write`` reporting a clean write must MEAN the write is readable elsewhere.

    The masking property this bead exists to remove is precisely a disagreement between these two:
    pre-fix, ``verify_write`` returned ``{}`` for a ``.wma`` that ``es.MetadataReader`` could not
    read one field of. Asserting them together is what makes the verifier's success claim mean
    something -- a green ``verify_write`` alone is the thing that cannot be trusted.
    """
    path = _make_container(tmp_path, ext)
    write_tags(str(path), dict(_TAGS))

    discrepancies = verify_write(str(path), dict(_TAGS))
    independent = _essentia_read(path)

    assert discrepancies == {}, f".{ext}: verify_write reported {discrepancies!r}"
    assert independent[_ES_ARTIST] == "Test Artist", (
        f".{ext}: verify_write reported a clean write but the independent reader sees {independent!r} -- "
        "this is the self-consistent masking phaze-wt9vw exists to remove"
    )


@pytest.mark.parametrize(
    ("ext", "expected_branch"),
    [
        ("mp3", "id3"),
        ("wav", "id3"),
        ("aiff", "id3"),
        ("m4a", "mp4"),
        ("flac", "vorbis"),
        ("ogg", "vorbis"),
        ("opus", "vorbis"),
    ],
)
def test_dispatch_branch_taken_per_container(tmp_path: Path, ext: str, expected_branch: str) -> None:
    """Which arm of ``write_tags``'s dispatch each real container takes. Pins a SUBTLE correctness.

    **Read this before "fixing" the dispatch.** ``.wav`` and ``.aiff`` reach the ID3 arm, and it
    looks like they should not: ``mutagen.File()`` returns ``WAVE``/``AIFF`` with ``tags is None``,
    so an ``isinstance(audio.tags, ID3)`` check against a freshly-opened file is ``False`` for both.
    They work because of ORDERING -- ``write_tags`` calls ``audio.add_tags()`` (line 93) BEFORE the
    isinstance test (line 95), and ``add_tags()`` installs ``_WaveID3`` / ``_IFFID3``, both of which
    are ID3 SUBCLASSES:

        _WaveID3 MRO: _WaveID3 -> ID3 -> ID3Tags -> DictProxy -> DictMixin
        _IFFID3  MRO: _IFFID3 -> IffID3 -> ID3 -> ID3Tags -> DictProxy

    So the dispatch is correct by subtype, and both formats get real ID3 frames. Anyone who reads
    line 95 while picturing a not-yet-tagged file will conclude ``.wav``/``.aiff`` fall through to
    the Vorbis catch-all -- the phaze-wt9vw bead was filed on exactly that reading, and measurement
    corrected it. Reordering the ``add_tags()`` call, or narrowing the isinstance test to exactly
    ``ID3``, silently breaks both formats and NOTHING ELSE in the suite would notice.
    """
    path = _make_container(tmp_path, ext)
    write_tags(str(path), dict(_TAGS))

    audio = mutagen.File(str(path))
    branch = "id3" if isinstance(audio.tags, ID3) else ("mp4" if isinstance(audio, MP4) else "vorbis")

    assert branch == expected_branch, f".{ext} took the {branch!r} arm of write_tags's dispatch, expected {expected_branch!r}"


def test_wav_and_aiff_receive_genuine_id3_frames(tmp_path: Path) -> None:
    """The other half of the pin above: the ID3 arm must land real FRAME IDs, not Vorbis keys.

    Separate from the branch assertion because taking the right arm and writing the right keys are
    two different claims, and a future refactor could break the second while preserving the first.
    """
    for ext in ("wav", "aiff"):
        path = _make_container(tmp_path, ext)
        write_tags(str(path), dict(_TAGS))

        keys = set(mutagen.File(str(path)).tags.keys())

        assert {"TIT2", "TPE1", "TALB", "TDRC", "TCON", "TRCK"} <= keys, f".{ext} is missing ID3 frames; on-disk keys were {sorted(keys)}"
        assert not {"artist", "title", "album", "date", "genre", "tracknumber"} & keys, (
            f".{ext} received literal Vorbis comment keys -- the catch-all branch ran. On-disk keys: {sorted(keys)}"
        )


def test_aac_write_refuses_loudly_rather_than_writing_wrong_keys(tmp_path: Path) -> None:
    """``.aac`` is admitted as MUSIC by EXTENSION_MAP but supports no tags. It must FAIL, not lie.

    ``mutagen.aac.AAC`` has no tag container: ``add_tags()`` raises ``AACError("doesn't support
    tags")``, which ``write_and_verify_sync`` records as ``FAILED`` with that message. That is
    already the right OUTCOME, but it is reached by exception coincidence rather than by an
    explicit decision, and nothing pinned it. Pinned here so that a future dispatch change cannot
    quietly turn a loud refusal into a silent wrong write -- which is the whole defect class of
    this bead.
    """
    path = _make_container(tmp_path, "aac")

    status, discrepancies, error_message, _before = write_and_verify_sync(str(path), dict(_TAGS))

    assert status.value == "failed", f"expected a loud refusal for .aac, got status={status!r}"
    assert discrepancies is None
    assert error_message is not None and "support tags" in error_message, f"the refusal must say why; got {error_message!r}"


def test_vorbis_delete_branch_removes_tags_from_real_container(tmp_path: Path) -> None:
    """B3: ``_write_vorbis``'s ``del audio[key]`` arm, against real FLAC/Ogg/Opus. UNDO'S MECHANISM.

    An undo re-applies a before-snapshot in which an absent field is an explicit ``None``
    (``_extract_before_tags``, phaze-52qd), and ``None`` means DELETE. That delete branch --
    ``tag_write_disk.py:136`` -- had never executed against a real Vorbis container in any test;
    every existing exercise of it is a ``MagicMock`` whose ``__contains__`` and ``__delitem__``
    are themselves mocks, so it could not have caught a container that refuses a delete, or one
    where the key survives the ``save()``.

    Asserted through the independent reader as well as mutagen: a tag that is gone from mutagen's
    view but still legible to another implementation is an undo that did not happen.
    """
    for ext in ("flac", "ogg", "opus"):
        path = _make_container(tmp_path, ext)
        write_tags(str(path), dict(_TAGS))
        assert _essentia_read(path)[_ES_ARTIST] == "Test Artist", f".{ext}: setup write did not land"

        write_tags(str(path), dict.fromkeys(_TAGS))

        remaining = set(mutagen.File(str(path)).tags.keys())
        assert not {"artist", "title", "album", "date", "genre", "tracknumber"} & remaining, (
            f".{ext}: undo left Vorbis comments on disk: {sorted(remaining)}"
        )

        after = _essentia_read(path)
        assert after[_ES_ARTIST] == "", f".{ext}: artist still readable after delete: {after!r}"
        assert after[_ES_TITLE] == "", f".{ext}: title still readable after delete: {after!r}"


def test_mp4_dispatch_branch_writes_real_atoms(tmp_path: Path) -> None:
    """B4: the ``elif isinstance(audio, MP4)`` arm at ``tag_write_disk.py:97``, on a real .m4a.

    No test took this branch -- ``TestWriteMP4Format`` passes a ``MagicMock`` straight to
    ``_write_mp4``, bypassing ``write_tags``'s dispatch entirely, so the isinstance test itself was
    never evaluated against a real ``MP4`` object. 253 files in the archive are ``.m4a``, the
    second-largest population after mp3.

    Also covers the ``trkn`` atom's tuple form, which is unique to MP4 among the three writers.
    """
    path = _make_container(tmp_path, "m4a")
    write_tags(str(path), dict(_TAGS))

    audio = mutagen.File(str(path))
    assert isinstance(audio, MP4), "fixture is not an MP4 -- the dispatch branch under test cannot be reached"

    assert audio["\xa9ART"] == ["Test Artist"]
    assert audio["\xa9nam"] == ["Test Title"]
    assert audio["\xa9alb"] == ["Test Album"]
    assert audio["\xa9day"] == ["2024"]
    assert audio["\xa9gen"] == ["Rock"]
    assert audio["trkn"] == [(3, 0)], "track number must be MP4's (track, total) tuple form"


def test_mp4_delete_branch_removes_atoms_from_real_container(tmp_path: Path) -> None:
    """B4's other never-executed arm: ``del audio[mp4_key]`` at ``tag_write_disk.py:174``.

    Same reasoning as the Vorbis delete test -- this is how an undo removes an atom a prior write
    added, and it had only ever run against a mock.
    """
    path = _make_container(tmp_path, "m4a")
    write_tags(str(path), dict(_TAGS))
    assert _essentia_read(path)[_ES_ARTIST] == "Test Artist", "setup write did not land"

    write_tags(str(path), dict.fromkeys(_TAGS))

    remaining = set(mutagen.File(str(path)).tags.keys())
    assert not {"\xa9ART", "\xa9nam", "\xa9alb", "\xa9day", "\xa9gen", "trkn"} & remaining, f"undo left MP4 atoms on disk: {sorted(remaining)}"
    assert _essentia_read(path)[_ES_ARTIST] == "", "artist still readable after delete"


@pytest.mark.parametrize("ext", _TAGGABLE)
def test_phaze_can_read_back_what_phaze_wrote(tmp_path: Path, ext: str) -> None:
    """The write path and the READ path must agree on a real container, for every taggable format.

    Distinct from ``verify_write``: this calls ``extract_tags`` the way INGEST does. A format phaze
    can write but not read back is a file whose catalog metadata silently goes empty at the next
    scan -- the read-side half of the ASF defect, where ``metadata_parsing.py``'s ``_VORBIS_MAP``
    fallback serves ASF too and returns ``None`` for all six fields on a correctly-tagged ``.wma``.
    """
    path = _make_container(tmp_path, ext)
    write_tags(str(path), dict(_TAGS))

    tags = extract_tags(str(path))

    assert tags.artist == "Test Artist", f".{ext}: ingest read-back lost artist (got {tags.artist!r})"
    assert tags.title == "Test Title", f".{ext}: ingest read-back lost title (got {tags.title!r})"
    assert tags.album == "Test Album", f".{ext}: ingest read-back lost album (got {tags.album!r})"
    assert tags.genre == "Rock", f".{ext}: ingest read-back lost genre (got {tags.genre!r})"
    assert tags.year == 2024, f".{ext}: ingest read-back lost year (got {tags.year!r})"
    assert tags.track_number == 3, f".{ext}: ingest read-back lost track number (got {tags.track_number!r})"


# ---------------------------------------------------------------------------
# AC 4 -- the verifier must not be re-maskable. These are structural assertions
# about the SHAPE of the read/write split, not about any one format's behaviour:
# they are what stops the next unmapped container from reproducing the ASF bug.
# ---------------------------------------------------------------------------


def test_write_and_read_maps_are_mutual_inverses() -> None:
    """Each format's write table and read table must invert each other, key for key.

    The two tables are authored separately and deliberately NOT shared (see ``_ASF_MAP``'s comment
    for why: a single shared table hides a typo from every test that does not consult an external
    reader, whereas two tables make the same typo a round-trip failure). Separateness only buys
    that if something checks they agree -- this is that check.

    It is intentionally a pure data assertion with no file I/O: it runs on every platform, needs no
    ffmpeg, and fails the instant someone adds a field to one side only.
    """
    pairs = [
        ("ID3", {field: cls.__name__ for field, cls in _WRITE_ID3_MAP.items()}, _ID3_MAP),
        ("MP4", _WRITE_MP4_MAP, _MP4_MAP),
        ("Vorbis", _WRITE_VORBIS_MAP, _VORBIS_MAP),
        ("ASF", _WRITE_ASF_MAP, _ASF_MAP),
    ]
    for name, write_map, read_map in pairs:
        assert {v: k for k, v in write_map.items()} == read_map, (
            f"{name}: the write map and the read map disagree. Write {write_map!r} inverts to "
            f"{ {v: k for k, v in write_map.items()}!r}, but the read map is {read_map!r}"
        )


def test_every_tag_format_has_a_writer_and_a_reader() -> None:
    """No ``TagFormat`` member may exist without both halves. Fails the moment one is added alone.

    ``resolve_tag_format`` can only return a member of this enum, and ``_WRITERS`` is indexed by it
    with no ``.get`` and no default -- so a member without a writer is a ``KeyError`` at write time
    rather than a silent fallback. This test moves that failure to CI.
    """
    for fmt in TagFormat:
        assert fmt in _WRITERS, f"TagFormat.{fmt.name} has no writer -- write_tags would KeyError on it"

    read_maps = {TagFormat.ID3: _ID3_MAP, TagFormat.MP4: _MP4_MAP, TagFormat.VORBIS: _VORBIS_MAP, TagFormat.ASF: _ASF_MAP}
    for fmt in TagFormat:
        assert fmt in read_maps, f"TagFormat.{fmt.name} has no read map -- parse_format_tags would fall through to Vorbis"


def test_resolver_refuses_an_unmapped_container_instead_of_guessing() -> None:
    """The absence of a catch-all, asserted directly. THIS is the AC 4 property.

    The ASF defect needed two things: a writer that guessed a format, and a verifier that guessed
    the SAME format. Both guesses came from an unguarded ``else`` arm. With the guess removed there
    is nothing for a future unmapped container to fall into -- it raises on the way in, so it can
    never be written wrongly and therefore never confirmed wrongly.

    A plain object stands in for "a container mutagen supports that phaze has not mapped". Using a
    real one would mean shipping a format we deliberately do not handle; the resolver's contract is
    about what it does with an unrecognised type, which this exercises exactly.
    """

    class _UnmappedAudio:
        pass

    class _UnmappedTags:
        pass

    with pytest.raises(UnsupportedTagFormatError) as excinfo:
        resolve_tag_format(_UnmappedAudio(), _UnmappedTags())

    assert "_UnmappedAudio" in str(excinfo.value), "the refusal must name the container it could not handle"


def test_write_tags_refuses_an_unmapped_container_rather_than_writing_vorbis_keys(tmp_path: Path) -> None:
    """End-to-end form of the above, through the real ``write_and_verify_sync`` status machinery.

    Guards the property that matters operationally: an unwritable file is recorded FAILED, not
    COMPLETED. Pre-fix, an unmapped container took the Vorbis arm, wrote keys the format does not
    use, verified them against those same keys, and was stamped COMPLETED -- the exact sequence
    that produced this bead.
    """
    path = _make_container(tmp_path, "flac")

    def _reject_everything(audio: object, tags: object) -> TagFormat:
        msg = "unsupported tag format: simulated unmapped container"
        raise UnsupportedTagFormatError(msg)

    with patch("phaze.services.tag_write_disk.resolve_tag_format", _reject_everything):
        status, discrepancies, error_message, _before = write_and_verify_sync(str(path), dict(_TAGS))

    assert tag_write_disk.resolve_tag_format is resolve_tag_format, "the patch must not leak out of the context manager"
    assert status.value == "failed", f"an unmapped container must FAIL the write, got {status!r}"
    assert discrepancies is None, "a refused write has no per-field discrepancies to report"
    assert error_message is not None and "unsupported tag format" in error_message


def test_verifier_does_not_reach_for_the_writers_key_table(tmp_path: Path) -> None:
    """``verify_write`` must not consult the WRITE-side maps -- AC 4, asserted mechanically.

    Reading back through the writer's own table is what made the ASF write self-confirming, and it
    is the kind of coupling a later refactor could reintroduce "to avoid duplication" without any
    test noticing. Here every write-side table is replaced with garbage AFTER the write has landed:
    a verifier that reads through them cannot survive it, and one that reads the format's real keys
    is unaffected.
    """
    path = _make_container(tmp_path, "wma")
    write_tags(str(path), dict(_TAGS))

    poison = dict.fromkeys(_TAGS, "GARBAGE_KEY_THAT_IS_NOT_ON_DISK")
    with (
        patch.dict("phaze.services.tag_write_disk._WRITE_ASF_MAP", poison, clear=True),
        patch.dict("phaze.services.tag_write_disk._WRITE_VORBIS_MAP", poison, clear=True),
        patch.dict("phaze.services.tag_write_disk._WRITE_MP4_MAP", poison, clear=True),
    ):
        discrepancies = verify_write(str(path), dict(_TAGS))

    assert discrepancies == {}, f"verify_write consulted a write-side map -- it must read the format's own keys. Got {discrepancies!r}"


def test_a_wrong_write_is_now_caught_by_the_verifier(tmp_path: Path) -> None:
    """The end of the masking, stated as behaviour: write ASF the OLD (wrong) way, and verify FAILS.

    This is the regression test for the defect itself rather than for the fix's structure. It
    reproduces the pre-fix write exactly -- literal Vorbis keys into an ASF container -- and
    asserts phaze's own verifier now reports discrepancies where it previously returned ``{}``.

    If this test ever goes green-by-returning-``{}`` again, the read path has re-acquired a Vorbis
    fallback for ASF and the bug is back, whatever the writer is doing.
    """
    path = _make_container(tmp_path, "wma")

    audio = mutagen.File(str(path))
    for field, key in (("artist", "artist"), ("title", "title"), ("album", "album"), ("genre", "genre")):
        audio[key] = [str(_TAGS[field])]
    audio["date"] = ["2024"]
    audio["tracknumber"] = ["3"]
    audio.save()

    discrepancies = verify_write(str(path), dict(_TAGS))

    assert discrepancies, "the pre-fix ASF write must now be REPORTED, not confirmed -- this is the masking property, restored"
    assert set(discrepancies) == set(_TAGS), (
        f"every field was written to the wrong key, so every field should be flagged; got {sorted(discrepancies)}"
    )
    assert all(entry["actual"] is None for entry in discrepancies.values()), (
        f"a format-correct reader sees nothing at those keys, so every 'actual' should be None; got {discrepancies!r}"
    )
