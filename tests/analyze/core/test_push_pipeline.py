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

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import uuid

from pydantic import SecretStr, ValidationError
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
        # Phase 73 (D-04): the destination is payload-driven. These match the _fake_cfg globals so
        # the argv-invariant tests stay byte-identical while exercising the per-file dest path.
        dest_host="compute.tailnet.ts.net",
        dest_scratch_dir="/srv/scratch",
    )


# ----------------------------------------------------------------------
# Phase 73 (Task 2): PushFilePayload per-file destination fields + validators
# ----------------------------------------------------------------------
_VALID_DEST = {"dest_host": "oci-a1.push.example", "dest_scratch_dir": "/srv/scratch"}


def _dest_payload(**overrides: Any) -> PushFilePayload:
    kwargs: dict[str, Any] = {
        "file_id": uuid.uuid4(),
        "original_path": "/media/set.flac",
        "file_type": "flac",
        "agent_id": "fileserver-01",
        **_VALID_DEST,
    }
    kwargs.update(overrides)
    return PushFilePayload(**kwargs)


def test_push_payload_accepts_valid_dest_fields() -> None:
    """A validated non-secret destination (host + absolute scratch + optional user) constructs cleanly."""
    p = _dest_payload(dest_ssh_user="phaze")
    assert p.dest_host == "oci-a1.push.example"
    assert p.dest_scratch_dir == "/srv/scratch"
    assert p.dest_ssh_user == "phaze"
    # dest_ssh_user is optional -> defaults None when omitted.
    assert _dest_payload().dest_ssh_user is None


def test_push_payload_four_field_construction_still_valid() -> None:
    """Interface-first: the legacy four-field construction still validates (dest_* are optional here).

    The /mismatch re-drive producer (agent_push.py) is wired to supply dest_* in Plan 03; until then it
    constructs the four-field payload, so dest_* must not be pydantic-required in this plan.
    """
    p = PushFilePayload(file_id=uuid.uuid4(), original_path="/media/set.flac", file_type="flac", agent_id="fs")
    assert p.dest_host is None
    assert p.dest_scratch_dir is None


def test_push_payload_dest_scratch_dir_must_be_absolute() -> None:
    """dest_scratch_dir not starting with '/' raises (same shape as _original_path_absolute)."""
    with pytest.raises(ValidationError, match="absolute path"):
        _dest_payload(dest_scratch_dir="relative/x")


@pytest.mark.parametrize("bad", [" ", "\t", ";", "|", "&", "$", "`", "(", ")", "<", ">", "\n"])
def test_push_payload_dest_host_rejects_whitespace_and_shell_metacharacters(bad: str) -> None:
    """dest_host lands in the ssh remote spec, so any whitespace/shell metachar is rejected (T-73-01)."""
    with pytest.raises(ValidationError):
        _dest_payload(dest_host=f"host{bad}evil")


def test_push_payload_dest_host_injection_shape_rejected() -> None:
    """The acceptance-criteria injection shape (``host; rm -rf /``) is rejected."""
    with pytest.raises(ValidationError):
        _dest_payload(dest_host="host; rm -rf /", dest_scratch_dir="/srv/scratch")


def test_push_payload_dest_ssh_user_rejects_whitespace() -> None:
    """A non-None dest_ssh_user must be a plain non-whitespace token."""
    with pytest.raises(ValidationError):
        _dest_payload(dest_ssh_user="ph aze")


def test_push_payload_preserves_extra_forbid_with_dest_fields() -> None:
    """extra='forbid' is preserved: an unknown field still raises even alongside the new dest_* fields."""
    with pytest.raises(ValidationError):
        _dest_payload(unexpected="x")


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
# Phase 73 (Task 1): the remote_dest is payload-driven (per-file), D-04 retires the
# fileserver's single-global remote-target env read.
# ----------------------------------------------------------------------


def test_rsync_argv_remote_dest_is_payload_driven_per_file() -> None:
    # MCOMP-03: two payloads with distinct dest_host/dest_scratch_dir produce two DISTINCT
    # remote_dest strings — the destination is resolved per file from the payload, not from a
    # single fileserver global.
    cfg = _fake_cfg()
    p1 = _dest_payload(dest_host="oci-a1.push.example", dest_scratch_dir="/srv/scratch-a")
    p2 = _dest_payload(dest_host="oci-a2.push.example", dest_scratch_dir="/srv/scratch-b")
    argv1 = push._build_rsync_argv(cfg, p1, key_path="/k", known_hosts_path="/kh")
    argv2 = push._build_rsync_argv(cfg, p2, key_path="/k", known_hosts_path="/kh")

    assert argv1[-1] == f"bursty@oci-a1.push.example:/srv/scratch-a/{p1.file_id}.flac"
    assert argv2[-1] == f"bursty@oci-a2.push.example:/srv/scratch-b/{p2.file_id}.flac"
    assert argv1[-1] != argv2[-1]


def test_rsync_argv_dest_ssh_user_none_falls_back_to_cfg_user() -> None:
    # D-03/≤1-compute byte-identical: dest_ssh_user=None → the user segment is cfg.push_ssh_user.
    cfg = _fake_cfg()
    payload = _dest_payload()  # dest_ssh_user defaults None
    argv = push._build_rsync_argv(cfg, payload, key_path="/k", known_hosts_path="/kh")

    assert argv[-1].startswith("bursty@")


def test_rsync_argv_dest_ssh_user_set_overrides_cfg_user() -> None:
    # A non-None dest_ssh_user is used verbatim — cfg.push_ssh_user is never consulted.
    cfg = _fake_cfg()
    payload = _dest_payload(dest_ssh_user="oci-user")
    argv = push._build_rsync_argv(cfg, payload, key_path="/k", known_hosts_path="/kh")

    assert argv[-1].startswith("oci-user@")
    assert "bursty@" not in argv[-1]


def test_rsync_argv_does_not_leak_cfg_remote_target() -> None:
    # D-04: the retired remote-target globals (cfg.push_ssh_host + cfg.cloud_scratch_dir) never
    # appear in the produced argv when the payload carries a different destination.
    cfg = _fake_cfg()
    payload = _dest_payload(dest_host="oci-a1.push.example", dest_scratch_dir="/srv/other")
    argv = push._build_rsync_argv(cfg, payload, key_path="/k", known_hosts_path="/kh")

    joined = " ".join(argv)
    assert cfg.push_ssh_host not in joined
    assert cfg.cloud_scratch_dir not in joined
    # The argv terminator + source ordering is preserved (argv-injection defense).
    assert argv[-3] == "--"
    assert argv[-2] == payload.original_path
    assert argv[-1].endswith(f"/{payload.file_id}.flac")


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
# WR-03 — SAQ cancellation reaps the rsync child before shredding the key
# ----------------------------------------------------------------------


async def test_saq_cancellation_reaps_child_before_secret_shred(monkeypatch: pytest.MonkeyPatch) -> None:
    # WR-03: a SAQ job-net timeout cancels the coroutine via CancelledError (NOT TimeoutError). The
    # task must still kill the rsync child so no live ``ssh -i`` reads the key/known_hosts the
    # finally is about to shred, and no rsync child is orphaned.
    cfg = _fake_cfg()
    payload = _payload()
    monkeypatch.setattr(push, "_agent_settings", lambda: cfg)

    proc = _FakeProc(returncode=0)

    async def _cancelled_communicate() -> tuple[bytes, bytes]:
        raise asyncio.CancelledError

    proc.communicate = _cancelled_communicate  # type: ignore[method-assign]

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return proc

    monkeypatch.setattr(push.asyncio, "create_subprocess_exec", _fake_exec)
    api = _FakeApi()

    with pytest.raises(asyncio.CancelledError):
        await push.push_file({"api_client": api}, **payload.model_dump(mode="json"))

    assert proc.killed is True, "the rsync child must be reaped on a SAQ cancellation"
    assert api.pushed == [], "no success callback fires on a cancelled push"


def test_push_file_saq_timeout_above_asyncio_outer_guard() -> None:
    # WR-03: the SAQ job-net timeout MUST sit strictly above the asyncio outer guard
    # (push_timeout_sec + buffer) for the documented default so a job-net cancellation never
    # pre-empts the guard that reaps the child. Layering: rsync(600) < asyncio outer(630) < SAQ net.
    default_push_timeout = 600
    asyncio_outer = default_push_timeout + push._OUTER_TIMEOUT_BUFFER_SEC
    assert asyncio_outer < push.PUSH_FILE_SAQ_TIMEOUT_SEC


def test_push_budget_scales_with_file_size_not_a_fixed_cap() -> None:
    # phaze-2qpn: the outer wall-clock guard is size-derived, so a healthy long transfer is NOT
    # killed by a fixed ~630s cap. A 4 GB file gets a budget far above the small-file floor.
    small = push.push_transfer_budget_sec(1000, io_stall_timeout_sec=600)
    huge = push.push_transfer_budget_sec(4 * 1024**3, io_stall_timeout_sec=600)
    # Small file floors at push_timeout_sec + buffer (historical ~630s).
    assert small == 600 + push._OUTER_TIMEOUT_BUFFER_SEC
    # 4 GB / ~8 Mbps floor is thousands of seconds -- far above the old fixed cap.
    assert huge > 3600
    assert huge > small
    # The SAQ net sits strictly above the outer guard by the fixed margin, at every size.
    assert push.push_file_saq_timeout_sec(4 * 1024**3) == huge + push._SAQ_JOB_TIMEOUT_MARGIN_SEC
    # Small-file SAQ net equals the retained floor baseline constant.
    assert push.push_file_saq_timeout_sec(0) == push.PUSH_FILE_SAQ_TIMEOUT_SEC


async def test_push_file_large_source_gets_scaled_outer_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # phaze-2qpn: push_file stats the source and derives the outer guard from its size. Assert the
    # timeout passed to asyncio.wait_for scales with the (faked) large file size, not a fixed ~630s cap.
    payload = _payload()
    monkeypatch.setattr(push, "_agent_settings", lambda: _fake_cfg())

    captured: dict[str, float] = {}

    async def _fake_wait_for(awaitable: Any, timeout: float) -> Any:  # type: ignore[no-untyped-def]
        captured["timeout"] = timeout
        return await awaitable

    async def _fake_exec(*_args: Any, **_kwargs: Any) -> _FakeProc:
        return _FakeProc(returncode=0)

    def _fake_stat(_self: Path, *_a: Any, **_k: Any) -> Any:  # type: ignore[no-untyped-def]
        return SimpleNamespace(st_size=4 * 1024**3)  # pretend the source is 4 GB

    monkeypatch.setattr(push.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(push.asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(Path, "stat", _fake_stat)

    api = _FakeApi()
    await push.push_file({"api_client": api}, **payload.model_dump(mode="json"))

    assert captured["timeout"] == push.push_transfer_budget_sec(4 * 1024**3, io_stall_timeout_sec=600)
    assert captured["timeout"] > 3600  # a healthy multi-GB push is NOT capped at ~630s


def test_require_push_config_passes_at_default_timeout() -> None:
    # WR-03 guard: the documented default (push_timeout_sec=600) keeps the layering valid, so the
    # fail-fast guard stays silent on a correctly-configured agent.
    push._require_push_config(_fake_cfg())  # must NOT raise


def test_require_push_config_rejects_inverted_timeout_layering() -> None:
    # WR-03 guard: raising PHAZE_PUSH_TIMEOUT_SEC on the agent without bumping the control-side SAQ
    # net inverts the layering (outer guard >= SAQ net). The agent must fail fast at its first push
    # with a clear error instead of silently letting SAQ cancel healthy long transfers.
    too_high = push.PUSH_FILE_SAQ_TIMEOUT_SEC  # outer = too_high + buffer >> SAQ net
    with pytest.raises(RuntimeError, match="timeout layering inverted"):
        push._require_push_config(_fake_cfg(push_timeout_sec=too_high))


def test_require_push_config_rejects_exact_boundary() -> None:
    # The constraint is STRICT (<). push_timeout_sec + buffer == SAQ net must also be rejected, since
    # equal timeouts race nondeterministically.
    boundary = push.PUSH_FILE_SAQ_TIMEOUT_SEC - push._OUTER_TIMEOUT_BUFFER_SEC
    with pytest.raises(RuntimeError, match="timeout layering inverted"):
        push._require_push_config(_fake_cfg(push_timeout_sec=boundary))


def test_require_push_config_no_longer_requires_retired_remote_target() -> None:
    # D-04: push_ssh_host + cloud_scratch_dir are payload-carried now (the fileserver's remote-target
    # env is retired), so their absence must NOT raise as long as the secrets + fallback user are set.
    cfg = _fake_cfg(push_ssh_host=None, cloud_scratch_dir=None)
    push._require_push_config(cfg)  # must NOT raise


@pytest.mark.parametrize("missing_field", ["push_ssh_user", "push_ssh_key", "push_known_hosts"])
def test_require_push_config_still_requires_secret_material_and_fallback_user(missing_field: str) -> None:
    # D-03: the SSH secret material (push_ssh_key + push_known_hosts) AND the dest_ssh_user None-fallback
    # source (push_ssh_user) stay required — dropping any of them still fails fast.
    with pytest.raises(RuntimeError, match="missing required push config"):
        push._require_push_config(_fake_cfg(**{missing_field: None}))


def test_require_push_config_rejects_empty_string_ssh_user() -> None:
    # IN-01: an operator-set empty string (PHAZE_PUSH_SSH_USER="") must fail fast the same as None —
    # otherwise it falls through as the `dest_ssh_user or cfg.push_ssh_user` source and builds a
    # broken "@host:..." remote spec.
    with pytest.raises(RuntimeError, match="missing required push config"):
        push._require_push_config(_fake_cfg(push_ssh_user=""))


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
