"""Unit tests for inline ``*_file`` secret resolution on registry submodels (REG-03, D-04/D-06).

Per-entry registry secrets bind via inline mount-path ``*_file`` fields in ``backends.toml``
(``kubeconfig_file`` / ``sa_token_file`` / ``access_key_id_file`` / ``secret_access_key_file``) --
a DISTINCT mechanism from the env ``<VAR>_FILE`` convention (config.py). The path is read eagerly
at construction via the shared ``_read_secret_file`` helper, which mirrors the existing
strip-vs-verbatim rule (config.py:143-145): key material (kubeconfig) verbatim, tokens/access-keys
stripped. A missing/unreadable path fails fast. These are pure model-construction tests -- no DB,
no Redis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError
import pytest

from phaze.config_backends import BucketConfig, KubeConfig, _read_secret_file


if TYPE_CHECKING:
    from pathlib import Path


_ENDPOINT = "https://minio.homelab:9000"


# --------------------------------------------------------------------------- #
# _read_secret_file helper (shared strip-vs-verbatim rule, D-06)
# --------------------------------------------------------------------------- #
def test_read_secret_file_strips_by_default(tmp_path: Path) -> None:
    """A token file read with preserve_whitespace=False is .strip()ed (heredoc newline dropped)."""
    secret = tmp_path / "token"
    secret.write_text("phaze-token\n", encoding="utf-8")
    assert _read_secret_file(str(secret), preserve_whitespace=False) == "phaze-token"


def test_read_secret_file_preserves_verbatim(tmp_path: Path) -> None:
    """Key material read with preserve_whitespace=True keeps its trailing newline verbatim."""
    secret = tmp_path / "kubeconfig"
    secret.write_text("apiVersion: v1\n", encoding="utf-8")
    assert _read_secret_file(str(secret), preserve_whitespace=True) == "apiVersion: v1\n"


def test_read_secret_file_missing_path_raises(tmp_path: Path) -> None:
    """An unreadable path fails fast with a ValueError naming the path."""
    missing = tmp_path / "nope"
    with pytest.raises(ValueError, match=r"could not be read"):
        _read_secret_file(str(missing), preserve_whitespace=False)


# --------------------------------------------------------------------------- #
# KubeConfig inline *_file before-validator
# --------------------------------------------------------------------------- #
def test_kubeconfig_file_preserves_trailing_newline(tmp_path: Path) -> None:
    """kubeconfig_file resolves kube.kubeconfig verbatim -- the trailing newline IS preserved (key material)."""
    kc = tmp_path / "kubeconfig"
    kc.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
    kube = KubeConfig(kubeconfig_file=str(kc))
    assert kube.kubeconfig is not None
    assert kube.kubeconfig.get_secret_value() == "apiVersion: v1\nkind: Config\n"


def test_sa_token_file_is_stripped(tmp_path: Path) -> None:
    """sa_token_file resolves kube.sa_token stripped -- NO trailing newline (mirrors config.py:145)."""
    tok = tmp_path / "sa_token"
    tok.write_text("eyJ-token-value\n", encoding="utf-8")
    kube = KubeConfig(sa_token_file=str(tok))
    assert kube.sa_token is not None
    assert kube.sa_token.get_secret_value() == "eyJ-token-value"


def test_kubeconfig_file_missing_fails_fast(tmp_path: Path) -> None:
    """A kubeconfig_file pointing at a nonexistent path fails fast at construction (D-06)."""
    missing = tmp_path / "absent-kubeconfig"
    with pytest.raises(ValidationError, match=r"could not be read"):
        KubeConfig(kubeconfig_file=str(missing))


# --------------------------------------------------------------------------- #
# BucketConfig inline *_file before-validator
# --------------------------------------------------------------------------- #
def test_bucket_access_key_files_are_stripped(tmp_path: Path) -> None:
    """access_key_id_file / secret_access_key_file resolve to stripped SecretStr values."""
    ak = tmp_path / "access_key"
    sk = tmp_path / "secret_key"
    ak.write_text("AKIAEXAMPLE\n", encoding="utf-8")
    sk.write_text("s3-secret-value\n", encoding="utf-8")
    bucket = BucketConfig(
        id="b1",
        scope="shared",
        endpoint_url=_ENDPOINT,
        bucket="phaze-staging",
        access_key_id_file=str(ak),
        secret_access_key_file=str(sk),
    )
    assert bucket.access_key_id is not None
    assert bucket.secret_access_key is not None
    assert bucket.access_key_id.get_secret_value() == "AKIAEXAMPLE"
    assert bucket.secret_access_key.get_secret_value() == "s3-secret-value"


def test_bucket_secret_file_missing_fails_fast(tmp_path: Path) -> None:
    """A bucket secret_access_key_file pointing at a nonexistent path fails fast at construction (D-06)."""
    missing = tmp_path / "absent-secret"
    with pytest.raises(ValidationError, match=r"could not be read"):
        BucketConfig(
            id="b1",
            scope="shared",
            endpoint_url=_ENDPOINT,
            bucket="phaze-staging",
            secret_access_key_file=str(missing),
        )
