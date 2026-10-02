"""Bounded host-side deployment inventory wire format."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


HOST_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
IMAGE_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_./:@-]*$"
DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"


class ContainerReport(BaseModel):
    """One observed running container; optional fields stay unknown when unavailable."""

    model_config = ConfigDict(extra="ignore")

    container_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    service: Literal["api", "worker", "worker-analyze", "worker-meta", "worker-io", "worker-drain", "watcher"]
    role: Literal["api", "control", "agent"] | None = None
    lane: Literal["analyze", "meta", "io", "drain"] | None = None
    app_version: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
    image_ref: str | None = Field(default=None, max_length=256, pattern=IMAGE_REF_PATTERN)
    image_digest: str | None = Field(default=None, max_length=71, pattern=DIGEST_PATTERN)

    @field_validator("image_ref")
    @classmethod
    def reject_image_credentials(cls, value: str | None) -> str | None:
        """Allow registry digest references, but not userinfo-like references."""
        if value is not None and "@" in value:
            _repository, _separator, digest = value.partition("@")
            if len(digest) != 71 or not digest.startswith("sha256:") or any(char not in "0123456789abcdef" for char in digest[7:]):
                raise ValueError("image reference must not contain credentials")
        return value


class DeploymentReport(BaseModel):
    """Atomic inventory of running Phaze services on one host."""

    model_config = ConfigDict(extra="ignore")

    host: str = Field(max_length=128, pattern=HOST_PATTERN)
    containers: list[ContainerReport] = Field(max_length=32)
