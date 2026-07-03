"""Tests for `phaze.entrypoint.main` (Phase 29 D-02, RESEARCH Pattern 2).

Three LOCKED cases:
1. Defaults: with no env vars set, `main()` calls `ensure_certs_present`
   with `Path("/certs"), cn="localhost", sans_csv="localhost,127.0.0.1,api"`
   and then `execvp`s uvicorn with the expected argv (host 0.0.0.0, port 8000,
   ssl-keyfile `/certs/phaze-server.key`, ssl-certfile `/certs/phaze-server.crt`).
2. Overrides: PHAZE_CERTS_DIR / PHAZE_API_HOST / PHAZE_API_TLS_SANS values
   are threaded through `ensure_certs_present` and the `--ssl-*` flags.
3. Sequencing: `ensure_certs_present` MUST run BEFORE `os.execvp`
   (RESEARCH Pattern 2 invariant — cert files must exist when uvicorn boots).

`os.execvp` is monkeypatched to a recording stub so the test process is not
replaced. `ensure_certs_present` is monkeypatched to a recording stub so
the test does not depend on `cryptography` round-trip behavior — that is
already covered by `tests/test_cert_bootstrap.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import phaze.entrypoint as entrypoint


if TYPE_CHECKING:
    import pytest


def _install_recorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub `ensure_certs_present` and `os.execvp`; return the recorder dict."""
    calls: dict[str, Any] = {"ensure": None, "execvp": None, "order": []}

    def fake_ensure(certs_dir: Path, cn: str, sans_csv: str) -> None:
        calls["ensure"] = (certs_dir, cn, sans_csv)
        calls["order"].append("ensure")

    def fake_execvp(file: str, args: list[str]) -> None:
        calls["execvp"] = (file, args)
        calls["order"].append("execvp")

    monkeypatch.setattr(entrypoint, "ensure_certs_present", fake_ensure)
    monkeypatch.setattr(entrypoint.os, "execvp", fake_execvp)
    return calls


def test_main_defaults_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 1: with no PHAZE_* env vars, the docstring defaults are used end-to-end."""
    monkeypatch.delenv("PHAZE_CERTS_DIR", raising=False)
    monkeypatch.delenv("PHAZE_API_HOST", raising=False)
    monkeypatch.delenv("PHAZE_API_TLS_SANS", raising=False)
    calls = _install_recorder(monkeypatch)

    entrypoint.main()

    assert calls["ensure"] == (Path("/certs"), "localhost", "localhost,127.0.0.1,api")
    file_arg, argv = calls["execvp"]
    assert file_arg == "uv"
    assert argv[:4] == ["uv", "run", "uvicorn", "phaze.main:app"]
    # Required flags + default-derived cert paths under /certs/.
    assert "--host" in argv and "0.0.0.0" in argv  # noqa: S104  # nosec B104  # asserting on entrypoint's container-bind flag
    assert "--port" in argv and "8000" in argv
    assert "--ssl-keyfile" in argv
    assert argv[argv.index("--ssl-keyfile") + 1] == "/certs/phaze-server.key"
    assert "--ssl-certfile" in argv
    assert argv[argv.index("--ssl-certfile") + 1] == "/certs/phaze-server.crt"


def test_main_honors_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test 2: PHAZE_CERTS_DIR / PHAZE_API_HOST / PHAZE_API_TLS_SANS flow through."""
    monkeypatch.setenv("PHAZE_CERTS_DIR", str(tmp_path))
    monkeypatch.setenv("PHAZE_API_HOST", "phaze.lan")
    monkeypatch.setenv("PHAZE_API_TLS_SANS", "phaze.lan,10.0.0.5")
    calls = _install_recorder(monkeypatch)

    entrypoint.main()

    # ensure_certs_present sees the operator-supplied values.
    assert calls["ensure"] == (tmp_path, "phaze.lan", "phaze.lan,10.0.0.5")
    # execvp's --ssl-* flags point at the operator-supplied certs dir.
    _file, argv = calls["execvp"]
    assert argv[argv.index("--ssl-keyfile") + 1] == str(tmp_path / "phaze-server.key")
    assert argv[argv.index("--ssl-certfile") + 1] == str(tmp_path / "phaze-server.crt")


def test_main_runs_ensure_before_execvp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 3 (sequencing invariant): ensure_certs_present must run BEFORE execvp.

    If execvp ran first, uvicorn would boot against the still-empty /certs/
    bind mount and crash on missing --ssl-keyfile / --ssl-certfile paths.
    """
    monkeypatch.delenv("PHAZE_CERTS_DIR", raising=False)
    monkeypatch.delenv("PHAZE_API_HOST", raising=False)
    monkeypatch.delenv("PHAZE_API_TLS_SANS", raising=False)
    calls = _install_recorder(monkeypatch)

    entrypoint.main()

    assert calls["order"] == ["ensure", "execvp"]
