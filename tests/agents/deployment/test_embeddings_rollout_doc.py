"""phaze-qijoh: docs/deployment.md states the rollout order for the embeddings field, and its body-size figure."""

from __future__ import annotations

from pathlib import Path
import re

from phaze.schemas.agent_analysis import EMBEDDING_VECTOR_CHARS, MAX_EMBEDDING_WINDOWS


_DOC = " ".join((Path(__file__).resolve().parents[3] / "docs" / "deployment.md").read_text().split())


def test_deployment_doc_says_server_first_and_names_the_embeddings_field() -> None:
    section = _DOC.split("## Upgrading: server first, then agents", 1)[1].split(" ## Rollback Procedure", 1)[0]
    assert "Upgrade the **application server first**" in section
    assert "`embeddings`" in section
    assert "analysis_embeddings" in section


def test_deployment_doc_body_size_figures_match_the_schema() -> None:
    assert f"{EMBEDDING_VECTOR_CHARS:,} base64 characters" in _DOC
    assert re.search(rf"caps `embeddings` at {MAX_EMBEDDING_WINDOWS:,} windows", _DOC)
