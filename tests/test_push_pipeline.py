"""File-server push pipeline tests (CLOUDPIPE-02 / -04), implemented in Plan 50-03.

Replaces the Wave 0 (`Plan 50-00`) skip stubs with real assertions against the novel
`push_file` agent task (rsync-over-SSH argv construction + exit-code handling) and the
compute-only startup scratch janitor.

Selectors (50-03):
  * ``-k argv``     → rsync argv list (no shell; pinned known_hosts + StrictHostKeyChecking)
  * ``-k exit_code``→ non-zero/partial rsync exit → job fails, no callback, re-drivable
  * ``-k janitor``  → orphaned scratch swept on compute-worker start, fileserver no-op

The subprocess is mocked everywhere — a real rsync-over-Tailscale transfer is a Phase 51
manual verification (50-VALIDATION.md Manual-Only).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

from pydantic import SecretStr
import pytest

from phaze.schemas.agent_tasks import PushFilePayload
from phaze.tasks import push


# ----------------------------------------------------------------------
# Test doubles
# ----------------------------------------------------------------------


def _fake_cfg(**overrides: Any) -> SimpleNamespace:
    """A duck-typed AgentSettings stand-in carrying only the push fields push_file reads."""
    base: dict[str, Any] = {
        "kind": "fileserver",
        "push_ssh_host": "compute.tailnet.ts.net",
        "push_ssh_user": "bursty",
        "cloud_scratch_dir": "/srv/scratch",
        "push_timeout_sec": 600,
        "push_connect_timeout_sec": 30,
        "push_ssh_key": SecretStr("PRIVATE-KEY-DATA"),
        "push_known_hosts": SecretStr("compute.tailnet.ts.net ssh-ed25519 AAAA..."),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _payload() -> PushFilePayload:
    return PushFilePayload(
        file_id=uuid.uuid4(),
        # original_path carries a space + the human filename: it MUST NOT leak into the
        # remote dest (which is UUID-derived) — proves path-traversal/metachar safety.
        original_path="/media/Coachella 2026 - Some Long Set.flac",
        file_type="flac",
        agent_id="fileserver-01",
    )


class _FakeProc:
    """Minimal asyncio.subprocess.Process stand-in."""

    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return (b"", self._stderr)

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


class _FakeApi:
    """Records report_pushed / report_push_mismatch calls."""

    def __init__(self) -> None:
        self.pushed: list[uuid.UUID] = []
        self.mismatched: list[uuid.UUID] = []

    async def report_pushed(self, file_id: uuid.UUID) -> None:
        self.pushed.append(file_id)

    async def report_push_mismatch(self, file_id: uuid.UUID) -> None:
        self.mismatched.append(file_id)


# ----------------------------------------------------------------------
# argv builder — pure, no subprocess
# ----------------------------------------------------------------------


def test_rsync_argv_no_shell_pinned_known_hosts() -> None:
    # CLOUDPIPE-02 / T-50-injection / T-50-spoof: argv is a list (shell=False) whose -e ssh
    # element pins the host key (StrictHostKeyChecking=yes + a fixed UserKnownHostsFile) and
    # never prompts (BatchMode=yes).
    cfg = _fake_cfg()
    payload = _payload()
    argv = push._build_rsync_argv(cfg, payload, key_path="/run/secrets/key", known_hosts_path="/run/secrets/known_hosts")

    assert isinstance(argv, list)
    assert all(isinstance(elem, str) for elem in argv)
    assert argv[0] == "rsync"

    e_idx = argv.index("-e")
    ssh_elem = argv[e_idx + 1]
    assert "ssh " in ssh_elem
    assert "-i /run/secrets/key" in ssh_elem
    assert "StrictHostKeyChecking=yes" in ssh_elem
    assert "UserKnownHostsFile=/run/secrets/known_hosts" in ssh_elem
    assert "BatchMode=yes" in ssh_elem
    assert "ConnectTimeout=30" in ssh_elem


def test_rsync_argv_flag_discipline() -> None:
    # CLOUDPIPE-02: --partial-dir + --timeout present for resumable, stall-bounded transfer;
    # --inplace / -z / -c / -a are forbidden (atomicity + no wasted CPU on already-compressed audio).
    cfg = _fake_cfg()
    payload = _payload()
    argv = push._build_rsync_argv(cfg, payload, key_path="/k", known_hosts_path="/kh")

    assert "--partial-dir=.rsync-partial" in argv
    assert any(a.startswith("--timeout=") for a in argv)
    assert "--inplace" not in argv
    assert "-z" not in argv
    assert "-c" not in argv
    assert "-a" not in argv


def test_rsync_argv_remote_dest_is_file_id_not_filename() -> None:
    # T-50-injection: the remote scratch path is <scratch_dir>/<file_id>.<ext> (server UUID),
    # never the untrusted original filename — no path-traversal/metachar surface in the dest.
    cfg = _fake_cfg()
    payload = _payload()
    argv = push._build_rsync_argv(cfg, payload, key_path="/k", known_hosts_path="/kh")

    expected = f"bursty@compute.tailnet.ts.net:/srv/scratch/{payload.file_id}.flac"
    assert argv[-1] == expected
    # The human filename never appears in the remote dest.
    assert "Some Long Set" not in argv[-1]


# ----------------------------------------------------------------------
# exit-code handling — subprocess mocked
# ----------------------------------------------------------------------


async def test_rsync_exit_code_zero_calls_report_pushed(monkeypatch: pytest.MonkeyPatch) -> None:
    # rc==0 → the success callback fires exactly once and the task returns status "pushed".
    cfg = _fake_cfg()
    payload = _payload()
    monkeypatch.setattr(push, "_agent_settings", lambda: cfg)

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=0)

    monkeypatch.setattr(push.asyncio, "create_subprocess_exec", _fake_exec)
    api = _FakeApi()

    result = await push.push_file({"api_client": api}, **payload.model_dump(mode="json"))

    assert api.pushed == [payload.file_id]
    assert result["status"] == "pushed"


async def test_rsync_exit_code_nonzero_raises_no_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    # CLOUDPIPE-02/-05: rc!=0 raises (SAQ retry) and fires NO success callback — re-drivable.
    cfg = _fake_cfg()
    payload = _payload()
    monkeypatch.setattr(push, "_agent_settings", lambda: cfg)

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=23, stderr=b"rsync: partial transfer")

    monkeypatch.setattr(push.asyncio, "create_subprocess_exec", _fake_exec)
    api = _FakeApi()

    with pytest.raises(RuntimeError):
        await push.push_file({"api_client": api}, **payload.model_dump(mode="json"))
    assert api.pushed == []


async def test_rsync_exit_code_missing_binary_terminal_no_local_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # T-50-no-fallback: a missing rsync/ssh binary → clear terminal error, NO callback, and the
    # task NEVER analyzes locally (it only ever transfers).
    cfg = _fake_cfg()
    payload = _payload()
    monkeypatch.setattr(push, "_agent_settings", lambda: cfg)

    async def _raise_fnf(*_args: Any, **_kwargs: Any) -> _FakeProc:
        raise FileNotFoundError(2, "No such file or directory", "rsync")

    monkeypatch.setattr(push.asyncio, "create_subprocess_exec", _raise_fnf)
    api = _FakeApi()

    with pytest.raises(RuntimeError) as exc_info:
        await push.push_file({"api_client": api}, **payload.model_dump(mode="json"))
    assert "rsync" in str(exc_info.value)
    assert api.pushed == []


async def test_rsync_exit_code_stderr_truncated_no_key_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    # T-50-secret-leak: a non-zero exit surfaces only a BOUNDED stderr snippet, and never the
    # SSH key contents.
    cfg = _fake_cfg()
    payload = _payload()
    monkeypatch.setattr(push, "_agent_settings", lambda: cfg)

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=12, stderr=b"X" * 50_000)

    monkeypatch.setattr(push.asyncio, "create_subprocess_exec", _fake_exec)
    api = _FakeApi()

    with pytest.raises(RuntimeError) as exc_info:
        await push.push_file({"api_client": api}, **payload.model_dump(mode="json"))
    msg = str(exc_info.value)
    assert len(msg) < 2000
    assert "PRIVATE-KEY-DATA" not in msg


# ----------------------------------------------------------------------
# compute-only startup janitor (Task 2 — converted from the Wave 0 stub there)
# ----------------------------------------------------------------------


def _import_agent_worker(monkeypatch: pytest.MonkeyPatch) -> Any:
    # The module builds a Queue at import time, which needs the agent env present.
    monkeypatch.setenv("PHAZE_ROLE", "agent")
    monkeypatch.setenv("PHAZE_AGENT_API_URL", "http://test")
    monkeypatch.setenv("PHAZE_AGENT_TOKEN", "phaze_agent_test-token-1234567890abcdef")
    monkeypatch.setenv("PHAZE_AGENT_QUEUE", "phaze-agent-test-id")
    monkeypatch.setenv("PHAZE_AGENT_SCAN_ROOTS", "/var/empty")
    monkeypatch.setenv("PHAZE_REDIS_URL", "redis://localhost:6379/0")
    import phaze.tasks.agent_worker as aw

    return aw


def test_startup_janitor_sweep_removes_files_and_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLOUDPIPE-04: _sweep_scratch unlinks every file AND the .rsync-partial dir under the scratch
    # dir, and tolerates a missing dir (no raise).
    aw = _import_agent_worker(monkeypatch)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "orphan-1.flac").write_bytes(b"a")
    (scratch / "orphan-2.mp3").write_bytes(b"b")
    partial = scratch / ".rsync-partial"
    partial.mkdir()
    (partial / "in-flight.flac.tmp").write_bytes(b"c")

    aw._sweep_scratch(scratch)

    assert list(scratch.iterdir()) == []
    assert not partial.exists()

    # Missing dir is tolerated.
    aw._sweep_scratch(tmp_path / "does-not-exist")


async def test_startup_janitor_compute_only_gating(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLOUDPIPE-04 / D-14: the gate sweeps ONLY when kind == "compute" AND a scratch dir is set.
    # The fileserver agent runs the SAME module and must NOT sweep (it has no scratch dir).
    aw = _import_agent_worker(monkeypatch)

    calls: list[Path] = []
    monkeypatch.setattr(aw, "_sweep_scratch", calls.append)

    scratch = str(tmp_path / "scratch")
    await aw._maybe_sweep_scratch(SimpleNamespace(kind="compute", cloud_scratch_dir=scratch))
    assert calls == [Path(scratch)]

    calls.clear()
    await aw._maybe_sweep_scratch(SimpleNamespace(kind="fileserver", cloud_scratch_dir=scratch))
    assert calls == []

    # compute but no scratch dir configured → no-op.
    await aw._maybe_sweep_scratch(SimpleNamespace(kind="compute", cloud_scratch_dir=None))
    assert calls == []


def test_push_file_registered_on_agent_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    # push_file must be present in the worker functions[] list so the fileserver agent can run it.
    aw = _import_agent_worker(monkeypatch)
    assert aw.push_file in aw.settings["functions"]
