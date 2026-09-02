"""Unit tests for the sentence-transformers cross-encoder reranker.

The wrapper's own logic (score-based ordering, tie-breaking, top-k
truncation, and the empty-candidates fast path) had no dedicated coverage:
``tests/test_rag_hybrid.py`` only exercises the ``Reranker`` protocol via a
fake stand-in. ``CrossEncoder`` is monkeypatched so these stay offline unit
tests with no model download, consistent with the rest of the suite.
"""

from typing import Any

import pytest

from app.rag.reranking import SentenceTransformerReranker
from app.schemas.rag import Chunk, RetrievedChunk


class _FakeCrossEncoder:
    """Stand-in for ``sentence_transformers.CrossEncoder``.

    Returns a caller-supplied score per (query, text) pair instead of
    running a real model, and records the kwargs it was called with.
    """

    def __init__(self, scores_by_text: dict[str, float]) -> None:
        self._scores_by_text = scores_by_text
        self.predict_calls: list[dict[str, Any]] = []

    def predict(
        self, pairs: list[tuple[str, str]], *, show_progress_bar: bool, batch_size: int
    ) -> list[float]:
        self.predict_calls.append(
            {"pairs": pairs, "show_progress_bar": show_progress_bar, "batch_size": batch_size}
        )
        return [self._scores_by_text[text] for _query, text in pairs]


def _chunk(chunk_id: str, text: str) -> Chunk:
    return Chunk(chunk_id=chunk_id, doc_id="doc-1", text=text, page_start=1, page_end=1)


def _install_fake_cross_encoder(
    monkeypatch: pytest.MonkeyPatch, scores_by_text: dict[str, float]
) -> _FakeCrossEncoder:
    import sentence_transformers

    fake = _FakeCrossEncoder(scores_by_text)
    monkeypatch.setattr(sentence_transformers, "CrossEncoder", lambda *args, **kwargs: fake)
    return fake


def test_model_name_returns_the_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_cross_encoder(monkeypatch, {})
    reranker = SentenceTransformerReranker("some/cross-encoder-model")

    assert reranker.model_name == "some/cross-encoder-model"


@pytest.mark.asyncio
async def test_rerank_returns_empty_without_calling_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_cross_encoder(monkeypatch, {})
    reranker = SentenceTransformerReranker("some/cross-encoder-model")

    result = await reranker.rerank("query", [], k=5)

    assert result == []
    assert fake.predict_calls == []


@pytest.mark.asyncio
async def test_rerank_orders_by_descending_score_and_truncates_to_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_cross_encoder(monkeypatch, {"low": 0.1, "high": 0.9, "mid": 0.5})
    reranker = SentenceTransformerReranker("some/cross-encoder-model", batch_size=4)
    candidates = [
        RetrievedChunk(chunk=_chunk("c:low", "low"), score=0.0),
        RetrievedChunk(chunk=_chunk("c:high", "high"), score=0.0),
        RetrievedChunk(chunk=_chunk("c:mid", "mid"), score=0.0),
    ]

    result = await reranker.rerank("query", candidates, k=2)

    assert [item.chunk.chunk_id for item in result] == ["c:high", "c:mid"]
    assert [item.score for item in result] == [0.9, 0.5]
    assert fake.predict_calls == [
        {
            "pairs": [("query", "low"), ("query", "high"), ("query", "mid")],
            "show_progress_bar": False,
            "batch_size": 4,
        }
    ]


@pytest.mark.asyncio
async def test_rerank_breaks_score_ties_by_chunk_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_cross_encoder(monkeypatch, {"a": 0.5, "b": 0.5})
    reranker = SentenceTransformerReranker("some/cross-encoder-model")
    candidates = [
        RetrievedChunk(chunk=_chunk("c:zebra", "a"), score=0.0),
        RetrievedChunk(chunk=_chunk("c:apple", "b"), score=0.0),
    ]

    result = await reranker.rerank("query", candidates, k=2)

    assert [item.chunk.chunk_id for item in result] == ["c:apple", "c:zebra"]
