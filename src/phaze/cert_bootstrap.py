"""Pre-uvicorn cert bootstrap (Phase 29 D-02, extended by issue #247 / phaze-0gu).

Generates a self-signed CA + leaf certificate pair into ``/certs/`` on first
startup of the application server. The CA is precious and long-lived (10-year
validity, distributed out-of-band to every TLS client that trusts it): it is
generated ONLY when absent or unparseable.

The leaf is cheap and short-lived by comparison. On every start, once the CA
is confirmed present, the leaf is re-issued (signed by the EXISTING CA -- it
is never touched) whenever it is missing, unparseable, near its expiry, or its
SubjectAlternativeName set no longer matches the desired SANs computed from
``PHAZE_API_TLS_SANS``. This means a SAN config change takes effect on the
next restart with zero CA redistribution, and a deleted/corrupt leaf never
silently mints a brand-new CA (issue #247's two foot-guns).

IMPORT-BOUNDARY INVARIANT (extends Phase 26 D-25 + Phase 27 D-22):
    This module MUST NOT import ``phaze.database``, ``phaze.tasks.session``,
    or ``sqlalchemy.ext.asyncio``. Verified in CI by
    ``tests/test_task_split.py::test_cert_bootstrap_stays_postgres_free``.

    The bootstrap runs in the api container's pre-uvicorn entrypoint
    (RESEARCH Pattern 2), which boots BEFORE the FastAPI lifespan opens
    the database connection pool. Dragging in Postgres at this layer
    would either (a) fail because the DB isn't reachable yet, or
    (b) succeed but mask the boundary that allows the agent role to run
    on hosts without Postgres reachability.

Banner discretion (CONTEXT D-02 "Both"):
    On actual generation (not on the no-op idempotent path), the loud
    "GENERATED NEW PHAZE INTERNAL CA" banner is emitted via BOTH
    ``print()`` AND ``logger.warning()``. ``print()`` surfaces in
    interactive ``docker compose up`` output; ``logger.warning()`` lands
    in ``docker compose logs api`` and any aggregated log sink. The
    banner is a LITERAL CONSTANT that references ONLY the public CA cert
    path (``phaze-ca.crt``); the private key path is never templated
    into the banner (RESEARCH Pitfall 4 -- private-key-leak guard).

Algorithm (RESEARCH Pattern 1):
    - ECDSA P-256 keys (CONTEXT D-discretion: faster + smaller than RSA-3072)
    - CA: 10-year validity, CN ``"Phaze Internal CA (<cn>)"``,
      BasicConstraints(ca=True) critical, KeyUsage(key_cert_sign+crl_sign+digital_signature) critical.
    - Leaf: 2-year validity, SubjectAlternativeName from sans_csv,
      BasicConstraints(ca=False) critical, KeyUsage(digital_signature+key_encipherment) critical.
    - Private keys serialized as PKCS8 / NoEncryption: this relies on the KEY
      FILE ITSELF being written 0600 from the first byte (``_write_private_key_file``,
      phaze-d39i) plus the containing ``certs_dir`` being created 0700, not on
      any assumption about the host-side bind-mount's own permissions.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
from typing import TYPE_CHECKING, NoReturn

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
import structlog


if TYPE_CHECKING:
    from pathlib import Path


logger = structlog.get_logger(__name__)


_BANNER = """\
==============================================================
GENERATED NEW PHAZE INTERNAL CA at {ca_path}
COPY THIS FILE TO EVERY FILE SERVER and point each agent's
PHAZE_AGENT_CA_FILE env var at it. EXISTING AGENTS WILL FAIL
TO CONNECT UNTIL THEY HAVE THIS NEW CA.
=============================================================="""

# Leaves are re-issued (from the existing CA) once they are within this many
# days of their `not_valid_after`. 730-day leaf validity / 30-day threshold
# gives ample lead time across restart/redeploy cadences without churning
# certs on every boot.
_LEAF_RENEWAL_THRESHOLD = datetime.timedelta(days=30)


def _parse_san_entries(sans_csv: str) -> list[x509.GeneralName]:
    """Parse comma-separated SAN list: DNSName for hostnames, IPAddress for IPs.

    Empty / whitespace-only entries are skipped silently. The dispatch is
    via ``ipaddress.ip_address`` -- if the parse succeeds the value becomes
    an ``x509.IPAddress``; otherwise it falls back to ``x509.DNSName``.
    """
    entries: list[x509.GeneralName] = []
    for raw in (s.strip() for s in sans_csv.split(",") if s.strip()):
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(raw)))
        except ValueError:
            entries.append(x509.DNSName(raw))
    return entries


def _generate_ca(cn: str) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """Generate a self-signed CA (ECDSA P-256, 10-year validity)."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.UTC)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        # SubjectKeyIdentifier on the CA is the key that leaves below reference
        # via AuthorityKeyIdentifier. Python 3.13's ssl module rejects the
        # validation chain with "Missing Authority Key Identifier" if either
        # extension is missing -- see test_correct_ca_succeeds.
        .add_extension(ski, critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _generate_leaf(
    ca_key: ec.EllipticCurvePrivateKey,
    ca_cert: x509.Certificate,
    cn: str,
    sans: list[x509.GeneralName],
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """Generate a CA-signed leaf cert (ECDSA P-256, 2-year validity)."""
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    # AuthorityKeyIdentifier ties the leaf to the CA's SubjectKeyIdentifier --
    # required by Python 3.13's TLS verification path (test_correct_ca_succeeds
    # fails with "Missing Authority Key Identifier" without these extensions).
    aki = x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key())
    ski = x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=730))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(aki, critical=False)
        .add_extension(ski, critical=False)
        # ExtendedKeyUsage: server-auth required by Python 3.13's strict TLS
        # validation. Without it, the leaf is recognized as a generic cert
        # but rejected when presented to a TLS client expecting a server cert.
        .add_extension(
            x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return leaf_key, leaf_cert


def _san_entry_key(entry: x509.GeneralName) -> tuple[str, str]:
    """Normalize one SAN GeneralName into an order-independent, comparable key."""
    if isinstance(entry, x509.IPAddress):
        return ("ip", str(entry.value))
    if isinstance(entry, x509.DNSName):
        return ("dns", entry.value)
    return (type(entry).__name__, str(entry.value))  # pragma: no cover  # _parse_san_entries only emits DNS/IP


def _san_set(entries: list[x509.GeneralName]) -> frozenset[tuple[str, str]]:
    """Order-independent comparison key for a SAN entry list."""
    return frozenset(_san_entry_key(entry) for entry in entries)


def _write_private_key_file(path: Path, data: bytes) -> None:
    """Write private-key bytes to ``path`` at mode 0600 from the FIRST byte (phaze-d39i).

    ``Path.write_bytes`` creates the file at 0o666 masked by the process umask
    (typically 0644, world-readable) and only a SUBSEQUENT ``chmod`` narrows it,
    leaving a window -- on a host bind mount -- where the private key is
    world-readable, and a permanent 0644 file if the process dies in that
    window. ``os.open`` with an explicit ``mode`` applies it atomically at
    creation time (still subject to umask for bits it doesn't request, but
    0o600 requests no group/other bits at all, so there is nothing for the
    umask to widen).
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _write_ca(ca_crt: Path, ca_key_path: Path, ca_key: ec.EllipticCurvePrivateKey, ca_cert: x509.Certificate) -> None:
    """Write the CA cert (0644) + private key (0600, from birth) to disk."""
    ca_crt.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    ca_crt.chmod(0o644)
    _write_private_key_file(
        ca_key_path,
        ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def _write_leaf(server_crt: Path, server_key: Path, leaf_key: ec.EllipticCurvePrivateKey, leaf_cert: x509.Certificate) -> None:
    """Write the leaf cert (0644) + private key (0600, from birth) to disk."""
    server_crt.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    server_crt.chmod(0o644)
    _write_private_key_file(
        server_key,
        leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )


def _load_ca(ca_crt: Path, ca_key_path: Path) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate] | None:
    """Load an existing CA key + cert pair from disk.

    Returns ``None`` when either file is missing, unparseable, or the key is
    not the expected EC key type -- any of which means there is no usable CA
    to sign a leaf with, so the caller must bootstrap a fresh CA (+ leaf).
    """
    if not (ca_crt.exists() and ca_key_path.exists()):
        return None
    try:
        ca_cert = x509.load_pem_x509_certificate(ca_crt.read_bytes())
        ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    except ValueError:
        return None
    if not isinstance(ca_key, ec.EllipticCurvePrivateKey):
        return None  # pragma: no cover  # defensive -- this module only ever writes EC keys
    return ca_key, ca_cert


def _leaf_needs_reissue(server_crt: Path, server_key: Path, desired_sans: list[x509.GeneralName]) -> bool:
    """True when the leaf must be (re-)issued from the existing CA.

    Covers all re-issue triggers from the acceptance criteria plus the
    private-key half of the pair: missing cert, missing key, either
    unparseable, cert/key public-key mismatch (a corrupt or swapped key would
    otherwise leave uvicorn unable to load the keypair even though the cert
    alone looks fine), near/past expiry, or a SAN set that no longer matches
    ``desired_sans`` (order-independent).
    """
    if not server_crt.exists() or not server_key.exists():
        return True
    try:
        leaf = x509.load_pem_x509_certificate(server_crt.read_bytes())
    except ValueError:
        return True
    try:
        leaf_key = serialization.load_pem_private_key(server_key.read_bytes(), password=None)
    except ValueError:
        return True
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    cert_pub_der = leaf.public_key().public_bytes(encoding=serialization.Encoding.DER, format=spki)
    key_pub_der = leaf_key.public_key().public_bytes(encoding=serialization.Encoding.DER, format=spki)
    if cert_pub_der != key_pub_der:
        return True
    now = datetime.datetime.now(datetime.UTC)
    if leaf.not_valid_after_utc - now <= _LEAF_RENEWAL_THRESHOLD:
        return True
    try:
        existing_sans = list(leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
    except x509.ExtensionNotFound:
        return True
    return _san_set(existing_sans) != _san_set(desired_sans)


def _reraise_actionable_permission_error(certs_dir: Path, exc: PermissionError) -> NoReturn:
    """Translate a raw EACCES on the certs dir into an operator-actionable message (phaze-he8m).

    The api image runs as the fixed non-root ``phaze`` user (uid/gid 1000) and
    bind-mounts ``./certs:/certs:rw``. On a rootful Linux docker engine a MISSING
    bind-mount source dir is auto-created by dockerd as ``root:root`` mode 755, so
    the uid-1000 cert bootstrap cannot write ``phaze-ca.crt`` into it and dies with
    a bare ``PermissionError`` before uvicorn ever binds — an opaque crash-loop.
    Re-raise with the host-side fix spelled out.
    """
    raise PermissionError(
        f"cert_bootstrap: permission denied writing into {certs_dir} ({exc}). This directory is almost "
        f"certainly owned by root — rootful dockerd auto-creates a MISSING bind-mount source dir as "
        f"root:root, but the api container runs as uid 1000 (the 'phaze' user). Fix on the HOST and "
        f"restart: `mkdir -p certs && sudo chown -R 1000:1000 certs` (or just `just up`, which now "
        f"pre-creates ./certs owned by the invoking operator; a fresh `git clone` also materializes "
        f"./certs via its committed .gitkeep)."
    ) from exc


def ensure_certs_present(certs_dir: Path, cn: str, sans_csv: str) -> None:
    """Idempotent CA bootstrap + SAN/expiry-aware leaf re-issue.

    Behavior (issue #247 / phaze-0gu):
        - CA (``phaze-ca.{crt,key}``) is generated ONLY when absent or
          unparseable. It is never regenerated once present -- a missing or
          stale leaf never mints a new CA.
        - Once a usable CA is confirmed, the leaf (``phaze-server.{crt,key}``)
          is re-issued -- signed by that SAME existing CA -- whenever either
          the cert or the key is missing/unparseable, the key's public key
          does not match the cert's, the cert is within
          ``_LEAF_RENEWAL_THRESHOLD`` of expiry, or its SAN set differs from
          the desired set computed from ``sans_csv``. Otherwise this is a
          no-op.

    File modes (RESEARCH Pattern 1 + CONTEXT.md specifics; dir mode + key
    create-mode hardened by phaze-d39i):
        - ``certs_dir``           0700  (created from birth; other same-uid-1000
                                    sidecar containers still traverse it fine,
                                    see docker-compose*.yml)
        - ``phaze-ca.crt``        0644  (public; distributed to agents)
        - ``phaze-ca.key``        0600  (private CA signing key, from birth;
                                    never leaves app server)
        - ``phaze-server.crt``    0644
        - ``phaze-server.key``    0600  (from birth)

    On actual CA generation the loud banner is emitted via BOTH ``print()``
    (stdout, ``# noqa: T201`` intentional) AND ``logger.warning()``
    (CONTEXT D-02 D-discretion "Both"; verified by tests 3 + 7). The banner is
    CA-specific (operators must redistribute the CA) and is intentionally NOT
    emitted on a leaf-only re-issue, since the CA -- the thing every remote
    client trusts -- did not change.
    """
    try:
        certs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except PermissionError as exc:
        _reraise_actionable_permission_error(certs_dir, exc)
    ca_crt = certs_dir / "phaze-ca.crt"
    ca_key_path = certs_dir / "phaze-ca.key"
    server_crt = certs_dir / "phaze-server.crt"
    server_key = certs_dir / "phaze-server.key"

    sans = _parse_san_entries(sans_csv)

    ca_files_exist = ca_crt.exists() and ca_key_path.exists()
    existing_ca = _load_ca(ca_crt, ca_key_path)

    if existing_ca is None:
        if ca_files_exist:
            logger.warning("cert_bootstrap: existing certs unparseable; regenerating")
        ca_key, ca_cert = _generate_ca(cn=f"Phaze Internal CA ({cn})")
        leaf_key, leaf_cert = _generate_leaf(ca_key, ca_cert, cn=cn, sans=sans)
        try:
            _write_ca(ca_crt, ca_key_path, ca_key, ca_cert)
            _write_leaf(server_crt, server_key, leaf_key, leaf_cert)
        except PermissionError as exc:
            _reraise_actionable_permission_error(certs_dir, exc)

        # Banner emitted via BOTH channels (CONTEXT D-02 D-discretion "Both").
        banner = _BANNER.format(ca_path=ca_crt)
        print(banner, flush=True)  # noqa: T201  # intentional operator-facing stdout banner
        for line in banner.splitlines():
            logger.warning(line)
        return

    ca_key, ca_cert = existing_ca
    if not _leaf_needs_reissue(server_crt, server_key, sans):
        logger.info("cert_bootstrap: existing certs at %s -- no-op", certs_dir)
        return

    logger.info("cert_bootstrap: re-issuing leaf from existing CA at %s (SANs/expiry changed); CA untouched", certs_dir)
    leaf_key, leaf_cert = _generate_leaf(ca_key, ca_cert, cn=cn, sans=sans)
    try:
        _write_leaf(server_crt, server_key, leaf_key, leaf_cert)
    except PermissionError as exc:
        _reraise_actionable_permission_error(certs_dir, exc)
