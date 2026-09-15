"""Wire-contract tests for orphan COMPANION diagnostic chunks."""

from pydantic import ValidationError
import pytest

from phaze.config import settings
from phaze.schemas.agent_orphan_companions import OrphanCompanionChunk


def _record(path: str = "/archive/orphans/info.nfo", extension: str = ".nfo") -> dict[str, str]:
    return {"normalized_path": path, "companion_extension": extension}


def test_chunk_and_nested_records_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        OrphanCompanionChunk.model_validate({"diagnostics": [_record()], "agent_id": "forged"})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        OrphanCompanionChunk.model_validate({"diagnostics": [{**_record(), "content": "secret"}]})


def test_chunk_is_bounded_by_existing_agent_file_ceiling() -> None:
    diagnostics = [_record(path=f"/archive/orphans/{index}.nfo") for index in range(settings.agent_file_chunk_max + 1)]
    with pytest.raises(ValidationError, match="too_long"):
        OrphanCompanionChunk(diagnostics=diagnostics)


@pytest.mark.parametrize("extension", [".jpg", ".jpeg", ".png", ".gif", ".sfv", ".md5"])
def test_excluded_companion_extensions_are_rejected(extension: str) -> None:
    with pytest.raises(ValidationError):
        OrphanCompanionChunk(diagnostics=[_record(path=f"/archive/orphans/file{extension}", extension=extension)])


def test_nul_and_mismatched_extension_are_rejected() -> None:
    with pytest.raises(ValidationError, match="NUL"):
        OrphanCompanionChunk(diagnostics=[_record(path="/archive/bad\0.nfo")])
    with pytest.raises(ValidationError, match="must match"):
        OrphanCompanionChunk(diagnostics=[_record(path="/archive/file.txt", extension=".nfo")])
