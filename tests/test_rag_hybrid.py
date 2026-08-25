"""Offline tests for purpose-aware embeddings and hybrid retrieval."""

import hashlib
from typing import Any

import pytest

from app.core.config import EmbeddingProvider, LLMProvider, RetrievalMode, Settings
from app.rag.embeddings import MockEmbeddingClient, embed_query_texts
from app.rag.factory import create_embedding_client, create_vector_store
from app.rag.pipeline import RagPipeline, reciprocal_rank_fusion
from app.rag.vector_store import InMemoryVectorStore, versioned_collection_name
from app.schemas.rag import Chunk, RetrievedChunk


def _chunk(chunk_id: str, text: str, **metadata: Any) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id="cdc-2026-08",
        text=text,
        page_start=1,
        page_end=1,
        metadata=metadata,
    )


async def test_pipeline_uses_separate_document_and_query_embedding_methods() -> None:
    class PurposeAwareEmbedder:
        model_name = "purpose-aware-test"

        def __init__(self) -> None:
            self.documents: list[list[str]] = []
            self.queries: list[str] = []

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.documents.append(texts)
            return [[1.0, 0.0] for _ in texts]

        async def embed_query(self, text: str) -> list[float]:
            self.queries.append(text)
            return [1.0, 0.0]

    embedder = PurposeAwareEmbedder()
    pipeline = RagPipeline(embedder=embedder, store=InMemoryVectorStore())
    chunk = _chunk("cdc:42", "Art. 42. Repeticao do indebito.")

    await pipeline.index_chunks([chunk])
    await pipeline.retrieve_many(["cobranca indevida", "devolucao em dobro"], doc_id=chunk.doc_id)

    assert embedder.documents == [[chunk.text]]
    assert embedder.queries == ["cobranca indevida", "devolucao em dobro"]


async def test_query_instruction_is_applied_only_to_queries() -> None:
    class RecordingMock(MockEmbeddingClient):
        def __init__(self) -> None:
            super().__init__(query_instruction="Represent this legal query")
            self.inputs: list[list[str]] = []

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.inputs.append(texts)
            return await super().embed(texts)

    embedder = RecordingMock()
    await embedder.embed_documents(["texto oficial do artigo"])
    await embed_query_texts(embedder, ["qual artigo trata da cobranca?"])

    assert embedder.inputs == [
        ["texto oficial do artigo"],
        ["Represent this legal query qual artigo trata da cobranca?"],
    ]


async def test_dependency_free_bm25_ranks_exact_legal_terms() -> None:
    store = InMemoryVectorStore()
    article_42 = _chunk(
        "cdc:42",
        "Artigo 42. O consumidor cobrado tem direito a repeticao do indebito.",
    )
    article_18 = _chunk(
        "cdc:18",
        "Artigo 18. Os fornecedores respondem pelos vicios do produto.",
    )
    await store.upsert([article_18, article_42], [[1.0], [1.0]])

    results = await store.lexical_query(
        "artigo 42 repeticao do indebito", doc_id=article_42.doc_id, k=2
    )

    assert [item.chunk.chunk_id for item in results] == ["cdc:42", "cdc:18"]
    assert results[0].score > results[1].score


def test_rrf_is_weighted_and_deterministic() -> None:
    article_18 = RetrievedChunk(chunk=_chunk("cdc:18", "vicio"), score=0.99)
    article_42 = RetrievedChunk(chunk=_chunk("cdc:42", "cobranca"), score=8.0)

    fused = reciprocal_rank_fusion(
        [[article_18, article_42], [article_42, article_18]],
        k=2,
        constant=10,
        weights=[1.0, 2.0],
    )

    assert [item.chunk.chunk_id for item in fused] == ["cdc:42", "cdc:18"]
    assert fused[0].score > fused[1].score


async def test_hybrid_trace_reports_rrf_and_preserves_source_metadata() -> None:
    article_18 = _chunk("cdc:18", "vicio do produto", provision_id="CDC-18")
    article_42 = _chunk(
        "cdc:42",
        "cobranca indevida repeticao do indebito",
        provision_id="CDC-42",
        unit_id="CDC-42-PU",
        status="active",
        source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
        chunking_version="legal-hierarchy-v1:target=1200",
        corpus_release_id="cdc-planalto-2026-08-04",
        source_snapshot_sha256="a" * 64,
        content_sha256="b" * 64,
    )
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(query_instruction="Represent legal query"),
        store=InMemoryVectorStore(index_name="cdc-index-v1"),
        retrieval_mode=RetrievalMode.HYBRID,
        lexical_weight=2.0,
        corpus_version="cdc-planalto-2026-08-04",
        default_k=2,
    )
    await pipeline.index_chunks([article_18, article_42])

    results, trace = await pipeline.retrieve_with_trace(
        "artigo sobre cobranca indevida",
        doc_id=article_42.doc_id,
        agent="consumer_legal",
    )

    assert results[0].chunk.chunk_id == "cdc:42"
    assert trace.score_type == "rrf_score"
    assert trace.retrieval_mode == "hybrid"
    assert trace.candidate_k == 8
    assert trace.candidate_multiplier == 4
    assert trace.rrf_constant == 60
    assert trace.dense_weight == 1.0
    assert trace.lexical_weight == 2.0
    assert trace.embedding_query_instruction == "Represent legal query"
    assert (
        trace.embedding_query_instruction_sha256
        == hashlib.sha256(b"Represent legal query").hexdigest()
    )
    assert trace.results[0].source_metadata["provision_id"] == "CDC-42"
    assert trace.chunking_version == "legal-hierarchy-v1:target=1200"
    assert trace.results[0].source_url == (
        "https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm"
    )
    assert trace.results[0].source_release_id == "cdc-planalto-2026-08-04"
    assert trace.results[0].source_snapshot_sha256 == "a" * 64
    assert trace.results[0].source_content_sha256 == "b" * 64
    assert trace.results[0].source_provision_id == "CDC-42"
    assert trace.results[0].source_unit_id == "CDC-42-PU"
    assert "corpus=cdc-planalto-2026-08-04" in trace.index_version
    assert "embedding=mock-hashed-bow" in trace.index_version
    assert "index=cdc-index-v1" in trace.index_version


async def test_optional_reranker_controls_final_order_without_model_download() -> None:
    class LegalReranker:
        model_name = "fake-legal-reranker"
        model_revision = "revision-abc123"

        async def rerank(
            self, query: str, candidates: list[RetrievedChunk], k: int
        ) -> list[RetrievedChunk]:
            assert query == "devolucao de cobranca"
            rescored = [
                RetrievedChunk(
                    chunk=item.chunk,
                    score=10.0 if item.chunk.chunk_id == "cdc:42" else 0.0,
                )
                for item in candidates
            ]
            return sorted(rescored, key=lambda item: -item.score)[:k]

    chunks = [
        _chunk("cdc:18", "devolucao produto"),
        _chunk("cdc:42", "repeticao do indebito"),
    ]
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(),
        store=InMemoryVectorStore(),
        reranker=LegalReranker(),
        default_k=1,
    )
    await pipeline.index_chunks(chunks)

    results, trace = await pipeline.retrieve_with_trace(
        "devolucao de cobranca", doc_id=chunks[0].doc_id, agent="consumer_legal"
    )

    assert results[0].chunk.chunk_id == "cdc:42"
    assert trace.score_type == "reranker_score"
    assert trace.candidate_k == 4
    assert trace.reranker_model == "fake-legal-reranker"
    assert trace.reranker_model_revision == "revision-abc123"
    assert "reranker=fake-legal-reranker" in trace.index_version


async def test_document_replacement_failure_preserves_previous_index() -> None:
    class FailingReplaceStore(InMemoryVectorStore):
        async def replace_document(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
            del chunks, vectors
            raise RuntimeError("simulated provider failure")

    store = FailingReplaceStore()
    old = _chunk("cdc:old", "texto anterior")
    await store.upsert([old], [[1.0, 0.0]])
    pipeline = RagPipeline(embedder=MockEmbeddingClient(dimensions=2), store=store)

    with pytest.raises(RuntimeError, match="simulated provider failure"):
        await pipeline.index_chunks([_chunk("cdc:new", "texto novo")])

    remaining = await store.query([1.0, 0.0], doc_id=old.doc_id, k=5)
    assert [item.chunk.chunk_id for item in remaining] == ["cdc:old"]


def test_collection_namespace_changes_with_corpus_or_embedding() -> None:
    baseline = versioned_collection_name("cdc-v1", "text-embedding-3-small")

    assert baseline == versioned_collection_name("cdc-v1", "text-embedding-3-small")
    assert baseline != versioned_collection_name("cdc-v2", "text-embedding-3-small")
    assert baseline != versioned_collection_name("cdc-v1", "BAAI/bge-m3")


def test_factory_keeps_mock_mode_offline_and_versions_memory_index() -> None:
    settings = Settings(
        llm_provider=LLMProvider.MOCK,
        embedding_provider=EmbeddingProvider.AUTO,
        vector_store="memory",
        rag_corpus_version="cdc-v1",
        _env_file=None,
    )

    embedder = create_embedding_client(settings)
    store = create_vector_store(settings, embedding_model=embedder.model_name)

    assert isinstance(embedder, MockEmbeddingClient)
    assert store.index_name == versioned_collection_name(
        "cdc-v1", embedder.model_name, prefix="give-exit-consumer"
    )
    assert settings.retrieval_mode is RetrievalMode.HYBRID


async def test_chroma_roundtrip_preserves_structured_metadata(tmp_path) -> None:
    pytest.importorskip("chromadb")
    from app.rag.vector_store import ChromaVectorStore

    chunk = _chunk(
        "cdc:42:paragrafo-unico",
        "O consumidor cobrado em quantia indevida tem direito a repeticao.",
        provision_id="CDC-42-PU",
        article="42",
        paragraph="unico",
        corpus_release="cdc-planalto-v1",
        content_sha256="abc123",
    )
    pipeline = RagPipeline(
        embedder=MockEmbeddingClient(),
        store=ChromaVectorStore(tmp_path / "chroma", collection_name="cdc-metadata-roundtrip"),
        default_k=1,
    )
    await pipeline.index_chunks([chunk])

    [result] = await pipeline.retrieve("repeticao indevida", doc_id=chunk.doc_id)

    assert result.chunk.metadata == chunk.metadata
