"""Tests for phaze.cert_bootstrap (Phase 29 D-02, D-22; extended by issue #247 / phaze-0gu).

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

Plus the phaze-0gu / issue #247 SAN-diff + near-expiry leaf re-issue cases,
each asserting the CA fingerprint AND the CA key file bytes are unchanged
(the CA must never be touched by a leaf-only re-issue).
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
from typing import TYPE_CHECKING

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import pytest

from phaze.cert_bootstrap import _parse_san_entries, _write_private_key_file, ensure_certs_present


if TYPE_CHECKING:
    from pathlib import Path


_DEFAULT_SANS = "localhost,127.0.0.1,api"


def _load_ca_cert(certs_dir: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate((certs_dir / "phaze-ca.crt").read_bytes())


def _ca_fingerprint(certs_dir: Path) -> bytes:
    return _load_ca_cert(certs_dir).fingerprint(hashes.SHA256())


def _assert_signed_by_ca(leaf: x509.Certificate, ca_cert: x509.Certificate) -> None:
    """Assert `leaf` verifies against `ca_cert`'s (EC) public key -- proof it's signed by this exact CA."""
    ca_public_key = ca_cert.public_key()
    assert isinstance(ca_public_key, ec.EllipticCurvePublicKey)
    assert leaf.signature_hash_algorithm is not None
    ca_public_key.verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(leaf.signature_hash_algorithm))


def _write_leaf_with_sans_and_expiry(
    certs_dir: Path,
    sans: list[x509.GeneralName],
    not_valid_after: datetime.datetime,
) -> None:
    """Overwrite phaze-server.{crt,key} with a leaf signed by the on-disk CA.

    Used to synthesize "stale" leaves (wrong SANs / near-expiry) against a
    real, already-bootstrapped CA, independent of `cert_bootstrap`'s own
    `_generate_leaf` so the test exercises `ensure_certs_present` through its
    public surface only.
    """
    ca_cert = _load_ca_cert(certs_dir)
    ca_key = serialization.load_pem_private_key((certs_dir / "phaze-ca.key").read_bytes(), password=None)
    assert isinstance(ca_key, ec.EllipticCurvePrivateKey)

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(not_valid_after)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    (certs_dir / "phaze-server.crt").write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    (certs_dir / "phaze-server.key").write_bytes(
        leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


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


def test_private_key_file_is_0600_from_birth_not_via_later_chmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-d39i: `_write_private_key_file` must create the file at mode 0600 via `os.open`'s
    own `mode` argument, not `write_bytes()` (0o666 & ~umask, typically 0644) followed by a
    later `.chmod(0o600)` -- the create-then-chmod pattern that leaves the private key
    world-readable for a window on a host bind mount (and permanently so if the process dies
    in that window). Assert both the final mode AND that `Path.chmod` is never invoked by the
    helper, so a regression back to write-then-chmod is caught even if the end mode happens to
    match."""
    from pathlib import Path as _Path

    chmod_calls: list[tuple[_Path, int]] = []
    real_chmod = _Path.chmod

    def _tracking_chmod(self: _Path, mode: int, **kwargs: object) -> None:
        chmod_calls.append((self, mode))
        real_chmod(self, mode, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_Path, "chmod", _tracking_chmod)

    key_path = tmp_path / "some.key"
    _write_private_key_file(key_path, b"fake-private-key-bytes")

    assert key_path.stat().st_mode & 0o777 == 0o600
    assert key_path.read_bytes() == b"fake-private-key-bytes"
    assert chmod_calls == [], f"_write_private_key_file must not narrow permissions after the fact via chmod: {chmod_calls}"


def test_certs_dir_created_with_0700_mode(tmp_path: Path) -> None:
    """phaze-d39i: `ensure_certs_present` must create a brand-new certs dir at 0700, not the
    world-traversable 0755 default `Path.mkdir(parents=True, exist_ok=True)` produces -- the dir
    sits on a host bind mount that holds the CA private key."""
    certs_dir = tmp_path / "certs"
    ensure_certs_present(certs_dir, cn="localhost", sans_csv=_DEFAULT_SANS)
    assert certs_dir.stat().st_mode & 0o777 == 0o700


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


def test_unparseable_existing_certs_trigger_regeneration(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Test 8: when all 4 files exist but the CA cert (or leaf) does not parse,
    `ensure_certs_present` logs `existing certs unparseable; regenerating` and
    rewrites all four. Closes the WARNING-8 regeneration branch (lines 202-203)
    that the happy-path tests cannot reach."""
    # Pre-populate all 4 expected paths with garbage so `all(p.exists())` is
    # True, but `x509.load_pem_x509_certificate` raises ValueError.
    for name in ("phaze-ca.crt", "phaze-ca.key", "phaze-server.crt", "phaze-server.key"):
        (tmp_path / name).write_text("NOT-A-CERT")

    with caplog.at_level(logging.WARNING, logger="phaze.cert_bootstrap"):
        ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    # The regeneration warning fired.
    assert any(r.levelname == "WARNING" and "existing certs unparseable" in r.getMessage() for r in caplog.records), (
        f"Expected the 'unparseable; regenerating' warning; got: {[r.getMessage() for r in caplog.records]}"
    )

    # And the four files now parse cleanly.
    x509.load_pem_x509_certificate((tmp_path / "phaze-ca.crt").read_bytes())
    x509.load_pem_x509_certificate((tmp_path / "phaze-server.crt").read_bytes())
    serialization.load_pem_private_key((tmp_path / "phaze-ca.key").read_bytes(), password=None)
    serialization.load_pem_private_key((tmp_path / "phaze-server.key").read_bytes(), password=None)


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


# --- phaze-0gu / issue #247: SAN-diff + near-expiry leaf re-issue, CA preserved -------------


def test_leaf_reissued_when_sans_change_ca_preserved(tmp_path: Path) -> None:
    """Acceptance: changing PHAZE_API_TLS_SANS + restart re-issues the leaf with the new SANs;
    CA fingerprint (and key bytes) are unchanged."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    ca_fingerprint_before = _ca_fingerprint(tmp_path)
    ca_key_bytes_before = (tmp_path / "phaze-ca.key").read_bytes()
    old_leaf_bytes = (tmp_path / "phaze-server.crt").read_bytes()

    new_sans = "localhost,127.0.0.1,api,tailnet.example.ts.net,100.72.77.110"
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=new_sans)

    # CA is byte-for-byte untouched.
    assert _ca_fingerprint(tmp_path) == ca_fingerprint_before
    assert (tmp_path / "phaze-ca.key").read_bytes() == ca_key_bytes_before

    # Leaf changed and now carries the new SAN.
    new_leaf_bytes = (tmp_path / "phaze-server.crt").read_bytes()
    assert new_leaf_bytes != old_leaf_bytes
    leaf = x509.load_pem_x509_certificate(new_leaf_bytes)
    san_names = [n.value for n in leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value if isinstance(n, x509.DNSName)]
    assert "tailnet.example.ts.net" in san_names
    ip_names = [str(n.value) for n in leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value if isinstance(n, x509.IPAddress)]
    assert "100.72.77.110" in ip_names

    # And the re-issued leaf is signed by the SAME (unchanged) CA.
    _assert_signed_by_ca(leaf, _load_ca_cert(tmp_path))


def test_leaf_reissued_when_missing_ca_preserved(tmp_path: Path) -> None:
    """Acceptance: deleting only the leaf and restarting regenerates the leaf from the existing
    CA (CA fingerprint unchanged) -- never mints a new CA."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    ca_fingerprint_before = _ca_fingerprint(tmp_path)
    ca_key_bytes_before = (tmp_path / "phaze-ca.key").read_bytes()

    (tmp_path / "phaze-server.crt").unlink()
    (tmp_path / "phaze-server.key").unlink()

    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    assert _ca_fingerprint(tmp_path) == ca_fingerprint_before
    assert (tmp_path / "phaze-ca.key").read_bytes() == ca_key_bytes_before
    assert (tmp_path / "phaze-server.crt").exists()
    assert (tmp_path / "phaze-server.key").exists()
    x509.load_pem_x509_certificate((tmp_path / "phaze-server.crt").read_bytes())


def test_leaf_reissued_when_unparseable_ca_preserved(tmp_path: Path) -> None:
    """A corrupt leaf (CA intact) is re-issued from the existing CA; CA is untouched."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    ca_fingerprint_before = _ca_fingerprint(tmp_path)
    ca_key_bytes_before = (tmp_path / "phaze-ca.key").read_bytes()

    (tmp_path / "phaze-server.crt").write_text("NOT-A-CERT")

    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    assert _ca_fingerprint(tmp_path) == ca_fingerprint_before
    assert (tmp_path / "phaze-ca.key").read_bytes() == ca_key_bytes_before
    x509.load_pem_x509_certificate((tmp_path / "phaze-server.crt").read_bytes())


def test_leaf_reissued_when_near_expiry_ca_preserved(tmp_path: Path) -> None:
    """A leaf inside the renewal window (even with matching SANs) is re-issued; CA untouched."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    ca_fingerprint_before = _ca_fingerprint(tmp_path)
    ca_key_bytes_before = (tmp_path / "phaze-ca.key").read_bytes()

    sans = _parse_san_entries(_DEFAULT_SANS)
    near_expiry = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=10)
    _write_leaf_with_sans_and_expiry(tmp_path, sans, near_expiry)

    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    assert _ca_fingerprint(tmp_path) == ca_fingerprint_before
    assert (tmp_path / "phaze-ca.key").read_bytes() == ca_key_bytes_before
    leaf = x509.load_pem_x509_certificate((tmp_path / "phaze-server.crt").read_bytes())
    # Freshly issued leaf carries the full ~2y validity again, well past the renewal window.
    assert leaf.not_valid_after_utc - datetime.datetime.now(datetime.UTC) > datetime.timedelta(days=300)


def test_leaf_not_reissued_when_current_and_sans_match(tmp_path: Path) -> None:
    """No-op: an existing, current leaf whose SANs already match is left byte-for-byte alone."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    leaf_bytes_before = (tmp_path / "phaze-server.crt").read_bytes()
    leaf_key_bytes_before = (tmp_path / "phaze-server.key").read_bytes()
    ca_fingerprint_before = _ca_fingerprint(tmp_path)

    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    assert (tmp_path / "phaze-server.crt").read_bytes() == leaf_bytes_before
    assert (tmp_path / "phaze-server.key").read_bytes() == leaf_key_bytes_before
    assert _ca_fingerprint(tmp_path) == ca_fingerprint_before


def test_absent_ca_bootstraps_fresh_ca_and_leaf_even_with_stale_leaf_present(tmp_path: Path) -> None:
    """Acceptance: deleting the CA (or a fresh volume) still bootstraps a new CA + leaf as today
    -- even if a leftover leaf file is still present, its presence must not suppress CA generation."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    old_ca_fingerprint = _ca_fingerprint(tmp_path)

    # Simulate an operator deleting only the CA (leaf left behind).
    (tmp_path / "phaze-ca.crt").unlink()
    (tmp_path / "phaze-ca.key").unlink()

    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    new_ca_fingerprint = _ca_fingerprint(tmp_path)
    assert new_ca_fingerprint != old_ca_fingerprint
    for path in (tmp_path / "phaze-ca.crt", tmp_path / "phaze-ca.key", tmp_path / "phaze-server.crt", tmp_path / "phaze-server.key"):
        assert path.exists()
    x509.load_pem_x509_certificate((tmp_path / "phaze-server.crt").read_bytes())


def test_leaf_reissue_does_not_emit_new_ca_banner(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A leaf-only re-issue must NOT print/log the 'GENERATED NEW PHAZE INTERNAL CA' banner --
    that banner tells operators to redistribute the CA, and the CA did not change."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    capsys.readouterr()  # discard the first-call banner

    new_sans = f"{_DEFAULT_SANS},extra-host"
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=new_sans)

    captured = capsys.readouterr()
    assert "GENERATED NEW PHAZE INTERNAL CA" not in captured.out


def test_leaf_reissued_when_key_missing_but_cert_intact_ca_preserved(tmp_path: Path) -> None:
    """A healthy, current, correctly-SAN'd leaf CERT with a MISSING key must still be re-issued --
    otherwise uvicorn has no private key to pair with the (perfectly fine-looking) cert."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    ca_fingerprint_before = _ca_fingerprint(tmp_path)
    ca_key_bytes_before = (tmp_path / "phaze-ca.key").read_bytes()
    old_leaf_crt_bytes = (tmp_path / "phaze-server.crt").read_bytes()

    (tmp_path / "phaze-server.key").unlink()

    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    assert _ca_fingerprint(tmp_path) == ca_fingerprint_before
    assert (tmp_path / "phaze-ca.key").read_bytes() == ca_key_bytes_before
    assert (tmp_path / "phaze-server.key").exists()
    # The leaf cert itself was re-issued (fresh pair), not just re-paired with a stray key.
    new_leaf_crt_bytes = (tmp_path / "phaze-server.crt").read_bytes()
    assert new_leaf_crt_bytes != old_leaf_crt_bytes
    leaf = x509.load_pem_x509_certificate(new_leaf_crt_bytes)
    leaf_key = serialization.load_pem_private_key((tmp_path / "phaze-server.key").read_bytes(), password=None)
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    assert leaf.public_key().public_bytes(encoding=serialization.Encoding.DER, format=spki) == leaf_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER, format=spki
    )


def test_leaf_reissued_when_key_unparseable_ca_preserved(tmp_path: Path) -> None:
    """A garbage phaze-server.key (cert otherwise healthy) is re-issued; CA untouched."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    ca_fingerprint_before = _ca_fingerprint(tmp_path)
    ca_key_bytes_before = (tmp_path / "phaze-ca.key").read_bytes()

    (tmp_path / "phaze-server.key").write_text("NOT-A-KEY")

    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    assert _ca_fingerprint(tmp_path) == ca_fingerprint_before
    assert (tmp_path / "phaze-ca.key").read_bytes() == ca_key_bytes_before
    x509.load_pem_x509_certificate((tmp_path / "phaze-server.crt").read_bytes())
    serialization.load_pem_private_key((tmp_path / "phaze-server.key").read_bytes(), password=None)


def test_leaf_reissued_when_key_cert_mismatch_ca_preserved(tmp_path: Path) -> None:
    """A phaze-server.key that parses fine but does NOT match phaze-server.crt's public key
    (e.g. a stale/swapped key left over from an out-of-band operation) is re-issued; CA untouched.
    This is the exact gap the old all-four-files-exist check caught and the SAN/expiry-only
    rewrite regressed: a healthy-looking cert paired with a mismatched key would otherwise be
    treated as current, leaving uvicorn unable to load the keypair."""
    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)
    ca_fingerprint_before = _ca_fingerprint(tmp_path)
    ca_key_bytes_before = (tmp_path / "phaze-ca.key").read_bytes()
    old_leaf_crt_bytes = (tmp_path / "phaze-server.crt").read_bytes()

    # Swap in an unrelated, but perfectly valid, EC private key -- the cert is untouched, so
    # `_leaf_needs_reissue`'s cert-only checks (exists/parses/SANs/expiry) would all pass.
    mismatched_key = ec.generate_private_key(ec.SECP256R1())
    (tmp_path / "phaze-server.key").write_bytes(
        mismatched_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    assert _ca_fingerprint(tmp_path) == ca_fingerprint_before
    assert (tmp_path / "phaze-ca.key").read_bytes() == ca_key_bytes_before
    new_leaf_crt_bytes = (tmp_path / "phaze-server.crt").read_bytes()
    assert new_leaf_crt_bytes != old_leaf_crt_bytes

    leaf = x509.load_pem_x509_certificate(new_leaf_crt_bytes)
    leaf_key = serialization.load_pem_private_key((tmp_path / "phaze-server.key").read_bytes(), password=None)
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    assert leaf.public_key().public_bytes(encoding=serialization.Encoding.DER, format=spki) == leaf_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER, format=spki
    )


def test_permission_error_reraised_with_actionable_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-he8m: a raw EACCES writing into a root-owned certs dir is re-raised with the host fix.

    On a rootful Linux docker engine a MISSING bind-mount source dir is auto-created
    by the daemon as root:root, so the uid-1000 cert bootstrap cannot write
    phaze-ca.crt and dies with a bare PermissionError before uvicorn binds. The
    bootstrap now translates that into an operator-actionable message naming the
    uid-1000 ownership cause and the `chown` fix, instead of an opaque crash-loop.
    """
    import phaze.cert_bootstrap as cb

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    # Simulate the daemon-created root-owned dir: the write itself is denied.
    monkeypatch.setattr(cb, "_write_ca", _boom)

    with pytest.raises(PermissionError) as excinfo:
        ensure_certs_present(tmp_path, cn="localhost", sans_csv=_DEFAULT_SANS)

    message = str(excinfo.value)
    assert "uid 1000" in message, message
    assert "chown" in message, message
    assert str(tmp_path) in message, message
    # The original EACCES is chained for debuggability.
    assert isinstance(excinfo.value.__cause__, PermissionError)


def test_permission_error_on_mkdir_reraised_with_actionable_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """phaze-he8m: an EACCES creating the certs dir itself is also translated (parent root-owned)."""
    from pathlib import Path as _Path

    def _boom_mkdir(self: _Path, *args: object, **kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(_Path, "mkdir", _boom_mkdir)

    with pytest.raises(PermissionError) as excinfo:
        ensure_certs_present(tmp_path / "certs", cn="localhost", sans_csv=_DEFAULT_SANS)

    assert "chown" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, PermissionError)
