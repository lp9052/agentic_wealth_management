"""Tests for app/proposer/rag.py — ChromaDB-backed regulation retrieval."""

import json
from unittest.mock import MagicMock

import pytest

from app.proposer import rag
from app.proposer.rag import ingest_regulations, init_chroma, retrieve_regulations


def test_init_chroma_builds_vectorstore(monkeypatch):
    """init_chroma wires GoogleEmbeddings and a Chroma store with the right dir."""
    fake_embed = object()
    captured = {}

    def fake_embeddings(model):
        captured["model"] = model
        return fake_embed

    def fake_chroma(**kw):
        captured.update(kw)
        return MagicMock()

    monkeypatch.setattr(rag, "GoogleGenerativeAIEmbeddings", fake_embeddings)
    monkeypatch.setattr(rag, "Chroma", fake_chroma)
    out = init_chroma()
    assert out is not None
    assert captured["model"].startswith("models/gemini-embedding")
    assert captured["collection_name"] == "regulations"
    assert captured["embedding_function"] is fake_embed


def test_init_chroma_caches_vectorstore(monkeypatch):
    """init_chroma builds the store once and returns the cached handle after."""
    build_count = {"n": 0}

    monkeypatch.setattr(rag, "GoogleGenerativeAIEmbeddings", lambda model: object())

    def fake_chroma(**kw):
        build_count["n"] += 1
        return MagicMock()

    monkeypatch.setattr(rag, "Chroma", fake_chroma)
    rag._vectorstore = None

    first = init_chroma()
    second = init_chroma()
    assert first is second
    assert build_count["n"] == 1


def test_ingest_regulations_loads_and_adds_documents(tmp_path, monkeypatch, capsys):
    regs = [{
        "id": "FINRA_2111",
        "metadata": {"rule": "Suitability", "related": ["SEC_REG_BI"], "tags": ["x", "y"]},
        "text": "Body text",
    }]
    # Replace the regulations.json file via the constructed path
    real_path = rag.os.path.join(
        rag.os.path.dirname(rag.__file__), "..", "..", "data", "regulations.json"
    )
    real_path = rag.os.path.normpath(real_path)

    p = tmp_path / "regulations.json"
    p.write_text(json.dumps(regs))
    monkeypatch.setattr(rag.os.path, "join", lambda *parts: str(p) if "regulations.json" in parts else rag.os.path.sep.join(parts))

    # Capture add_documents call
    captured = {}
    fake_store = MagicMock()
    fake_store.add_documents = lambda docs: captured.setdefault("docs", docs)
    monkeypatch.setattr(rag, "init_chroma", lambda: fake_store)

    ingest_regulations()
    assert "docs" in captured
    assert len(captured["docs"]) == 1
    doc = captured["docs"][0]
    assert "FINRA_2111" in doc.page_content
    assert "Body text" in doc.page_content
    assert doc.metadata["id"] == "FINRA_2111"
    assert doc.metadata["related"] == "SEC_REG_BI"
    assert "x, y" == doc.metadata["tags"]


def test_retrieve_regulations_returns_chunks(monkeypatch):
    fake_store = MagicMock()
    doc1 = MagicMock()
    doc1.page_content = "chunk one"
    doc2 = MagicMock()
    doc2.page_content = "chunk two"
    fake_store.similarity_search = lambda q, k: [doc1, doc2]
    monkeypatch.setattr(rag, "init_chroma", lambda: fake_store)

    out = retrieve_regulations("buy SPY", k=2)
    assert out == ["chunk one", "chunk two"]
