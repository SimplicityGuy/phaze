"""Static deployment guard: fingerprint sidecars pin their user to uid/gid 1000 (phaze-v0vj).

The main image pins its runtime user to uid/gid 1000 (Dockerfile) so the container
can read media owned by uid 1000 (mode 700/770); its own comment records that the
previous ``-r`` system account auto-assigned uid ~999 and "could not read
uid-1000-owned files and silently produced 0-file scans."

Both fingerprint sidecars (``audfprint``, ``panako``) bind-mount that SAME media
read-only (``${SCAN_PATH}:/data/music:ro``) and their subprocesses open the files
directly, so they MUST pin the identical uid/gid 1000 — an auto-uid ``useradd -r``
account re-introduces the exact EACCES → HTTP 500 fingerprint-stage breakage the
main image fixed. These text-grep guards lock the pin against a regression.
"""

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[3]
AUDFPRINT_DOCKERFILE = REPO_ROOT / "services" / "audfprint" / "Dockerfile.audfprint"
PANAKO_DOCKERFILE = REPO_ROOT / "services" / "panako" / "Dockerfile.panako"


def _assert_pins_uid_1000(dockerfile: Path, user: str) -> None:
    assert dockerfile.exists(), f"sidecar Dockerfile missing at {dockerfile}"
    text = dockerfile.read_text()

    # The user is created with an explicit uid/gid 1000 (groupadd -g 1000 + useradd -u 1000 -g 1000).
    assert re.search(rf"groupadd\s+-g\s+1000\s+{re.escape(user)}\b", text), (
        f"{dockerfile.name} must create the '{user}' group with explicit gid 1000 "
        f"(groupadd -g 1000 {user}) so it can read uid-1000-owned media — phaze-v0vj."
    )
    assert re.search(rf"useradd\s+-m\s+-u\s+1000\s+-g\s+1000\s+{re.escape(user)}\b", text), (
        f"{dockerfile.name} must create the '{user}' user pinned to uid/gid 1000 (useradd -m -u 1000 -g 1000 {user}) — phaze-v0vj."
    )

    # The auto-uid `-r` system account (the documented uid-999 incident) must be gone.
    assert not re.search(rf"useradd\s+-m\s+-r\s+{re.escape(user)}\b", text), (
        f"{dockerfile.name} still creates '{user}' as a `useradd -m -r` system account (auto uid ~999); "
        f"this re-introduces the uid-1000 media-read incident the main image fixed — phaze-v0vj."
    )

    # And the runtime USER is that pinned account.
    assert re.search(rf"^USER\s+{re.escape(user)}\b", text, re.MULTILINE), f"{dockerfile.name} must run as USER {user}."


def test_audfprint_sidecar_pins_uid_1000() -> None:
    _assert_pins_uid_1000(AUDFPRINT_DOCKERFILE, "audfprint")


def test_panako_sidecar_pins_uid_1000() -> None:
    _assert_pins_uid_1000(PANAKO_DOCKERFILE, "panako")


def test_panako_home_is_owned_by_pinned_uid() -> None:
    """Panako sets HOME=/data/fprint for LMDB; the chown must keep it writable by the pinned uid."""
    text = PANAKO_DOCKERFILE.read_text()
    assert "ENV HOME=/data/fprint" in text, "panako must set HOME=/data/fprint for LMDB."
    assert re.search(r"chown\s+panako:panako\s+/data/fprint", text), (
        "panako's /data/fprint (its LMDB HOME) must be chowned to the pinned panako user — phaze-v0vj."
    )
