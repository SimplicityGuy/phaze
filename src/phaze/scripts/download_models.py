"""Python helper that fetches the essentia weight files (Phase 29 D-21).

The same URL list + SHA manifest the existing bash script uses, exposed as a Python
function so both bash (``scripts/download-models.sh``) and the agent bootstrap
(``phaze.tasks._shared.model_bootstrap.ensure_models_present``) can drive the
download. Idempotent: skips files that already exist; verifies SHA-256 if provided
(deferred to a future plan).

Atomicity (T-29-05-03): each download writes to ``<dest>.part`` and atomically
renames to ``<dest>`` only after the byte stream completes; a crash mid-download
leaves only the ``.part`` file which is NOT counted by ``models_dir.glob("*.pb")``
in the bootstrap caller.

CLI entry:
    python -m phaze.scripts.download_models [output_dir]

The single positional argument defaults to ``./models``. The Bash shim
``scripts/download-models.sh`` is a thin wrapper that invokes this module.
"""

from __future__ import annotations

from pathlib import Path
import sys

import httpx


_CLASSIFIER_BASE = "https://essentia.upf.edu/models/classifiers"
_GENRE_BASE = "https://essentia.upf.edu/models/music-style-classification/discogs-effnet"

# 11 classifier model families x 3 variants = 33 models = 66 files (.pb + .json each).
# Byte-for-byte aligned with scripts/download-models.sh lines 16-50; order matters
# for diff-against-bash.
CLASSIFIER_MODELS: tuple[str, ...] = (
    "mood_acoustic/mood_acoustic-musicnn-msd-2",
    "mood_acoustic/mood_acoustic-musicnn-mtt-2",
    "mood_acoustic/mood_acoustic-vggish-audioset-1",
    "mood_electronic/mood_electronic-musicnn-msd-2",
    "mood_electronic/mood_electronic-musicnn-mtt-2",
    "mood_electronic/mood_electronic-vggish-audioset-1",
    "mood_aggressive/mood_aggressive-musicnn-msd-2",
    "mood_aggressive/mood_aggressive-musicnn-mtt-2",
    "mood_aggressive/mood_aggressive-vggish-audioset-1",
    "mood_relaxed/mood_relaxed-musicnn-msd-2",
    "mood_relaxed/mood_relaxed-musicnn-mtt-2",
    "mood_relaxed/mood_relaxed-vggish-audioset-1",
    "mood_happy/mood_happy-musicnn-msd-2",
    "mood_happy/mood_happy-musicnn-mtt-2",
    "mood_happy/mood_happy-vggish-audioset-1",
    "mood_sad/mood_sad-musicnn-msd-2",
    "mood_sad/mood_sad-musicnn-mtt-2",
    "mood_sad/mood_sad-vggish-audioset-1",
    "mood_party/mood_party-musicnn-msd-2",
    "mood_party/mood_party-musicnn-mtt-2",
    "mood_party/mood_party-vggish-audioset-1",
    "danceability/danceability-musicnn-msd-2",
    "danceability/danceability-musicnn-mtt-2",
    "danceability/danceability-vggish-audioset-1",
    "gender/gender-musicnn-msd-2",
    "gender/gender-musicnn-mtt-2",
    "gender/gender-vggish-audioset-1",
    "tonal_atonal/tonal_atonal-musicnn-msd-2",
    "tonal_atonal/tonal_atonal-musicnn-mtt-2",
    "tonal_atonal/tonal_atonal-vggish-audioset-1",
    "voice_instrumental/voice_instrumental-musicnn-msd-1",
    "voice_instrumental/voice_instrumental-musicnn-mtt-2",
    "voice_instrumental/voice_instrumental-vggish-audioset-1",
)

GENRE_MODELS: tuple[str, ...] = ("discogs-effnet-bs64-1",)


def _download_one(url: str, dest: Path) -> None:
    """Download ``url`` to ``dest`` using an atomic ``.part`` rename.

    Idempotent: if ``dest`` already exists, returns immediately without touching
    the network. A crash mid-stream leaves only ``<dest>.part`` which the
    bootstrap's ``*.pb`` glob does NOT match -- the next start will retry.
    """
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
        response.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                fh.write(chunk)
    tmp.rename(dest)  # POSIX-atomic per file


def download_to(target_dir: Path) -> None:
    """Download all classifier + genre weight files into ``target_dir``.

    Idempotent at the per-file level (``_download_one`` skips existing files).
    A partial-completion scenario (e.g., network drop after 17/33 classifiers)
    can be safely resumed by re-running ``download_to`` on the same directory.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    for model_path in CLASSIFIER_MODELS:
        filename = model_path.rsplit("/", 1)[-1]
        _download_one(f"{_CLASSIFIER_BASE}/{model_path}.pb", target_dir / f"{filename}.pb")
        _download_one(f"{_CLASSIFIER_BASE}/{model_path}.json", target_dir / f"{filename}.json")
    for model in GENRE_MODELS:
        _download_one(f"{_GENRE_BASE}/{model}.pb", target_dir / f"{model}.pb")
        _download_one(f"{_GENRE_BASE}/{model}.json", target_dir / f"{model}.json")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "./models")
    download_to(target)
