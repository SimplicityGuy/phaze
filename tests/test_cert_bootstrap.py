"""Tests for phaze.cert_bootstrap (Phase 29 D-02, D-22).

Verifies the 7 LOCKED test cases per Plan 29-01 task 1:
    1. First-call generates 4 files; all parse via x509 / serialization.
    2. Second call is a no-op (mtimes unchanged).
    3. Banner-via-stdout (capsys): contains "GENERATED NEW PHAZE INTERNAL CA";
       contains neither "BEGIN" nor "PRIVATE KEY" (Pitfall 4).
    4. File modes: 0o644 on certs; 0o600 on keys.
    5. Leaf SubjectAlternativeName entries match the sans_csv input.
    6. _parse_san_entries dispatches DNSName vs IPAddress correctly.
    7. WARNING-8: banner emitted via logger.warning() (caplog) -- both
       channels (print + logger) are mandatory per CONTEXT D-02 "Both".
"""

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from phaze.cert_bootstrap import _parse_san_entries, ensure_certs_present


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_DEFAULT_SANS = "localhost,127.0.0.1,api"


def test_first_call_generates_four_parseable_files(tmp_path: Path) -> None:
    """Test 1: first call writes 4 files; CA + leaf parse, both keys parse."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    ca_crt = tmp_path / "phaze-ca.crt"
    ca_key = tmp_path / "phaze-ca.key"
    server_crt = tmp_path / "phaze-server.crt"
    server_key = tmp_path / "phaze-server.key"

    for path in (ca_crt, ca_key, server_crt, server_key):
        assert path.exists(), f"missing file: {path.name}"

    # Both certs parse via cryptography.x509.
    x509.load_pem_x509_certificate(ca_crt.read_bytes())
    x509.load_pem_x509_certificate(server_crt.read_bytes())
    # Both keys parse via serialization.
    serialization.load_pem_private_key(ca_key.read_bytes(), password=None)
    serialization.load_pem_private_key(server_key.read_bytes(), password=None)


def test_second_call_is_noop_mtimes_unchanged(tmp_path: Path) -> None:
    """Test 2: second invocation on a populated dir does not change mtimes."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    files = [
        tmp_path / "phaze-ca.crt",
        tmp_path / "phaze-ca.key",
        tmp_path / "phaze-server.crt",
        tmp_path / "phaze-server.key",
    ]
    mtimes_before = {p: p.stat().st_mtime_ns for p in files}

    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    for path in files:
        assert path.stat().st_mtime_ns == mtimes_before[path], f"mtime changed on idempotent call: {path.name}"


def test_banner_stdout_contains_message_and_no_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Test 3: stdout banner contains the message; never leaks BEGIN or PRIVATE KEY (Pitfall 4)."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    captured = capsys.readouterr()
    assert "GENERATED NEW PHAZE INTERNAL CA" in captured.out
    assert "BEGIN" not in captured.out, f"banner leaked PEM marker on stdout: {captured.out!r}"
    assert "PRIVATE KEY" not in captured.out, f"banner leaked private-key string on stdout: {captured.out!r}"


def test_file_modes_are_correct(tmp_path: Path) -> None:
    """Test 4: certs are 0o644, keys are 0o600."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    assert (tmp_path / "phaze-ca.crt").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "phaze-server.crt").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "phaze-ca.key").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "phaze-server.key").stat().st_mode & 0o777 == 0o600


def test_leaf_san_entries_match_input(tmp_path: Path) -> None:
    """Test 5: leaf cert's SubjectAlternativeName contains the supplied SANs."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    leaf = x509.load_pem_x509_certificate((tmp_path / "phaze-server.crt").read_bytes())
    san_ext = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    san_value = san_ext.value
    # Default has 3 entries: localhost (DNS), 127.0.0.1 (IP), api (DNS).
    names: list[x509.GeneralName] = list(san_value)
    assert len(names) == 3, f"expected 3 SAN entries, got {len(names)}: {names!r}"

    dns_names = [n.value for n in names if isinstance(n, x509.DNSName)]
    ip_addrs = [str(n.value) for n in names if isinstance(n, x509.IPAddress)]
    assert "localhost" in dns_names
    assert "api" in dns_names
    assert "127.0.0.1" in ip_addrs


def test_parse_san_entries_mixed_dns_and_ip() -> None:
    """Test 6: _parse_san_entries dispatches DNSName for hostnames, IPAddress for IPs."""
    result = _parse_san_entries("localhost,127.0.0.1,api")
    assert len(result) == 3, f"expected 3 entries, got {len(result)}"
    assert isinstance(result[0], x509.DNSName) and result[0].value == "localhost"
    assert isinstance(result[1], x509.IPAddress) and result[1].value == ipaddress.IPv4Address("127.0.0.1")
    assert isinstance(result[2], x509.DNSName) and result[2].value == "api"


def test_banner_emitted_via_logger_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test 7 (WARNING-8): banner MUST be emitted via logger.warning() per CONTEXT D-02 D-discretion 'Both'.

    Test 3 (capsys) covers the print() path; this test covers the
    logger.warning() path independently -- a future refactor that drops
    one path while keeping the other would slip past Test 3 alone.
    """
    with caplog.at_level(logging.WARNING, logger="phaze.cert_bootstrap"):
        ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    banner_records = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and r.name == "phaze.cert_bootstrap" and "GENERATED NEW PHAZE INTERNAL CA" in r.getMessage()
    ]
    assert banner_records, (
        f"Expected at least one WARNING-level log record from phaze.cert_bootstrap "
        f"containing 'GENERATED NEW PHAZE INTERNAL CA'; got records: "
        f"{[(r.levelname, r.name, r.getMessage()) for r in caplog.records]}"
    )
    # Also assert the logger never leaks the private-key blob (parity with Test 3 for the print path):
    for r in banner_records:
        assert "BEGIN" not in r.getMessage(), f"banner record leaked PEM marker: {r.getMessage()}"
        assert "PRIVATE KEY" not in r.getMessage(), f"banner record leaked private-key string: {r.getMessage()}"
