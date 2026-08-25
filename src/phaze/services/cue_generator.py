"""CUE sheet generation service.

Generates CUE sheet content from tracklist data with Discogs metadata
enrichment via REM comments. Handles timestamp conversion to CUE's
MM:SS:FF format at 75 frames per second, file type mapping, version
suffix naming, and UTF-8 BOM file writing.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class CueTrackData:
    """Data for a single track in a CUE sheet."""

    position: int
    title: str | None = None
    artist: str | None = None
    timestamp_seconds: float | None = None
    genre: str | None = None
    label: str | None = None
    year: int | None = None


_FILE_TYPE_MAP: dict[str, str] = {
    "mp3": "MP3",
    "wav": "WAVE",
    "wave": "WAVE",
    "aiff": "AIFF",
    "aif": "AIFF",
    "flac": "WAVE",
    "ogg": "WAVE",
    "m4a": "WAVE",
    "opus": "WAVE",
}

# C0 control characters (including CR/LF, the CUE record delimiter) plus DEL, matched for
# stripping from any value interpolated into a CUE double-quoted field. See _cue_quote (phaze-4ea3).
_CUE_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# The single top-level ``FILE "<name>"`` directive, matched so the name can be RETARGETED to the
# basename the sheet is really written beside (phaze-pqib3). ``_cue_quote`` strips every literal
# quote from an interpolated value, so ``[^"]*`` cannot under-run a filename containing one, and
# the trailing type token is left untouched by matching only up to the closing quote.
_CUE_FILE_DIRECTIVE_RE = re.compile(r'^FILE "[^"]*"', re.MULTILINE)


def seconds_to_cue_timestamp(total_seconds: float) -> str:
    """Convert seconds to CUE MM:SS:FF format at 75 frames per second.

    Args:
        total_seconds: Time in seconds (float).

    Returns:
        String in MM:SS:FF format where FF is 00-74.
    """
    total_frames = int(total_seconds * 75)
    frames = total_frames % 75
    total_secs = total_frames // 75
    minutes = total_secs // 60
    seconds = total_secs % 60
    return f"{minutes:02d}:{seconds:02d}:{frames:02d}"


def parse_timestamp_string(ts: str | None) -> float | None:
    """Parse a timestamp string to total seconds.

    Supports formats:
        - "HH:MM:SS" -> hours*3600 + minutes*60 + seconds
        - "MM:SS" -> minutes*60 + seconds
        - "123.45" -> raw seconds as float

    Total function (phaze-97u7): ``ts`` is unvalidated free-form text -- scraped verbatim from
    1001Tracklists markup (an empty cue-time cell yields "", not None) and, before phaze-jsl9,
    writable verbatim via the inline editor. Any of "", whitespace, a decorated/bracketed time
    ("~5:00", "[1:02:03]"), or a non-numeric segment must return None per this docstring's
    contract, NOT raise -- callers (routers/cue.py ``_build_cue_tracks``, both single and batch
    generate) run this per track OUTSIDE their own try/except and rely on the documented None
    contract to route to the friendly "No tracks have timestamps" toast instead of a 500.

    Args:
        ts: Timestamp string or None.

    Returns:
        Total seconds as float, or None if input is None or unparseable.
    """
    if ts is None:
        return None

    parts = ts.split(":")
    try:
        if len(parts) == 3:
            return float(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
        if len(parts) == 2:
            return float(int(parts[0]) * 60 + int(parts[1]))
        return float(ts)
    except ValueError:
        return None


def _cue_quote(value: str) -> str:
    """Sanitize a value for interpolation inside a CUE double-quoted field (phaze-oo35, phaze-4ea3).

    CUE's quoted-string syntax has no escape mechanism, so an embedded ``"`` terminates the field
    early and leaves trailing garbage on the line (e.g. a track titled ``ID "Unreleased" Mix``
    would emit ``TITLE "ID "Unreleased" Mix"``, which most consumers either truncate or reject as
    malformed). Strip the literal quote the same way the filename was already handled (Pitfall 4)
    -- shared by every quoted CUE directive: FILE, TITLE, PERFORMER, and the REM fields.

    CUE is also LINE-oriented: the double quote delimits the *field*, but ``\\n`` (and ``\\r``)
    delimits the *record*. An unsanitized newline surviving inside a title/artist/genre/label lets
    an agent- or scraper-supplied value terminate the current directive early and inject an
    arbitrary new top-level CUE command on the next line -- including a second ``FILE`` directive
    that redirects every subsequent TRACK/INDEX to a different audio file (phaze-4ea3). Replace
    every control character (CR, LF, and the rest of the C0/DEL range) with a space so an injected
    payload collapses onto the same line instead of starting a new one.
    """
    return _CUE_CONTROL_CHARS_RE.sub(" ", value).replace('"', "")


def generate_cue_content(audio_filename: str, file_type: str, tracks: list[CueTrackData]) -> str:
    """Generate CUE sheet content as a string.

    Args:
        audio_filename: Name of the audio file (not full path).
        file_type: Audio file extension (e.g. "mp3", "wav").
        tracks: List of track data. Tracks without timestamp_seconds are omitted.

    Returns:
        Complete CUE sheet content string with trailing newline.
    """
    # Strip double quotes from filename (Pitfall 4)
    safe_filename = _cue_quote(audio_filename)

    cue_type = _FILE_TYPE_MAP.get(file_type.lower(), "WAVE")

    lines: list[str] = [
        'REM COMMENT "Generated by Phaze"',
        f'FILE "{safe_filename}" {cue_type}',
    ]

    # Filter out tracks without timestamps and sort by position
    valid_tracks = sorted(
        [t for t in tracks if t.timestamp_seconds is not None],
        key=lambda t: t.position,
    )

    for i, track in enumerate(valid_tracks, start=1):
        lines.append(f"  TRACK {i:02d} AUDIO")

        # REM comments from Discogs metadata (D-08, D-09) -- phaze-oo35: every value interpolated
        # inside CUE's double quotes must be sanitized the same way, not just the filename.
        if track.genre:
            lines.append(f'    REM GENRE "{_cue_quote(track.genre)}"')
        if track.label:
            lines.append(f'    REM LABEL "{_cue_quote(track.label)}"')
        if track.year:
            lines.append(f'    REM YEAR "{track.year}"')

        # Track metadata
        if track.title:
            lines.append(f'    TITLE "{_cue_quote(track.title)}"')
        if track.artist:
            lines.append(f'    PERFORMER "{_cue_quote(track.artist)}"')

        # INDEX command with converted timestamp (timestamp_seconds guaranteed non-None by filter above)
        if track.timestamp_seconds is None:  # pragma: no cover — defensive guard; valid_tracks filters None above
            continue
        lines.append(f"    INDEX 01 {seconds_to_cue_timestamp(track.timestamp_seconds)}")

    return "\n".join(lines) + "\n"


def retarget_cue_file_line(content: str, audio_filename: str) -> str:
    """Rewrite a rendered sheet's ``FILE`` directive to name ``audio_filename`` (phaze-pqib3, seam C5).

    A CUE sheet's ``FILE`` is a BARE NAME, and its real consumer -- a parser or player opening the
    ``.cue`` -- resolves it **relative to the .cue's own directory**. That makes the correct name a
    fact about where the sheet lands, which is decided by :func:`write_cue_file`, not by whoever
    rendered the text.

    Those two were different machines and they disagreed. ``routers/cue.py::generate_cue`` renders
    ``FILE`` from the UNRESOLVED ``FileRecord.current_path`` while ``tasks/cue_write.py::_write_sync``
    containment-resolves that path and writes beside the RESOLVED file, so any entry whose
    ``current_path`` is a symlink into a differently-named target produced a sheet naming a sibling
    that was not there. The api cannot render it correctly and must not try: it mounts no media
    (DIST-01), so ``Path.resolve()`` there normalizes lexically and proves nothing -- only the agent
    holding the mount can resolve a symlink honestly. Hence a retarget at the write, rather than a
    "fix" at the render.

    The new name is sanitized exactly like the rendered one (:func:`_cue_quote`): it is an
    agent-filesystem basename, so a literal quote or control character in it would terminate the
    field or the record early, which is the ``phaze-4ea3`` FILE-injection shape.

    Args:
        content: A rendered CUE sheet carrying exactly one ``FILE`` directive.
        audio_filename: The basename the sheet will actually sit beside.

    Returns:
        ``content`` with that directive's quoted name replaced. Byte-identical to ``content`` when
        the name already matched, which is every non-symlinked file in the archive.

    Raises:
        ValueError: ``content`` does not carry exactly one ``FILE`` directive. It is rendered by
            :func:`generate_cue_content`, which emits exactly one and sanitizes every interpolated
            value so a second cannot be injected -- so neither zero nor two is a state a caller can
            reach honestly, and writing a sheet whose audio reference is missing or ambiguous is
            worse than failing the job.
    """
    # A REPLACEMENT FUNCTION, not a template string: a basename may legitimately contain a
    # backslash escape (``\\1``, ``\\g``) that ``re.sub`` would otherwise interpret as a
    # backreference into the pattern.
    replacement = f'FILE "{_cue_quote(audio_filename)}"'
    patched, count = _CUE_FILE_DIRECTIVE_RE.subn(lambda _match: replacement, content)
    if count != 1:
        msg = f"CUE content carries {count} FILE directives, expected exactly 1 -- refusing to write a sheet with an unclear audio reference"
        raise ValueError(msg)
    return patched


def next_cue_path_and_version(audio_path: Path) -> tuple[Path, int]:
    """Determine the next CUE file path AND the version number that file will carry.

    First CUE: ``("audio.cue", 1)``
    Second:    ``("audio.v2.cue", 2)``
    Third:     ``("audio.v3.cue", 3)``

    phaze-9dwb: the version is returned alongside the path because the caller needs BOTH and the
    directory scan that computes them is the same scan. The previous shape returned only the Path
    and the caller (``routers/cue.py::_get_cue_version``) re-ran an identical ``exists()`` +
    ``iterdir()`` sweep of the archive directory microseconds later purely to recover the number it
    had just thrown away -- doubling the syscall cost of every generate on a directory that, for a
    concert archive, can hold thousands of siblings.

    Args:
        audio_path: Path to the audio file.

    Returns:
        ``(path, version)`` for the next CUE file.
    """
    base = audio_path.stem
    parent = audio_path.parent
    base_cue = parent / f"{base}.cue"

    if not base_cue.exists():
        return base_cue, 1

    # Scan for existing versioned CUE files
    pattern = re.compile(rf"^{re.escape(base)}\.v(\d+)\.cue$")
    max_version = 1  # base_cue counts as v1

    for f in parent.iterdir():
        m = pattern.match(f.name)
        if m:
            max_version = max(max_version, int(m.group(1)))

    return parent / f"{base}.v{max_version + 1}.cue", max_version + 1


def next_cue_path(audio_path: Path) -> Path:
    """Path-only view of :func:`next_cue_path_and_version` (unchanged legacy shape)."""
    return next_cue_path_and_version(audio_path)[0]


# phaze-d16ax: a bound on the exclusive-create retry loop below, not a realistic expectation --
# each retry means another writer won the SAME predicted version out from under this one, and
# the version number is monotonically increasing, so genuine unbounded contention is not a shape
# this file's callers produce (write_cue_sheet is one job per generate click). The cap exists so a
# persistent, unrelated failure (e.g. a directory that vanished) reports an error instead of
# spinning forever.
_MAX_VERSION_CLAIM_ATTEMPTS = 50


def write_cue_file(content: str, audio_path: Path) -> tuple[Path, int]:
    """Write CUE content to filesystem with UTF-8 BOM encoding.

    Uses version suffix naming if a CUE file already exists.

    DIST-01 / phaze-6bkk: this is agent-side code. The only caller is
    ``phaze.tasks.cue_write.write_cue_sheet``, which runs on the file server's ``meta`` lane where
    the archive is actually mounted. It must NEVER be called from the api or controller process --
    neither has a media mount, so a raw open there raises ``FileNotFoundError`` on a parent
    directory that does not exist in that container.

    phaze-d16ax: ``next_cue_path_and_version`` is a pure check-then-act PREDICTION of a free path
    (an ``exists()`` check plus an ``iterdir()`` sweep) -- it does not CLAIM one. ``write_cue_sheet``
    runs on the meta lane at concurrency 2 with the scan+write offloaded to a worker thread, so two
    concurrent generates for the SAME audio file can genuinely resolve the identical ``(path,
    version)`` and, with a truncating ``open("w")``, the second silently overwrites the first's
    content while both report success. The open below is exclusive create (``O_CREAT | O_EXCL``,
    mirroring the ``phaze-otoqj`` pattern in ``tasks/execution.py``) so a losing writer gets
    ``FileExistsError`` instead of clobbering -- it re-derives the next free version (the winner's
    file now exists, so the scan naturally advances) and retries, rather than losing the loser's
    content.

    phaze-pqib3 (seam C5): this function writes ``content`` VERBATIM and does not reconcile it with
    ``audio_path`` -- ``test_content_matches`` pins that byte-identity, and the versioning and
    concurrency tests around it depend on being able to pass arbitrary marker content. The sheet's
    ``FILE`` directive must still name the basename it lands beside, but that reconciliation is the
    job of the caller that RESOLVES the path and so creates the discrepancy in the first place; see
    :func:`retarget_cue_file_line` and ``tasks/cue_write.py::_write_sync``.

    Args:
        content: CUE sheet content string.
        audio_path: Path to the audio file (used to determine CUE path).

    Returns:
        ``(path, version)`` of the written CUE file.

    Raises:
        RuntimeError: every attempt within :data:`_MAX_VERSION_CLAIM_ATTEMPTS` lost the exclusive
            create -- a persistent collision rather than a single race, surfaced instead of spun on.
    """
    encoded = content.encode("utf-8-sig")
    for _attempt in range(_MAX_VERSION_CLAIM_ATTEMPTS):
        path, version = next_cue_path_and_version(audio_path)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
        return path, version

    raise RuntimeError(
        f"Could not claim a free CUE version for {audio_path} after {_MAX_VERSION_CLAIM_ATTEMPTS} attempts (persistent version collisions)"
    )
