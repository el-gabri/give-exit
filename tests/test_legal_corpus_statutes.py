"""Hierarchy, index scope and performance of a corpus built from statutes."""

from __future__ import annotations

from functools import lru_cache

import pytest

from app.consumer.legal_corpus import LegalCorpus, get_default_legal_corpus
from app.schemas.document import ParsedDocument
from app.schemas.rag import RetrievedChunk


@lru_cache(maxsize=1)
def _with_civil_code() -> LegalCorpus:
    return get_default_legal_corpus()


def test_hierarchy_reaches_provisions_chunks_and_citations() -> None:
    corpus = _with_civil_code()
    chunk = next(
        item
        for item in corpus.as_chunks()
        if item.metadata["unit_id"] == "br-cc-art-927-caput"
    )

    citation = corpus.authority_for_chunk(RetrievedChunk(chunk=chunk, score=1.0))

    assert corpus.get("br-cc-art-927").book == "LIVRO I DO DIREITO DAS OBRIGAÇÕES"
    assert chunk.text.startswith(
        "Código Civil (Lei nº 10.406/2002) > PARTE ESPECIAL > "
        "LIVRO I DO DIREITO DAS OBRIGAÇÕES > TÍTULO IX"
    )
    assert (chunk.metadata["part"], chunk.metadata["book"]) == (
        "PARTE ESPECIAL",
        "LIVRO I DO DIREITO DAS OBRIGAÇÕES",
    )
    assert citation.citation_label == "Código Civil, art. 927"
    assert citation.book == "LIVRO I DO DIREITO DAS OBRIGAÇÕES"


def test_audit_only_books_stay_in_the_corpus_but_not_in_the_index() -> None:
    corpus = _with_civil_code()
    family = corpus.get("br-cc-art-1511")
    indexed = {chunk.metadata["provision_id"] for chunk in corpus.as_chunks()}
    retrievable = {item.provision_id for item in corpus.retrievable_provisions()}

    assert family.index_scope == "audit_only"
    assert corpus.get("br-cc-art-927").index_scope == "indexed"
    assert "br-cc-art-1511" not in indexed | retrievable
    assert "br-cc-art-927" in indexed & retrievable


def test_civil_code_summaries_drop_the_article_heading() -> None:
    corpus = _with_civil_code()

    assert corpus.get("br-cc-art-1358-a").summary.startswith("Pode haver, em terrenos")
    assert corpus.get("br-cc-art-3").summary.startswith("São absolutamente incapazes")


def test_the_document_id_is_hashed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}
    original = ParsedDocument.doc_id

    def counting(self: ParsedDocument) -> str:
        calls["count"] += 1
        return original.fget(self)  # type: ignore[attr-defined, no-any-return]

    monkeypatch.setattr(ParsedDocument, "doc_id", property(counting))
    corpus = LegalCorpus(get_default_legal_corpus().provisions)

    corpus.as_chunks()
    corpus.as_chunks()

    assert calls["count"] == 1
