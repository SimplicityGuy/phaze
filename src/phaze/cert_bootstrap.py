"""Pre-uvicorn cert bootstrap (Phase 29 D-02).

Generates a self-signed CA + leaf certificate pair into ``/certs/`` on first
startup of the application server. Idempotent: existing parseable certs are
left untouched.

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
    - Private keys serialized as PKCS8 / NoEncryption (bind-mount is 0600).
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


if TYPE_CHECKING:
    from pathlib import Path


logger = logging.getLogger(__name__)


_BANNER = """\
==============================================================
GENERATED NEW PHAZE INTERNAL CA at {ca_path}
COPY THIS FILE TO EVERY FILE SERVER and point each agent's
PHAZE_AGENT_CA_FILE env var at it. EXISTING AGENTS WILL FAIL
TO CONNECT UNTIL THEY HAVE THIS NEW CA.
=============================================================="""


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


def ensure_certs_present(certs_dir: Path, cn: str, sans_csv: str) -> None:
    """Idempotent CA + leaf bootstrap.

    Generates a fresh CA + leaf pair ONLY if the four target files are
    missing or unparseable. Returns immediately (no-op) when an existing
    pair parses cleanly.

    File modes (RESEARCH Pattern 1 + CONTEXT.md specifics):
        - ``phaze-ca.crt``        0644  (public; distributed to agents)
        - ``phaze-ca.key``        0600  (private CA signing key; never leaves app server)
        - ``phaze-server.crt``    0644
        - ``phaze-server.key``    0600

    On actual generation the loud banner is emitted via BOTH ``print()``
    (stdout, ``# noqa: T201`` intentional) AND ``logger.warning()``
    (CONTEXT D-02 D-discretion "Both"; verified by tests 3 + 7).
    """
    certs_dir.mkdir(parents=True, exist_ok=True)
    ca_crt = certs_dir / "phaze-ca.crt"
    ca_key_path = certs_dir / "phaze-ca.key"
    server_crt = certs_dir / "phaze-server.crt"
    server_key = certs_dir / "phaze-server.key"

    # Idempotency: all four exist AND CA + leaf parse cleanly.
    if all(p.exists() for p in (ca_crt, ca_key_path, server_crt, server_key)):
        try:
            x509.load_pem_x509_certificate(ca_crt.read_bytes())
            x509.load_pem_x509_certificate(server_crt.read_bytes())
        except ValueError:
            logger.warning("cert_bootstrap: existing certs unparseable; regenerating")
        else:
            logger.info("cert_bootstrap: existing certs at %s -- no-op", certs_dir)
            return

    sans = _parse_san_entries(sans_csv)
    ca_key, ca_cert = _generate_ca(cn=f"Phaze Internal CA ({cn})")
    leaf_key, leaf_cert = _generate_leaf(ca_key, ca_cert, cn=cn, sans=sans)

    # Write CA cert + private key.
    ca_crt.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    ca_crt.chmod(0o644)
    ca_key_path.write_bytes(
        ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    ca_key_path.chmod(0o600)

    # Write leaf cert + private key.
    server_crt.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    server_crt.chmod(0o644)
    server_key.write_bytes(
        leaf_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    server_key.chmod(0o600)

    # Banner emitted via BOTH channels (CONTEXT D-02 D-discretion "Both").
    banner = _BANNER.format(ca_path=ca_crt)
    print(banner, flush=True)  # noqa: T201  # intentional operator-facing stdout banner
    for line in banner.splitlines():
        logger.warning(line)
