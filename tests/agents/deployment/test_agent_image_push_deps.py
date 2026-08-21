"""Static deployment guard for the `io` lane's rsync/ssh runtime dependency (phaze-w64q).

The `io` lane's `push_file` task (src/phaze/tasks/push.py) shells out directly to
``rsync -e "ssh -i …"`` for the cloud-burst push pipeline. The x86 agent/api image
(``Dockerfile``) is what ``docker-compose.agent.yml`` runs for ``worker-io``, but its only
apt layer historically installed audio/DB natives and neither ``rsync`` nor
``openssh-client`` — every push terminalized with ``FileNotFoundError`` once a compute
backend was configured. This test parses ``Dockerfile`` as text (no live docker build) and
asserts both binaries are installed on the runtime image, with a build-time
``command -v`` assertion as regression insurance against a future slimming pass.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"


def _dockerfile_text() -> str:
    assert DOCKERFILE_PATH.exists(), f"Dockerfile missing at {DOCKERFILE_PATH}"
    return DOCKERFILE_PATH.read_text()


def test_agent_image_installs_rsync_and_openssh_client() -> None:
    """The runtime apt layer must install both `rsync` and `openssh-client`.

    `push_file` invokes `rsync` directly and passes `-e "ssh -i …"`, so both the rsync
    binary and an ssh client are required at runtime on any image that can run the `io`
    lane (phaze-w64q).
    """
    text = _dockerfile_text()

    # Join line continuations BEFORE scanning (phaze-b62ri). A package list split across
    # backslash-continued lines is one `apt-get install` command but several text lines, so a
    # per-line scan reports a package as missing purely because of how the layer is wrapped.
    # That produced a false failure here when the runtime apt layer was re-wrapped to carry a
    # pinned `ffmpeg=<version>` -- rsync and openssh-client were installed the whole time.
    joined = text.replace("\\\n", " ")
    install_lines = [line for line in joined.splitlines() if "apt-get install" in line and not line.lstrip().startswith("#")]
    assert install_lines, "Dockerfile has no `apt-get install` line."

    combined = "\n".join(install_lines)
    assert "rsync" in combined, (
        "Dockerfile's apt-get install must include `rsync` — the `io` lane's push_file task shells out to it directly (phaze-w64q)."
    )
    assert "openssh-client" in combined, (
        "Dockerfile's apt-get install must include `openssh-client` — rsync's `-e \"ssh -i …\"` transport needs an ssh client binary (phaze-w64q)."
    )


def test_agent_image_asserts_rsync_and_ssh_present_at_build_time() -> None:
    """A `command -v rsync ssh` build-time check must guard against silent regression.

    Without this, a future dependency-slimming pass could drop `rsync`/`openssh-client`
    from the apt layer and the failure would only surface per-file at runtime deep inside
    the push pipeline, exactly as it did before this fix (phaze-w64q).
    """
    text = _dockerfile_text()
    assert "command -v rsync ssh" in text, (
        "Dockerfile must assert `command -v rsync ssh` in the runtime apt RUN layer so a "
        "future slimming pass fails the build instead of shipping a broken push pipeline (phaze-w64q)."
    )
