"""Hard-link the selected on-host model pairs for the one-variant candidate."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from phaze.services.analysis_models import GENRE_MODEL, MODEL_SETS


def stage(source: Path, target: Path) -> int:
    if any(target.iterdir()):
        raise ValueError("candidate model directory must be empty before staging")
    chosen = [next(model for model in group.models if model.variant == "musicnn_msd") for group in MODEL_SETS]
    chosen.append(GENRE_MODEL)
    paths = [source / f"{model.filename}{suffix}" for model in chosen for suffix in (".pb", ".json")]
    if any(not path.is_file() for path in paths):
        raise FileNotFoundError("one or more selected source model files are missing")
    for path in paths:
        os.link(path, target / path.name)
    return len(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    print(f"staged {stage(args.source, args.target)} on-host model files")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
